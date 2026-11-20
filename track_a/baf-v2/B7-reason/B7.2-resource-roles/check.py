import json
import sys
from pathlib import Path


def check(workdir):
    roles = (workdir / "answer.txt").read_text(encoding="utf-8").strip().splitlines()
    roles = [role.strip() for role in roles]
    expected = {"reader", "coordinator", "assistant", "auditor"}
    passed = set(roles) == expected and len(roles) == len(expected)
    return {"pass": passed, "reason": "ok" if passed else "Множество ролей не совпало"}

def main():
    try:
        result = check(Path(sys.argv[1]))
    except (OSError, ValueError):
        result = {"pass": False, "reason": "Не удалось прочитать ответ в ожидаемом формате"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
