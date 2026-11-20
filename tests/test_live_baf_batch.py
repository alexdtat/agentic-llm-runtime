"""Offline coverage for the full mounted BAF driver's dispatch boundaries."""
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from track_a.baf_attempt import load_frozen_task, persist
from track_a.baf_plan import build_plan


class LiveBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parents[1] / 'prototypes/mounted-runtime/live_baf_batch.py'
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            cls.impl = SimpleNamespace(**runpy.run_path(str(script)))

    def test_all_39_fixtures_load_and_117_attempts_are_planned(self):
        plan = build_plan()
        self.assertEqual((len(plan['tasks']), len(plan['attempts'])), (39, 117))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            persist(path, plan)
            for task in plan['tasks']:
                entry, prompt, snapshot = load_frozen_task(path, plan['suite_sha256'], task['id'], 1)
                self.assertTrue(prompt and snapshot[1])
                if entry['id'] in {'B5.2', 'B5.3'}:
                    self.assertIn('\n\n', prompt)

    def test_provider_http_errors_and_invalid_model_actions_have_distinct_outcomes(self):
        impl = self.impl
        url = impl.loopback(impl.DEFAULT_BASE)
        for other in ('http://localhost:8000/v1', 'https://127.0.0.1:8000/v1', 'http://10.0.0.1:8000/v1'):
            with self.subTest(endpoint=other), self.assertRaises((ValueError, RuntimeError)):
                impl.loopback(other)
        opener = Mock()
        opener.open.side_effect = HTTPError(url, 503, 'unavailable', {}, None)
        model = impl.ObservedModel(url, 'local-id', 'private', opener)
        proxy = impl.ObservedProxy(model)
        arguments = ('POST', '/generate', {'messages': [{'role': 'user', 'content': 'test'}]})
        receipt = dict(close='confirmed', events={}, phase='trace', outcome='infra_error',
                       result=dict(cleanup_state='confirmed', reason='upstream_rejected'))
        receipt['events'] = {'count': 4}
        with self.assertRaises(HTTPError):
            proxy.route(*arguments)
        self.assertEqual(impl.attempt_failure(receipt, proxy), ('infra_error', 'provider_request_failed'))
        self.assertEqual(model.failure, {'kind': 'HTTPError', 'http_status': 503})
        opener.open.side_effect = None
        opener.open.return_value = io.BytesIO(json.dumps({
            'choices': [{'message': {'content': 'not a JSON action'}}],
        }).encode())
        proxy = impl.ObservedProxy(impl.ObservedModel(url, 'local-id', 'private', opener))
        with self.assertRaises(ValueError):
            proxy.route(*arguments)
        self.assertEqual(impl.attempt_failure(receipt, proxy), ('fail', 'invalid_model_action'))

    def test_resume_skips_confirmed_case_and_reservation_stops_before_reexecution(self):
        impl = self.impl
        globals_ = impl.run_batch.__globals__
        tasks = [{'task_id': 'B1.1', 'repeat': repeat} for repeat in (1, 2, 3)]
        plan = {'suite_sha256': 'suite', 'attempts': tasks}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            evidence = output / '01-B1.1'
            evidence.mkdir()
            identity = dict(suite_sha256='suite', git_revision='revision', runtime_sha256='digest',
                            model='model', model_endpoint=impl.DEFAULT_BASE, profile='mounted_v2_live')
            persist(evidence / 'summary.json', dict(task='B1.1', repeat=1, mode='mounted_live_baf',
                                                   status='fail', provider_calls=1, identity=identity,
                                                   stages={'cleanup': {'status': 'passed'}}))
            persist(evidence / 'grade.json', {'outcome': 'fail'})
            pending = output / '02-B1.1'
            pending.mkdir()
            persist(pending / 'summary.json', {'status': 'running'})
            case = Mock()
            with patch.dict(globals_, {'build_plan': lambda: plan, 'Case': case,
                                       'runtime_identity': lambda: ('revision', 'digest')}):
                self.assertEqual(impl.run_batch(output, (impl.DEFAULT_BASE, 'model', '', Mock())), 2)
            progress = json.loads((output / 'progress.json').read_text())
            self.assertEqual(progress['attempts'], [{**tasks[0], 'outcome': 'fail'}])
            self.assertEqual(progress['status'], 'stopped')
            case.assert_not_called()
