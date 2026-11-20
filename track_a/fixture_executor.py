"""Offline execution API. Runs no subprocesses and exports no client directories."""
import copy
import os
import threading

from track_a.contracts import fields, file_path, fixture_files, tool_call
from track_a.http_api import Conflict, key, serve
from track_a.storage import SessionJournals


class FixtureExecutor:
    def __init__(self, root=None):
        self.store = SessionJournals(root)
        self.sessions = self.store.load()
        self.lock = threading.Lock()

    def health(self):
        return 200, {'status': 'ok', 'backend': 'fixture', 'client_mount': False}

    def shutdown(self):
        pass

    def route(self, method, path, body):
        parts = path.strip('/').split('/')
        if len(parts) != 3 or parts[0] != 'fixture':
            return 404, {'error': 'unknown_route'}
        _, sid, operation = parts
        key(sid)
        with self.lock:
            if operation == 'provision' and method == 'POST':
                fields(body, 'files')
                initial = fixture_files(body['files'])
                session = self.sessions.get(sid)
                if session is None:
                    session = dict(id=sid, generation=1, active=None, closed=False,
                                   files=initial, operations={}, stopped=[])
                    self.store.save(session)
                    self.sessions[sid] = session
                return 200, {'generation': session['generation'], 'closed': session['closed']}

            if operation == 'release' and method == 'POST':
                fields(body)
                tombstone = dict(id=sid, generation=1, closed=True)
                self.store.save(tombstone)
                self.sessions[sid] = tombstone
                return 200, {'cleanup_state': 'confirmed'}
            session = self.sessions[sid]
            if session['closed']:
                return 410, {'error': 'released'}
            return self.use(session, method, operation, body)

    def use(self, session, method, operation, body):
        if operation == 'operation' and method == 'POST':
            fields(body, 'generation', 'op_id')
        elif operation == 'exec' and method == 'POST':
            fields(body, 'generation', 'run_id', 'op_id', 'call')
        elif operation in {'start', 'stop', 'collect'} and method == 'POST':
            fields(body, 'generation', 'run_id')
        else:
            return 404, {'error': 'unknown_route'}
        if type(body['generation']) is not int or body['generation'] != session['generation']:
            raise Conflict('Stale generation')
        if operation == 'operation':
            return 200, copy.deepcopy(session['operations'][body['op_id']]['result'])
        rid = body['run_id']
        key(rid)
        if operation == 'start':
            if rid in session['stopped'] or session['active'] not in {None, rid}:
                raise Conflict('Cannot start run')
            session['active'] = rid
            self.store.save(session)
            return 200, {}
        if operation == 'stop':
            if session['active'] not in {None, rid}:
                raise Conflict('Another run owns the workspace')
            if rid not in session['stopped']:
                session['stopped'].append(rid)
            session['active'] = None
            self.store.save(session)
            return 200, {'cleanup_state': 'confirmed'}
        if operation == 'collect':
            if session['active'] is not None or rid not in session['stopped']:
                raise Conflict('Workspace is active')
            return 200, {'files': dict(session['files'])}
        op_id = body['op_id']
        key(op_id)
        tool_call(body['call'])
        previous = session['operations'].get(op_id)
        if previous is not None:
            if previous['request'] != body:
                raise Conflict('Operation key reused with different payload')
            return 200, copy.deepcopy(previous['result'])
        if session['active'] != rid:
            raise Conflict('Run is inactive')
        # The mock mutates only this snapshot. Its intent, write and result can commit together.
        updated = copy.deepcopy(session)
        result = self.apply(updated['files'], body['call'])
        updated['operations'][op_id] = {'request': body, 'result': result}
        self.store.save(updated)
        self.sessions[session['id']] = updated
        return 200, result

    @staticmethod
    def apply(files, call):
        tool, args = call['tool'], call['args']
        result = dict(exit_code=0, stdout='', stderr='', error_kind=None, truncated=False)
        if tool == 'RunCommand':
            # A deliberately small fixture vocabulary, never an actual shell.
            if args['command'] == 'fixture:list':
                result['stdout'] = '\n'.join(sorted(files))
            else:
                result.update(exit_code=127, stderr='Unsupported fixture command', error_kind='command_failure')
        elif tool == 'ReadFile':
            path = file_path(args['path'])
            if path in files:
                result.update(data=files[path], encoding='base64')
            else:
                result.update(exit_code=1, stderr='File not found', error_kind='command_failure')
        else:
            path = file_path(args['path'])
            if path not in files and len(files) >= 32:
                result.update(exit_code=1, stderr='Fixture capacity reached', error_kind='command_failure')
            else:
                files[path] = args['data']
        return result


if __name__ == '__main__':
    serve(FixtureExecutor(os.environ.get('STATE_DIR')), int(os.environ.get('PORT', '8081')))
