"""v2 Application. Public session/run state is independent of execution state."""
import copy
import hashlib
import json
import os
import secrets
import threading
import time
import uuid

from track_a.client import Client
from track_a.contracts import fields, fixture_files
from track_a.http_api import Conflict, key, serve
from track_a.run_loop import RunError, run_agent
from track_a.storage import SessionJournals

CONFIG = {'model': 'default', 'harness': 'sequential-v2', 'steps': 8,
          'context_bytes': 14000, 'seconds': 60, 'execution': 'fixture'}


class Application:
    def __init__(self, executor, proxy, owners, root=None, harness=run_agent, *, mount_owner=None):
        self.executor, self.proxy, self.harness = executor, proxy, harness
        if not isinstance(owners, dict) or not owners or any(
                not isinstance(token, str) or not token for token in owners.values()
        ) or len(set(owners.values())) != len(owners):
            raise ValueError('Configure distinct owner tokens')
        self.owners = {owner: ('Bearer ' + token).encode() for owner, token in owners.items()}
        if mount_owner is not None and set(owners) != {mount_owner}:
            raise ValueError('The mounted profile requires one configured owner')
        self.execution_profile = 'mounted' if mount_owner is not None else 'fixture'
        self.store = SessionJournals(root)
        self.sessions = self.store.load()
        self.locks = {sid: threading.RLock() for sid in self.sessions}
        self.lock = threading.Lock()
        self.stopping = False

    def authenticate(self, header):
        supplied = header.encode()
        return next((owner for owner, token in self.owners.items()
                     if secrets.compare_digest(token, supplied)), None)

    @staticmethod
    def request(client, path, body):
        status, value = client.request('POST', path, body, timeout=10)
        if status != 200:
            raise RunError('upstream_rejected')
        return value

    def execution(self, session, action, body):
        return self.request(self.executor, f"/{self.execution_profile}/{session['id']}/{action}", body)

    def health(self):
        try:
            for client in (self.proxy, self.executor):
                if client.request('GET', '/v0/health', timeout=2)[0] != 200:
                    raise RunError('dependency_unavailable')
            return 200, {'status': 'ok', 'version': 'v2', 'execution': self.execution_profile,
                         'client_mount': self.execution_profile == 'mounted'}
        except (OSError, RunError):
            return 503, {'status': 'dependency_unavailable'}

    def create(self, owner, body):
        workspace = 'grant' if self.execution_profile == 'mounted' else 'files'
        fields(body, 'request_id', workspace)
        key(body['request_id'])
        if workspace == 'grant':
            key(body['grant'])
            provision = {'grant': body['grant']}
            fingerprint_input = provision
        else:
            files = fixture_files(body['files'])
            provision = {'files': files}
            fingerprint_input = files
        fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True).encode()).hexdigest()
        with self.lock:
            session = next((s for s in self.sessions.values() if
                            s['owner'] == owner and s['request_id'] == body['request_id']), None)
            if session is None:
                if self.stopping or sum(s['status'] != 'closed' for s in self.sessions.values()) >= 16:
                    raise Conflict('Session capacity reached')
                sid = uuid.uuid4().hex
                session = dict(id=sid, owner=owner, request_id=body['request_id'],
                               fingerprint=fingerprint, status='provisioning', generation=None,
                               runs={}, active_run=None, history=[], events=[], cursor=0)
                self.store.save(session)
                self.sessions[sid] = session
                self.locks[sid] = threading.RLock()
            sid = session['id']
        with self.locks[sid]:
            if session['fingerprint'] != fingerprint:
                raise Conflict('Session key reused with different payload')
            if session['status'] == 'provisioning':
                lease = self.execution(session, 'provision', provision)
                if lease['closed']:
                    raise RunError('provisioning_failed')
                session.update(status='open', generation=lease['generation'])
                self.store.save(session)
            return 200, self.snapshot(session)

    @staticmethod
    def snapshot(session):
        return {key: session.get(key) for key in ('id', 'status', 'active_run', 'cursor')}

    def event(self, session, run, event_type, **payload):
        session['cursor'] += 1
        sequence = session['cursor']
        session['events'].append(dict(schema_version=2, event_id=f"{session['id']}:{sequence}",
                                      sequence=sequence, timestamp=time.time(), session_id=session['id'],
                                      run_id=run['id'], type=event_type, payload=payload))
        session['events'] = session['events'][-128:]
        self.store.save(session)

    def start(self, session, body):
        fields(body, 'request_id', 'message')
        key(body['request_id'])
        message = body['message']
        if not isinstance(message, str) or not 1 <= len(message) <= 8000:
            raise TypeError('Invalid message')
        for run in session['runs'].values():
            if run['request_id'] == body['request_id']:
                if run['message'] != message:
                    raise Conflict('Run key reused with different payload')
                return run
        if self.stopping or session['status'] != 'open' or session['active_run']:
            raise Conflict('Session unavailable or busy')
        if len(session['runs']) >= 32:
            raise Conflict('Session run capacity reached')
        rid = uuid.uuid4().hex
        run = dict(id=rid, session_id=session['id'], request_id=body['request_id'], message=message,
                   status='queued', cleanup_state='pending', result=None,
                   config={**CONFIG, 'execution': self.execution_profile},
                   deadline=time.time() + CONFIG['seconds'])
        session['runs'][rid] = run
        session['active_run'] = rid
        self.event(session, run, 'run.status', status='queued')
        threading.Thread(target=self.execute, args=(session, run), daemon=True).start()
        return run

    def finish(self, session, run, status, reason=None, answer=None, usage=None):
        if run['result'] is not None:
            return
        phase = 'cancelling' if status == 'cancelled' else 'finalizing'
        run['status'] = phase
        self.event(session, run, 'run.status', status=phase)
        try:
            cleanup = self.execution(session, 'stop', self.scope(session, run))
            if cleanup['cleanup_state'] != 'confirmed':
                raise RunError('cleanup_unconfirmed')
        except (OSError, RunError):
            status, reason, answer = 'interrupted', 'cleanup_unconfirmed', None
            run['cleanup_state'] = 'quarantined'
            session['status'] = 'quarantined'
        else:
            run['cleanup_state'] = 'confirmed'
        if status == 'completed':
            session['history'].extend([{'role': 'user', 'content': run['message']},
                                       {'role': 'assistant', 'content': answer}])
        run['status'] = status
        run['result'] = {'status': status, 'reason': reason, 'answer': answer, 'usage': usage,
                         'cleanup_state': run['cleanup_state']}
        session['active_run'] = None
        self.event(session, run, 'run.result', **run['result'])

    @staticmethod
    def scope(session, run):
        return {'generation': session['generation'], 'run_id': run['id']}

    def execute(self, session, run):
        lock = self.locks[session['id']]

        def check_active():
            with lock:
                if run['result'] is not None:
                    raise RunError('run_finished')
                if time.time() >= run['deadline']:
                    raise RunError('deadline_exceeded')

        def emit(kind, **payload):
            with lock:
                check_active()
                self.event(session, run, kind, **payload)

        def tool(call):
            with lock:
                check_active()
                op_id = uuid.uuid4().hex
                emit('tool.started', op_id=op_id, call=call)
                scope = self.scope(session, run)
            try:
                result = self.execution(session, 'exec', {**scope, 'op_id': op_id, 'call': call})
            except (OSError, RunError):
                check_active()
                try:
                    result = self.execution(session, 'operation',
                                            {'generation': scope['generation'], 'op_id': op_id})
                except (OSError, RunError) as exc:
                    raise RunError('outcome_unknown') from exc
            emit('tool.result', op_id=op_id, result=result)
            return result

        try:
            with lock:
                check_active()
                self.execution(session, 'start', self.scope(session, run))
                run['status'] = 'running'
                self.event(session, run, 'run.status', status='running')
                history = copy.deepcopy(session['history'])
            answer, usage = self.harness(
                history, run['message'], run['config'],
                lambda messages: self.request(self.proxy, '/generate', {'messages': messages}),
                tool, emit, check_active,
            )
            with lock:
                check_active()
                self.finish(session, run, 'completed', answer=answer, usage=usage)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RunError) else 'component_error'
            with lock:
                status = 'interrupted' if reason == 'outcome_unknown' else 'failed'
                self.finish(session, run, status, reason=reason)

    def recover(self):
        for session in self.sessions.values():
            if session.get('active_run'):
                run = session['runs'][session['active_run']]
                self.finish(session, run, 'interrupted', reason='application_restart')

    def close(self, session):
        if session.get('active_run'):
            self.finish(session, session['runs'][session['active_run']], 'cancelled', reason='session_closed')
        self.execution(session, 'release', {})
        tombstone = {key: session[key] for key in ('id', 'owner', 'request_id', 'fingerprint')}
        tombstone['status'] = 'closed'
        self.store.save(tombstone)
        session.clear()
        session.update(tombstone)

    def route(self, owner, method, path, body, query):
        parts = path.strip('/').split('/')
        if method == 'POST' and parts == ['v2', 'sessions']:
            return self.create(owner, body)
        if len(parts) not in (3, 4, 5, 6) or parts[:2] != ['v2', 'sessions']:
            return 404, {'error': 'unknown_route'}
        _, _, sid, *suffix = parts
        with self.lock:
            session = self.sessions[sid]
            lock = self.locks[sid]
        with lock:
            if session['owner'] != owner:
                raise KeyError(sid)
            if suffix == [] and method == 'GET':
                return 200, self.snapshot(session)
            if suffix == ['close'] and method == 'POST':
                fields(body)
                if session['status'] != 'closed':
                    self.close(session)
                return 200, self.snapshot(session)
            if session['status'] == 'closed':
                return 410, {'error': 'session_closed'}
            if suffix == ['runs'] and method == 'POST':
                return 200, copy.deepcopy(self.start(session, body))
            if suffix == ['events'] and method == 'GET':
                after = int(query.get('after', ['0'])[0])
                events = session['events']
                if after < 0 or after > session['cursor']:
                    raise TypeError('Invalid event cursor')
                if events and after < events[0]['sequence'] - 1:
                    return 410, {'error': 'cursor_expired', 'snapshot': self.snapshot(session)}
                return 200, [event for event in events if event['sequence'] > after][:32]
            if len(suffix) in (2, 3) and suffix[0] == 'runs':
                run = session['runs'][suffix[1]]
                if len(suffix) == 2 and method == 'GET':
                    return 200, copy.deepcopy(run)
                if len(suffix) == 3 and method == 'POST':
                    fields(body)
                    if suffix[2] == 'cancel':
                        self.finish(session, run, 'cancelled', reason='requested')
                        return 200, copy.deepcopy(run)
                    if suffix[2] == 'collect':
                        if not run['result'] or run['cleanup_state'] != 'confirmed':
                            raise Conflict('Run cleanup unconfirmed')
                        if next(reversed(session['runs'])) != run['id']:
                            raise Conflict('Only the latest run has a workspace snapshot')
                        return 200, self.execution(session, 'collect', self.scope(session, run))
        return 404, {'error': 'unknown_route'}

    def shutdown(self):
        self.stopping = True
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            with self.locks[session['id']]:
                if session.get('active_run'):
                    self.finish(session, session['runs'][session['active_run']],
                                'interrupted', reason='application_shutdown')


if __name__ == '__main__':
    app = Application(Client(os.environ['EXECUTOR_URL'], os.environ['EXECUTOR_TOKEN']),
                      Client(os.environ['MODEL_PROXY_URL'], os.environ['MODEL_PROXY_TOKEN']),
                      json.loads(os.environ['OWNER_TOKENS']), os.environ.get('STATE_DIR'),
                      mount_owner=os.environ.get('MOUNT_OWNER'))
    app.recover()
    serve(app, int(os.environ.get('PORT', '8080')))
