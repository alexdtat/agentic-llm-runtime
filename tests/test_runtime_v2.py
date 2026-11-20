import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from track_a.application import Application
from track_a.client import Client
from track_a.fixture_attempt import attempt
from track_a.fixture_executor import FixtureExecutor
from track_a.http_api import JsonHandler
from track_a.model_proxy import ModelProxy


class RuntimeV2Tests(unittest.TestCase):
    def service(self, app, token):
        server = ThreadingHTTPServer(('127.0.0.1', 0), JsonHandler)
        server.app, server.token = app, token
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return Client(f'http://127.0.0.1:{server.server_port}', token)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fixture = FixtureExecutor(self.root / 'executor')
        self.proxy = ModelProxy(SimpleNamespace(backend='mock'))
        self.executor = self.service(self.fixture, 'executor')
        self.model = self.service(self.proxy, 'model')
        self.app = Application(self.executor, self.model, {'a': 'alice', 'b': 'bob'}, self.root / 'app')
        self.client = self.service(self.app, 'alice')

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', 'track_a.cli', '--url', self.client.base, *args],
                                capture_output=True, text=True, timeout=5,
                                env={'PATH': os.defpath, 'PROBE_TOKEN': 'alice', 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def session(self):
        return self.client.v2('POST', '', {'request_id': 'session', 'files': {}})['id']

    def test_cli_history_replay_and_close(self):
        sid = self.cli('fixture-new', '--request-id', 'session')
        self.assertEqual(sid, self.session())
        rid = self.cli('run', sid, 'Make a fixture', '--request-id', 'run')
        result = self.client.wait_run(sid, rid)['result']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['cleanup_state'], 'confirmed')
        self.assertEqual(self.cli('run', sid, 'Make a fixture', '--request-id', 'run'), rid)
        self.assertEqual(self.client.request('POST', f'/v2/sessions/{sid}/runs',
                                            {'request_id': 'run', 'message': 'changed'})[0], 409)
        self.assertEqual(Client(self.client.base, 'bob').request('GET', f'/v2/sessions/{sid}')[0], 404)
        files = json.loads(self.cli('run-collect', sid, rid))['files']
        self.assertEqual(files, {'mock.txt': 'bW9jay1vawo='})
        events = [json.loads(line) for line in self.cli('events', sid).splitlines()]
        self.assertEqual([e['type'] for e in events].count('tool.result'), 2)
        status, replay = self.client.events(sid, events[-2]['sequence'])
        self.assertEqual((status, replay), (200, [events[-1]]))
        next_id = self.cli('run', sid, 'fixture:history')
        self.assertEqual(self.client.wait_run(sid, next_id)['result']['answer'], 'Prior assistant messages: 1')
        self.cli('session-close', sid)
        self.assertEqual(self.client.events(sid, 0)[0], 410)
        self.assertEqual(self.session(), sid)
        self.assertNotIn(b'Make a fixture', (self.root / 'app' / f'{sid}.sqlite').read_bytes())
        self.assertNotIn(b'bW9jay1vawo=', (self.root / 'executor' / f'{sid}.sqlite').read_bytes())

    def test_cancel_during_model_and_no_late_tool(self):
        entered, resume, exited = threading.Event(), threading.Event(), threading.Event()
        mock = self.proxy.mock

        def blocked(messages):
            entered.set()
            resume.wait(2)
            exited.set()
            return mock(messages)

        self.proxy.mock = blocked
        sid = self.session()
        body = {'request_id': 'run', 'message': 'cancel me'}
        rid = self.client.v2('POST', f'/{sid}/runs', body)['id']
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.client.v2('POST', f'/{sid}/runs', body)['id'], rid)
            self.assertEqual(self.client.request('POST', f'/v2/sessions/{sid}/runs',
                                                {**body, 'request_id': 'other'})[0], 409)
            result = self.client.v2('POST', f'/{sid}/runs/{rid}/cancel', {})['result']
            self.assertEqual(result['status'], 'cancelled')
        finally:
            resume.set()
        self.assertTrue(exited.wait(1))
        files = self.client.v2('POST', f'/{sid}/runs/{rid}/collect', {})['files']
        self.assertEqual(files, {})
        _, events = self.client.events(sid, 0)
        self.assertEqual([e['type'] for e in events].count('run.result'), 1)
        self.assertFalse(any(e['type'] == 'tool.started' for e in events))

    def test_operation_lookup_and_recovery_without_replay(self):
        sid = self.session()
        path = f'/fixture/{sid}'
        scope = {'run_id': 'recover', 'generation': 1}
        self.executor.request('POST', path + '/start', scope)
        command = {**scope, 'op_id': 'op', 'call': {
            'id': 'write', 'tool': 'WriteFile', 'args': {'path': 'result.txt', 'data': 'QQ=='}}}
        status, result = self.executor.request('POST', path + '/exec', command)
        self.assertEqual(status, 200)
        self.assertEqual(self.executor.request('POST', path + '/exec', command), (200, result))
        self.assertEqual(self.executor.request('POST', path + '/exec', {**command, 'generation': 2})[0], 409)
        restored_executor = FixtureExecutor(self.root / 'executor')
        status, known = restored_executor.route('POST', path + '/operation', {'op_id': 'op', 'generation': 1})
        self.assertEqual((status, known), (200, result))

        session = self.app.sessions[sid]
        run = {'id': 'recover', 'status': 'running', 'result': None, 'message': 'uncommitted final'}
        session['runs']['recover'] = run
        session['active_run'] = run['id']
        self.app.store.save(session)
        restored = Application(self.executor, self.model, {'a': 'alice'}, self.root / 'app')
        restored.recover()
        _, value = restored.route('a', 'GET', f'/v2/sessions/{sid}/runs/recover', {}, {})
        self.assertEqual(value['result']['reason'], 'application_restart')
        self.assertEqual(value['status'], 'interrupted')
        self.assertEqual(list(self.fixture.sessions[sid]['operations']), ['op'])

    def test_cancel_during_tool_request_does_not_wait_for_reply(self):
        sid = self.session()
        entered, resume = threading.Event(), threading.Event()
        request = self.executor.request

        def delayed(method, path, body=None, timeout=20):
            if path.endswith('/exec'):
                entered.set()
                resume.wait(3)
            return request(method, path, body, timeout)

        self.executor.request = delayed
        rid = self.client.v2('POST', f'/{sid}/runs', {'request_id': 'run', 'message': 'mock'})['id']
        try:
            self.assertTrue(entered.wait(1))
            run = self.client.v2('POST', f'/{sid}/runs/{rid}/cancel', {}, timeout=1)
            self.assertEqual(run['result']['status'], 'cancelled')
        finally:
            resume.set()
        files = self.client.v2('POST', f'/{sid}/runs/{rid}/collect', {})
        self.assertEqual(files, {'files': {}})

    def test_lost_exec_response_uses_lookup_and_unconfirmed_stop_quarantines(self):
        sid = self.session()
        request = self.executor.request
        operations = []

        def drop_after_commit(method, path, body=None, timeout=20):
            if path.endswith('/exec'):
                operations.append(body['op_id'])
                request(method, path, body, timeout)
                raise TimeoutError('response lost')
            if path.endswith('/stop'):
                raise TimeoutError('cleanup unavailable')
            return request(method, path, body, timeout)

        self.executor.request = drop_after_commit
        rid = self.client.v2('POST', f'/{sid}/runs', {'request_id': 'first', 'message': 'mock'})['id']
        result = self.client.wait_run(sid, rid)['result']
        self.assertEqual((result['status'], result['cleanup_state']), ('interrupted', 'quarantined'))
        self.assertEqual(operations, list(self.fixture.sessions[sid]['operations']))
        self.assertEqual(len(operations), 2)
        self.assertEqual(self.client.request('POST', f'/v2/sessions/{sid}/runs',
                                            {'request_id': 'new', 'message': 'mock'})[0], 409)
        self.assertEqual(self.client.v2('POST', f'/{sid}/close', {})['status'], 'closed')

    def test_attempt_separates_checker_and_infrastructure(self):
        task = {'fixture': {}, 'expected': {'mock.txt': 'bW9jay1vawo='}, 'message': 'mock'}
        first = attempt(self.client, task, self.root)
        second = attempt(self.client, {**task, 'message': 'fixture:nonzero'}, self.root)
        third = attempt(Client(self.client.base, 'invalid'), task, self.root)
        self.assertEqual([r['outcome'] for r in (first, second, third)], ['pass', 'fail', 'infra_error'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertTrue((self.root / first['id'] / 'attempt.json').exists())


if __name__ == '__main__':
    unittest.main()
