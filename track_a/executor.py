"""Private, fixed-policy Docker executor. No model keys or client mounts."""
import copy
import json
import os
import signal
import subprocess
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

from track_a.http_api import Conflict, command, key, serve
from track_a.storage import Journal

WORKER = Path(__file__).with_name('worker.py').read_text()


def docker(*args, timeout=15, input=None):
    return subprocess.run(['docker', *map(str, args)], input=input, text=True,
                          capture_output=True, timeout=timeout)


def failure(kind, status='failed'):
    return dict(status=status, error_kind=kind, exit_code=None, stdout='', stderr='')


def sandbox_args(name, scope):
    return [
        '--name', name, '--pull', 'never', '--label', 'agentic.scope=' + scope,
        '--runtime', 'runc', '--init', '--network', 'none', '--cpus', '1',
        '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--tmpfs', '/tmp:rw,size=16m,mode=1777', '--user', '1000:1000',
    ]


class State:
    def __init__(self, path=':memory:', scope='local', image='agentic-sandbox:cpu-v1'):
        self.lock = threading.RLock()
        self.journal = Journal(path)
        saved = self.journal.load()
        self.sessions = saved.get('sessions', {})
        self.runs = saved.get('runs', {})
        self.scope, self.image = scope, image
        self.stopping = False

    def save(self):
        self.journal.save({'sessions': self.sessions, 'runs': self.runs})

    def terminal(self, run, result):
        if run['result'] is None:
            run.update(result=result, status=result['status'])
            run['events'].append({'type': 'result', 'sequence': len(run['events']), **result})
            self.save()

    def remove(self, name):
        result = docker('rm', '-f', name)
        if result.returncode:
            # Inspect failure alone does not prove absence: the daemon may be down.
            listing = docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}')
            if listing.returncode or name in listing.stdout.splitlines():
                raise RuntimeError('Sandbox removal failed')

    def recover(self):
        # Label scope is stable across restarts, isolated across Compose projects.
        result = docker('ps', '-a', '--filter', 'label=agentic.scope=' + self.scope,
                        '--format', '{{.Names}}')
        if result.returncode:
            raise RuntimeError('Cannot reconcile sandbox containers')
        for name in result.stdout.splitlines():
            self.remove(name)
        with self.lock:
            for session in self.sessions.values():
                session['status'] = 'closed'
            for run in self.runs.values():
                self.terminal(run, failure('interrupted'))
            self.save()

    def health(self):
        return 200, {'version': 'v0', 'status': 'ok', 'component': 'executor'}

    def create(self, request_id):
        with self.lock:
            for session in self.sessions.values():
                if session['request_id'] == request_id:
                    return session
            if self.stopping:
                raise Conflict('Executor is stopping')
            if sum(s['status'] != 'closed' for s in self.sessions.values()) >= 16:
                raise Conflict('Session capacity reached')
            sid = uuid.uuid4().hex
            name = 'agentic-' + sid
            session = dict(id=sid, container=name, status='provisioning', request_id=request_id)
            self.sessions[sid] = session
            self.save()
            try:
                result = docker('run', '-d', *sandbox_args(name, self.scope),
                                '--tmpfs', '/work:rw,size=16m,mode=1777', self.image)
                if result.returncode:
                    raise RuntimeError('Sandbox provisioning failed')
                session['status'] = 'open'
            except Exception:
                session['status'] = 'cleanup_failed'
                self.save()
                self.remove(name)
                session['status'] = 'closed'
                self.save()
                raise
            self.save()
            return session

    def start(self, sid, payload):
        with self.lock:
            session = self.sessions[sid]
            # A retry must return the original outcome even after session closure.
            for run in self.runs.values():
                if run['session_id'] == sid and run['request']['request_id'] == payload['request_id']:
                    if run['request'] != payload:
                        raise Conflict('Idempotency key reused with different payload')
                    return run
            if self.stopping or session['status'] != 'open':
                raise Conflict('Session is not open')
            if any(r['session_id'] == sid and r['result'] is None for r in self.runs.values()):
                raise Conflict('Session already has an active command')
            rid = uuid.uuid4().hex
            run = dict(id=rid, session_id=sid, status='running', request=payload,
                       events=[{'type': 'started', 'sequence': 0}], result=None)
            self.runs[rid] = run
            self.save()
            threading.Thread(target=self.execute, args=(run, session), daemon=True).start()
            return run

    def execute(self, run, session):
        try:
            result = docker('exec', '-i', session['container'], 'python3', '-c', WORKER,
                            input=json.dumps(run['request']), timeout=run['request']['timeout_seconds'] + 5)
            if result.returncode:
                outcome = failure('sandbox')
            else:
                outcome = json.loads(result.stdout)
                if outcome.get('status') not in {'complete', 'failed', 'timed_out'}:
                    raise ValueError('Invalid worker result')
        except subprocess.TimeoutExpired:
            outcome = failure('transport')
        except Exception:
            outcome = failure('sandbox')
        with self.lock:
            if run['result'] is not None:
                return
            if outcome['error_kind'] in {'sandbox', 'transport', 'timeout'}:
                # A failed worker cannot certify process cleanup. Destroy the sandbox.
                try:
                    self.close(session['id'], outcome)
                except Exception:
                    # No terminal success/cancel before cleanup is confirmed.
                    session['status'] = 'cleanup_failed'
                    self.terminal(run, failure('cleanup_failed'))
            else:
                self.terminal(run, outcome)

    def close(self, sid, active_result=None):
        with self.lock:
            session = self.sessions[sid]
            if session['status'] == 'closed':
                return session
            session['status'] = 'closing'
            self.save()
            try:
                self.remove(session['container'])
            except Exception:
                session['status'] = 'cleanup_failed'
                self.save()
                raise
            session['status'] = 'closed'
            for run in self.runs.values():
                if run['session_id'] == sid:
                    self.terminal(run, active_result or failure('cancelled', 'cancelled'))
            self.save()
            return session

    def cancel(self, rid):
        with self.lock:
            run = self.runs[rid]
            if run['result'] is None:
                self.close(run['session_id'])
            return run

    def route(self, method, path, body):
        parts = path.strip('/').split('/')
        with self.lock:
            if method == 'POST' and parts == ['v0', 'sessions']:
                if set(body) != {'request_id'}:
                    raise TypeError('Invalid session fields; mounting is not implemented')
                key(body['request_id'])
                value = self.create(body['request_id'])
            elif method == 'GET' and len(parts) == 3 and parts[:2] == ['v0', 'sessions']:
                value = self.sessions[parts[2]]
            elif method == 'POST' and len(parts) == 4 and parts[:2] == ['v0', 'sessions'] and parts[3] == 'runs':
                command(body)
                value = self.start(parts[2], body)
            elif method == 'GET' and len(parts) == 3 and parts[:2] == ['v0', 'runs']:
                value = self.runs[parts[2]]
            elif method == 'POST' and len(parts) == 4 and parts[:2] == ['v0', 'runs'] and parts[3] == 'cancel':
                if body:
                    raise TypeError('Cancel body must be empty')
                value = self.cancel(parts[2])
            elif method == 'DELETE' and len(parts) == 3 and parts[:2] == ['v0', 'sessions']:
                if body:
                    raise TypeError('Release body must be empty')
                value = self.close(parts[2])
            else:
                return 404, {'error': 'unknown_route'}
            return 200, copy.deepcopy(value)

    def shutdown(self):
        with self.lock:
            self.stopping = True
        errors = []
        for sid in list(self.sessions):
            try:
                self.close(sid)
            except Exception:
                errors.append(sid)
        if errors:
            raise RuntimeError('Some sandboxes require startup reconciliation')


def main():
    state = State(os.environ.get('STATE_FILE', ':memory:'),
                  os.environ.get('RUNTIME_SCOPE', 'local'),
                  os.environ.get('SANDBOX_IMAGE', 'agentic-sandbox:cpu-v1'))
    state.recover()
    serve(state, int(os.environ.get('PORT', '8081')))


if __name__ == '__main__':
    main()
