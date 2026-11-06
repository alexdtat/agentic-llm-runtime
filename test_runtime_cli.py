"""UI contract tests: no Docker, model, or network required."""
import contextlib
import io
import os
import unittest
from unittest.mock import patch

from track_a.cli import main


class RuntimeCliTests(unittest.TestCase):
    @patch.dict(os.environ, {'PROBE_TOKEN': 'mock-only'})
    @patch('track_a.cli.Client')
    def test_command_is_forwarded_as_argv_without_host_shell(self, factory):
        client = factory.return_value
        client.request.return_value = (200, {'id': 'run-1'})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['exec', 'session-1', '--', 'python3', '-c', 'print("$HOME;hello")'])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), 'run-1')
        payload = client.request.call_args.args[2]
        self.assertEqual(payload['argv'], ['python3', '-c', 'print("$HOME;hello")'])

    @patch.dict(os.environ, {'PROBE_TOKEN': 'mock-only'})
    @patch('track_a.cli.Client')
    def test_watch_prints_each_event_once_and_command_failure_is_failure(self, factory):
        started = {'type': 'started', 'sequence': 0}
        result = {'status': 'failed', 'exit_code': 3}
        terminal = {'type': 'result', 'sequence': 1, **result}
        factory.return_value.request.side_effect = [
            (200, {'events': [started], 'result': None}),
            (200, {'events': [started, terminal], 'result': result}),
        ]
        output = io.StringIO()
        with patch('track_a.cli.time.sleep'), contextlib.redirect_stdout(output):
            code = main(['watch', 'run-1'])
        self.assertEqual(code, 1)
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    @patch.dict(os.environ, {'PROBE_TOKEN': 'mock-only'})
    @patch('track_a.cli.Client')
    def test_auth_error_is_reported_without_token(self, factory):
        factory.return_value.request.return_value = (401, {'error': 'unauthorized'})
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = main(['new'])
        self.assertEqual(code, 1)
        self.assertIn('401', output.getvalue())
        self.assertNotIn('mock-only', output.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
