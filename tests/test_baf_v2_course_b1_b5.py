"""Offline checks of course medium/trap fixtures and trusted grading interfaces."""
import ast
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

BASE = Path(__file__).resolve().parents[1] / 'track_a/baf-v2'
PATHS = {
    'B1.2': 'B1-read/B1.2-contract-payment',
    'B1.3': 'B1-read/B1.3-contract-penalty',
    'B2.2': 'B2-search/B2.2-db-url-reads',
    'B2.3': 'B2-search/B2.3-timezone-parser',
    'B3.2': 'B3-create/B3.2-sorted-report',
    'B3.3': 'B3-create/B3.3-yaml-strings',
    'B4.2': 'B4-edit/B4.2-rename-total',
    'B4.3': 'B4-edit/B4.3-cp1251-price',
    'B5.2': 'B5-tool/B5.2-failed-tests',
    'B5.3': 'B5-tool/B5.3-build-status',
}


class CourseLowTasks(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def fixture(self, tid):
        task = BASE / PATHS[tid]
        work = self.root / tid
        shutil.copytree(task / 'fixture', work)
        return task, work, runpy.run_path(str(task / 'check.py'))

    def positive(self, tid, task, work):
        if tid == 'B1.2':
            (work / 'answer.txt').write_text('30\n')
        elif tid == 'B1.3':
            (work / 'answer.txt').write_text('НЕТ ДАННЫХ\n')
        elif tid == 'B2.2':
            (work / 'answer.txt').write_text('\n'.join(json.loads((task / 'expected.json').read_text())) + '\n')
        elif tid == 'B2.3':
            (work / 'answer.txt').write_text('api/timestamps.py\n')
        elif tid == 'B3.2':
            import csv
            with (work / 'report.csv').open('w', newline='') as stream:
                csv.writer(stream).writerows(json.loads((task / 'expected.json').read_text()))
        elif tid == 'B3.3':
            (work / 'countries.yaml').write_text('Norway: "NO"\nOntario: "ON"\nGermany: DE\n')
            (work / 'postal.yaml').write_text('Nord: "0123"\nWest: "0042"\nCentral: "1000"\n')
        elif tid == 'B4.2':
            metadata = json.loads((task / 'task.json').read_text())
            for name in metadata['mutable_fixture_files']:
                path = work / name
                path.write_text(path.read_text().replace('calc_total', 'compute_total'))
        elif tid == 'B4.3':
            path = work / 'prices.csv'
            path.write_bytes(path.read_bytes().replace(b'1290', b'990'))
        elif tid == 'B5.2':
            (work / 'failed.txt').write_text('\n'.join(json.loads((task / 'expected.json').read_text())) + '\n')
        elif tid == 'B5.3':
            (work / 'answer.txt').write_text('no\nrequired manifest missing\n')

    def observation(self, tid, work):
        # Only our pristine trusted fixture program is executed, never candidate B4 code.
        argv = [sys.executable, '-B', 'run_tests.py'] if tid == 'B5.2' else ['sh', 'build.sh']
        result = subprocess.run(argv, cwd=work, capture_output=True, text=True, timeout=5,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 1)
        self.assertLessEqual(len(result.stdout.encode()), 1024)
        self.assertLessEqual(len(result.stderr.encode()), 1024)
        observed = ['python3', 'run_tests.py'] if tid == 'B5.2' else argv
        return [{'request': {'argv': observed}, 'result': {
            'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
            'error_kind': 'command_failure', 'truncated': False,
        }}]

    def grade(self, tid, module, work, evidence=None):
        if tid == 'B4.2':
            return module['static_check'](work)
        if tid.startswith('B5'):
            return module['check_with_evidence'](work, evidence)
        return module['check'](work)

    def test_ten_positive_and_negative_outputs(self):
        for tid in PATHS:
            with self.subTest(task=tid):
                task, work, module = self.fixture(tid)
                self.positive(tid, task, work)
                evidence = self.observation(tid, work) if tid.startswith('B5') else None
                self.assertTrue(self.grade(tid, module, work, evidence)['pass'])
                if tid.startswith(('B1', 'B2')) or tid == 'B5.3':
                    (work / 'answer.txt').write_text('wrong\n')
                elif tid == 'B3.2':
                    (work / 'report.csv').write_text('date,city,amount\n')
                elif tid == 'B3.3':
                    (work / 'countries.yaml').write_text('Norway: NO\nOntario: ON\nGermany: DE\n')
                elif tid == 'B4.2':
                    path = work / 'invoice.py'
                    path.write_text(path.read_text().replace('compute_total', 'calc_total'))
                elif tid == 'B4.3':
                    path = work / 'prices.csv'
                    path.write_text(path.read_bytes().decode('cp1251'))
                elif tid == 'B5.2':
                    (work / 'failed.txt').write_text('test_project.TestProject.test_case_04\n')
                self.assertFalse(self.grade(tid, module, work, evidence)['pass'])

    def test_materialized_search_fixture_has_exactly_twelve_reads_in_two_hundred_files(self):
        task = BASE / PATHS['B2.2']
        modules = sorted((task / 'fixture').rglob('*.py'))
        self.assertEqual(len(modules), 200)
        readers = []
        for path in modules:
            tree = ast.parse(path.read_text())
            if any(isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.environ.get'
                   and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'DB_URL'
                   for node in ast.walk(tree)):
                readers.append(path.relative_to(task / 'fixture').as_posix())
        self.assertEqual(len(readers), 12)
        self.assertEqual(readers, json.loads((task / 'expected.json').read_text()))

    def test_contract_has_fifteen_explicit_pages_and_single_payment_clause(self):
        text = (BASE / PATHS['B1.2'] / 'fixture/contract.md').read_text()
        self.assertEqual(text.count('<!-- Страница '), 15)
        self.assertGreater(len(text.split()), 4000)
        self.assertEqual(text.count('30 календарных дней'), 1)

    def test_cp1251_crlf_requires_preservation_of_other_rows(self):
        task, work, module = self.fixture('B4.3')
        data = (work / 'prices.csv').read_bytes()
        with self.assertRaises(UnicodeDecodeError):
            data.decode('utf-8')
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        self.positive('B4.3', task, work)
        self.assertTrue(module['check'](work)['pass'])
        path = work / 'prices.csv'
        path.write_bytes(path.read_bytes().replace(b'250', b'251'))
        self.assertFalse(module['check'](work)['pass'])

    def test_yaml_rejects_implicit_scalars_and_accepts_json_subset(self):
        task, work, module = self.fixture('B3.3')
        self.positive('B3.3', task, work)
        (work / 'countries.yaml').write_text(json.dumps({'Norway': 'NO', 'Ontario': 'ON', 'Germany': 'DE'}))
        self.assertTrue(module['check'](work)['pass'])
        for value in ('Nord: 0123\nWest: "0042"\nCentral: "1000"\n', '[bad YAML', '!!python/object:foo {}'):
            (work / 'postal.yaml').write_text(value)
            self.assertFalse(module['check'](work)['pass'])

    def test_rename_isolated_interface_never_imports_candidate_on_host(self):
        task, work, module = self.fixture('B4.2')
        self.positive('B4.2', task, work)
        self.assertEqual(module['check'](work)['outcome'], 'infra_error')
        isolated = Mock(return_value=json.dumps(['0.00', '0.30', '4.50', '2.00', '12.00']))
        self.assertTrue(module['check_isolated'](work, isolated)['pass'])
        program = isolated.call_args.args[0]
        self.assertIn('from billing.pricing import compute_total', program)
        self.assertNotIn('4.50', program)
        for output in ('not JSON', '{}', '["wrong"]', '{"passed": 5}'):
            with self.subTest(output=output):
                self.assertFalse(module['check_isolated'](work, Mock(return_value=output))['pass'])
        unavailable = module['check_isolated'](work, Mock(side_effect=OSError('Docker unavailable')))
        self.assertEqual(unavailable['outcome'], 'infra_error')
        # This is interface validation. The isolated behavior tests are not run by this host test.
        marker = self.root / 'candidate-executed'
        (work / 'invoice.py').write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        isolated.reset_mock()
        self.assertFalse(module['check_isolated'](work, isolated)['pass'])
        isolated.assert_not_called()
        self.assertFalse(marker.exists())

    def test_tool_evidence_requires_observed_command_exit_stderr_and_no_truncation(self):
        for tid in ('B5.2', 'B5.3'):
            with self.subTest(task=tid):
                task, work, module = self.fixture(tid)
                self.positive(tid, task, work)
                actual = self.observation(tid, work)
                self.assertTrue(module['check_with_evidence'](work, actual)['pass'])
                self.assertEqual(module['check_with_evidence'](work, None)['outcome'], 'infra_error')
                for field, value in (('exit_code', 0), ('stderr', ''), ('truncated', True)):
                    changed = json.loads(json.dumps(actual))
                    changed[0]['result'][field] = value
                    self.assertFalse(module['check_with_evidence'](work, changed)['pass'])
                changed = json.loads(json.dumps(actual))
                changed[0]['request']['argv'] = ['echo', 'OK']
                self.assertFalse(module['check_with_evidence'](work, changed)['pass'])

    def test_immutable_fixtures_and_answer_symlinks_are_rejected(self):
        task, work, module = self.fixture('B1.2')
        self.positive('B1.2', task, work)
        (work / 'contract.md').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'Changed immutable'):
            module['check'](work)
        (work / 'contract.md').write_bytes((task / 'fixture/contract.md').read_bytes())
        (work / 'answer.txt').unlink()
        answer = self.root / 'outside-answer'
        answer.write_text('30')
        (work / 'answer.txt').symlink_to(answer)
        with self.assertRaisesRegex(ValueError, 'Links are forbidden'):
            module['check'](work)

    def test_checker_cli_and_fail_closed_dependency_and_evidence(self):
        for tid in PATHS:
            with self.subTest(task=tid):
                task, work, _ = self.fixture(tid)
                self.positive(tid, task, work)
                arguments = [sys.executable, str(task / 'check.py'), str(work)]
                if tid.startswith('B5'):
                    evidence = self.root / (tid + '-evidence.json')
                    evidence.write_text(json.dumps({'tool_runs': self.observation(tid, work)}))
                    arguments.append(str(evidence))
                process = subprocess.run(arguments, capture_output=True, text=True, timeout=5)
                self.assertEqual(process.returncode, 0, process.stderr)
                value = json.loads(process.stdout)
                if tid == 'B4.2':
                    self.assertFalse(value['pass'])
                    self.assertEqual(value['outcome'], 'infra_error')
                else:
                    self.assertTrue(value['pass'], value)
        yaml_task = BASE / PATHS['B3.3']
        process = subprocess.run([sys.executable, '-S', str(yaml_task / 'check.py'), str(self.root / 'B3.3')],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['outcome'], 'infra_error')
        task = BASE / PATHS['B5.3']
        work = self.root / 'B5.3'
        forged = work / 'evidence.json'
        forged.write_text(json.dumps({'tool_runs': []}))
        process = subprocess.run([sys.executable, str(task / 'check.py'), str(work), str(forged)],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(json.loads(process.stdout)['pass'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
