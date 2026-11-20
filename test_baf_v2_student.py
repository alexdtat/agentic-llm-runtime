"""Verify locally authored positive and negative witnesses for every student task."""
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent / "track_a" / "baf-v2" / "student"

DISPATCH = {
    "route": "Q-35",
    "sources": ["logs/early.csv", "logs/late.csv"],
    "parcels": 3,
    "boxes": 12,
    "status": "ready",
}
WITNESSES = {
    "S-B1": ("35\n", "45\n"),
    "S-B2": ("services/ledger/routes.conf\n", "services/preview/routes.conf\n"),
    "S-B3": ('{"alerts":false,"queue":"007","profile":"night"}', '{"alerts":0,"queue":"007","profile":"night"}'),
    "S-B5": ("16\n", "37\n"),
    "S-B7": ("H-34\n", "H-48\n"),
    "S-B8": ('{"east":4,"north":8,"west":11}', '{"east":4,"north":10,"west":11}'),
    "S-N1": ("RX | Томск | 007\n", "RX | Томск | 7\n"),
    "S-N2": (
        'K-08 :: «Станция ""Север""»\nK-03 :: «Сводка: берег»\nK-11 :: «Пункт ""А"", сектор ""Б""»\n',
        'K-08 :: «Станция "Север"»\nK-03 :: «Сводка: берег»\nK-11 :: «Пункт "А", сектор "Б"»\n',
    ),
    "S-N3": (
        "[—]\n[NA]\n[N/A]\n[north]\n[NA-2]\n[\\NA]\n",
        "[—]\n[—]\n[N/A]\n[north]\n[NA-2]\n[NA]\n",
    ),
    "S-C1": (json.dumps(DISPATCH), json.dumps({**DISPATCH, "status": "hold"})),
}

CORRECT_CODE = """\
def join_ranges(ranges):
    if any(start > end for start, end in ranges):
        raise ValueError("backwards interval")

    result = []
    for start, end in sorted(ranges):
        if start == end:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result
"""
OMIT_TOUCHING = CORRECT_CODE.replace("start <= result", "start < result")
MUTATE_INPUT = CORRECT_CODE.replace("result = []", "ranges.sort()\n    result = []")


class StudentCheckers(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def prepare(self, task_id):
        source = BASE / task_id
        task = json.loads((source / "task.json").read_text(encoding="utf-8"))
        workdir = self.root / task_id
        shutil.copytree(source / "fixture", workdir)
        return workdir, workdir / task["answer_file"]

    def invoke_checker(self, task_id, workdir):
        result = subprocess.run(
            [sys.executable, str(BASE / task_id / "check.py"), str(workdir)],
            text=True, capture_output=True, check=True, timeout=5,
        )
        return json.loads(result.stdout)

    def test_task_ids_and_fixture_contract(self):
        expected = WITNESSES.keys() | {"S-B4", "S-B6"}
        self.assertEqual({path.name for path in BASE.iterdir() if path.is_dir()}, expected)
        for task_id in sorted(expected):
            with self.subTest(task=task_id):
                source = BASE / task_id
                task = json.loads((source / "task.json").read_text(encoding="utf-8"))
                self.assertEqual(task["id"], task_id)
                self.assertTrue(task["messages"][0])
                self.assertGreater(task["timeout_s"], 0)
                fixtures = {
                    path.relative_to(source / "fixture").as_posix()
                    for path in (source / "fixture").rglob("*")
                    if path.is_file()
                }
                self.assertTrue(fixtures)
                self.assertFalse(fixtures & {"check.py", "task.json", "expected.json"})
                self.assertEqual(
                    task["mutable_fixture_files"], ["manifest.toml"] if task_id == "S-B4" else [],
                )
        task = json.loads((BASE / "S-C1" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["decomposition"], ["B2", "B1", "B8", "B7", "B3"])

    def test_each_text_or_json_submission(self):
        for task_id, (good, bad) in WITNESSES.items():
            with self.subTest(task=task_id):
                workdir, answer = self.prepare(task_id)
                self.assertFalse(self.invoke_checker(task_id, workdir)["pass"])
                answer.write_bytes(good.encode("utf-8"))
                self.assertTrue(self.invoke_checker(task_id, workdir)["pass"])
                answer.write_bytes(bad.encode("utf-8"))
                self.assertFalse(self.invoke_checker(task_id, workdir)["pass"])

    def test_json_rejects_duplicates_and_composite_wrong_stage_results(self):
        workdir, answer = self.prepare("S-B3")
        answer.write_text('{"profile":"day","profile":"night","alerts":false,"queue":"007"}')
        self.assertFalse(self.invoke_checker("S-B3", workdir)["pass"])

        workdir, answer = self.prepare("S-C1")
        for change in (
            {"route": "Q-37"},
            {"sources": ["logs/late.csv", "logs/early.csv"]},
            {"parcels": 4, "boxes": 15},
        ):
            with self.subTest(change=change):
                answer.write_text(json.dumps({**DISPATCH, **change}))
                self.assertFalse(self.invoke_checker("S-C1", workdir)["pass"])

    def test_edit_rejects_changes_outside_active_value(self):
        workdir, answer = self.prepare("S-B4")
        initial = answer.read_bytes()
        answer.write_bytes(initial.replace(b"retry_delay  = 15", b"retry_delay  = 25", 1))
        self.assertTrue(self.invoke_checker("S-B4", workdir)["pass"])
        answer.write_bytes(initial.replace(b"retry_delay  = 15", b"retry_delay  = 25"))
        self.assertFalse(self.invoke_checker("S-B4", workdir)["pass"])

    def test_tool_requires_matching_confirmed_v2_execution(self):
        check_trace = runpy.run_path(str(BASE / "S-B5" / "check.py"))["check_trace"]

        def trace(command, *, op="meter-op", code=0, truncated=False):
            attempt = {"schema_version": 2, "session_id": "session-a", "run_id": "run-a"}
            return [
                {
                    **attempt, "sequence": 1, "type": "tool.started",
                    "payload": {"op_id": "meter-op", "call": {"tool": "RunCommand", "args": {"command": command}}},
                },
                {
                    **attempt, "sequence": 2, "type": "tool.result",
                    "payload": {"op_id": op, "result": {
                        "exit_code": code, "error_kind": "command_failure" if code else None,
                        "truncated": truncated, "stdout": "observed_minutes=3\npeak_load=16\ntotal_load=37\n",
                    }},
                },
            ]

        command = "python3 meter.py readings.csv"
        correct = trace(command)
        self.assertTrue(check_trace(correct)["pass"])
        self.assertTrue(check_trace(trace("/usr/bin/python3 /work/meter.py ./readings.csv"))["pass"])
        for events in (
            [], correct[:1], correct[1:], correct[::-1],
            [correct[0], {**correct[1], "run_id": "other-run"}],
            trace("echo python3 meter.py readings.csv"),
            trace(command + " && true"), trace("python3\nmeter.py readings.csv"),
            trace(command, op="another-op"), trace(command, code=1), trace(command, truncated=True),
        ):
            with self.subTest(events=events):
                self.assertFalse(check_trace(events)["pass"])

    def test_code_witnesses_are_evaluated_only_via_supplied_executor(self):
        task_id = "S-B6"
        workdir, answer = self.prepare(task_id)
        check_isolated = runpy.run_path(str(BASE / task_id / "check.py"))["check_isolated"]

        def trusted_test_executor(program):
            # This test provides every executed source itself.
            self.assertIn(answer.read_text(encoding="utf-8"), (CORRECT_CODE, OMIT_TOUCHING, MUTATE_INPUT))
            result = subprocess.run(
                [sys.executable, "-I", "-c", program], cwd=self.root,
                text=True, capture_output=True, check=True, timeout=5,
            )
            return result.stdout

        for source, passed in ((CORRECT_CODE, True), (OMIT_TOUCHING, False), (MUTATE_INPUT, False)):
            with self.subTest(passed=passed, source=source):
                answer.write_text(source, encoding="utf-8")
                self.assertIs(check_isolated(workdir, trusted_test_executor)["pass"], passed)

        answer.write_text("raise AssertionError('must never execute on local CLI')\n", encoding="utf-8")
        fallback = self.invoke_checker(task_id, workdir)
        self.assertFalse(fallback["pass"])
        self.assertEqual(fallback["outcome"], "infra_error")

        def unavailable(_program):
            raise RuntimeError("executor unavailable")

        self.assertEqual(check_isolated(workdir, unavailable)["outcome"], "infra_error")


if __name__ == "__main__":
    unittest.main()
