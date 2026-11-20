"""One-host CLI + mock + Docker + SSHFS integration check for the synthetic export."""
import contextlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from track_a.application import Application
from track_a.client import Client
from track_a.executor import docker
from track_a.http_api import JsonHandler
from track_a.model_proxy import ModelProxy
from track_a.mounted_executor import MountedExecutor

BOUNDARY = """
from pathlib import Path
import os
assert Path('client.txt').read_text() == 'client-updated\\n'
assert not os.listdir('.git') and not os.listdir('nested/repo/.git')
assert Path('nested/worktree/.git').read_bytes() == b''
for op in (
    lambda: Path('.git/probe').read_bytes(),
    lambda: Path('nested/repo/.git/probe').read_bytes(),
    lambda: Path('.git/probe').write_bytes(b'bad'),
    lambda: Path('nested/repo/.git/probe').write_bytes(b'bad'),
    lambda: Path('nested/worktree/.git').write_bytes(b'bad'),
    lambda: os.rename('.git', 'stolen'),
    lambda: os.unlink('nested/worktree/.git'),
    lambda: os.link('client.txt', 'hardlink'),
    lambda: os.symlink('../outside.txt', 'outward'),
    lambda: Path('/work/../outside.txt').read_bytes(),
):
    try:
        op()
    except OSError:
        continue
    raise AssertionError('Boundary operation succeeded')
print('sandbox boundary confirmed')
"""
WRITER = """
import time
with open('heartbeat', 'ab', buffering=0) as stream:
    while True:
        stream.write(b'x')
        time.sleep(.03)
"""
DETACHED = f"""
import subprocess
import sys
import time
subprocess.Popen([sys.executable, '-c', {WRITER!r}, 'mount-v2-detached-writer'], start_new_session=True)
while True:
    time.sleep(.1)
"""


class SmokeProxy(ModelProxy):
    @staticmethod
    def mock(messages):
        last = messages[-1]['content']
        script = {'mount:boundary': BOUNDARY, 'mount:cancel': DETACHED}.get(last)
        if script is not None:
            command = 'exec python3 -c ' + shlex.quote(script)
            return {'calls': [{'id': 'probe', 'tool': 'RunCommand', 'args': {'command': command}}]}
        return ModelProxy.mock(messages)


def check_sftp(stack):
    allowed = 'get /work/client.txt /tmp/canary\n'
    denied = [
        'get /outside.txt /tmp/canary',
        'get /work/../outside.txt /tmp/canary',
        'get /work/.git/probe /tmp/canary',
        'get /work/nested/repo/.git/probe /tmp/canary',
        'put /tmp/canary /work/nested/worktree/.git',
        'rename /work/.git /work/moved',
        'rm /work/nested/worktree/.git',
        'ln /work/client.txt /work/hardlink',
        'ln -s ../outside.txt /work/outward',
    ]
    for command in ['', *denied]:
        result = subprocess.run([str(stack), 'sftp'], input=allowed + command + '\n',
                                capture_output=True, text=True, timeout=9)
        assert 'sftp> get /work/client.txt /tmp/canary' in result.stdout, result.stderr
        if command:
            assert 'sftp> ' + command in result.stdout and result.returncode == 1, result
        else:
            assert result.returncode == 0, result
    print('Direct SFTP boundary confirmed', flush=True)


@contextlib.contextmanager
def service(app, token, *, port=0, handler=JsonHandler):
    with ThreadingHTTPServer(('127.0.0.1', port), handler) as server:
        server.app, server.token = app, token
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .03})
        thread.start()
        try:
            yield Client(f'http://127.0.0.1:{server.server_port}', token)
        finally:
            server.shutdown()
            thread.join()
            app.shutdown()


def main():
    root = Path(os.environ['PROBE_ROOT'])
    client_dir = root / 'client'
    assert root.is_absolute() and (root / 'marker').read_text() == 'agentic-mounted-runtime\n'
    assert (client_dir / 'client.txt').read_text() == 'client-initial\n'
    stack = Path(__file__).with_name('stack.sh')
    scope = subprocess.check_output([str(stack), 'scope'], text=True).strip()
    check_sftp(stack)
    grant, owner = 'local-smoke', secrets.token_hex(24)
    os.environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
    os.environ.pop('DOCKER_CONTEXT', None)
    executor = MountedExecutor(str(root / 'mount/work'), grant, scope,
                               'agentic-sandbox:cpu-v1', root / 'execution-state')
    executor.check_mount()
    with contextlib.ExitStack() as services:
        backend = services.enter_context(service(executor, secrets.token_hex(24)))
        model = services.enter_context(service(SmokeProxy(SimpleNamespace(backend='mock')), secrets.token_hex(24)))
        app = Application(backend, model, {'local': owner}, root / 'app-state', mount_owner='local')
        api = services.enter_context(service(app, owner))

        def cli(*args):
            return subprocess.check_output(
                [sys.executable, '-m', 'track_a.cli', '--url', api.base, *args],
                env={**os.environ, 'PROBE_TOKEN': owner}, text=True, timeout=12,
            ).strip()

        assert api.request('POST', '/v2/sessions', {'grant': '/etc', 'request_id': 'bad'})[0] == 400
        assert api.request('POST', '/v2/sessions', {'files': {}, 'request_id': 'fixture'})[0] == 400
        sid = cli('mount-new', grant)
        try:
            rid = cli('run', sid, 'Make a mounted file')
            assert api.wait_run(sid, rid, 8)['result']['status'] == 'completed'
            assert (client_dir / 'mock.txt').read_bytes() == b'mock-ok\n'
            assert json.loads(cli('run-collect', sid, rid))['files'] == {'mock.txt': 'bW9jay1vawo='}
            print('CLI write, tool readback and post-stop collect confirmed', flush=True)

            (client_dir / 'client.txt').write_text('client-updated\n')
            rid = cli('run', sid, 'mount:boundary')
            assert api.wait_run(sid, rid, 8)['result']['status'] == 'completed'
            batch = [json.loads(line) for line in cli('events', sid).splitlines()]
            outcome = [event['payload']['result'] for event in batch
                       if event['run_id'] == rid and event['type'] == 'tool.result']
            assert outcome and outcome[0]['stdout'].strip() == 'sandbox boundary confirmed', outcome
            for path in ('.git/probe', 'nested/repo/.git/probe', 'nested/worktree/.git'):
                assert (client_dir / path).read_text() == 'protected-git-canary\n'
            print('Client edit and sandbox boundary confirmed', flush=True)

            rid = cli('run', sid, 'mount:cancel')
            heartbeat = client_dir / 'heartbeat'
            deadline = time.monotonic() + 4
            while (not heartbeat.is_file() or heartbeat.stat().st_size < 2) and time.monotonic() < deadline:
                time.sleep(.04)
            assert heartbeat.stat().st_size >= 2, 'Detached writer did not start'
            runner = f'agentic-mounted-{sid}-{rid}'
            listing = docker('top', runner, '-eo', 'pid,args', timeout=2)
            assert listing.returncode == 0
            writers = [line.split()[0] for line in listing.stdout.splitlines()
                       if line.endswith(' mount-v2-detached-writer')]
            assert len(writers) == 1 and writers[0].isdigit(), listing.stdout
            start = time.monotonic()
            result = json.loads(cli('run-cancel', sid, rid))['result']
            assert result['status'] == 'cancelled' and result['cleanup_state'] == 'confirmed', result
            assert time.monotonic() - start < 4, 'Cancellation blocked on docker exec'
            assert not Path('/proc', writers[0]).exists(), 'Detached writer PID remains'
            saved = heartbeat.read_bytes()
            time.sleep(.25)
            assert saved == heartbeat.read_bytes(), 'Client file changed after cancellation'
            remaining = docker('ps', '-aq', '--filter', 'label=agentic.scope=mounted-' + scope, timeout=3)
            assert remaining.returncode == 0 and not remaining.stdout.strip()
            print('Cancellation removed the sandbox and detached writer; client heartbeat stopped', flush=True)
        finally:
            cli('session-close', sid)


if __name__ == '__main__':
    main()
