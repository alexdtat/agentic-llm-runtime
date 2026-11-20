import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from track_a.application import Application
from track_a.baf_attempt import AttemptError, MountedClient, endpoint, run_attempt
from track_a.baf_grade import grade, read_trace, workspace_program
from track_a.baf_plan import TRACK_A, build_plan
from track_a.baf_smoke import LocalClient
from track_a.baf_smoke_mock import ScriptedModel
from track_a.mounted_executor import MountedExecutor


class MountedAPI:
    def __init__(self, work, scenario):
        self.work, self.scenario = work, scenario
        self.calls = []
        self.suite_request = None
        self.trace = []
        self.done = False
        self.result = {'status': 'completed', 'reason': None, 'answer': 'Created.',
                       'usage': [{'prompt_tokens': 12}], 'cleanup_state': 'confirmed'}
        if scenario == 'quarantined':
            self.result.update(status='interrupted', reason='cleanup_unconfirmed', answer=None,
                               usage=None, cleanup_state='quarantined')

    def request(self, method, path, timeout):
        self.calls.append((method, path))
        return 200, {'status': 'ok', 'version': 'v2', 'execution': 'mounted', 'client_mount': True}

    def v2(self, method, path, body=None, timeout=20):
        self.calls.append((method, path))
        if (method, path) == ('POST', ''):
            assert body == {'request_id': body['request_id'], 'grant': 'configured-grant'}
            self.suite_request = body['request_id']
            return {'id': 'session', 'status': 'open', 'active_run': None, 'cursor': 0}
        if (method, path) == ('POST', '/session/runs'):
            assert body['request_id'] == self.suite_request
            assert 'порт' in body['message']
            return self.run_value()
        if path == '/session/runs/run/cancel':
            self.finish(status='cancelled')
            return self.run_value()
        if path == '/session' and method == 'GET':
            return {'id': 'session', 'status': 'quarantined' if self.scenario == 'quarantined' else 'open',
                    'active_run': None, 'cursor': len(self.trace)}
        if path == '/session/close':
            if self.scenario == 'close_lost':
                raise TimeoutError
            return {'id': 'session', 'status': 'closed'}
        raise AssertionError((method, path))

    def run_value(self):
        return {'id': 'run', 'session_id': 'session', 'request_id': self.suite_request,
                'config': {'execution': 'mounted', 'harness': 'sequential-v2', 'model': 'default', 'seconds': 60},
                'status': self.result['status'] if self.done else 'queued',
                'cleanup_state': self.result['cleanup_state'] if self.done else 'pending',
                'result': self.result if self.done else None}

    def finish(self, status=None):
        self.done = True
        if status is not None:
            self.result.update(status=status, reason='requested', answer=None, usage=None)
        calls = [dict(id=str(i), tool='ReadFile', args={'path': 'README.md'}) for i in range(16)]
        usage = {'prompt_tokens': 12}
        self.result['usage'] = [usage] * 5 if self.result['status'] == 'completed' else None
        payloads = [('run.status', {'status': 'queued'})]
        for step in range(4):
            batch = calls[step * 4:step * 4 + 4]
            payloads.extend([('model.started', {'step': step}),
                             ('model.completed', {'step': step, 'usage': usage, 'action': {'calls': batch}})])
            for call in batch:
                payloads.extend([('tool.started', {'op_id': call['id'], 'call': call}),
                                 ('tool.result', {'op_id': call['id'],
                                                  'result': {'exit_code': 0, 'truncated': False}})])
        payloads.extend([('model.started', {'step': 4}),
                         ('model.completed', {'step': 4, 'usage': usage,
                                              'action': {'answer': 'Created.'}}),
                         ('run.result', self.result)])
        self.trace = [{'schema_version': 2, 'session_id': 'session', 'run_id': 'run',
                       'sequence': seq, 'event_id': f'session:{seq}', 'timestamp': seq + .1,
                       'type': kind, 'payload': data} for seq, (kind, data) in enumerate(payloads, 1)]
        if self.scenario == 'event_gap':
            del self.trace[32]

    def wait_run(self, sid, rid, timeout):
        self.calls.append(('WAIT', rid))
        if self.scenario == 'timeout':
            raise TimeoutError
        if self.scenario in ('quarantined', 'unsafe_snapshot'):
            (self.work / 'answer.txt').symlink_to(self.work / 'README.md')
        else:
            (self.work / 'answer.txt').write_text('8137\n')
        self.finish()
        return self.run_value()

    def events(self, sid, after):
        self.calls.append(('EVENTS', after))
        return 200, self.trace[after:after + 32]


class BafAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build_plan()
        cls.task = next(task for task in cls.plan['tasks'] if task['id'] == 'B1.1')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.export = self.root / 'export'
        self.plan_path = self.root / 'plan.json'
        self.plan_path.write_text(json.dumps(self.plan))
        self.output = self.root / 'attempts'
        shutil.copytree(TRACK_A / self.task['path'] / 'fixture', self.export)

    def attempt(self, scenario=None, **overrides):
        api = MountedAPI(self.export, scenario)
        options = dict(plan_path=self.plan_path, suite_digest=self.plan['suite_sha256'], task_id='B1.1',
                       repeat=2, grant='configured-grant', export=self.export, output=self.output,
                       provider_label='operator/model', runtime_revision='runtime-commit')
        options.update(overrides)
        folder, result = run_attempt(api, **options)
        self.assertEqual(json.loads((folder / 'attempt.json').read_text()), result)
        self.assertNotIn('configured-grant', (folder / 'attempt.json').read_text())
        return api, folder, result

    def test_collects_full_trace_and_shell_output_while_lease_is_held(self):
        import track_a.baf_attempt as module
        ordering = []
        original_snapshot, original_v2 = module.store_snapshot, MountedAPI.v2

        def snapshot(*args):
            ordering.append('snapshot')
            return original_snapshot(*args)

        def v2(api, *args, **kwargs):
            if args[:2] == ('POST', '/session/close'):
                ordering.append('close')
            return original_v2(api, *args, **kwargs)

        with patch.object(module, 'store_snapshot', side_effect=snapshot), patch.object(MountedAPI, 'v2', v2):
            api, folder, result = self.attempt()
        self.assertEqual(ordering, ['snapshot', 'close'])
        self.assertEqual(result['outcome'], 'collected')
        self.assertTrue(result['grade_ready'])
        self.assertEqual(result['close'], 'confirmed')
        self.assertEqual(result['result']['usage'], [{'prompt_tokens': 12}] * 5)
        self.assertEqual(result['runtime_config']['execution'], 'mounted')
        self.assertEqual((folder / 'snapshot' / 'answer.txt').read_text(), '8137\n')
        self.assertEqual(json.loads((folder / 'events.json').read_text()), api.trace)
        self.assertIn(('EVENTS', 32), api.calls)
        self.assertEqual(api.calls.count(('POST', '/session/runs')), 1)

    def test_frozen_plan_and_fresh_export_fail_before_any_api_exchange(self):
        api, _, result = self.attempt(suite_digest='a' * 64)
        self.assertEqual(result['reason'], 'frozen_plan_mismatch')
        self.assertEqual(api.calls, [])
        (self.export / 'extra').mkdir()
        api, _, result = self.attempt()
        self.assertEqual(result['reason'], 'initial_fixture_mismatch')
        self.assertEqual(api.calls, [])
        (self.export / 'extra').rmdir()
        os.link(self.export / 'README.md', self.root / 'other-link')
        api, _, result = self.attempt()
        self.assertEqual(result['reason'], 'unsupported_snapshot_entry')
        self.assertEqual(api.calls, [])

    def test_quarantine_and_event_gap_preserve_evidence_without_snapshot(self):
        for scenario, reason in [('quarantined', 'stop_or_lease_unconfirmed'),
                                 ('event_gap', 'event_sequence_or_identity')]:
            with self.subTest(scenario=scenario):
                api, folder, result = self.attempt(scenario)
                self.assertEqual(result['outcome'], 'infra_error')
                self.assertFalse(result['grade_ready'])
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['close'], 'confirmed')
                self.assertEqual(api.calls[-1], ('POST', '/session/close'))
                self.assertFalse((folder / 'snapshot').exists())
                self.assertTrue((folder / 'events.json').exists())
                (self.export / 'answer.txt').unlink()

    def test_timeout_cancels_and_closes_once_without_snapshot(self):
        api, folder, result = self.attempt('timeout')
        self.assertEqual(result['reason'], 'TimeoutError')
        self.assertEqual(result['cancel_cleanup'], 'confirmed')
        self.assertFalse((folder / 'snapshot').exists())
        self.assertEqual(api.calls.count(('POST', '/session/runs')), 1)
        self.assertEqual(api.calls.count(('POST', '/session/runs/run/cancel')), 1)
        self.assertEqual(api.calls[-1], ('POST', '/session/close'))

    def test_unsafe_output_and_unconfirmed_close_are_ineligible_for_grading(self):
        _, folder, result = self.attempt('unsafe_snapshot')
        self.assertEqual(result['reason'], 'unsupported_snapshot_entry')
        self.assertFalse((folder / 'snapshot').exists())
        (self.export / 'answer.txt').unlink()
        _, folder, result = self.attempt('close_lost')
        self.assertTrue((folder / 'snapshot').is_dir())
        self.assertEqual(result['close'], 'unconfirmed')
        self.assertEqual(result['outcome'], 'infra_error')
        self.assertFalse(result['grade_ready'])

    def test_transport_validates_address_frame_and_credentials_without_network(self):
        self.assertEqual(endpoint('http://127.0.0.1:8080/'), 'http://127.0.0.1:8080')
        self.assertEqual(endpoint('https://runtime.example'), 'https://runtime.example')
        for url in ('http://runtime.example', 'http://192.0.2.1', 'https://operator:pass@runtime.example'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                endpoint(url)
        client = MountedClient('http://127.0.0.1:8080', 'secret-token-long', 'export-grant')
        with self.assertRaisesRegex(AttemptError, 'credential_in_response'):
            client.decode(b'{"answer":"contains secret-token-long here"}')
        with patch.object(client, 'exchange', return_value=(200, b'id: 2\ndata: {"sequence":1}\n\n')):
            with self.assertRaisesRegex(AttemptError, 'event_cursor_mismatch'):
                client.events('sid', 0)

    def test_b6_collects_real_v2_trace_and_grades_the_snapshot_offline(self):
        task = next(task for task in self.plan['tasks'] if task['id'] == 'B6.1')
        export = self.root / 'b6-export'
        shutil.copytree(TRACK_A / task['path'] / 'fixture', export)
        grant = 'test-grant'
        executor = MountedExecutor(str(export), grant, 'test-scope',
                                   'test-image', self.root / 'execution-state')
        model = LocalClient(ScriptedModel('B6.1'))
        app = Application(LocalClient(executor), model, {'local': 'test-token'},
                          self.root / 'app-state', mount_owner='local')
        client = LocalClient(app, owner='local')
        tools = []

        def execute(_name, call):
            tool, args = call['tool'], call['args']
            tools.append((tool, args['path']))
            path = export / args['path']
            result = dict(exit_code=0, stdout='', stderr='', error_kind=None, truncated=False)
            if tool == 'ReadFile':
                result['data'] = base64.b64encode(path.read_bytes()).decode()
            else:
                self.assertEqual(tool, 'WriteFile')
                path.write_bytes(base64.b64decode(args['data']))
            return result

        success = subprocess.CompletedProcess([], 0)
        with patch.object(executor, 'check_mount'), patch.object(executor, 'run_container', return_value=success), \
                patch('track_a.mounted_executor.docker', return_value=success), \
                patch.object(executor, 'execute_tool', side_effect=execute), \
                patch.object(executor.containers, 'remove') as stopped:
            folder, receipt = run_attempt(
                client, plan_path=self.plan_path, suite_digest=self.plan['suite_sha256'], task_id='B6.1', repeat=1,
                grant=grant, export=export, output=self.output, provider_label='test/mock', runtime_revision='test')
            self.assertTrue(receipt['grade_ready'], receipt)
            self.assertEqual(receipt['close'], 'confirmed')
            stopped.assert_called_once()
        self.assertEqual(tools, [('ReadFile', 'spec.md'), ('WriteFile', 'slug.py')])
        self.assertTrue(executor.sessions[receipt['session_id']]['closed'])
        snapshot = folder / 'snapshot'
        self.assertEqual(sorted(item.name for item in snapshot.iterdir()), ['slug.py', 'spec.md'])

        def trusted_python(workspace):
            return lambda program: subprocess.check_output(
                [sys.executable, '-I', '-B', '-c', workspace_program(program, str(workspace))],
                cwd=workspace, text=True, timeout=5)

        result = grade('B6.1', snapshot, read_trace(folder / 'events.json', snapshot),
                       isolated_python=trusted_python)
        self.assertEqual(result['outcome'], 'pass', result)
