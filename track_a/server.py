"""Public gateway, durable tasks and server-side agent loop. No Docker socket."""
import copy
import json
import os
import threading
import time
import uuid

from track_a.client import Client
from track_a.http_api import Conflict, command, key, serve
from track_a.model import Model, SYSTEM
from track_a.storage import Journal


class Gateway:
    def __init__(self, client, model, path=':memory:'):
        self.client, self.model = client, model
        self.lock = threading.RLock()
        self.journal = Journal(path)
        self.tasks = self.journal.load().get('tasks', {})
        self.stopping = False

    def save(self):
        self.journal.save({'tasks': self.tasks})

    def event(self, task, kind, **values):
        task['events'].append(dict(type=kind, sequence=len(task['events']),
                                   time=time.time(), task_id=task['id'], **values))
        self.save()

    def terminal(self, task, status, error_kind=None, answer=None):
        if task['result'] is None:
            task['status'] = status
            task['result'] = dict(status=status, error_kind=error_kind, answer=answer)
            self.event(task, 'result', **task['result'])

    def request(self, method, path, body=None):
        status, value = self.client.request(method, path, body)
        if status != 200:
            raise RuntimeError('Executor request failed')
        return value

    def recover(self):
        # Never restart a task or repeat a possibly committed tool after a crash.
        with self.lock:
            for task in self.tasks.values():
                if task['result'] is None:
                    self.request('DELETE', '/v0/sessions/' + task['session_id'])
                    self.terminal(task, 'failed', 'interrupted')

    def health(self):
        try:
            self.request('GET', '/v0/health')
            return 200, {'version': 'v1', 'status': 'ok', 'model_backend': self.model.backend}
        except Exception:
            return 503, {'status': 'executor_unavailable'}

    def active(self, sid):
        return next((t for t in self.tasks.values() if t['session_id'] == sid and t['result'] is None), None)

    def create(self, sid, body):
        if set(body) != {'request_id', 'message'}:
            raise TypeError('Expected request_id and message')
        key(body['request_id'])
        if not isinstance(body['message'], str) or not 1 <= len(body['message']) <= 8000:
            raise TypeError('Invalid message')
        with self.lock:
            for task in self.tasks.values():
                if task['session_id'] == sid and task['request']['request_id'] == body['request_id']:
                    if task['request'] != body:
                        raise Conflict('Idempotency key reused with different payload')
                    return task
            if self.stopping or self.active(sid):
                raise Conflict('Session already has a task or server is stopping')
            session = self.request('GET', '/v0/sessions/' + sid)
            if session['status'] != 'open':
                raise Conflict('Session is not open')
            tid = uuid.uuid4().hex
            task = dict(id=tid, session_id=sid, request=body, status='running', result=None,
                        model_backend=self.model.backend, events=[])
            self.tasks[tid] = task
            self.event(task, 'started')
            threading.Thread(target=self.execute, args=(task,), daemon=True).start()
            return task

    def execute(self, task):
        deadline = time.monotonic() + 60
        messages = [{'role': 'system', 'content': SYSTEM},
                    {'role': 'user', 'content': task['request']['message']}]
        kind = 'model'
        try:
            for step in range(8):
                with self.lock:
                    if task['result'] is not None:
                        return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if sum(len(m['content']) for m in messages) > 128000:
                    with self.lock:
                        self.terminal(task, 'failed', 'context_limit')
                    return
                kind = 'model'
                started = time.monotonic()
                action, content, usage = self.model.ask(messages, remaining)
                with self.lock:
                    if task['result'] is not None:
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    self.event(task, 'model', seconds=time.monotonic() - started, usage=usage,
                               backend=self.model.backend, content=content)
                    messages.append({'role': 'assistant', 'content': content})
                    if action.get('tool') == 'finish':
                        if not isinstance(action.get('answer'), str):
                            raise ValueError('Invalid answer')
                        self.terminal(task, 'complete', answer=action['answer'])
                        return
                    if action.get('tool') != 'exec':
                        raise ValueError('Unsupported action')
                    payload = {'request_id': f'task-{task["id"]}-{step}', 'argv': action.get('argv'),
                               'timeout_seconds': min(3, max(.05, deadline-time.monotonic()))}
                    command(payload)
                    kind = 'transport'
                    run = self.request('POST', f'/v0/sessions/{task["session_id"]}/runs', payload)
                    self.event(task, 'tool_started', run_id=run['id'], argv=payload['argv'])
                while True:
                    with self.lock:
                        if task['result'] is not None:
                            return
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    run = self.request('GET', '/v0/runs/' + run['id'])
                    if run['result'] is not None:
                        break
                    time.sleep(.05)
                with self.lock:
                    if task['result'] is not None:
                        return
                    self.event(task, 'tool_result', run_id=run['id'], result=run['result'])
                    if run['result']['error_kind'] not in {None, 'command'}:
                        self.terminal(task, 'failed', run['result']['error_kind'])
                        return
                    messages.append({'role': 'user', 'content': 'Tool result: ' + json.dumps(run['result'])})
            with self.lock:
                self.terminal(task, 'failed', 'step_limit')
        except Exception as exc:
            with self.lock:
                if task['result'] is not None:
                    return
                try:
                    self.request('DELETE', '/v0/sessions/' + task['session_id'])
                    self.terminal(task, 'failed', 'timeout' if isinstance(exc, TimeoutError) else kind)
                except Exception:
                    self.terminal(task, 'failed', 'cleanup_failed')

    def cancel(self, tid):
        with self.lock:
            task = self.tasks[tid]
            if task['result'] is None:
                self.request('DELETE', '/v0/sessions/' + task['session_id'])
                self.terminal(task, 'cancelled', 'cancelled')
            return task

    def route(self, method, path, body):
        parts = path.strip('/').split('/')
        with self.lock:
            if method == 'POST' and len(parts) == 4 and parts[:2] == ['v1', 'sessions'] and parts[3] == 'tasks':
                return 200, copy.deepcopy(self.create(parts[2], body))
            if method == 'GET' and len(parts) == 3 and parts[:2] == ['v1', 'tasks']:
                return 200, copy.deepcopy(self.tasks[parts[2]])
            if method == 'POST' and len(parts) == 4 and parts[:2] == ['v1', 'tasks'] and parts[3] == 'cancel':
                if body:
                    raise TypeError('Cancel body must be empty')
                return 200, copy.deepcopy(self.cancel(parts[2]))
            if parts[:1] != ['v0']:
                return 404, {'error': 'unknown_route'}
            # Legacy command interface stays available for fixtures and headless tests.
            if len(parts) >= 3 and parts[:2] == ['v0', 'sessions']:
                active = self.active(parts[2])
                if active and method == 'DELETE':
                    self.cancel(active['id'])
                elif active and method != 'GET':
                    raise Conflict('Session is owned by an active task')
            if method == 'POST' and len(parts) == 4 and parts[:2] == ['v0', 'runs'] and parts[3] == 'cancel':
                run = self.request('GET', '/v0/runs/' + parts[2])
                if self.active(run['session_id']):
                    raise Conflict('Cancel the owning task')
            return self.client.request(method, path, body if method == 'POST' else None)

    def shutdown(self):
        with self.lock:
            self.stopping = True
        for task in list(self.tasks.values()):
            self.cancel(task['id'])


def main():
    client = Client(os.environ.get('EXECUTOR_URL', 'http://executor:8081'), os.environ['PROBE_TOKEN'])
    gateway = Gateway(client, Model(), os.environ.get('STATE_FILE', ':memory:'))
    gateway.recover()
    serve(gateway, int(os.environ.get('PORT', '8080')))


if __name__ == '__main__':
    main()
