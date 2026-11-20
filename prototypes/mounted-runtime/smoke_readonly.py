"""Opt-in smoke for an already running, synthetic read-only SSHFS stack."""
import base64
import contextlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from smoke import service
from track_a.application import Application
from track_a.model_proxy import ModelProxy
from track_a.mounted_executor import MountedExecutor

STACK = Path(__file__).with_name('stack.sh')
LOCAL = 'unix:///var/run/docker.sock'
CLIENT = b'client-initial\n'
FIXTURE = {
    'client.txt': CLIENT,
    'upload.txt': b'distinct-upload-canary\n',
    '.git/probe': b'protected-git-canary\n',
    'nested/repo/.git/probe': b'protected-git-canary\n',
    'nested/worktree/.git': b'protected-git-canary\n',
}
COMMAND = """
import errno
from pathlib import Path
path = Path('client.txt')
if path.read_bytes() != b'client-initial\\n':
    raise RuntimeError('Unexpected sandbox input')
for label, operation in (
    ('create', lambda: Path('command-new.txt').write_bytes(b'new')),
    ('overwrite', lambda: path.write_bytes(b'changed')),
    ('rename', lambda: path.rename('command-renamed.txt')),
    ('delete', lambda: path.unlink()),
):
    try:
        operation()
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
        print(label + '=EROFS')
    else:
        raise RuntimeError(label + ' succeeded')
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def files(root):
    client = root / 'client'
    folders = {parent for name in FIXTURE for parent in Path(name).parents}
    snapshot = {}
    for path in client.rglob('*'):
        require(not path.is_symlink(), 'Synthetic export contains a link')
        relative = path.relative_to(client)
        if path.is_dir():
            require(relative in folders, 'Unexpected directory in synthetic client')
            continue
        name = relative.as_posix()
        require(name in FIXTURE and path.is_file(), 'Unexpected file in synthetic client')
        info = path.stat()
        require(info.st_nlink == 1 and info.st_size == len(FIXTURE[name]), 'Unexpected client metadata')
        snapshot[name] = path.read_bytes()
    return snapshot


def local_docker(*args):
    return subprocess.check_output(['docker', '--host', LOCAL, *args], text=True, timeout=5)


def preflight():
    supplied = os.environ.get('PROBE_ROOT', '')
    require(re.fullmatch(r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}', supplied),
            'Set PROBE_ROOT to a fresh synthetic directory from prepare.py')
    root = Path(supplied)
    require(root.resolve(strict=True) == root and root.stat().st_uid == os.getuid(),
            'Synthetic root must be owned by the current operator')
    require((root / 'marker').read_text() == 'agentic-mounted-runtime\n'
            and (root / 'access-mode').read_text() == 'ro\n'
            and (root / 'client').resolve(strict=True) == root / 'client'
            and files(root) == FIXTURE, 'Expected untouched synthetic RO fixture')
    require(not os.environ.get('DOCKER_CONTEXT')
            and os.environ.get('DOCKER_HOST', LOCAL) == LOCAL, 'Use the local Docker socket')
    scope = subprocess.check_output([str(STACK), 'scope'], text=True, timeout=5).strip()
    exporter = local_docker('ps', '-q', '--no-trunc',
                            '--filter', 'label=com.docker.compose.project=' + scope,
                            '--filter', 'label=com.docker.compose.service=exporter').split()
    require(len(exporter) == 1 and re.fullmatch(r'[a-f0-9]{64}', exporter[0]),
            'Expected one running exporter for the synthetic project')
    mounts = json.loads(local_docker('inspect', '--format', '{{json .Mounts}}', exporter[0]))
    for source, target in ((root / 'client', '/probe/work'), (root / 'sshd.conf', '/probe-config/sshd.conf')):
        require(any(item['Source'] == str(source) and item['Destination'] == target
                    and not item['RW'] for item in mounts), 'Exporter must bind the synthetic RO source')
    require(not local_docker('ps', '-aq', '--filter', 'label=agentic.scope=mounted-' + scope).strip(),
            'Expected a fresh stack without sandbox containers')
    return root, scope


def check_sftp():
    read = 'get /work/upload.txt /tmp/source'
    commands = [
        '',
        'put /tmp/source /work/sftp-new.txt',
        'put /tmp/source /work/client.txt',
        'rename /work/client.txt /work/sftp-renamed.txt',
        'rm /work/client.txt',
    ]
    for command in commands:
        batch = '\n'.join([read, command, ''])
        result = subprocess.run([str(STACK), 'sftp'], input=batch, text=True,
                                capture_output=True, timeout=10)
        require('sftp> ' + read in result.stdout, 'SFTP did not reach the export')
        if command:
            require('sftp> ' + command in result.stdout and result.returncode == 1
                    and 'permission denied' in result.stderr.lower(),
                    f'SFTP did not confirm permission denial for {command}: {result}')
        else:
            require(result.returncode == 0, f'SFTP read failed: {result}')


class ReadonlyProxy(ModelProxy):
    @staticmethod
    def mock(messages):
        if messages[-1]['content'] == 'mount:read-only':
            return {'calls': [
                {'id': 'write', 'tool': 'WriteFile', 'args': {'path': 'client.txt', 'data': 'bmV3Cg=='}},
                {'id': 'read', 'tool': 'ReadFile', 'args': {'path': 'client.txt'}},
                {'id': 'command', 'tool': 'RunCommand',
                 'args': {'command': 'exec python3 -c ' + shlex.quote(COMMAND)}},
            ]}
        return ModelProxy.mock(messages)


def check_runtime(executor, state, grant):
    owner = secrets.token_hex(24)
    with contextlib.ExitStack() as services:
        backend = services.enter_context(service(executor, secrets.token_hex(24)))
        proxy = ReadonlyProxy(SimpleNamespace(backend='mock'))
        model = services.enter_context(service(proxy, secrets.token_hex(24)))
        app = Application(backend, model, {'local': owner}, state, mount_owner='local')
        api = services.enter_context(service(app, owner))

        def cli(*args):
            return subprocess.check_output(
                [sys.executable, '-m', 'track_a.cli', '--url', api.base, *args],
                env={**os.environ, 'PROBE_TOKEN': owner}, text=True, timeout=12,
            ).strip()

        status, _ = api.request('POST', '/v2/sessions',
                                {'request_id': 'upgrade', 'grant': grant, 'access_mode': 'rw'}, timeout=5)
        require(status == 400, 'Public API accepted an access mode override')
        sid = cli('mount-new', grant)
        try:
            rid = cli('run', sid, 'mount:read-only')
            run = api.wait_run(sid, rid, 35)['result']
            require(run['status'] == 'completed' and run['cleanup_state'] == 'confirmed',
                    f'Mounted run failed: {run}')
            batch = [json.loads(line) for line in cli('events', sid).splitlines()]
            results = [event['payload']['result'] for event in batch
                       if event['type'] == 'tool.result' and event['run_id'] == rid]
            require(len(results) == 3, 'Expected separate write, read and command tool results')
            write, read, command = results
            encoded = base64.b64encode(CLIENT).decode()
            require(write['exit_code'] == 1 and write['error_kind'] == 'command_failure'
                    and write['stderr'] == 'Read-only workspace', f'WriteFile outcome: {write}')
            require(read['exit_code'] == 0 and read['data'] == encoded, f'ReadFile outcome: {read}')
            require(command['exit_code'] == 0 and command['stdout'].splitlines()
                    == [f'{action}=EROFS' for action in ('create', 'overwrite', 'rename', 'delete')],
                    f'RunCommand outcome: {command}')
            collect = json.loads(cli('run-collect', sid, rid))
            require(collect == {'files': {'client.txt': encoded}}, f'Collect outcome: {collect}')
        finally:
            cli('session-close', sid)


def main():
    root, scope = preflight()
    os.environ['DOCKER_HOST'] = LOCAL
    grant = secrets.token_hex(24)
    with tempfile.TemporaryDirectory(prefix='ro-smoke-state.', dir=root) as state:
        executor = MountedExecutor(str(root / 'mount/work'), grant, scope, 'agentic-sandbox:cpu-v1',
                                   Path(state) / 'execution', access_mode='ro')
        executor.check_mount()
        check_sftp()
        require(files(root) == FIXTURE, 'SFTP changed the client tree')
        check_runtime(executor, Path(state) / 'app', grant)
    require(files(root) == FIXTURE, 'Sandbox changed the client tree')
    require(not local_docker('ps', '-aq', '--filter', 'label=agentic.scope=mounted-' + scope).strip(),
            'Sandbox containers remain after session close')
    print(json.dumps({'status': 'passed', 'sftp_read': 1, 'sftp_denials': 4, 'sandbox_denials': 5,
                      'read_file': 1, 'collect': 1, 'unchanged_files': len(FIXTURE)}))


if __name__ == '__main__':
    main()
