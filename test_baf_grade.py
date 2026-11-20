"""Offline grader regressions. Only authored reference code runs without Docker."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from track_a import baf_grade as grader


def trace(*tools):
    recorded = [('run.status', {'status': 'queued'}), *tools,
                ('run.result', {'status': 'completed', 'cleanup_state': 'confirmed'})]
    return [dict(schema_version=2, event_id=f'session:{index}', sequence=index,
                 session_id='session', run_id='run', type=kind, payload=payload)
            for index, (kind, payload) in enumerate(recorded, 1)]


def execution(command, *, exit_code=0, stdout='', stderr=''):
    return [
        ('tool.started', {'op_id': 'actual-operation',
                          'call': {'id': 'model-call', 'tool': 'RunCommand',
                                   'args': {'command': command}}}),
        ('tool.result', {'op_id': 'actual-operation',
                         'result': {'exit_code': exit_code, 'truncated': False,
                                    'stdout': stdout, 'stderr': stderr,
                                    'error_kind': 'command_failure' if exit_code else None}}),
    ]


class GradingTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)

    def snapshot(self, task_id):
        _, fixture, _ = grader.source_task(task_id)
        work = self.directory / 'snapshot'
        shutil.copytree(fixture, work)
        return work

    def test_trusted_file_checker_compares_immutable_fixtures(self):
        snapshot = self.snapshot('B1.1')
        (snapshot / 'answer.txt').write_text('8137\n')
        (snapshot / 'check.py').write_text('raise RuntimeError("snapshot checker must never run")\n')
        self.assertEqual(grader.grade('B1.1', snapshot, trace())['outcome'], 'pass')
        (snapshot / 'config/app.yaml').write_text('port: 4444\n')
        result = grader.grade('B1.1', snapshot, trace())
        self.assertEqual(result['outcome'], 'fail')
        self.assertEqual(result['changed'], ['config/app.yaml'])
        self.assertEqual(grader.grade('unknown', snapshot, trace())['outcome'], 'infra_error')

    def test_no_follow_or_oversize_reads_from_submission(self):
        snapshot = self.snapshot('B1.1')
        artifact = snapshot / 'answer.txt'
        outside = self.directory / 'outside'
        outside.write_text('8137\n')
        for create in (lambda: artifact.symlink_to(outside), lambda: os.link(outside, artifact),
                       lambda: os.mkfifo(artifact)):
            with self.subTest(create=create):
                create()
                self.assertEqual(grader.grade('B1.1', snapshot, trace())['outcome'], 'fail')
                artifact.unlink()
        with artifact.open('wb') as stream:
            stream.truncate(grader.MAX_FILE + 1)
        self.assertEqual(grader.grade('B1.1', snapshot, trace())['outcome'], 'fail')

    def test_tool_success_requires_real_paired_op_evidence(self):
        snapshot = self.snapshot('B5.1')
        (snapshot / 'answer.txt').write_text('7\n')
        self.assertEqual(grader.grade('B5.1', snapshot, trace())['outcome'], 'fail')
        attempts = execution('python3 stats.py data.csv', stdout='7\n')
        recorded = trace(*attempts)
        self.assertEqual(grader.grade('B5.1', snapshot, recorded)['outcome'], 'pass')
        recorded[2]['payload']['op_id'] = 'forged-operation'
        self.assertEqual(grader.grade('B5.1', snapshot, recorded)['outcome'], 'infra_error')
        attempts = execution('python3\nstats.py data.csv', stdout='7\n')
        self.assertEqual(grader.grade('B5.1', snapshot, trace(*attempts))['outcome'], 'fail')

    def test_confirmed_nonzero_command_can_be_correct_tool_evidence(self):
        snapshot = self.snapshot('B5.3')
        (snapshot / 'answer.txt').write_text('no\nrequired manifest missing\n')
        recorded = trace(*execution('sh build.sh', exit_code=1, stdout='OK\n',
                                    stderr='required manifest missing\n'))
        self.assertEqual(grader.grade('B5.3', snapshot, recorded)['outcome'], 'pass')
        recorded[2]['payload']['result']['truncated'] = True
        self.assertEqual(grader.grade('B5.3', snapshot, recorded)['outcome'], 'fail')

    def test_external_trace_requires_regular_file_outside_snapshot(self):
        snapshot = self.snapshot('B1.1')
        events = snapshot / 'events.json'
        events.write_text(json.dumps(trace()))
        with self.assertRaisesRegex(ValueError, 'outside'):
            grader.read_trace(events, snapshot)
        outside = self.directory / 'events.json'
        events.rename(outside)
        self.assertEqual(grader.read_trace(outside, snapshot), trace())

    def test_trusted_multifile_reference_with_the_isolated_python_argv(self):
        snapshot = self.snapshot('C3')
        (snapshot / 'tracker/__init__.py').write_text(
            'from .formatting import format_minutes\nfrom .duration import parse_duration\n')
        duration = snapshot / 'tracker/duration.py'
        duration.write_text(
            'import re\n'
            'def parse_duration(text):\n'
            '    parts = re.fullmatch(r"(?:([0-9]+)h *)?(?:([0-9]+)m *)?(?:([0-9]+)s)?", text.strip(" "))\n'
            '    if parts is None or not any(parts.groups()):\n'
            '        raise ValueError(text)\n'
            '    return sum(int(n or 0) * unit for n, unit in zip(parts.groups(), (3600, 60, 1)))\n')
        (snapshot / 'tests/test_duration.py').write_text(
            'import unittest\nfrom tracker import parse_duration\n'
            'class DurationTests(unittest.TestCase):\n'
            '    def test_valid(self):\n'
            '        self.assertEqual(parse_duration("1m1s"), 61)\n'
            '    def test_invalid(self):\n'
            '        with self.assertRaises(ValueError):\n'
            '            parse_duration("1m2h")\n')
        cases = []

        def trusted_python(workspace):
            def run(program):
                cases.append(program)
                return subprocess.check_output([
                    sys.executable, '-I', '-B', '-c', grader.workspace_program(program, str(workspace)),
                ], cwd=workspace, text=True, timeout=5)
            return run

        with patch.object(grader, 'IsolatedPython', side_effect=trusted_python):
            expected = grader.grade('C3', snapshot, trace())
            self.assertEqual(expected['outcome'], 'pass', expected)
            self.assertEqual(len(cases), 16)
            duration.write_text('def parse_duration(text):\n    return 999\n')
            self.assertEqual(grader.grade('C3', snapshot, trace())['outcome'], 'fail')


class IsolationTests(unittest.TestCase):
    @staticmethod
    def result(out=b'', code=0):
        return subprocess.CompletedProcess([], code, out, b'')

    def test_fresh_container_policy_and_cleanup_for_each_hidden_case(self):
        names = []

        def docker(*args, seconds=5):
            name = args[args.index('--name') + 1] if args[0] == 'create' else names[-1]
            if args[0] == 'create':
                names.append(name)
                for flag, value in (('--network', 'none'), ('--user', '1000:1000'), ('--pull', 'never'),
                                    ('--workdir', '/work'), ('--cap-drop', 'ALL')):
                    self.assertEqual(args[args.index(flag) + 1], value)
                self.assertIn('--read-only', args)
                self.assertIn('--tmpfs', args)
                self.assertIn('type=bind,src=/tmp/snapshot,dst=/work,readonly,bind-recursive=disabled', args)
                self.assertIn('sys.path.append(\'/work\')', args[-1])
                return self.result()
            if args[0] == 'start':
                self.assertEqual(args[-1], name)
                return self.result(b'42\n')
            if args[0] == 'inspect':
                return self.result(b'{"Status":"exited","ExitCode":0,"OOMKilled":false,"Error":""}')
            if args[0] == 'rm':
                self.assertEqual(args, ('rm', '-f', name))
            if args[0] == 'ps':
                self.assertEqual(args[-1], f'name=^/{name}$')
            return self.result()

        backend = grader.IsolatedPython(Path('/tmp/snapshot'))
        with patch.object(backend, 'docker', side_effect=docker) as invoked:
            for _ in range(16):
                self.assertEqual(backend('print(42)'), '42\n')
            with self.assertRaisesRegex(RuntimeError, 'excessive'):
                backend('print(42)')
        self.assertEqual(len(set(names)), 16)
        self.assertEqual([call.args[0] for call in invoked.call_args_list],
                         ['create', 'start', 'inspect', 'rm', 'ps'] * 16)

    def test_daemon_failure_or_unconfirmed_cleanup_cannot_pass(self):
        backend = grader.IsolatedPython(Path('/tmp/snapshot'))
        created = self.result()
        good_state = self.result(b'{"Status":"exited","ExitCode":0,"OOMKilled":false,"Error":""}')
        for responses, error in (
            ([self.result(code=125), self.result(code=1), self.result()], 'Cannot create'),
            ([created, self.result(code=1),
              self.result(b'{"Status":"created","ExitCode":0,"OOMKilled":false,"Error":"daemon"}'),
              self.result(), self.result()], 'did not confirm'),
            ([created, self.result(b'42\n'), good_state, self.result(), self.result(b'remains\n')],
             'cleanup was not confirmed'),
        ):
            with self.subTest(error=error), patch.object(backend, 'docker', side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, error):
                    backend('print(42)')

    def test_output_limit_is_enforced_while_reading(self):
        with self.assertRaisesRegex(grader.CommandLimit, 'Output exceeds'):
            grader.command([sys.executable, '-c', f'print("x" * {grader.MAX_OUTPUT})'], seconds=3)


if __name__ == '__main__':
    unittest.main()
