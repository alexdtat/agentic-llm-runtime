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


def check_with_evidence(root, runs):
    preserved(root)
    answer = read(root, 'answer.txt').decode().strip()
    if not answer.lower().startswith('no') or 'required manifest missing' not in answer:
        return result(False, 'Expected no and the stderr reason')
    if runs is None:
        return {'pass': False, 'outcome': 'infra_error', 'reason': 'trusted tool evidence required'}
    for run in runs:
        value = run.get('result', {})
        if matches(run, ['sh', 'build.sh']) and value.get('exit_code') == 1 and value.get('error_kind') == 'command_failure' and value.get('truncated') is False:
            if value.get('stdout', '').strip() == 'OK' and value.get('stderr', '').strip() == 'required manifest missing':
                return result(True)
    return result(False, 'Required build command, exit status and stderr evidence are missing')


def tool_runs():
    if len(sys.argv) < 3:
        return None
    evidence = Path(sys.argv[2]).resolve()
    work = Path(sys.argv[1]).resolve()
    if evidence == work or work in evidence.parents:
        raise ValueError('Evidence must be supplied outside agent workspace')
    if evidence.stat().st_size > 262144:
        raise ValueError('Evidence exceeds size limit')
    value = json.loads(evidence.read_text())
    return value['tool_runs']


def matches(run, argv):
    import shlex
    request = run.get('request', {})
    actual = request.get('argv')
    if actual is None:
        call = request.get('call', {})
        if call.get('tool') != 'RunCommand':
            return False
        actual = shlex.split(call['args']['command'])
    if not actual or Path(actual[0]).name not in {'python', 'python3', 'sh', 'bash'}:
        return False
    return [Path(actual[0]).name, *actual[1:]] == argv


def check(root):
    return check_with_evidence(root, tool_runs())


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
