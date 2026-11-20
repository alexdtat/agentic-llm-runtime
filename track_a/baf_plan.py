"""Validate the CH2 full BAF suite and print an offline, repeatable execution plan."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


TRACK_A = Path(__file__).resolve().parent
FULL_MANIFEST = TRACK_A / "baf-v2" / "full.json"
REPEATS = 3
LEVELS = ("simple", "medium", "trap")
HISTORICAL_IDS = {f"B{baf}.1" for baf in range(1, 6)}
EXPECTED_IDS = (
    {f"B{baf}.{level}" for baf in range(1, 9) for level in range(1, 4)}
    | {f"C{number}" for number in range(1, 4)}
    | {f"S-B{baf}" for baf in range(1, 9)}
    | {f"S-N{number}" for number in range(1, 4)}
    | {"S-C1"}
)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def load_entries(manifest_path):
    manifest = read_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"version", "tasks"}
        or type(manifest["version"]) is not int
        or manifest["version"] != 1
        or not isinstance(manifest["tasks"], list)
    ):
        raise ValueError("Expected a version 1 full suite manifest with a tasks list")
    entries = manifest["tasks"]
    ids = set()
    paths = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "path"}
            or not isinstance(entry["id"], str)
            or not isinstance(entry["path"], str)
        ):
            raise ValueError("Each manifest entry must contain a string id and path")
        task_id, path = entry["id"], entry["path"]
        if task_id in ids or path in paths:
            raise ValueError(f"Duplicate task id or path for {task_id!r}")
        ids.add(task_id)
        paths.add(path)
    if ids != EXPECTED_IDS:
        missing = ", ".join(sorted(EXPECTED_IDS - ids))
        unexpected = ", ".join(sorted(ids - EXPECTED_IDS))
        raise ValueError(f"Full suite IDs differ (missing: {missing}; unexpected: {unexpected})")
    return entries


def task_directory(track_root, entry):
    relative = PurePosixPath(entry["path"])
    expected_root = "baf" if entry["id"] in HISTORICAL_IDS else "baf-v2"
    if (
        relative.as_posix() != entry["path"]
        or len(relative.parts) != 3
        or relative.parts[0] != expected_root
        or ".." in relative.parts
        or "\\" in entry["path"]
    ):
        raise ValueError(f"Invalid path for {entry['id']}: {entry['path']!r}")
    current = track_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ValueError(f"Expected a real task directory: {current}")
    return current


def validate_task(task_id, task):
    if not isinstance(task, dict) or task.get("id") != task_id:
        raise ValueError(f"task.json identity differs from manifest for {task_id}")
    if task_id.startswith("S-"):
        baf = task_id[2:] if task_id.startswith("S-B") else task_id[2]
    else:
        baf = task_id.split(".")[0] if task_id.startswith("B") else "C"
    messages = task.get("messages")
    timeout = task.get("timeout_s")
    if (
        task.get("baf") != baf
        or not isinstance(messages, list)
        or not messages
        or any(not isinstance(message, str) or not message.strip() for message in messages)
        or type(timeout) is not int
        or timeout <= 0
    ):
        raise ValueError(f"Invalid BAF, prompt or timeout for {task_id}")
    if task_id.startswith("B") and task.get("level") != LEVELS[int(task_id[-1]) - 1]:
        raise ValueError(f"Incorrect course level for {task_id}")
    if baf == "C":
        decomposition = task.get("decomposition")
        if (
            task.get("level") != "composite"
            or not isinstance(decomposition, list)
            or not decomposition
            or any(not isinstance(step, str) or step not in {f"B{i}" for i in range(1, 9)}
                   for step in decomposition)
        ):
            raise ValueError(f"Invalid composite decomposition for {task_id}")


def fingerprint_task(track_root, entry):
    directory = task_directory(track_root, entry)
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not supported in the suite: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Unsupported task file: {path}")
        relative = path.relative_to(directory)
        if relative.parts[0] != "fixture" and relative.parent.name == "__pycache__" and path.suffix == ".pyc":
            continue
        with path.open("rb") as stream:
            files[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    if {"task.json", "check.py"} - files.keys() or not any(path.startswith("fixture/") for path in files):
        raise ValueError(f"Missing task.json, check.py or fixture files for {entry['id']}")
    validate_task(entry["id"], read_json(directory / "task.json"))
    record = {**entry, "files": files}
    return {**record, "sha256": digest(record)}


def build_plan(manifest_path=FULL_MANIFEST, track_root=TRACK_A):
    entries = load_entries(manifest_path)
    tasks = [fingerprint_task(track_root, entry) for entry in entries]
    identity = {"version": 1, "repeats": REPEATS, "tasks": tasks}
    attempts = [
        {"task_id": entry["id"], "repeat": repeat}
        for repeat in range(1, REPEATS + 1)
        for entry in entries
    ]
    return {**identity, "suite_sha256": digest(identity), "attempts": attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        result = build_plan()
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Invalid full BAF suite: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
