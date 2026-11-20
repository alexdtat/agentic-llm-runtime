import json
import sys
from pathlib import Path

EXPECTED = {'north': 8, 'west': 11, 'east': 4}


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
            (workdir / 'totals.json').read_text(encoding="utf-8"),
            object_pairs_hook=unique_keys,
        )
        passed = json.dumps(actual, sort_keys=True) == json.dumps(EXPECTED, sort_keys=True)
    except (OSError, ValueError, RecursionError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Итоги должны охватывать обе смены и учитывать повторную строку только один раз.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
