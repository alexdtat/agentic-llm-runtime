import json
import re
import sys
from pathlib import Path


EXPECTED = {
    "Закрыто заявок": 5,
    "Трудозатраты, ч": 35,
    "Север, закрыто": 2,
    "Юг, закрыто": 3,
}


def table_cells(line):
    return [cell.strip() for cell in line.strip().removeprefix("|").removesuffix("|").split("|")]


def check(workdir):
    lines = (workdir / "report.md").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines[1:], 1):
        separator = table_cells(line)
        if len(table_cells(lines[index - 1])) != 2 or len(separator) != 2:
            continue
        if not all(re.fullmatch(r":?-+:?", cell) for cell in separator):
            continue

        actual = {}
        for row in lines[index + 1:]:
            cells = table_cells(row)
            if len(cells) != 2:
                break
            label, value = cells
            if label not in EXPECTED:
                continue
            if label in actual or re.fullmatch(r"[0-9]+", value) is None:
                return {"pass": False, "reason": "Некорректная или повторная строка показателя"}
            actual[label] = int(value)
        if actual == EXPECTED:
            return {"pass": True, "reason": "ok"}

    return {"pass": False, "reason": "Ключевые показатели отчёта не совпали"}


def main():
    try:
        result = check(Path(sys.argv[1]))
    except (OSError, ValueError):
        result = {"pass": False, "reason": "Не удалось прочитать ответ в ожидаемом формате"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
