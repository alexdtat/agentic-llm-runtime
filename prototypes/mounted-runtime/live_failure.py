"""Bounded host-only SSHFS failure check using a fresh synthetic export."""
import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from track_a import mounted_executor
from track_a.application import Application
from track_a.model_proxy import ModelProxy
from smoke import service

STACK = Path(__file__).with_name('stack.sh')
LOCAL = ['docker', '--host', 'unix:///var/run/docker.sock']
IMAGE = 'sha256:0b57fcf6e6b5d59a6a9f3255b89a1f2ba198d1870b63b6ad06d3199e1fb384f6'
SCRIPT = """
import os
import time
from pathlib import Path
with open('single-write.txt', 'ab', buffering=0) as output:
    output.write(b't08-once\\n')
    os.fsync(output.fileno())
Path('/tmp/t08-ready').touch()
deadline = time.monotonic() + 4
while not Path('/tmp/t08-cut').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('HANDSHAKE_TIMEOUT')
    time.sleep(.02)
print('IO_BEGIN', flush=True)
try:
    Path('after-cut.txt').read_bytes()
except OSError as error:
    print('IO_ERRNO=' + str(error.errno), flush=True)
    raise
print('IO_END', flush=True)
"""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(args, *, seconds=6, env=None, check=True, log=None):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                            timeout=seconds, env=env, cwd=REPOSITORY)
    if log:
        log.write(json.dumps(dict(command=list(map(str, args)), returncode=result.returncode,
                                  stdout=result.stdout, stderr=result.stderr)) + '\n')
        log.flush()
    if check:
        require(result.returncode == 0, f'Command failed ({result.returncode}): {args}')
    return result


def preflight():
    require(not os.environ.get('DOCKER_CONTEXT'), 'Preflight rejects DOCKER_CONTEXT')
    require(os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock') == 'unix:///var/run/docker.sock',
            'Preflight requires the local Docker socket')
    status = Path('/proc/self/status').read_text()
    require(re.search(r'^NoNewPrivs:\s+0$', status, re.M), 'Preflight blocked: NoNewPrivs must be 0')
    require(os.getuid() == 1000, 'Preflight requires the synthetic exporter UID 1000')
    require(Path('/dev/fuse').exists() and stat.S_ISCHR(Path('/dev/fuse').stat().st_mode),
            'Preflight requires the host /dev/fuse character device')
    for binary in ('docker', 'sudo', 'timeout', 'findmnt', 'mount', 'umount', 'ssh-keygen',
                   'realpath', 'sha256sum', 'cut', 'sort', 'grep'):
        require(shutil.which(binary), f'Missing host command: {binary}')
    run([*LOCAL, 'info', '--format', '{{.ServerVersion}}'])
    run([*LOCAL, 'compose', 'version'])
    image = run([*LOCAL, 'image', 'inspect', 'course-feasibility:2026-11-06',
                 '--format', '{{.Id}}']).stdout.strip()
    require(image == IMAGE, 'Unexpected diagnostic image; no pull/build is performed')
    run([*LOCAL, 'image', 'inspect', 'agentic-sandbox:cpu-v1', '--format', '{{.Id}}'])
    run(['sudo', '-n', '-v'])


def process_identity(pid):
    try:
        return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        return None


def io_evidence(worker):
    output = worker.get('stdout', '')
    require('HANDSHAKE_TIMEOUT' not in worker.get('stderr', ''), 'Inconclusive: handshake timed out')
    require('IO_BEGIN\n' in output, 'Inconclusive: command did not reach I/O after cut')
    errno = re.search(r'^IO_ERRNO=(\d+)$', output, re.M)
    blocked = worker.get('error_kind') == 'timeout' and 'IO_END\n' not in output
    require(errno or blocked, 'Inconclusive: no I/O errno or unfinished I/O at worker timeout')
    return {'errno': int(errno[1]) if errno else None, 'io_timed_out': blocked}


class CutProxy(ModelProxy):
    @staticmethod
    def mock(messages):
        if messages[-1]['content'] == 't08-live-cut':
            return {'calls': [{'id': 'cut', 'tool': 'RunCommand',
                               'args': {'command': 'exec python3 -c ' + shlex.quote(SCRIPT)}}]}
        return ModelProxy.mock(messages)


class Probe:
    def __init__(self, root, log):
        self.root, self.log = root, log
        self.env = {**os.environ, 'PROBE_ROOT': str(root), 'DOCKER_HOST': 'unix:///var/run/docker.sock'}
        self.env.pop('DOCKER_CONTEXT', None)
        self.scope = self.command([STACK, 'scope']).stdout.strip()
        self.compose = [*LOCAL, 'compose', '--env-file', '/dev/null', '-p', self.scope,
                        '-f', STACK.with_name('compose.yaml'), '-f', root / 'workspace.json']
        self.identities = {}
        self.bind_attempted = False
        self.stack_attempted = False

    def command(self, args, **kwargs):
        return run(args, env=kwargs.pop('env', self.env), log=self.log, **kwargs)

    def privileged(self, *args):
        return self.command(['sudo', '-n', 'timeout', '-k', '2', '6', *args], seconds=10)

    def containers(self):
        ids = set()
        for label in ('com.docker.compose.project=' + self.scope, 'agentic.scope=mounted-' + self.scope):
            ids.update(self.command([*LOCAL, 'ps', '-aq', '--filter', 'label=' + label]).stdout.split())
        return sorted(ids)

    def remember_processes(self):
        for container in self.containers():
            running = self.command([*LOCAL, 'inspect', container, '--format', '{{.State.Running}}']).stdout.strip()
            if running == 'true':
                self.remember_container(container)

    def remember_container(self, container):
        identities = {}
        output = self.command([*LOCAL, 'top', container, '-eo', 'pid']).stdout
        for line in output.splitlines()[1:]:
            pid = line.split()[0]
            require(pid.isdigit(), 'Invalid host PID from docker top')
            identity = process_identity(pid)
            require(identity is not None, 'Inconclusive: process exited before PID identity capture')
            self.identities[pid] = identity
            identities[pid] = identity
        (self.root / 'owned-processes.json').write_text(json.dumps(self.identities, indent=2))
        return identities

    def mount_type(self, path):
        result = self.command(['findmnt', '-rnC', '--mountpoint', path, '-o', 'FSTYPE'], check=False)
        require(result.returncode in (0, 1), 'Cannot inspect exact mountpoint')
        return result.stdout.strip() if result.returncode == 0 else None

    def cleanup(self):
        if not self.bind_attempted and not self.stack_attempted:
            return {'status': 'not_acquired'}
        # Preserve service diagnostics before stack.sh removes this exact project.
        cleanup_exit = None
        if self.stack_attempted:
            try:
                self.command([*self.compose, 'logs', '--no-color', '--tail', '100'], check=False)
                self.remember_processes()
            except BaseException:
                (self.root / 'diagnostic-failure.txt').write_text(traceback.format_exc())
            cleanup = self.command([STACK, 'cleanup'], seconds=45, check=False)
            cleanup_exit = cleanup.returncode
            require(not self.containers(), 'Cleanup left owned containers')
            networks = self.command([*LOCAL, 'network', 'ls', '-q', '--filter',
                                     'label=com.docker.compose.project=' + self.scope]).stdout.strip()
            require(not networks, 'Cleanup left project networks')
        child = self.root / 'mount/work'
        child_type = self.mount_type(child)
        if child_type is not None:
            require(self.stack_attempted, 'Refusing to unmount an unowned child mount')
            require(child_type == 'fuse.sshfs', 'Refusing to unmount an unexpected child mount')
            self.privileged('umount', '--', child)
        require(self.mount_type(child) is None, 'SSHFS mount remains')
        parent = self.root / 'mount'
        if self.bind_attempted and self.mount_type(parent) is not None:
            self.privileged('umount', '--', parent)
        require(self.mount_type(parent) is None, 'Self-bind remains')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            remaining = [pid for pid, identity in self.identities.items() if process_identity(pid) == identity]
            if not remaining:
                break
            time.sleep(.05)
        require(not remaining, 'Owned host processes remain: ' + repr(remaining))
        written = self.root / 'client/single-write.txt'
        if written.exists():
            require(written.read_bytes() == b't08-once\n', 'Client file changed during cleanup')
        return {'containers': [], 'networks': [], 'mounts': [], 'owned_processes': [],
                'stack_cleanup_exit': cleanup_exit}

    def setup(self):
        require(self.mount_type(self.root / 'mount') is None, 'Fresh self-bind path already mounted')
        require(self.mount_type(self.root / 'mount/work') is None, 'Fresh child path already mounted')
        require(not self.containers(), 'Refusing to acquire an existing project or runtime scope')
        networks = self.command([*LOCAL, 'network', 'ls', '-q', '--filter',
                                 'label=com.docker.compose.project=' + self.scope]).stdout.strip()
        require(not networks, 'Refusing to acquire an existing project network')
        self.bind_attempted = True
        self.privileged('mount', '--bind', self.root / 'mount', self.root / 'mount')
        self.privileged('mount', '--make-private', self.root / 'mount')
        self.privileged('mount', '--make-rshared', self.root / 'mount')
        self.stack_attempted = True
        self.command([STACK, 'up'], seconds=35)
        self.remember_processes()

    def compose_container(self, service_name):
        ids = self.command([*self.compose, 'ps', '-q', service_name]).stdout.split()
        require(len(ids) == 1 and re.fullmatch(r'[0-9a-f]{12,64}', ids[0]), 'No unique container for ' + service_name)
        container = ids[0]
        inspection = json.loads(self.command([*LOCAL, 'inspect', container, '--format',
                                              '{{json .Config.Labels}}']).stdout)
        require(inspection.get('com.docker.compose.project') == self.scope and
                inspection.get('com.docker.compose.service') == service_name, 'Compose ownership mismatch')
        return container

    def exercise(self):
        self.setup()
        (self.root / 'client/after-cut.txt').write_text('synthetic-after-cut\n')
        exporter = self.compose_container('exporter')
        root = self.root
        executor = mounted_executor.MountedExecutor(str(root / 'mount/work'), 'live-grant', self.scope,
                                                    'agentic-sandbox:cpu-v1', root / 'execution-state')
        executor.recover()
        worker_results = []
        real_docker = mounted_executor.docker

        def observed_docker(*args, **kwargs):
            # Observe the real Docker call unchanged, including worker output lost at the API boundary.
            try:
                result = real_docker(*args, **kwargs)
            except subprocess.TimeoutExpired as error:
                if args[:2] == ('exec', '-i'):
                    def decoded(value):
                        return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
                    result = SimpleNamespace(returncode=None, stdout=decoded(error.stdout),
                                             stderr=decoded(error.stderr))
                    worker_results.append(result)
                    self.log.write(json.dumps(dict(worker_transport_timeout=True,
                                                   stdout=result.stdout, stderr=result.stderr)) + '\n')
                    self.log.flush()
                raise
            if args[:2] == ('exec', '-i'):
                worker_results.append(result)
                self.log.write(json.dumps(dict(worker_returncode=result.returncode,
                                               stdout=result.stdout, stderr=result.stderr)) + '\n')
                self.log.flush()
            return result

        mounted_executor.docker = observed_docker
        try:
            with contextlib.ExitStack() as services:
                owner = secrets.token_hex(24)
                backend = services.enter_context(service(executor, secrets.token_hex(24)))
                model = services.enter_context(service(CutProxy(SimpleNamespace(backend='mock')), secrets.token_hex(24)))
                app = Application(backend, model, {'local': owner}, root / 'app-state', mount_owner='local')
                api = services.enter_context(service(app, owner))

                def cli(*args):
                    return self.command([sys.executable, '-m', 'track_a.cli', '--url', api.base, *args],
                                        seconds=12, env={**self.env, 'PROBE_TOKEN': owner}).stdout.strip()

                sid = cli('mount-new', 'live-grant')
                rid = cli('run', sid, 't08-live-cut')
                runner = f'agentic-mounted-{sid}-{rid}'
                deadline = time.monotonic() + 3
                while True:
                    probe = self.command([*LOCAL, 'exec', '--workdir', '/tmp', runner, 'test', '-f', '/tmp/t08-ready'],
                                         seconds=2, check=False)
                    if probe.returncode == 0:
                        break
                    require(time.monotonic() < deadline, 'Inconclusive: command did not reach handshake')
                    time.sleep(.03)
                require((root / 'client/single-write.txt').read_bytes() == b't08-once\n', 'First write is not unique')
                runner_pids = self.remember_container(runner)
                accepted = executor.store.journals[sid].load()
                require(len(accepted['operations']) == 1, 'Expected one durable operation')
                operation = next(iter(accepted['operations'].values()))
                require(operation['result'] is None, 'Operation completed before SSHFS cut')
                self.command([*LOCAL, 'stop', '--time', '1', exporter], seconds=4)
                stopped = self.command([*LOCAL, 'inspect', exporter, '--format', '{{.State.Running}}']).stdout.strip()
                require(stopped == 'false', 'Exporter stop was not confirmed')
                self.command([*LOCAL, 'exec', '--workdir', '/tmp', runner, 'touch', '/tmp/t08-cut'], seconds=2)
                result = api.wait_run(sid, rid, 12)['result']
                (root / 'run-result.json').write_text(json.dumps(result, indent=2))
                require(len(worker_results) == 1, 'Unexpected number of executed worker operations')
                raw = worker_results[0]
                worker = json.loads(raw.stdout)
                evidence = io_evidence(worker)
                require(result['status'] == 'interrupted' and result['reason'] == 'outcome_unknown',
                        'Actual mount cut did not produce the required unknown operation outcome')
                require(result['cleanup_state'] == 'confirmed', 'Application stop was not confirmed')
                remaining = self.command([*LOCAL, 'ps', '-aq', '--filter', 'name=^/' + runner + '$']).stdout.split()
                live_pids = [pid for pid, identity in runner_pids.items() if process_identity(pid) == identity]
                (root / 'product-stop-check.json').write_text(json.dumps(
                    {'containers': remaining, 'processes': live_pids}, indent=2))
                require(not remaining and not live_pids, 'Product stop left a sandbox or owned process')
                private_path = f'/mounted/{sid}'
                op_id = next(iter(accepted['operations']))
                request = operation['request']
                for path, body in (
                    ('operation', {'generation': 1, 'op_id': op_id}),
                    ('exec', request), ('exec', {**request, 'op_id': 'new-op'}),
                ):
                    status, _ = backend.request('POST', private_path + '/' + path, body, timeout=3)
                    require(status == 409, 'Operation was replayed or unknown result was confirmed')
                require(len(worker_results) == 1, 'A duplicate operation reached the worker')
                require(backend.request('POST', private_path + '/stop',
                                        {'generation': 1, 'run_id': rid}, timeout=3) ==
                        (200, {'cleanup_state': 'confirmed'}), 'Idempotent stop failed')
                require((root / 'client/single-write.txt').read_bytes() == b't08-once\n', 'Replay changed client file')
                cli('session-close', sid)
                return {'status': 'passed', 'scope': self.scope, 'session': sid, 'run': rid,
                        'exporter': exporter, 'single_write_bytes': 9, 'worker_dispatches': len(worker_results),
                        'io': evidence, 'run_result': result}
        finally:
            mounted_executor.docker = real_docker
            executor.containers.journal.db.close()
            for journal in executor.store.journals.values():
                journal.db.close()


class ProbeDeadline(BaseException):
    pass


def run_probe(probe_class, evidence_name, *, exercise_seconds=100):
    def interrupted(*_):
        raise KeyboardInterrupt()

    def deadline_exceeded(*_):
        raise ProbeDeadline('Probe deadline exceeded')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        preflight()
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'phase': 'preflight', 'error': str(error)}), flush=True)
        return 2
    os.environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
    os.environ.pop('DOCKER_CONTEXT', None)
    prepared = run([sys.executable, STACK.with_name('prepare.py')]).stdout.strip()
    require(re.fullmatch(r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}', prepared), 'Unexpected synthetic root')
    root = Path(prepared)
    require(root.resolve() == root and root.stat().st_uid == os.getuid() and
            (root / 'marker').read_text() == 'agentic-mounted-runtime\n', 'Invalid synthetic root ownership')
    print('Evidence directory: ' + str(root), flush=True)
    signal.signal(signal.SIGALRM, deadline_exceeded)
    signal.alarm(exercise_seconds)
    result = {'status': 'failed', 'root': str(root)}
    with (root / (evidence_name + '.jsonl')).open('a') as log:
        probe = None
        try:
            probe = probe_class(root, log)
            result.update(probe.exercise())
        except BaseException:
            (root / 'failure.txt').write_text(traceback.format_exc())
        finally:
            signal.alarm(90)
            try:
                if probe is not None:
                    result['ownership'] = {'bind_attempted': probe.bind_attempted,
                                           'stack_attempted': probe.stack_attempted}
                    result['cleanup'] = probe.cleanup()
            except BaseException:
                result['status'] = 'failed'
                result['cleanup'] = {'status': 'failed', 'error': str(sys.exc_info()[1])}
                (root / 'cleanup-failure.txt').write_text(traceback.format_exc())
            (root / (evidence_name + '-summary.json')).write_text(json.dumps(result, indent=2))
            signal.alarm(0)
    print(json.dumps(result), flush=True)
    return 0 if result['status'] == 'passed' else 1


def main():
    return run_probe(Probe, 'live-failure')


if __name__ == '__main__':
    sys.exit(main())
