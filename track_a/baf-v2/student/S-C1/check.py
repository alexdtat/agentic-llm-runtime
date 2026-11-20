import json
import sys
from pathlib import Path

EXPECTED = {'route': 'Q-35', 'sources': ['logs/early.csv', 'logs/late.csv'], 'parcels': 3, 'boxes': 12, 'status': 'ready'}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def check(workdir):
    try:
        actual = json.loads(
            (workdir / 'dispatch.json').read_text(encoding="utf-8"),
            object_pairs_hook=unique_keys,
        )
        passed = json.dumps(actual, sort_keys=True) == json.dumps(EXPECTED, sort_keys=True)
    except (OSError, ValueError, RecursionError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Не совпали маршрут, источники, сводные показатели или статус на пороге.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
