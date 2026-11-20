import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from track_a.baf_plan import FULL_MANIFEST, LEVELS, build_plan


class FullBafPlanTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.track_root = Path(folder.name)
        self.manifest_path = self.track_root / "full.json"
        self.manifest = json.loads(FULL_MANIFEST.read_text(encoding="utf-8"))
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        for entry in self.manifest["tasks"]:
            task_dir = self.track_root / entry["path"]
            (task_dir / "fixture").mkdir(parents=True)
            task_id = entry["id"]
            task = {"id": task_id, "messages": ["A synthetic task"], "timeout_s": 15}
            if task_id.startswith("B"):
                task.update(baf=task_id.split(".")[0], level=LEVELS[int(task_id[-1]) - 1])
            elif task_id.startswith(("C", "S-C")):
                task.update(baf="C", level="composite", decomposition=["B1", "B2"])
            else:
                task.update(baf=task_id[2:] if task_id.startswith("S-B") else "N", level="medium")
            (task_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")
            (task_dir / "check.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            (task_dir / "fixture" / "data.bin").write_bytes(b"\x00\xcf")

    def plan(self):
        return build_plan(self.manifest_path, self.track_root)

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def task_file(self, relative):
        return self.track_root / self.manifest["tasks"][0]["path"] / relative

    def test_complete_schedule_and_determinism(self):
        plan = self.plan()
        ids = [task["id"] for task in plan["tasks"]]
        self.assertEqual(len(ids), 39)
        self.assertEqual(len(set(ids)), 39)
        self.assertEqual(len(plan["attempts"]), 117)
        for repeat in range(1, 4):
            self.assertEqual([entry["task_id"] for entry in plan["attempts"]
                              if entry["repeat"] == repeat], ids)
        self.assertEqual(plan, self.plan())

    def test_fingerprint_records_byte_exact_changes(self):
        plan = self.plan()
        self.assertEqual(plan["tasks"][0]["files"]["fixture/data.bin"],
                         hashlib.sha256(b"\x00\xcf").hexdigest())
        for relative in ("task.json", "check.py", "fixture/data.bin"):
            with self.subTest(relative=relative):
                path = self.task_file(relative)
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                updated = self.plan()
                self.assertNotEqual(updated["tasks"][0]["sha256"], plan["tasks"][0]["sha256"])
                self.assertNotEqual(updated["suite_sha256"], plan["suite_sha256"])
                self.assertEqual(updated["tasks"][1:], plan["tasks"][1:])
                path.write_bytes(original)

    def test_fingerprint_ignores_checker_bytecode_but_keeps_fixture_bytes(self):
        plan = self.plan()
        cache = self.task_file("__pycache__/check.cpython-312.pyc")
        cache.parent.mkdir()
        cache.write_bytes(b"local bytecode")
        self.assertEqual(self.plan(), plan)
        fixture = self.task_file("fixture/__pycache__/example.cpython-312.pyc")
        fixture.parent.mkdir()
        fixture.write_bytes(b"intentional fixture data")
        updated = self.plan()
        self.assertIn("fixture/__pycache__/example.cpython-312.pyc", updated["tasks"][0]["files"])
        self.assertNotEqual(updated["suite_sha256"], plan["suite_sha256"])

    def test_rejects_missing_duplicate_and_unknown_ids(self):
        original = self.manifest["tasks"][:]
        invalid_task_lists = (original[:-1], original + original[:1],
                              original[:-1] + [{"id": "other", "path": original[-1]["path"]}])
        for tasks in invalid_task_lists:
            with self.subTest(task_count=len(tasks)):
                self.manifest["tasks"] = tasks
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.plan()

    def test_rejects_invalid_paths_missing_files_and_mismatched_metadata(self):
        entry = self.manifest["tasks"][0]
        original = entry["path"]
        entry["path"] = "baf/../outside"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Invalid path"):
            self.plan()
        entry["path"] = original
        self.save_manifest()
        checker = self.task_file("check.py")
        checker.unlink()
        with self.assertRaisesRegex(ValueError, "Missing task.json, check.py"):
            self.plan()
        checker.write_text("pass\n", encoding="utf-8")
        self.task_file("task.json").write_text('{"id":"B1.9"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity differs"):
            self.plan()

    def test_rejects_symlinks_before_hashing(self):
        path = self.task_file("fixture/data.bin")
        path.unlink()
        path.symlink_to(self.manifest_path)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
