"""Start or reuse the exact pinned CH2 test transport."""
import socket
import subprocess
import tempfile
import time
import os
from pathlib import Path

from two_host_pairing import config_root, private_directory, read_file, require

DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
IMAGE = 'sha256:0b57fcf6e6b5d59a6a9f3255b89a1f2ba198d1870b63b6ad06d3199e1fb384f6'
GATEWAY = 'chp2-thinkpad-tunnel'
EXPORTER = 'chp2-thinkpad-exporter'
COMMON = ('AllowUsers=ubuntu', 'AuthenticationMethods=publickey', 'PasswordAuthentication=no',
          'KbdInteractiveAuthentication=no', 'UsePAM=yes', 'PermitRootLogin=no')


def bind(source, destination, *, writable=False):
    require(',' not in str(source), 'Unexpected bind path')
    return ['--mount', f'type=bind,src={source},dst={destination}' + ('' if writable else ',readonly')]


def sshd(port, public_key, options):
    args = ['-D', '-e', '-f', '/dev/null', '-p', str(port), '-h', '/etc/ssh/chp2_host']
    for option in (*COMMON, 'PidFile=/run/sshd/chp2.pid', f'AuthorizedKeysFile={public_key}', *options):
        args.extend(('-o', option))
    return args


def current_container(command, docker, name):
    listing = command([*docker, 'container', 'ls', '-a', '--filter', f'name=^/{name}$', '--format', '{{.Names}}'])
    if not listing.stdout.strip():
        return False
    require(listing.stdout.strip() == name, 'Unexpected named container')
    identity = command([*docker, 'container', 'inspect', '--format', '{{.State.Running}} {{.Image}}', name])
    require(identity.stdout.strip() == f'true {IMAGE}', f'{name} is not a running pinned container')
    return True


def ensure_gateway(command):
    if current_container(command, DOCKER, GATEWAY):
        return
    keys = Path.home() / 'chp2-tunnel.3SY3Ma'
    private_directory(keys)
    read_file(keys / 'host', private=True)
    read_file(keys / 'client.pub', private=False)
    options = ('ListenAddress=192.168.0.8', 'MaxSessions=0', 'AllowTcpForwarding=yes', 'GatewayPorts=no',
               'PermitListen=127.0.0.1:24222', 'PermitOpen=127.0.0.1:24180', 'AllowStreamLocalForwarding=no',
               'AllowAgentForwarding=no', 'X11Forwarding=no')
    command([*DOCKER, 'run', '-d', '--rm', '--name', GATEWAY, '--pull', 'never', '--network', 'host',
             '--read-only', '--tmpfs', '/run/sshd:mode=0755,size=1m',
             *bind(keys / 'host', '/etc/ssh/chp2_host'), *bind(keys / 'client.pub', '/etc/ssh/chp2_client.pub'),
             '--entrypoint', '/usr/sbin/sshd', IMAGE, *sshd(22222, '/etc/ssh/chp2_client.pub', options)],
            seconds=20)


def wait_pinned_export(keys):
    deadline = time.monotonic() + 300
    command = ['sftp', '-F', '/dev/null', '-q', '-b', '-', '-P', '24222', '-i', str(keys / 'broker'),
               '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=2',
               '-o', 'GlobalKnownHostsFile=/dev/null',
               '-o', f'UserKnownHostsFile={keys / "known_hosts"}', 'ubuntu@127.0.0.1']
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(command, input='pwd\n', capture_output=True, text=True, timeout=4)
            if result.returncode == 0 and 'Remote working directory: /work' in result.stdout.splitlines():
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise TimeoutError('Pinned laptop SFTP was not reachable through the reverse forward')


def laptop_forwards(root):
    ssh = ['ssh', '-fNT', '-F', '/dev/null', '-i', str(root / 'tunnel-key'), '-p', '22222',
           '-o', 'IdentitiesOnly=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=4',
           '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={root / "gateway_known_hosts"}',
           '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
    peer = 'ubuntu@192.168.0.8'
    # A preexisting reverse forward is accepted only if the desktop's pinned
    # SFTP check succeeds before it starts the paired API.
    with tempfile.TemporaryFile() as diagnostics:
        reverse = subprocess.run([*ssh, '-R', '127.0.0.1:24222:127.0.0.1:24221', peer],
                                 stdout=subprocess.DEVNULL, stderr=diagnostics, timeout=12,
                                 env={**os.environ, 'LC_ALL': 'C'})
        diagnostics.seek(0)
        require(reverse.returncode == 0 or
                b'remote port forwarding failed for listen port 24222' in diagnostics.read(4096).lower(),
                'Could not establish the pinned reverse SSH forward')
    try:
        with socket.create_connection(('127.0.0.1', 24180), timeout=.5):
            return
    except OSError:
        pass
    subprocess.run([*ssh, '-L', '127.0.0.1:24180:127.0.0.1:24180', peer],
                   stdout=subprocess.DEVNULL, check=True, timeout=12)


def ensure_laptop_transport(previous_export):
    root = config_root('laptop')
    private_directory(root / 'work')
    for name in ('exporter-host', 'broker.pub', 'tunnel-key', 'gateway_known_hosts'):
        read_file(root / name, private=True)
    docker = ['sudo', *DOCKER]

    def command(args):
        # Keep any local sudo prompt on the operator terminal.
        return subprocess.run(args, stdout=subprocess.PIPE, text=True, check=True, timeout=20)

    export = root / 'work'
    if current_container(command, docker, EXPORTER):
        current = command([*docker, 'container', 'inspect', '--format',
                           '{{range .Mounts}}{{if eq .Destination "/probe/work"}}{{println .Source}}{{end}}{{end}}',
                           EXPORTER]).stdout.strip()
        require(current in {str(export), previous_export}, 'Running exporter does not use an enrolled synthetic root')
        export = Path(current)
    else:
        options = ('DisableForwarding=yes', 'PermitTTY=no', 'PermitUserRC=no', 'ChrootDirectory=/probe',
                   'Subsystem=sftp internal-sftp', 'ForceCommand=internal-sftp -d /work -P symlink,hardlink')
        command([*docker, 'run', '-d', '--rm', '--name', EXPORTER, '--pull', 'never',
                 '-p', '127.0.0.1:24221:2222', '--read-only', '--tmpfs', '/run/sshd:size=1m,mode=0755',
                 *bind(export, '/probe/work', writable=True), *bind(root / 'exporter-host', '/etc/ssh/chp2_host'),
                 *bind(root / 'broker.pub', '/etc/ssh/chp2_broker.pub'), '--entrypoint', '/usr/sbin/sshd',
                 IMAGE, *sshd(2222, '/etc/ssh/chp2_broker.pub', options)])
    laptop_forwards(root)
    return export
