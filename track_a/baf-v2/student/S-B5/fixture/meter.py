import csv
import sys
from collections import defaultdict


def main():
    load_by_minute = defaultdict(int)
    with open(sys.argv[1], encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            load_by_minute[row["minute"]] += int(row["load"])
    print(f"observed_minutes={len(load_by_minute)}")
    print(f"peak_load={max(load_by_minute.values())}")
    print(f"total_load={sum(load_by_minute.values())}")


if __name__ == "__main__":
    main()
