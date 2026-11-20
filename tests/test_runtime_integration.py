"""CPU HTTP integration against the actual Compose server, no Docker socket here."""
import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid

from track_a.client import Client
from track_a.baf_support import SessionTools, check_task
from baf_runner import replay_agent
from pathlib import Path


@unittest.skipUnless(os.environ.get('RUNTIME_URL') and os.environ.get('PROBE_TOKEN'),
                     'requires the Compose runtime and its probe token')
class RuntimeIntegration(unittest.TestCase):
    def setUp(self):
        from jsonschema import validate

        self.validate_schema = validate
        self.client = Client(os.environ['RUNTIME_URL'], os.environ['PROBE_TOKEN'])
        self.sessions = []

    def tearDown(self):
        for sid in self.sessions:
            self.client.request('DELETE', '/v0/sessions/' + sid)

    def call(self, method, path, body=None):
        status, value = self.client.request(method, path, body)
        self.assertEqual(status, 200, value)
        if 'events' in value:
            version, kind = ('v1', 'task') if path.startswith('/v1/') else ('v0', 'run')
            schema = json.loads(Path(f'/app/track_a/protocol-{version}.schema.json').read_text())
            self.validate_schema(value, {'$ref': f'#/$defs/{kind}', '$defs': schema['$defs']})
        elif 'container' in value:
            schema = json.loads(Path('/app/track_a/protocol-v0.schema.json').read_text())
            self.validate_schema(value, {'$ref': '#/$defs/session', '$defs': schema['$defs']})
        return value

    def new(self):
        body = {'request_id': uuid.uuid4().hex}
        session = self.call('POST', '/v0/sessions', body)
        self.sessions.append(session['id'])
        self.assertEqual(self.call('POST', '/v0/sessions', body)['id'], session['id'])
        return session['id']

    def run_command(self, sid, code, timeout=3):
        payload = {'request_id': uuid.uuid4().hex, 'argv': ['python3', '-c', code], 'timeout_seconds': timeout}
        run = self.call('POST', f'/v0/sessions/{sid}/runs', payload)
        run = self.client.wait(run['id'])
        self.assertEqual(len([e for e in run['events'] if e['type'] == 'result']), 1)
        return run, payload

    def wait_task(self, tid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.call('GET', '/v1/tasks/' + tid)
            if task['result'] is not None:
                return task
            time.sleep(.05)
        self.fail('Task did not finish')

    def test_input_auth_idempotency_and_command_errors(self):
        wrong = Client(self.client.base, 'wrong')
        self.assertEqual(wrong.request('POST', '/v0/sessions', {'request_id': 'x'})[0], 401)
        for raw in [b'[]', b'{', b'null']:
            req = urllib.request.Request(self.client.base + '/v0/sessions', data=raw,
                                         headers={'Authorization': 'Bearer ' + self.client.token})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(error.exception.code, 400)
        sid = self.new()
        self.assertEqual(self.client.request('POST', '/v0/sessions', {'request_id': 'x', 'mount': '/etc'})[0], 400)
        run, payload = self.run_command(sid, "from pathlib import Path;Path('once').open('a').write('x');print('ok')")
        self.assertEqual(run['result']['stdout'].strip(), 'ok')
        self.assertEqual(self.call('POST', f'/v0/sessions/{sid}/runs', payload)['id'], run['id'])
        self.assertEqual(self.client.request('POST', f'/v0/sessions/{sid}/runs', {**payload, 'argv': ['false']})[0], 409)
        read, _ = self.run_command(sid, "print(open('once').read())")
        self.assertEqual(read['result']['stdout'].strip(), 'x')
        bad, _ = self.run_command(sid, 'raise SystemExit(3)')
        self.assertEqual((bad['result']['error_kind'], bad['result']['exit_code']), ('command', 3))

    def test_descendants_timeout_and_cancel(self):
        sid = self.new()
        # A detached child survives killpg in the original worker implementation.
        run, _ = self.run_command(sid, "import subprocess; p=subprocess.Popen(['sleep','30'],start_new_session=True);print(p.pid)")
        pid = int(run['result']['stdout'])
        check, _ = self.run_command(sid, f"from pathlib import Path;print(Path('/proc/{pid}').exists())")
        self.assertEqual(check['result']['stdout'].strip(), 'False')
        timeout, _ = self.run_command(sid, 'import time;time.sleep(10)', .1)
        self.assertEqual(timeout['result']['error_kind'], 'timeout')
        self.assertEqual(self.call('GET', '/v0/sessions/' + sid)['status'], 'closed')
        sid = self.new()
        payload = {'request_id': 'cancel', 'argv': ['python3', '-c',
                   "import subprocess,time;subprocess.Popen(['sleep','30'],start_new_session=True);time.sleep(10)"],
                   'timeout_seconds': 10}
        run = self.call('POST', f'/v0/sessions/{sid}/runs', payload)
        concurrent = {**payload, 'request_id': 'parallel'}
        self.assertEqual(self.client.request('POST', f'/v0/sessions/{sid}/runs', concurrent)[0], 409)
        cancelled = self.call('POST', '/v0/runs/' + run['id'] + '/cancel', {})
        self.assertEqual(cancelled['result']['status'], 'cancelled')
        repeated = self.call('POST', '/v0/runs/' + run['id'] + '/cancel', {})
        self.assertEqual(len([e for e in repeated['events'] if e['type'] == 'result']), 1)
        self.assertEqual(self.call('POST', f'/v0/sessions/{sid}/runs', payload)['id'], run['id'])

    def test_server_agent_and_task_cancellation(self):
        sid = self.new()
        body = {'request_id': 'task', 'message': 'Infrastructure fixture, not a model evaluation'}
        task = self.call('POST', f'/v1/sessions/{sid}/tasks', body)
        task = self.wait_task(task['id'])
        self.assertEqual(task['model_backend'], 'mock')
        self.assertEqual(task['result']['status'], 'complete')
        self.assertEqual(len([e for e in task['events'] if e['type'] == 'result']), 1)
        self.assertTrue(any(e['type'] == 'tool_result' for e in task['events']))
        self.assertEqual(self.call('POST', f'/v1/sessions/{sid}/tasks', body)['id'], task['id'])
        self.assertEqual(self.client.request('POST', f'/v1/sessions/{sid}/tasks', {**body, 'message': 'changed'})[0], 409)
        output, _ = self.run_command(sid, "print(open('mock.txt').read())")
        self.assertEqual(output['result']['stdout'].strip(), 'mock-ok')
        sid = self.new()
        task = self.call('POST', f'/v1/sessions/{sid}/tasks', {'request_id': 'slow', 'message': 'mock:slow'})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = self.call('GET', '/v1/tasks/' + task['id'])
            if any(e['type'] == 'tool_started' for e in current['events']):
                break
            time.sleep(.01)
        else:
            self.fail('Slow tool did not start')
        self.assertEqual(self.client.request('POST', f'/v0/sessions/{sid}/runs',
                         {'request_id': 'race', 'argv': ['true'], 'timeout_seconds': 3})[0], 409)
        cancelled = self.call('POST', '/v1/tasks/' + task['id'] + '/cancel', {})
        self.assertEqual(cancelled['result']['status'], 'cancelled')
        self.assertEqual(self.call('GET', '/v0/sessions/' + sid)['status'], 'closed')

    def test_isolation_network_secrets_and_disk_limit(self):
        sid = self.new()
        code = """import os,json,socket
from pathlib import Path
status=Path('/proc/self/status').read_text()
s=socket.socket();s.settimeout(.1)
try:s.connect(('1.1.1.1',80));network=True
except OSError:network=False
try:Path('/root-write').write_text('x');readonly=False
except OSError:readonly=True
print(json.dumps({'uid':os.getuid(),'nnp':'NoNewPrivs:\\t1' in status,
'capdrop':'CapEff:\\t0000000000000000' in status,'readonly':readonly,'network':network,
'secrets':[k for k in os.environ if k in ['MODEL_API_KEY','PROBE_TOKEN']],
'socket':Path('/var/run/docker.sock').exists()}))
"""
        run, _ = self.run_command(sid, code)
        value = json.loads(run['result']['stdout'])
        self.assertEqual(value, dict(uid=1000, nnp=True, capdrop=True, readonly=True, network=False, secrets=[], socket=False))
        run, _ = self.run_command(sid, """import errno
try:
 with open('full','wb') as f:
  for _ in range(20):f.write(b'x'*1048576)
except OSError as e:print(e.errno)
""")
        self.assertEqual(run['result']['stdout'].strip(), '28')
        other = self.new()
        run, _ = self.run_command(other, "from pathlib import Path;print(Path('full').exists())")
        self.assertEqual(run['result']['stdout'].strip(), 'False')

    def test_baf_v1_replay_preserves_original_protocol(self):
        source = Path('/app/recordings/baf-v1')
        protocol = json.loads((source/'suite/protocol-v1.json').read_text())
        catalog = json.loads((source/'suite/catalog.json').read_text())
        import tempfile
        for entry in catalog['tasks']:
            with self.subTest(task=entry['id']), tempfile.TemporaryDirectory() as tmp:
                sid = self.new()
                task_dir = source/'suite'/entry['path']
                config = {**protocol}
                tools = SessionTools(self.client, sid)
                tools.upload(task_dir/'fixture')
                agent = replay_agent(self.client, sid, source/entry['id'], Path(tmp), config)
                tools.collect(Path(tmp)/'work', protocol)
                verdict = check_task(task_dir, Path(tmp)/'work', agent)
                self.assertTrue(verdict['pass'], verdict)
                self.assertEqual(agent['stop_reason'], 'finish')
                self.assertEqual(agent['model_calls'], [])
                self.call('DELETE', '/v0/sessions/' + sid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
