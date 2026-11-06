import csv
import json
import statistics
import sys
from decimal import Decimal,InvalidOperation
from pathlib import Path


def main():
    with (Path(__file__).parent/'fixture/data.csv').open(newline='') as stream:
        expected=statistics.median([Decimal(row['value']) for row in csv.DictReader(stream)])
    try:
        actual=Decimal((Path(sys.argv[1])/'answer.txt').read_text().strip())
        ok=actual.is_finite() and actual==expected
        result={'pass':ok,'reason':'ok' if ok else 'Медиана неверна'}
    except (OSError,InvalidOperation) as exc:result={'pass':False,'reason':str(exc)}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
