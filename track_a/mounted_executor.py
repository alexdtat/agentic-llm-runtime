"""Private v2 executor for one operator-provisioned SSHFS export on local Docker."""
import json
import os
import subprocess
import threading

from track_a.contracts import fields, tool_call
from track_a.executor import WORKER, State, docker, sandbox_args
from track_a.http_api import Conflict, key, serve
from track_a.storage import SessionJournals

MOUNT_CHECK = """
import os
from pathlib import Path
with open('/proc/self/mountinfo') as mounts:
    assert any(line.split()[4] == '/work' and line.split(' - ')[1].startswith('fuse.sshfs ')
               for line in mounts), 'Expected a real SSHFS mount in /work'
os.stat('/work')
"""


class MountedExecutor:
    def __init__(self, mount, grant, scope, image, root):
        key(grant)
        key(scope)
        if not os.path.isabs(mount) or ',' in mount or os.path.normpath(mount) != mount:
            raise ValueError('Configure an absolute, normalized SSHFS mountpoint')
        self.mount, self.grant = mount, grant
        self.containers = State(scope='mounted-' + scope, image=image)
        self.store = SessionJournals(root)
        self.sessions = self.store.load()
        self.lock = threading.Lock()
        self.stopping = False

    def health(self):
        return 200, {'status': 'ok', 'backend': 'mounted', 'client_mount': True}

    def name(self, session, run_id):
        return f"agentic-mounted-{session['id']}-{run_id}"

    def check_mount(self):
        result = subprocess.run(['findmnt', '-rnC', '--mountpoint', self.mount, '-o', 'FSTYPE'],
                                capture_output=True, text=True, timeout=2)
        if result.returncode or set(result.stdout.split()) != {'fuse.sshfs'}:
            raise RuntimeError('SSHFS mount is unavailable')

    def run_container(self, name, *command, readonly=False, detach=False, timeout=6, input=None):
        self.check_mount()
        bind = f'type=bind,src={self.mount},dst=/work,bind-propagation=rprivate,bind-recursive=disabled'
        if readonly:
            bind += ',readonly'
        mode = '-d' if detach else '-i'
        return docker('run', mode, *sandbox_args(name, self.containers.scope), '--mount', bind,
                      self.containers.image, *command, timeout=timeout, input=input)

    def recover(self):
        self.containers.recover()
        for session in self.sessions.values():
            if session.get('active') is not None:
                self.mark_stopped(session, session['active'])

    def mark_stopped(self, session, run_id):
        if run_id not in session['stopped']:
            session['stopped'].append(run_id)
        session['active'] = None
        self.store.save(session)

    def route(self, method, path, body):
        parts = path.strip('/').split('/')
        if method != 'POST' or len(parts) != 3 or parts[0] != 'mounted':
            return 404, {'error': 'unknown_route'}
        _, sid, action = parts
        key(sid)
        if action == 'exec':
            return self.execute(sid, body)
        with self.lock:
            if action == 'provision':
                fields(body, 'grant')
                if body['grant'] != self.grant:
                    raise Conflict('Unknown export grant')
                if sid not in self.sessions:
                    if self.stopping or any(not s['closed'] for s in self.sessions.values()):
                        raise Conflict('Export is already leased')
                    self.check_mount()
                    session = dict(id=sid, generation=1, active=None, closed=False,
                                   operations={}, stopped=[])
                    self.store.save(session)
                    self.sessions[sid] = session
                session = self.sessions[sid]
                return 200, {'generation': session['generation'], 'closed': session['closed']}
            if action == 'release':
                fields(body)
                session = self.sessions.get(sid)
                if session and session.get('active'):
                    self.stop(session, session['active'])
                tombstone = dict(id=sid, generation=1, closed=True)
                self.store.save(tombstone)
                self.sessions[sid] = tombstone
                return 200, {'cleanup_state': 'confirmed'}

            session = self.sessions[sid]
            if session['closed']:
                return 410, {'error': 'released'}
            if action == 'operation':
                fields(body, 'generation', 'op_id')
                self.check_generation(session, body)
                key(body['op_id'])
                return 200, self.operation_result(session['operations'][body['op_id']])
            if action not in {'start', 'stop', 'collect'}:
                return 404, {'error': 'unknown_route'}
            fields(body, 'generation', 'run_id')
            self.check_generation(session, body)
            rid = body['run_id']
            key(rid)
            if action == 'start':
                self.start(session, rid)
                return 200, {}
            if action == 'stop':
                self.stop(session, rid)
                return 200, {'cleanup_state': 'confirmed'}
            if session['active'] is not None or rid not in session['stopped']:
                raise Conflict('Workspace is active')
            return 200, self.collect(session, rid)

    @staticmethod
    def check_generation(session, body):
        if type(body['generation']) is not int or body['generation'] != session['generation']:
            raise Conflict('Stale generation')

    @staticmethod
    def operation_result(operation):
        if operation['result'] is None:
            raise Conflict('Operation outcome is unknown')
        return operation['result']

    def start(self, session, rid):
        if self.stopping or rid in session['stopped'] or session['active'] not in {None, rid}:
            raise Conflict('Cannot start run')
        if session['active'] == rid:
            raise Conflict('Run already started')
        session['active'] = rid
        self.store.save(session)
        name = self.name(session, rid)
        # The sandbox runs only trusted mount validation until tools arrive.
        ready = 'import time; from pathlib import Path; Path("/tmp/mount-ready").touch(); time.sleep(120)'
        result = self.run_container(name, 'python3', '-c', MOUNT_CHECK + ready, detach=True)
        if result.returncode:
            raise RuntimeError('Cannot start mounted sandbox')
        wait = 'import time; from pathlib import Path\n' + (
            'for _ in range(20):\n'
            '    if Path("/tmp/mount-ready").is_file(): break\n'
            '    time.sleep(.05)\n'
            'else: raise SystemExit(1)\n'
        )
        if docker('exec', name, 'python3', '-c', wait, timeout=2).returncode:
            raise RuntimeError('Sandbox did not confirm the SSHFS mount')

    def stop(self, session, rid):
        if rid in session['stopped']:
            return
        if session['active'] not in {None, rid}:
            raise Conflict('Another run owns the workspace')
        if session['active'] == rid:
            self.containers.remove(self.name(session, rid))
        self.mark_stopped(session, rid)

    def execute(self, sid, body):
        fields(body, 'generation', 'run_id', 'op_id', 'call')
        rid, op_id = body['run_id'], body['op_id']
        key(rid)
        key(op_id)
        call = tool_call(body['call'])
        with self.lock:
            session = self.sessions[sid]
            if session['closed']:
                return 410, {'error': 'released'}
            self.check_generation(session, body)
            if op_id in session['operations']:
                previous = session['operations'][op_id]
                if previous['request'] != body:
                    raise Conflict('Operation key reused with different payload')
                return 200, self.operation_result(previous)
            if self.stopping or session['active'] != rid:
                raise Conflict('Run is inactive')
            if any(op['request']['run_id'] == rid and op['result'] is None
                   for op in session['operations'].values()):
                raise Conflict('An operation outcome is unknown')
            operation = {'request': body, 'result': None}
            session['operations'][op_id] = operation
            self.store.save(session)

        # Never hold the executor lock while the worker or its descendants are running.
        name = self.name(session, rid)
        value = self.execute_tool(name, call)
        with self.lock:
            if self.sessions[sid] is not session or session['active'] != rid:
                raise Conflict('Run stopped during execution')
            operation['result'] = value
            self.store.save(session)
        return 200, value

    @staticmethod
    def execute_tool(name, call):
        tool, args = call['tool'], call['args']
        if tool == 'RunCommand':
            argv, limit = ['/bin/sh', '-c', args['command']], 1024
        else:
            argv = ['python3', '-c', WORKER, tool, args['path']]
            if tool == 'WriteFile':
                argv.append(args['data'])
            limit = 8193
        request = {'argv': argv, 'timeout_seconds': 6, 'output_bytes': limit}
        result = docker('exec', '-i', name, 'python3', '-c',
                        MOUNT_CHECK + WORKER.replace("if __name__ == '__main__':", "if False:") +
                        '\nmain()\nos.stat("/work")\n',
                        timeout=8, input=json.dumps(request))
        if result.returncode:
            raise RuntimeError('No confirmed worker result')
        value = json.loads(result.stdout)
        if value['error_kind'] not in {None, 'command'} or (tool == 'WriteFile' and value['exit_code']):
            raise RuntimeError('No confirmed tool outcome')
        outcome = {field: value[field] for field in ('exit_code', 'stdout', 'stderr', 'truncated')}
        outcome['error_kind'] = 'command_failure' if value['error_kind'] else None
        if tool == 'ReadFile' and value['exit_code'] == 0:
            outcome.update(encoding='base64', data=outcome.pop('stdout').strip(), stdout='')
        return outcome

    def collect(self, session, rid):
        # Collect named artifacts only, after removing the sandbox that could write them.
        paths = sorted({op['request']['call']['args']['path'] for op in session['operations'].values()
                        if op['request']['run_id'] == rid and op['result'] is not None
                        and op['result']['exit_code'] == 0
                        and op['request']['call']['tool'] in {'ReadFile', 'WriteFile'}})[:16]
        if not paths:
            return {'files': {}}
        source = MOUNT_CHECK + WORKER.replace("if __name__ == '__main__':", "if False:") + """
import contextlib
import io
files = {}
for path in json.load(sys.stdin):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        try:
            file_tool('ReadFile', path)
        except (FileNotFoundError, ValueError):
            continue
    files[path] = output.getvalue().strip()
print(json.dumps(files))
"""
        name = self.name(session, rid) + '-collect'
        try:
            result = self.run_container(name, 'python3', '-c', source, readonly=True,
                                        input=json.dumps(paths))
            if result.returncode:
                raise RuntimeError('Cannot collect mounted artifacts')
            return {'files': json.loads(result.stdout)}
        finally:
            self.containers.remove(name)

    def shutdown(self):
        with self.lock:
            self.stopping = True
            self.recover()


def main():
    if os.environ.get('DOCKER_CONTEXT') or os.environ.get('DOCKER_HOST') != 'unix:///var/run/docker.sock':
        raise RuntimeError('Use the local Docker socket for the mounted profile')
    backend = MountedExecutor(os.environ['SSHFS_MOUNT'], os.environ['MOUNT_GRANT'],
                              os.environ['RUNTIME_SCOPE'], os.environ.get('SANDBOX_IMAGE', 'agentic-sandbox:cpu-v1'),
                              os.environ['STATE_DIR'])
    backend.recover()
    backend.check_mount()
    serve(backend, int(os.environ.get('PORT', '8081')))


if __name__ == '__main__':
    main()
