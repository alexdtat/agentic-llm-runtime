import csv
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


def check(workdir):
    start = date(2026, 4, 1)
    end = date(2026, 6, 30)
    total = Decimal("0.00")
    for path in (Path(__file__).parent / "fixture" / "sales").glob("*.csv"):
        with path.open(encoding="utf-8", newline="") as stream:
            total += sum(
                Decimal(row["amount"]) for row in csv.DictReader(stream)
                if start <= date.fromisoformat(row["date"]) <= end
            )
    try:
        answer = (Path(workdir) / "answer.txt").read_text(encoding="utf-8").strip()
        ok = answer == f"{total:.2f}"
    except (OSError, UnicodeError):
        ok = False
    return {"pass": ok, "reason": "ok" if ok else "Неверная сумма продаж за квартал"}


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1]), ensure_ascii=False))
