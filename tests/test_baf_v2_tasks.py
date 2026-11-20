"""Local checker tests. Executable examples are trusted test-suite literals."""
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from track_a.baf_support import check_task


TRACK_A = Path(__file__).resolve().parents[1] / "track_a"
SUITE = TRACK_A / "baf-v2"
CATALOG = {
    entry["id"]: TRACK_A / entry["path"]
    for entry in json.loads((SUITE / "smoke.json").read_text(encoding="utf-8"))["tasks"]
}


class BafV2SmokeTasks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workdir = Path(folder.name) / "work"

    def test_smoke_catalog_resolves_all_eight_task_directories(self):
        self.assertEqual(list(CATALOG), [f"B{i}.1" for i in range(1, 9)])
        for task_id, task_dir in CATALOG.items():
            with self.subTest(task=task_id):
                task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
                self.assertEqual(task["id"], task_id)
                self.assertEqual(task["level"], "simple")
                self.assertTrue((task_dir / "fixture").is_dir())
                self.assertTrue((task_dir / "check.py").is_file())
                self.assertEqual(task_dir.relative_to(TRACK_A).parts[0],
                                 "baf" if task_id in {"B1.1", "B2.1", "B3.1", "B4.1", "B5.1"} else "baf-v2")

    def test_connection_checks_answer_and_time(self):
        task_dir = CATALOG["B7.1"]
        shutil.copytree(task_dir / "fixture", self.workdir)
        answer = self.workdir / "answer.txt"
        for value, expected in [("no\n18\n", True), ("yes\n18\n", False), ("no\n25\n", False)]:
            with self.subTest(answer=value):
                answer.write_text(value, encoding="utf-8")
                self.assertEqual(check_task(task_dir, self.workdir, {})["pass"], expected)

    def test_quarter_sales_checks_sum_from_five_files(self):
        task_dir = CATALOG["B8.1"]
        shutil.copytree(task_dir / "fixture", self.workdir)
        answer = self.workdir / "answer.txt"
        self.assertEqual(len(list((self.workdir / "sales").glob("*.csv"))), 5)
        for value, expected in [("227.31\n", True), ("227.30\n", False), ("227.310\n", False)]:
            with self.subTest(answer=value):
                answer.write_text(value, encoding="utf-8")
                self.assertEqual(check_task(task_dir, self.workdir, {})["pass"], expected)
        (self.workdir / "sales" / "east.csv").unlink()
        answer.write_text("227.31\n", encoding="utf-8")
        self.assertFalse(check_task(task_dir, self.workdir, {})["pass"])

    def test_slugify_checker_compares_trusted_examples(self):
        task_dir = CATALOG["B6.1"]
        check = runpy.run_path(str(task_dir / "check.py"))["check_isolated"]
        shutil.copytree(task_dir / "fixture", self.workdir)
        candidate = self.workdir / "slug.py"

        def run_trusted_example(program):
            # This subprocess is a test adapter. Production needs a real sandbox.
            return subprocess.run([sys.executable, "-I", "-c", program], check=True,
                                  capture_output=True, text=True, timeout=3).stdout

        samples = [
            ('import re\ndef slugify(text):\n    return re.sub("[^A-Za-z0-9]+", "-", text).strip("-").lower()\n', "pass"),
            ('def slugify(text):\n    return text.lower()\n', "fail"),
            ("def slugify(:\n    pass\n", "fail"),
        ]
        for source, expected in samples:
            with self.subTest(expected=expected, source=source):
                candidate.write_text(source, encoding="utf-8")
                verdict = check(self.workdir, run_trusted_example)
                self.assertEqual(verdict["outcome"], expected)
                self.assertEqual(verdict["pass"], expected == "pass")

        def unavailable(_program):
            raise RuntimeError("test executor unavailable")

        self.assertEqual(check(self.workdir, unavailable)["outcome"], "infra_error")

    def test_slugify_cli_does_not_execute_candidate(self):
        task_dir = CATALOG["B6.1"]
        shutil.copytree(task_dir / "fixture", self.workdir)
        marker = self.workdir / "executed"
        (self.workdir / "slug.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
        )
        result = check_task(task_dir, self.workdir, {})
        self.assertFalse(result["pass"])
        self.assertEqual(result["file_checker"]["outcome"], "infra_error")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
