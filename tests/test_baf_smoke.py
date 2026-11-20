import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from track_a.baf_smoke import MODE, LocalClient, load_task, read_catalog, run_case
from track_a.baf_smoke_mock import STATS_COMMAND, ScriptedModel, SmokeExecutor


class NoOutputModel(ScriptedModel):
    def mock(self, messages):
        return {'answer': 'No artifact.'}


class UnavailableModel(ScriptedModel):
    def route(self, method, path, body):
        return 503, {'error': 'unavailable'}


class BafSmokeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = Path(tmp.name)

    def test_catalog_loads_only_the_eight_independent_fixture_snapshots(self):
        entries = read_catalog()
        self.assertEqual([entry['id'] for entry in entries], [f'B{i}.1' for i in range(1, 9)])
        for entry in entries:
            with self.subTest(task=entry['id']):
                _, initial = load_task(entry)
                self.assertTrue(initial)
                self.assertNotIn('check.py', initial)
                self.assertNotIn('smoke.json', initial)
                initial.clear()
                self.assertTrue(load_task(entry)[1])

    def test_independent_attempts_and_outcomes(self):
        entry = read_catalog()[3]
        _, fixture = load_task(entry)
        with patch('socket.socket', side_effect=AssertionError('Unexpected socket')), \
             patch('subprocess.Popen', side_effect=AssertionError('Unexpected process')):
            first = run_case(entry, self.output)
        second = run_case(entry, self.output, NoOutputModel)
        third = run_case(entry, self.output, UnavailableModel)
        self.assertEqual([row['outcome'] for row in (first, second, third)], ['pass', 'fail', 'infra_error'])
        self.assertEqual(len({row['id'] for row in (first, second, third)}), 3)
        self.assertEqual(len({row['session_id'] for row in (first, second, third)}), 3)
        self.assertEqual({row['close'] for row in (first, second, third)}, {'confirmed'})
        first_folder = self.output / first['id']
        self.assertEqual(json.loads((first_folder / 'attempt.json').read_text()), first)
        self.assertEqual(first['mode'], MODE)
        self.assertTrue((first_folder / 'events.json').is_file())
        self.assertTrue(all(first['checks'].values()))
        files = json.loads((self.output / second['id'] / 'files.json').read_text())
        self.assertEqual(files, fixture)
        self.assertFalse(second['checks']['artifact_written'])
        self.assertEqual(third['reason'], 'upstream_rejected')

    def test_fixture_boundary_and_exact_command_dispatch(self):
        fixture = SmokeExecutor()
        executor = LocalClient(fixture)
        path = '/fixture/boundary'
        initial = {'files': {'stats.py': base64.b64encode(b'raise RuntimeError("unexpected evaluation")').decode(),
                             'data.csv': base64.b64encode(b'value\n3\n7\n14\n').decode()}}
        self.assertEqual(executor.request('POST', path + '/provision', initial)[0], 200)
        self.addCleanup(fixture.store.journals['boundary'].db.close)
        scope = {'generation': 1, 'run_id': 'one'}
        self.assertEqual(executor.request('POST', path + '/start', scope)[0], 200)
        traversal = {**scope, 'op_id': 'bad', 'call': {'id': 'bad', 'tool': 'ReadFile',
                                                      'args': {'path': '../check.py'}}}
        self.assertEqual(executor.request('POST', path + '/exec', traversal)[0], 400)
        for name, command, expected_exit in (
            ('unsupported', 'fixture:unsupported', 127),
            ('suffix', STATS_COMMAND + ' extra', 127),
            ('stats', STATS_COMMAND, 0),
        ):
            body = {**scope, 'op_id': name, 'call': {'id': name, 'tool': 'RunCommand',
                                                    'args': {'command': command}}}
            status, result = executor.request('POST', path + '/exec', body)
            self.assertEqual((status, result['exit_code']), (200, expected_exit))
        self.assertEqual(json.loads(result['stdout'])['median'], 7)
        self.assertEqual(executor.request('POST', path + '/stop', scope)[0], 200)
        self.assertEqual(executor.request('POST', path + '/collect', scope), (200, initial))
