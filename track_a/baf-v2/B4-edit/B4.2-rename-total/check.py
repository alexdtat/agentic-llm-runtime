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


def static_check(root):
    import ast
    preserved(root)
    for name in META['mutable_fixture_files']:
        expected = (TASK / 'fixture' / name).read_text().replace('calc_total', 'compute_total')
        actual = read(root, name).decode()
        try:
            actual_tree = ast.dump(ast.parse(actual))
        except SyntaxError:
            return result(False, 'Renamed source has invalid Python syntax')
        if 'calc_total' in actual or actual_tree != ast.dump(ast.parse(expected)):
            return result(False, 'Rename must preserve the program and update all code references')
    if any(path.is_symlink() for path in root.rglob('*')):
        return result(False, 'Links are forbidden')
    expected_files = {p.relative_to(TASK / 'fixture').as_posix() for p in (TASK / 'fixture').rglob('*') if p.is_file()}
    actual_files = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    return result(actual_files == expected_files, 'Unexpected project files')


def check(root):
    static = static_check(root)
    if not static['pass']:
        return static
    return {'pass': False, 'outcome': 'infra_error', 'reason': 'isolated_python executor required'}


def check_isolated(workdir, run_python):
    static = static_check(Path(workdir))
    if not static['pass']:
        return static
    program = (TASK / 'hidden_tests.py').read_text()
    try:
        output = run_python(program)
    except (RuntimeError, OSError) as error:
        return {'pass': False, 'outcome': 'infra_error', 'reason': str(error)}
    try:
        actual = json.loads(output)
    except (ValueError, TypeError):
        return result(False, 'Isolated solution returned invalid JSON')
    expected = ['0.00', '0.30', '4.50', '2.00', '12.00']
    return result(type(actual) is list and actual == expected, 'Isolated behavior tests failed')


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
