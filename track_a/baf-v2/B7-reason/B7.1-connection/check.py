import csv
import json
import sys
from datetime import datetime
from pathlib import Path


def check(workdir):
    reference = Path(__file__).parent / "fixture" / "schedule.csv"
    with reference.open(encoding="utf-8", newline="") as stream:
        connection = next(
            row for row in csv.DictReader(stream)
            if (row["city"], row["arriving_service"], row["departing_service"])
            == ("Y", "PN311", "PN720")
        )
    arrival = datetime.strptime(connection["arrival"], "%H:%M")
    departure = datetime.strptime(connection["departure"], "%H:%M")
    minutes = int((departure - arrival).total_seconds()) // 60
    expected = "yes" if minutes >= int(connection["minimum_connection_minutes"]) else "no"
    try:
        answer = (Path(workdir) / "answer.txt").read_text(encoding="utf-8").strip().splitlines()
        ok = len(answer) == 2 and (
            answer[0].strip(), answer[1].strip()
        ) == (expected, str(minutes))
    except (OSError, UnicodeError):
        ok = False
    return {"pass": ok, "reason": "ok" if ok else "Неверное решение о пересадке или время"}


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1]), ensure_ascii=False))
