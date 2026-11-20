import json
import sys
from pathlib import Path

EXPECTED = '[—]\n[NA]\n[N/A]\n[north]\n[NA-2]\n[\\NA]\n'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual == EXPECTED
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Нужно различить NA, экранированные литералы и похожие токены.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
