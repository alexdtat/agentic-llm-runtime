import json
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent
META = json.loads((TASK / 'task.json').read_text())


def read(root, name):
    path = root / name
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Links are forbidden in checker inputs')
    if path.stat().st_size > 131072:
        raise ValueError('Checker input exceeds 131072 bytes')
    return path.read_bytes()


def preserved(root):
    mutable = set(META['mutable_fixture_files'])
    for original in (TASK / 'fixture').rglob('*'):
        if original.is_file():
            name = original.relative_to(TASK / 'fixture').as_posix()
            if name not in mutable and read(root, name) != original.read_bytes():
                raise ValueError('Changed immutable fixture: ' + name)


def result(passed, reason='Output differs from the task specification'):
    return {'pass': bool(passed), 'reason': 'ok' if passed else reason}


def check(root):
    preserved(root)
    actual = read(root, 'answer.txt').decode().strip().splitlines()
    expected = json.loads((TASK / 'expected.json').read_text())
    return result(len(actual) == len(set(actual)) and set(actual) == set(expected))


def main():
    try:
        value = check(Path(sys.argv[1]))
    except ImportError as error:
        value = {'pass': False, 'outcome': 'infra_error', 'reason': str(error)}
    except (OSError, ValueError, KeyError, TypeError, IndexError, SyntaxError) as error:
        value = {'pass': False, 'reason': str(error)}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
