import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


def main():
    schema=json.loads(Path(__file__).with_name('schema.json').read_text())
    try:
        value=json.loads((Path(sys.argv[1])/'config.json').read_text())
        errors=list(Draft202012Validator(schema).iter_errors(value))
        result={'pass':not errors,'reason':'ok' if not errors else errors[0].message}
    except (OSError,ValueError) as exc:result={'pass':False,'reason':str(exc)}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
