import json
import sys
from pathlib import Path


def check(workdir):
    lines = (workdir / "answer.txt").read_text(encoding="utf-8").strip().splitlines()
    expected = ["Текущая: 2026-03-01 — 5 рабочих дней", "Предыдущая: 2025-10-01 — 8 рабочих дней"]
    passed = [" ".join(line.split()) for line in lines] == expected
    return {
        "pass": passed,
        "reason": "ok" if passed else "Ожидались действующий срок и отличие от предыдущей версии",
    }

def main():
    try:
        result = check(Path(sys.argv[1]))
    except (OSError, ValueError):
        result = {"pass": False, "reason": "Не удалось прочитать ответ в ожидаемом формате"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
