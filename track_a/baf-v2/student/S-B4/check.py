import json
import sys
from pathlib import Path


def check(workdir):
    initial = (Path(__file__).parent / "fixture" / "manifest.toml").read_bytes()
    expected = initial.replace(b"retry_delay  = 15", b"retry_delay  = 25", 1)
    try:
        passed = (workdir / "manifest.toml").read_bytes() == expected
    except OSError:
        passed = False
    return {"pass": passed, "reason": "ok" if passed else "Допустима только замена значения в секции [active]."}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1])), ensure_ascii=False))
