import json
import sys
from pathlib import Path
from collections import Counter


def unique_object(pairs):
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("duplicate JSON key")
    return record


def decision_keys(records):
    if not isinstance(records, list):
        raise ValueError("expected JSON array")
    keys = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"decision", "date", "owner"}:
            raise ValueError("unexpected record fields")
        key = (record["decision"], record["date"], record["owner"])
        if not all(isinstance(value, str) for value in key):
            raise ValueError("expected strings")
        keys.append(key)
    return Counter(keys)


def check(workdir):
    actual = json.loads(
        (workdir / "decisions.json").read_text(encoding="utf-8"),
        object_pairs_hook=unique_object,
    )
    expected = json.loads(Path(__file__).with_name("expected.json").read_text(encoding="utf-8"))
    passed = decision_keys(actual) == decision_keys(expected)
    return {"pass": passed, "reason": "ok" if passed else "Набор решений не совпал"}

def main():
    try:
        result = check(Path(sys.argv[1]))
    except (OSError, ValueError, RecursionError):
        result = {"pass": False, "reason": "Не удалось прочитать ответ в ожидаемом формате"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
