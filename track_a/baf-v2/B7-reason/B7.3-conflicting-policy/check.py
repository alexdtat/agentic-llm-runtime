import json
import sys
from pathlib import Path


def check(workdir):
    lines = (workdir / "answer.txt").read_text(encoding="utf-8").strip().splitlines()
    passed = (
        len(lines) == 2
        and lines[0].strip().upper() == "ПРОТИВОРЕЧИЕ"
        and sorted(lines[1].split()) == ["Q-exception", "Q-release"]
    )
    return {
        "pass": passed,
        "reason": "ok" if passed else "Ожидались противоречие и идентификаторы обоих правил",
    }

def main():
    try:
        result = check(Path(sys.argv[1]))
    except (OSError, ValueError):
        result = {"pass": False, "reason": "Не удалось прочитать ответ в ожидаемом формате"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
