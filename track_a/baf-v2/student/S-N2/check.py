import json
import sys
from pathlib import Path

EXPECTED = 'K-08 :: «Станция ""Север""»\nK-03 :: «Сводка: берег»\nK-11 :: «Пункт ""А"", сектор ""Б""»\n'


def check(workdir):
    try:
        actual = (workdir / "answer.txt").read_bytes().decode("utf-8")
        passed = actual == EXPECTED
    except (OSError, UnicodeError):
        passed = False
    return {"pass": passed, "reason": "ok" if passed else 'Формат требует внешних ёлочек, удвоения внутренних ASCII-кавычек и сохранения порядка.'}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
