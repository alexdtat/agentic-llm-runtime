import json
import sys
from pathlib import Path

EXPECTED = 'services/ledger/routes.conf'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual in (EXPECTED, EXPECTED + '\n')
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Нужна действующая директива с точными именами маршрута и worker.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
