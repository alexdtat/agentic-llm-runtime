import json
import sys
from pathlib import Path


def main():
    p=Path(sys.argv[1])/'answer.txt'
    if not p.is_file():result={'pass':False,'reason':'answer.txt не создан'}
    else:
        value=p.read_text().strip()
        if value.startswith('/work/'):value=value[len('/work/'):]
        if value.startswith('./'):value=value[2:]
        ok=value=='util/date_tools.py'
        result={'pass':ok,'reason':'ok' if ok else 'Указан не файл определения функции'}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
