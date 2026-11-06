import json
import sys
from pathlib import Path


def main():
    initial=(Path(__file__).parent/'fixture/README.md').read_bytes()
    expected=initial.replace(b'pip install example-package==1.2',b'pip install example-package==1.3')
    try:actual=(Path(sys.argv[1])/'README.md').read_bytes()
    except OSError:actual=None
    ok=actual==expected
    print(json.dumps({'pass':ok,'reason':'ok' if ok else 'Ожидалась только правка команды Install; остальные байты должны сохраниться'},ensure_ascii=False))


if __name__=='__main__':main()
