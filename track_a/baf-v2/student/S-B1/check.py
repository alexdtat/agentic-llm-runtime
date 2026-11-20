import json
import sys
from pathlib import Path

EXPECTED = '35'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual in (EXPECTED, EXPECTED + '\n')
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Нужно значение окна отката из подписанной строки Beacon.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
