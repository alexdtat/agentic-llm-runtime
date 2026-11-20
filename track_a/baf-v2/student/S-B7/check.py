import json
import sys
from pathlib import Path

EXPECTED = 'H-34'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual in (EXPECTED, EXPECTED + '\n')
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Нужно учесть длину, момент завершения проверки и границу запрета.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
