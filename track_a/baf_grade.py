"""Grade one completed v2 attempt from a trusted trace and a collected snapshot."""
import argparse
import hashlib
import importlib.util
import json
import os
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from track_a.contracts import tool_call
from track_a.executor import sandbox_args
from track_a.http_api import key

SOURCE = Path(__file__).resolve().parent
MANIFEST = SOURCE / 'baf-v2/full.json'
DOCKER_SOCKET = 'unix:///var/run/docker.sock'
IMAGE = 'agentic-sandbox:cpu-v1'
MAX_FILE, MAX_TOTAL, MAX_ENTRIES = 1 << 20, 4 << 20, 2048
MAX_TRACE, MAX_OUTPUT = 2 << 20, 32 << 10
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class SubmissionError(Exception):
    """The recorded attempt or the isolated submission failed a grading requirement."""


class CommandLimit(RuntimeError):
    pass


def verdict(outcome, reason, **details):
    return {'outcome': outcome, 'pass': outcome == 'pass', 'reason': reason, **details}


def source_task(task_id):
    manifest = json.loads(MANIFEST.read_text())
    if manifest['version'] != 1:
        raise ValueError('Unsupported full manifest')
    paths = [entry['path'] for entry in manifest['tasks'] if entry['id'] == task_id]
    if len(paths) != 1:
        raise ValueError('Unknown or ambiguous task ID')
    task_dir = (SOURCE / paths[0]).resolve(strict=True)
    if not task_dir.is_relative_to(SOURCE):
        raise ValueError('Task must belong to the trusted source tree')
    checker = (task_dir / 'check.py').resolve(strict=True)
    if checker != task_dir / 'check.py' or not checker.is_file():
        raise ValueError('Task checker must belong to the trusted task directory')
    task = json.loads((task_dir / 'task.json').read_text())
    if task['id'] != task_id:
        raise ValueError('Task metadata does not match manifest')
    return task, task_dir / 'fixture', checker


def same_node(left, right):
    return (left.st_dev, left.st_ino, left.st_ctime_ns, left.st_mtime_ns, left.st_size) == (
        right.st_dev, right.st_ino, right.st_ctime_ns, right.st_mtime_ns, right.st_size)


def copy_snapshot(source, target):
    """Copy a plain, bounded tree using dirfds and return hashes relative to its root."""
    hashes, total, count = {}, 0, 0

    def visit(fd, destination, relative, depth):
        nonlocal total, count
        if depth > 16:
            raise SubmissionError('Snapshot directory depth exceeds 16')
        parent = os.fstat(fd)
        destination.mkdir()
        destination.chmod(0o755)
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                path = relative / entry.name
                if count > MAX_ENTRIES or len(os.fsencode(path)) > 256:
                    raise SubmissionError('Snapshot tree exceeds entry or path limits')
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device:
                    raise SubmissionError('Snapshot crosses a filesystem boundary')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, DIRECTORY, dir_fd=fd)
                    try:
                        if not same_node(info, os.fstat(child)):
                            raise SubmissionError('Snapshot directory changed during copy')
                        visit(child, destination / entry.name, path, depth + 1)
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SubmissionError('Snapshot contains links or special files')
                if info.st_size > MAX_FILE or total + info.st_size > MAX_TOTAL:
                    raise SubmissionError('Snapshot exceeds file or total byte limit')
                file = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file, 'rb') as stream:
                    opened = os.fstat(file)
                    if not same_node(info, opened) or opened.st_nlink != 1:
                        raise SubmissionError('Snapshot file changed during copy')
                    data = stream.read(info.st_size + 1)
                    if len(data) != info.st_size or not same_node(info, os.fstat(file)):
                        raise SubmissionError('Snapshot file changed during copy')
                out = destination / entry.name
                out.write_bytes(data)
                out.chmod(0o644)
                total += len(data)
                hashes[path.as_posix()] = hashlib.sha256(data).hexdigest()
        if not same_node(parent, os.fstat(fd)):
            raise SubmissionError('Snapshot directory changed during copy')

    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise SubmissionError('Snapshot must be a plain directory')
    root = os.open(source, DIRECTORY)
    try:
        if not same_node(info, os.fstat(root)):
            raise SubmissionError('Snapshot root changed during copy')
        device = info.st_dev
        visit(root, target, Path(), 0)
    finally:
        os.close(root)
    return hashes


def command(argv, *, seconds, cwd=None):
    """Collect bounded stdout and stderr, including for an untrusted container."""
    with subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
        deadline = time.monotonic() + seconds
        try:
            with selectors.DefaultSelector() as poll:
                for pipe in buffers:
                    poll.register(pipe, selectors.EVENT_READ)
                while poll.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CommandLimit('Execution timed out')
                    for ready, _ in poll.select(remaining):
                        pipe = ready.fileobj
                        data = os.read(pipe.fileno(), 4096)
                        if not data:
                            poll.unregister(pipe)
                        elif len(buffers[pipe]) + len(data) > MAX_OUTPUT:
                            raise CommandLimit('Output exceeds the grading limit')
                        else:
                            buffers[pipe].extend(data)
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            return subprocess.CompletedProcess(argv, code, bytes(buffers[process.stdout]),
                                               bytes(buffers[process.stderr]))
        except subprocess.TimeoutExpired as exc:
            raise CommandLimit('Execution timed out') from exc
        finally:
            if process.poll() is None:
                process.kill()


class IsolatedPython:
    def __init__(self, workspace, scope='baf-grade'):
        self.workspace = workspace
        key(scope)
        self.scope = scope
        self.cases = 0

    @staticmethod
    def docker(*args, seconds=5):
        if os.environ.get('DOCKER_CONTEXT') or os.environ.get('DOCKER_HOST', DOCKER_SOCKET) != DOCKER_SOCKET:
            raise RuntimeError('Grading requires the local Docker socket')
        return command(['docker', '--host', DOCKER_SOCKET, *args], seconds=seconds)

    def state(self, name):
        result = self.docker('inspect', '--format', '{{json .State}}', name)
        if result.returncode:
            raise RuntimeError('Cannot inspect isolated container')
        return json.loads(result.stdout)

    def __call__(self, program):
        if not isinstance(program, str) or not 1 <= len(program.encode()) <= (64 << 10) or self.cases >= 16:
            raise RuntimeError('Invalid or excessive isolated checker cases')
        self.cases += 1
        program = workspace_program(program, '/work')
        name = 'agentic-baf-grade-' + uuid.uuid4().hex
        bind = f'type=bind,src={self.workspace},dst=/work,readonly,bind-recursive=disabled'
        try:
            created = self.docker('create', *sandbox_args(name, self.scope), '--mount', bind,
                                  '--workdir', '/work', '--log-driver', 'none', '--entrypoint', 'python3',
                                  IMAGE, '-I', '-B', '-c', program)
            if created.returncode:
                raise RuntimeError('Cannot create isolated container from cached image')
            try:
                result = self.docker('start', '-a', name, seconds=8)
            except CommandLimit as error:
                state = self.state(name)
                if state['Status'] in ('running', 'exited') and not state['Error']:
                    raise SubmissionError(str(error)) from error
                raise
            state = self.state(name)
            if state['Status'] != 'exited' or state['Error']:
                raise RuntimeError('Execution did not confirm a clean container exit')
            if state['ExitCode'] or state['OOMKilled']:
                raise SubmissionError('Isolated submission failed or exceeded memory')
            if result.returncode:
                raise RuntimeError('Could not collect isolated output')
            try:
                return result.stdout.decode()
            except UnicodeError as exc:
                raise SubmissionError('Isolated submission returned invalid UTF-8') from exc
        finally:
            # A failed create can still have crossed the Docker boundary.
            try:
                self.docker('rm', '-f', name)
            finally:
                remaining = self.docker('ps', '-aq', '--filter', 'name=^/' + name + '$')
                if remaining.returncode or remaining.stdout.strip():
                    raise RuntimeError('Isolated container cleanup was not confirmed')


def paired_tools(events):
    if not isinstance(events, list) or not 1 <= len(events) <= 512:
        raise ValueError('Expected one bounded v2 trace')
    session, run = events[0]['session_id'], events[0]['run_id']
    key(session)
    key(run)
    sequence = events[0]['sequence']
    if type(sequence) is not int or sequence < 1:
        raise ValueError('Invalid trace sequence')
    pending, results = {}, {}
    for event in events:
        if (event['schema_version'], event['session_id'], event['run_id'], event['sequence'],
                event['event_id']) != (2, session, run, sequence, f'{session}:{sequence}'):
            raise ValueError('Trace events must be contiguous and belong to one run')
        sequence += 1
        kind, payload = event['type'], event['payload']
        if kind in ('tool.started', 'tool.result'):
            op = payload['op_id']
            key(op)
            if kind == 'tool.started' and op not in pending and op not in results:
                pending[op] = tool_call(payload['call'])
            elif kind == 'tool.result' and op in pending:
                value = payload['result']
                if (type(value['exit_code']) is not int or type(value['truncated']) is not bool
                        or value['error_kind'] not in (None, 'command_failure')
                        or not isinstance(value['stdout'], str) or not isinstance(value['stderr'], str)):
                    raise ValueError('Invalid recorded tool result')
                results[op] = (pending.pop(op), value)
            else:
                raise ValueError('Tool events must pair exactly once by operation ID')
        if kind == 'run.result' and event is not events[-1]:
            raise ValueError('Trace contains a premature or duplicate terminal result')
    end = events[-1]['payload']
    if (events[0]['type'] != 'run.status' or events[0]['payload']['status'] != 'queued'
            or events[-1]['type'] != 'run.result' or end['cleanup_state'] != 'confirmed'):
        raise ValueError('Expected an entire run trace with confirmed cleanup')
    if end['status'] != 'completed':
        if end.get('reason') in {'step_limit', 'context_limit', 'duplicate_tool_call_id'}:
            raise SubmissionError('Attempt ended at its model or harness limit')
        raise ValueError('Attempt did not complete successfully')
    if pending:
        raise ValueError('Trace contains unconfirmed tool operations')
    return results


def command_tools(paired):
    for op, (call, result) in paired.items():
        if call['tool'] != 'RunCommand':
            continue
        submitted = call['args']['command']
        if '\n' in submitted or '\r' in submitted:
            continue
        try:
            argv = shlex.split(submitted)
        except ValueError:
            continue
        if argv:
            yield dict(op_id=op, request={'argv': argv}, result=result)


def module_from(path):
    spec = importlib.util.spec_from_file_location('trusted_baf_checker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workspace_program(program, path):
    return f'import sys\nsys.path.append({path!r})\n' + program


def checker_verdict(result):
    if not isinstance(result, dict) or type(result.get('pass')) is not bool:
        raise ValueError('Trusted checker returned an invalid verdict')
    outcome = result.get('outcome', 'pass' if result['pass'] else 'fail')
    if outcome not in ('pass', 'fail', 'infra_error') or result['pass'] != (outcome == 'pass'):
        raise ValueError('Trusted checker returned an inconsistent verdict')
    return verdict(outcome, str(result.get('reason', outcome)))


def run_checker(task, checker, snapshot, events, paired, *, isolated_python=IsolatedPython):
    kind = task.get('checker', 'file')
    tools = list(command_tools(paired))
    if kind == 'isolated_python':
        return checker_verdict(module_from(checker).check_isolated(snapshot, isolated_python(snapshot)))
    if kind == 'tool_evidence':
        required = task['required_tool']
        tools = [tool for tool in tools if tool['request']['argv'] == required['argv']]
        try:
            return checker_verdict(module_from(checker).check_with_evidence(snapshot, tools))
        except (OSError, ValueError, UnicodeError) as exc:
            raise SubmissionError('Checker could not read submitted answer') from exc
    if kind != 'file':
        raise ValueError('Unknown trusted checker mode')
    result = command([sys.executable, '-I', '-B', str(checker), str(snapshot)], cwd=checker.parent, seconds=8)
    if result.returncode:
        raise RuntimeError('Trusted file checker failed')
    value = checker_verdict(json.loads(result.stdout))
    if value['outcome'] != 'pass':
        return value
    if task.get('required_tool'):
        safe_ids = {tool['op_id'] for tool in tools}
        trace = [event for event in events if event['type'] in ('tool.started', 'tool.result')
                 and event['payload']['op_id'] in safe_ids]
        return checker_verdict(module_from(checker).check_trace(trace))
    if task['id'] == 'B5.1':
        if not any(tool['request']['argv'] == ['python3', 'stats.py', 'data.csv']
                   and tool['result']['exit_code'] == 0 and tool['result']['error_kind'] is None
                   and not tool['result']['truncated'] for tool in tools):
            raise SubmissionError('Trace lacks the required stats command')
    return value


def grade_snapshot(task, fixture, checker, snapshot, events, paired, *, isolated_python=None):
    with tempfile.TemporaryDirectory(prefix='baf-grade.', dir='/tmp') as directory:
        scratch = Path(directory)
        try:
            original = copy_snapshot(fixture, scratch / 'fixture')
        except SubmissionError as exc:
            raise RuntimeError('Invalid trusted fixture') from exc
        workspace = scratch / 'work'
        submitted = copy_snapshot(Path(snapshot), workspace)
        mutable = set(task.get('mutable_fixture_files', []))
        if not mutable <= original.keys():
            raise ValueError('Invalid mutable fixture declaration')
        changed = sorted(name for name in original.keys() - mutable if submitted.get(name) != original[name])
        if changed:
            return verdict('fail', 'Immutable fixtures changed', changed=changed)
        return run_checker(task, checker, workspace, events, paired,
                           isolated_python=isolated_python or IsolatedPython)


def grade(task_id, snapshot, events, *, isolated_python=None):
    try:
        task, fixture, checker = source_task(task_id)
        return grade_snapshot(task, fixture, checker, snapshot, events, paired_tools(events),
                              isolated_python=isolated_python)
    except SubmissionError as error:
        return verdict('fail', str(error))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError, ImportError) as error:
        return verdict('infra_error', str(error))


def read_trace(events, snapshot):
    if events.resolve(strict=True).is_relative_to(snapshot.resolve(strict=True)):
        raise ValueError('Runtime trace must be stored outside the agent snapshot')
    info = events.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_TRACE:
        raise ValueError('Runtime trace must be a bounded regular file')
    handle = os.open(events, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(handle, 'rb') as stream:
        if not same_node(info, os.fstat(handle)):
            raise ValueError('Runtime trace changed before reading')
        raw = stream.read(MAX_TRACE + 1)
        if len(raw) != info.st_size or not same_node(info, os.fstat(handle)):
            raise ValueError('Runtime trace changed during reading')
    return json.loads(raw)


def self_test():
    task, fixture, checker = source_task('B6.1')
    with tempfile.TemporaryDirectory(prefix='baf-grader-probe.', dir='/tmp') as directory:
        snapshot = Path(directory) / 'work'
        copy_snapshot(fixture, snapshot)
        (snapshot / 'slug.py').write_text(
            "import re\n\ndef slugify(text):\n"
            "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
        )
        try:
            result = grade_snapshot(task, fixture, checker, snapshot, None, {})
        except SubmissionError as error:
            result = verdict('fail', str(error))
        return {**result, 'mode': 'grader_self_test'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task_id', nargs='?')
    parser.add_argument('snapshot', nargs='?', type=Path)
    parser.add_argument('events', nargs='?', type=Path)
    parser.add_argument('--self-test', action='store_true', help='Run the trusted B6.1 probe without a model')
    args = parser.parse_args()
    if args.self_test and any((args.task_id, args.snapshot, args.events)):
        parser.error('--self-test takes no attempt inputs')
    if not args.self_test and not all((args.task_id, args.snapshot, args.events)):
        parser.error('A task ID, snapshot and external v2 trace are required')
    try:
        result = self_test() if args.self_test else grade(
            args.task_id, args.snapshot, read_trace(args.events, args.snapshot))
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        result = verdict('infra_error', str(error))
    print(json.dumps(result, ensure_ascii=False))
    return {'pass': 0, 'fail': 1, 'infra_error': 2}[result['outcome']]


if __name__ == '__main__':
    raise SystemExit(main())
