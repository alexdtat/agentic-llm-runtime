"""Exercise the course medium, trap and composite checker fixtures.

Only the static references and trusted fixtures defined by this test run in
local subprocesses. The test callback is not a sandbox or a live grader.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock

BASE = Path(__file__).resolve().parents[1] / "track_a" / "baf-v2"
PATHS = {
    "B6.2": "B6-code/B6.2-parse-logs",
    "B6.3": "B6-code/B6.3-days-between",
    "B7.2": "B7-reason/B7.2-resource-roles",
    "B7.3": "B7-reason/B7.3-conflicting-policy",
    "B8.2": "B8-synthesis/B8.2-meeting-decisions",
    "B8.3": "B8-synthesis/B8.3-current-regulation",
    "C1": "C-composite/C1-fix-bug",
    "C2": "C-composite/C2-department-report",
    "C3": "C-composite/C3-add-utility",
}
ISOLATED = {"B6.2", "B6.3", "C1", "C3"}

LOG_REFERENCE = r"""
    from datetime import datetime, timezone
    from pathlib import Path
    import json
    import re
    import sys

    timestamp = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})")
    result = {}
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        fields = line.split("|", 2)
        if len(fields) != 3:
            continue
        when, level, message = fields
        if not timestamp.fullmatch(when) or level not in {"INFO", "WARN", "ERROR"} or not message:
            continue
        try:
            local = datetime.fromisoformat(when.replace("Z", "+00:00"))
            utc = local.astimezone(timezone.utc)
        except ValueError:
            continue
        hour = f"{utc.date().isoformat()}T{utc.hour:02d}:00Z"
        bucket = result.setdefault(hour, {"total": 0, "errors": 0})
        bucket["total"] += 1
        if level == "ERROR":
            bucket["errors"] += 1
    print(json.dumps(result))
"""

DAYS_REFERENCE = r"""
    from datetime import date
    import re

    def parse(text):
        text = text.strip()
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
            return date.fromisoformat(text)
        if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}\u00a0г\.", text):
            day, month, year = map(int, text[:10].split("."))
            return date(year, month, day)
        raise ValueError

    def days_between(a, b):
        try:
            return (parse(b) - parse(a)).days
        except ValueError:
            return None
"""

BATCH_REFERENCE = """
    def plan_orders(orders, batch_size):
        if batch_size < 1:
            raise ValueError("batch size")
        batches = []
        for idx in range(0, len(orders), batch_size):
            batches.append(orders[idx:idx + batch_size])
        return batches
"""

DURATION_REFERENCE = r"""
    import re

    def parse_duration(text):
        text = text.strip(" ")
        result = re.fullmatch(r"(?:([0-9]+)h *)?(?:([0-9]+)m *)?(?:([0-9]+)s)?", text)
        if not text or not result:
            raise ValueError
        hours, minutes, seconds = (int(value or 0) for value in result.groups())
        return 3600 * hours + 60 * minutes + seconds
"""

DURATION_TESTS = """
    import unittest
    from tracker import parse_duration

    class DurationTests(unittest.TestCase):
        def test_valid(self):
            self.assertEqual(parse_duration("3h2m"), 10920)

        def test_invalid(self):
            with self.assertRaises(ValueError):
                parse_duration("3m 2h")
"""

DURATION_WEAK_TESTS = """
    import unittest
    from tracker import parse_duration

    class DurationTests(unittest.TestCase):
        def test_valid(self):
            self.assertEqual(parse_duration("3h2m"), 10920)
"""


def run_trusted_python(workdir, *command):
    completed = subprocess.run(
        [sys.executable, "-E", "-s", "-B", *command],
        cwd=workdir,
        capture_output=True,
        encoding="utf-8",
        timeout=5,
        check=True,
    )
    return completed.stdout


def install_trusted_reference(workdir, relative, source):
    target = workdir / relative
    target.parent.mkdir(exist_ok=True, parents=True)
    target.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


def load_trusted_checker(task_id):
    spec = importlib.util.spec_from_file_location(task_id, BASE / PATHS[task_id] / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decisions_from_fixture(fixture_dir):
    result = []
    for file in sorted(fixture_dir.glob("meetings/*.md")):
        text = file.read_text(encoding="utf-8")
        date = text.splitlines()[0].split()[-1]
        accepted = text.split("## Принято\n", 1)[1]
        for decision, owner in re.findall(r"^- (.+) Ответственный: \*\*(.+)\*\*$", accepted, re.M):
            result.append({"date": date, "decision": decision, "owner": owner})
    return result


class CourseHighCheckers(unittest.TestCase):
    def make_workdir(self, task_id, parent):
        return Path(shutil.copytree(BASE / PATHS[task_id] / "fixture", Path(parent) / "work"))

    def grade_data(self, task_id, workdir):
        checker = BASE / PATHS[task_id] / "check.py"
        return json.loads(run_trusted_python(workdir, str(checker), str(workdir)))

    def test_data_positive_and_negative(self):
        data = {
            "B7.2": ("answer.txt", "auditor\nassistant\nreader\ncoordinator\n", [
                "reader\ncoordinator\nassistant\nauditor\ndelegate\n",
                "reader\ncoordinator\nassistant\nauditor\nauditor\n",
            ]),
            "B7.3": ("answer.txt", "ПРОТИВОРЕЧИЕ\nQ-release Q-exception\n", [
                "NO\nQ-exception\n",
                "ПРОТИВОРЕЧИЕ\nQ-legacy Q-exception\n",
            ]),
            "B8.3": ("answer.txt", "Текущая: 2026-03-01 — 5 рабочих дней\nПредыдущая: 2025-10-01 — 8 рабочих дней\n", [
                "Текущая: 2025-10-01 — 8 рабочих дней\nПредыдущая: 2026-03-01 — 5 рабочих дней\n",
                "Текущая: 2026-03-01 — 5 рабочих дней\n",
            ]),
            "C2": ("report.md", "# Отчёт\n\n| Показатель | Значение |\n|---|---:|\n"
                   "| Юг, закрыто | 3 |\n| Север, закрыто | 2 |\n"
                   "| Закрыто заявок | 5 |\n| Трудозатраты, ч | 35 |\n", [
                "| Показатель | Значение |\n|---|---|\n| Закрыто заявок | 6 |\n| Трудозатраты, ч | 35 |\n| Север, закрыто | 2 |\n| Юг, закрыто | 3 |",
                "| Показатель | Значение |\n|---|---|\n| Закрыто заявок | 5 |\n| Трудозатраты, ч | около 35 |\n| Север, закрыто | 2 |\n| Юг, закрыто | 3 |",
            ]),
        }
        for task_id, (filename, good, bad_responses) in data.items():
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as temp:
                workdir = self.make_workdir(task_id, temp)
                answer = workdir / filename
                self.assertFalse(self.grade_data(task_id, workdir)["pass"])
                answer.write_text(good, encoding="utf-8")
                self.assertTrue(self.grade_data(task_id, workdir)["pass"])
                for bad in bad_responses:
                    answer.write_text(bad, encoding="utf-8")
                    self.assertFalse(self.grade_data(task_id, workdir)["pass"])

    def test_meeting_decisions_requires_all_records_with_types(self):
        with tempfile.TemporaryDirectory() as temp:
            workdir = self.make_workdir("B8.2", temp)
            self.assertEqual(len(list(workdir.glob("meetings/*.md"))), 10)
            records = decisions_from_fixture(workdir)
            self.assertEqual(len(records), 11)
            answer = workdir / "decisions.json"
            answer.write_text(json.dumps(records[::-1]), encoding="utf-8")
            self.assertTrue(self.grade_data("B8.2", workdir)["pass"])
            for invalid in [
                records[:-1],
                records + [records[0]],
                [{**records[0], "date": 20260202}, *records[1:]],
                [{**records[0], "owner": "Другой ответственный"}, *records[1:]],
            ]:
                answer.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertFalse(self.grade_data("B8.2", workdir)["pass"])

    def test_executable_checkers_fail_closed_without_isolation(self):
        for task_id in sorted(ISOLATED):
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as temp:
                workdir = self.make_workdir(task_id, temp)
                task_dir = BASE / PATHS[task_id]
                checker = load_trusted_checker(task_id)
                meta = json.loads((task_dir / "task.json").read_text())
                self.assertEqual(meta["checker"], "isolated_python")
                for relative in checker.REQUIRED_FILES:
                    install_trusted_reference(workdir, relative, "raise AssertionError('must not run')")

                result = json.loads(run_trusted_python(workdir, str(task_dir / "check.py"), str(workdir)))
                self.assertFalse(result["pass"])
                self.assertEqual(result["outcome"], "infra_error")

                callback = Mock(side_effect=RuntimeError("unavailable"))
                result = checker.check_isolated(workdir, callback)
                self.assertFalse(result["pass"])
                self.assertEqual(result["outcome"], "infra_error")
                callback.assert_called_once()

                (workdir / meta["answer_file"]).unlink()
                callback.reset_mock()
                self.assertFalse(checker.check_isolated(workdir, callback)["pass"])
                callback.assert_not_called()

    def test_trusted_reference_and_mutation_for_each_executable_checker(self):
        references = {
            "B6.2": ("parse_logs.py", LOG_REFERENCE, LOG_REFERENCE.replace("local.astimezone(timezone.utc)", "local")),
            "B6.3": ("days.py", DAYS_REFERENCE, DAYS_REFERENCE.replace("(parse(b) - parse(a)).days", "abs((parse(b) - parse(a)).days)")),
            "C1": ("dispatch/batches.py", BATCH_REFERENCE, None),
            "C3": ("tracker/duration.py", DURATION_REFERENCE, DURATION_REFERENCE.replace("3600 * hours", "60 * hours")),
        }
        for task_id, (path, reference, mutant) in references.items():
            with self.subTest(task=task_id), tempfile.TemporaryDirectory() as temp:
                workdir = self.make_workdir(task_id, temp)
                checker = load_trusted_checker(task_id)

                def run_on_trusted_reference(program):
                    return run_trusted_python(workdir, "-c", program)

                if task_id == "C3":
                    init = workdir / "tracker/__init__.py"
                    init.write_text(init.read_text() + "from .duration import parse_duration\n")
                    install_trusted_reference(workdir, "tests/test_duration.py", DURATION_TESTS)

                if task_id == "C1":
                    failed = checker.check_isolated(workdir, run_on_trusted_reference)
                    self.assertEqual(failed["outcome"], "fail", failed)

                install_trusted_reference(workdir, path, reference)
                verdict = checker.check_isolated(workdir, run_on_trusted_reference)
                self.assertTrue(verdict["pass"], verdict)

                if task_id == "C3":
                    install_trusted_reference(workdir, "tests/test_duration.py", DURATION_WEAK_TESTS)
                    weak_suite = checker.check_isolated(workdir, run_on_trusted_reference)
                    self.assertEqual(weak_suite["outcome"], "fail", weak_suite)
                    self.assertIn("ValueError", weak_suite["reason"])
                    install_trusted_reference(workdir, "tests/test_duration.py", DURATION_TESTS)

                if mutant is not None:
                    install_trusted_reference(workdir, path, mutant)
                    wrong = checker.check_isolated(workdir, run_on_trusted_reference)
                    self.assertEqual(wrong["outcome"], "fail", wrong)


if __name__ == "__main__":
    unittest.main()
