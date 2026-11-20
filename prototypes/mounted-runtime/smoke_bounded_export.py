"""Check both write paths on an already running synthetic bounded exporter."""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from prepare import BOUNDED_EXPORT_BYTES
from smoke_readonly import LOCAL, STACK, local_docker, require
from track_a.mounted_executor import MountedExecutor


HALF = BOUNDED_EXPORT_BYTES // 2
SANDBOX_SCRIPT = """
import os
import sys
from pathlib import Path

size, stage = int(sys.argv[1]), sys.argv[2]
space = os.statvfs('.')
assert space.f_blocks * space.f_frsize == 2 * size, space
if stage == 'seed':
    assert not list(Path('.').iterdir()), 'Expected a fresh empty export'
    with Path('shell.bin').open('xb') as stream:
        stream.write(b'x' * size)
        stream.flush()
        os.fsync(stream.fileno())
    print('shell write succeeded')
else:
    assert space.f_bavail == 0, space
    for name in ('shell.bin', 'sftp.bin'):
        assert Path(name).read_bytes() == b'x' * size, name
    if stage == 'full':
        try:
            with Path('shell-overflow.bin').open('xb') as stream:
                stream.write(b'y')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            assert Path('shell-overflow.bin').stat().st_size == 0
            print('shell overflow refused, errno=' + str(error.errno))
        else:
            raise RuntimeError('Shell exceeded the export limit')
    else:
        assert Path('sftp-overflow.bin').stat().st_size == 0
        print('original files intact and SFTP wrote no extra bytes')
"""


def sftp(batch):
    return subprocess.run([str(STACK), 'sftp'], input=batch, text=True,
                          capture_output=True, timeout=10)


def check_quota(executor):
    base = '/mounted/bounded-export'
    status, lease = executor.route('POST', base + '/provision', {'grant': executor.grant})
    require(status == 200 and not lease['closed'], f'Unexpected lease result: {lease}')
    run = {'generation': lease['generation'], 'run_id': 'quota'}

    def shell(stage, expected):
        command = 'exec python3 -c ' + shlex.quote(SANDBOX_SCRIPT) + f' {HALF} {stage}'
        call = {'id': stage, 'tool': 'RunCommand', 'args': {'command': command}}
        status, result = executor.route('POST', base + '/exec', {**run, 'op_id': stage, 'call': call})
        require(status == 200 and result['exit_code'] == 0
                and result['stdout'].startswith(expected), f'{stage}: {result}')
        print(result['stdout'].strip())

    try:
        executor.route('POST', base + '/start', run)
        shell('seed', 'shell write succeeded')
        download = 'get /work/shell.bin /tmp/payload\n'
        copied = sftp(download + 'put /tmp/payload /work/sftp.bin\n')
        require(copied.returncode == 0, f'SFTP write below capacity failed: {copied}')
        shell('full', 'shell overflow refused, errno=')

        # Attempt a small write to the same full filesystem. The client may report
        # a generic SFTP failure because SFTP v3 has no dedicated disk-full status.
        overflow = 'put /tmp/payload /work/sftp-overflow.bin'
        refused = sftp(download + overflow + '\n')
        require(refused.returncode == 1 and 'sftp> ' + overflow in refused.stdout
                and 'write remote' in refused.stderr.lower(), f'Unexpected SFTP result: {refused}')
        shell('verify', 'original files intact and SFTP wrote no extra bytes')
    finally:
        status, release = executor.route('POST', base + '/release', {})
        require(status == 200 and release['cleanup_state'] == 'confirmed',
                f'Unexpected release result: {release}')
    print(json.dumps({'status': 'passed', 'limit_bytes': BOUNDED_EXPORT_BYTES}))


def main():
    root_path = os.environ.get('PROBE_ROOT', '')
    require(re.fullmatch(r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}', root_path),
            'Set PROBE_ROOT to the output of prepare.py --bounded-export')
    root = Path(root_path)
    require(not os.environ.get('DOCKER_CONTEXT')
            and os.environ.get('DOCKER_HOST', LOCAL) == LOCAL, 'Use the local Docker socket')
    scope = subprocess.check_output([str(STACK), 'scope'], text=True, timeout=5).strip()
    exporters = local_docker('ps', '-q', '--no-trunc',
                             '--filter', 'label=com.docker.compose.project=' + scope,
                             '--filter', 'label=com.docker.compose.service=exporter').split()
    require(len(exporters) == 1 and re.fullmatch(r'[a-f0-9]{64}', exporters[0]),
            'Expected one running exporter for this diagnostic stack')
    mounts = json.loads(local_docker('inspect', '--format', '{{json .HostConfig.Mounts}}', exporters[0]))
    require(any(mount['Type'] == 'tmpfs' and mount['Target'] == '/probe/work'
                and mount['TmpfsOptions']['SizeBytes'] == BOUNDED_EXPORT_BYTES for mount in mounts or []),
            'Expected a bounded tmpfs at the exporter workdir')
    os.environ['DOCKER_HOST'] = LOCAL
    executor = MountedExecutor(str(root / 'mount/work'), 'bounded-export', scope,
                               'agentic-sandbox:cpu-v1', root / 'bounded-export-state')
    check_quota(executor)


if __name__ == '__main__':
    main()
