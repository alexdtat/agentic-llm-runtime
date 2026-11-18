"""Regression tests for durable intent, cleanup failures and cancellation races."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from track_a.executor import State, failure
from track_a.http_api import Conflict
from track_a.server import Gateway


class ExecutorTests(unittest.TestCase):
    def test_durable_idempotency_after_interruption_and_no_reexecution(self):
        with tempfile.TemporaryDirectory() as folder, patch('track_a.executor.docker') as docker:
            docker.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
            path = str(Path(folder) / 'state.sqlite')
            first = State(path)
            session = first.create('session')
            payload = {'request_id': 'write', 'argv': ['echo', 'x'], 'timeout_seconds': 3}
            with patch('track_a.executor.threading.Thread'):
                run = first.start(session['id'], payload)
            second = State(path)
            second.recover()
            docker.reset_mock()
            repeated = second.start(session['id'], payload)
            self.assertEqual(run['id'], repeated['id'])
            self.assertEqual(repeated['result']['error_kind'], 'interrupted')
            docker.assert_not_called()
            with self.assertRaises(Conflict):
                second.start(session['id'], {**payload, 'argv': ['echo', 'different']})
            second.terminal(repeated, failure('cancelled', 'cancelled'))
            self.assertEqual(len([e for e in repeated['events'] if e['type'] == 'result']), 1)

    def test_failed_removal_is_not_reported_as_closed_or_cancelled(self):
        with patch('track_a.executor.docker') as docker:
            docker.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
            state = State()
            session = state.create('session')
            with patch('track_a.executor.threading.Thread'):
                run = state.start(session['id'], {'request_id': 'x', 'argv': ['sleep', '10'], 'timeout_seconds': 10})
            docker.return_value = SimpleNamespace(returncode=1, stdout='', stderr='daemon unavailable')
            with self.assertRaises(RuntimeError):
                state.cancel(run['id'])
            self.assertEqual(session['status'], 'cleanup_failed')
            self.assertIsNone(run['result'])
            docker.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
            state.cancel(run['id'])
            self.assertEqual(run['result']['status'], 'cancelled')
            self.assertEqual(session['status'], 'closed')


class TaskTests(unittest.TestCase):
    def test_cancel_during_model_call_prevents_late_tool_execution(self):
        entered, resume = threading.Event(), threading.Event()
        class Model:
            backend = 'mock'
            def ask(self, *_):
                entered.set()
                resume.wait(2)
                return {'tool': 'exec', 'argv': ['echo', 'late']}, '{}', {}
        class Client:
            def __init__(self): self.calls = []
            def request(self, method, path, payload=None):
                self.calls.append((method, path))
                return 200, {'status': 'open'}
        client = Client()
        gateway = Gateway(client, Model())
        with patch('track_a.server.threading.Thread'):
            task = gateway.create('session', {'request_id': 'task', 'message': 'hello'})
        worker = threading.Thread(target=gateway.execute, args=(task,))
        worker.start()
        self.assertTrue(entered.wait(1))
        gateway.cancel(task['id'])
        resume.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        # A subsequent create retry returns the terminal record, not another thread.
        same = gateway.create('session', {'request_id': 'task', 'message': 'hello'})
        self.assertEqual(same['result']['status'], 'cancelled')
        self.assertFalse(any(path.endswith('/runs') for _, path in client.calls))
        self.assertEqual(len([e for e in same['events'] if e['type'] == 'result']), 1)


if __name__ == '__main__':
    unittest.main()
