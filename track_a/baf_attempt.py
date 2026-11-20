"""Collect evidence for one operator-provisioned mounted v2 BAF attempt."""
import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from track_a.baf_plan import TRACK_A, build_plan, read_json, unique_object
from track_a.client import Client
from track_a.contracts import model_response, tool_call
from track_a.http_api import key

MAX_ENTRIES = 1024
MAX_FILE = 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 12
MAX_RESPONSE = 512 * 1024
MAX_EVENTS = 128
HARNESS_FAILURES = {'context_limit', 'step_limit', 'duplicate_tool_call_id'}
EVENT_TYPES = {'run.status', 'run.result', 'model.started', 'model.completed', 'tool.started', 'tool.result'}


class AttemptError(RuntimeError):
    pass


def endpoint(value):
    url = urlsplit(value)
    if any(ch.isspace() or ch == '\\' for ch in value) or (
            url.scheme not in ('http', 'https') or not url.hostname or url.username is not None) or (
            url.path not in ('', '/') or url.query or url.fragment or url.port == 0):
        raise ValueError('Use a base HTTPS URL or an HTTP loopback URL')
    if url.scheme == 'http' and not ipaddress.ip_address(url.hostname).is_loopback:
        raise ValueError('HTTP requires a loopback address')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class MountedClient(Client):
    """Use the v2 client operations with bounded reads and same-origin requests."""

    def __init__(self, url, token, grant):
        super().__init__(endpoint(url), token)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.secrets = (token, grant)

    def exchange(self, method, path, payload, timeout, accept):
        request = Request(self.base + path, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Content-Type': 'application/json', 'Accept': accept})
        try:
            response = self.opener.open(request, timeout=timeout)
        except HTTPError as exc:
            with exc:
                return exc.code, b''
        with response:
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise AttemptError('response_limit')
            return response.status, data

    def decode(self, data):
        value = json.loads(data, object_pairs_hook=unique_object)
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and any(item == secret or len(secret) >= 12 and secret in item
                                               for secret in self.secrets):
                raise AttemptError('credential_in_response')
        return value

    def request(self, method, path, payload=None, timeout=20):
        status, data = self.exchange(method, path, payload, timeout, 'application/json')
        return status, self.decode(data) if status == 200 else {}

    def events(self, session_id, after, timeout=5):
        status, data = self.exchange('GET', f'/v2/sessions/{session_id}/events?after={after}',
                                     None, timeout, 'text/event-stream')
        if status != 200:
            return status, []
        if data and not data.endswith(b'\n\n'):
            raise AttemptError('incomplete_event_frame')
        events = []
        for frame in data.split(b'\n\n')[:-1]:
            lines = frame.split(b'\n')
            if len(lines) != 2 or not lines[0].startswith(b'id: ') or not lines[1].startswith(b'data: '):
                raise AttemptError('invalid_event_frame')
            event = self.decode(lines[1][6:])
            if int(lines[0][4:]) != event['sequence']:
                raise AttemptError('event_cursor_mismatch')
            events.append(event)
        return status, events


@contextmanager
def directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def identity(info):
    return info.st_dev, info.st_ino


def inventory(fd):
    """Capture bounded bytes using no-follow opens rooted at the supplied descriptor."""
    root_dev = os.fstat(fd).st_dev
    dirs, files = [], {}
    total = 0

    def walk(parent, prefix, depth):
        nonlocal total
        if depth > MAX_DEPTH:
            raise AttemptError('snapshot_depth_limit')
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name
                relative = prefix + name
                if len(dirs) + len(files) >= MAX_ENTRIES or len(relative) > 256:
                    raise AttemptError('snapshot_entry_limit')
                if name == '.git' or '\\' in name:
                    raise AttemptError('unsupported_snapshot_entry')
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != root_dev:
                    raise AttemptError('snapshot_filesystem_crossing')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        if identity(os.fstat(child)) != identity(info):
                            raise AttemptError('snapshot_entry_changed')
                        dirs.append(relative)
                        walk(child, relative + '/', depth + 1)
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise AttemptError('unsupported_snapshot_entry')
                limit = min(MAX_FILE, MAX_BYTES - total)
                if info.st_size > limit:
                    raise AttemptError('snapshot_byte_limit')
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(child, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if identity(before) != identity(info) or before.st_nlink != 1 or not stat.S_ISREG(before.st_mode):
                        raise AttemptError('snapshot_entry_changed')
                    data = stream.read(limit + 1)
                    after = os.fstat(stream.fileno())
                if len(data) > limit:
                    raise AttemptError('snapshot_byte_limit')
                if len(data) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
                        after.st_mtime_ns, after.st_ctime_ns, after.st_size) or after.st_nlink != 1:
                    raise AttemptError('snapshot_entry_changed')
                total += len(data)
                files[relative] = data

    walk(fd, '', 0)
    return sorted(dirs), dict(sorted(files.items()))


def fingerprints(files):
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def sync_dir(path):
    with directory(path) as fd:
        os.fsync(fd)


def write_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def persist(path, value):
    data = (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}')
    write_bytes(temporary, data)
    temporary.replace(path)
    sync_dir(path.parent)
    return hashlib.sha256(data).hexdigest()


def store_snapshot(folder, dirs, files):
    staging = folder / '.snapshot-incomplete'
    staging.mkdir(mode=0o700)
    for name in dirs:
        (staging / name).mkdir(mode=0o700)
    for name, data in files.items():
        write_bytes(staging / name, data)
    for name in reversed(dirs):
        sync_dir(staging / name)
    sync_dir(staging)
    staging.rename(folder / 'snapshot')
    sync_dir(folder)


def load_frozen_task(plan_path, expected_digest, task_id, repeat):
    plan = read_json(plan_path)
    current = build_plan()
    if plan != current or current['suite_sha256'] != expected_digest:
        raise AttemptError('frozen_plan_mismatch')
    if {'task_id': task_id, 'repeat': repeat} not in plan['attempts']:
        raise AttemptError('unplanned_attempt')
    task = next(task for task in plan['tasks'] if task['id'] == task_id)
    task_dir = TRACK_A / task['path']
    task_data = (task_dir / 'task.json').read_bytes()
    if hashlib.sha256(task_data).hexdigest() != task['files']['task.json']:
        raise AttemptError('task_changed')
    message = '\n\n'.join(json.loads(task_data, object_pairs_hook=unique_object)['messages'])
    with directory(task_dir / 'fixture') as fd:
        fixture_dirs, fixture_files = inventory(fd)
    expected_files = {name[len('fixture/'):]: value for name, value in task['files'].items()
                      if name.startswith('fixture/')}
    if fingerprints(fixture_files) != expected_files:
        raise AttemptError('fixture_changed')
    return task, message, (fixture_dirs, fixture_files)


def collect_trace(client, sid, rid, events, path):
    while True:
        status, page = client.events(sid, len(events))
        if status != 200 or not isinstance(page, list) or len(page) > 32:
            raise AttemptError('event_page_unavailable')
        if len(events) + len(page) > MAX_EVENTS:
            raise AttemptError('event_limit')
        for sequence, event in enumerate(page, len(events) + 1):
            if (event['schema_version'], event['session_id'], event['run_id'], event['sequence'],
                event['event_id']) != (2, sid, rid, sequence, f'{sid}:{sequence}') or (
                    type(event['sequence']) is not int or event['type'] not in EVENT_TYPES or
                    not isinstance(event['payload'], dict)):
                raise AttemptError('event_sequence_or_identity')
        if not page:
            return
        events.extend(page)
        persist(path, events)


def validate_trace(events, run, cursor):
    starts, finishes = {}, {}
    planned_calls, started_calls = [], []
    model_started, model_completed = [], []
    last = events[-1] if events else {}
    if cursor != len(events) or last.get('type') != 'run.result' or last['payload'] != run['result'] or (
            sum(event['type'] == 'run.result' for event in events) != 1):
        raise AttemptError('incomplete_terminal_trace')
    for event in events:
        payload = event['payload']
        if event['type'] == 'tool.started':
            op = payload['op_id']
            key(op)
            if op in starts:
                raise AttemptError('duplicate_tool_operation')
            starts[op] = event['sequence']
            started_calls.append(tool_call(payload['call']))
        elif event['type'] == 'tool.result':
            op = payload['op_id']
            if op not in starts or op in finishes:
                raise AttemptError('unpaired_tool_operation')
            tool_result = payload['result']
            if not isinstance(tool_result, dict) or type(tool_result['exit_code']) is not int or (
                    type(tool_result['truncated']) is not bool):
                raise AttemptError('invalid_tool_result')
            finishes[op] = event['sequence']
        elif event['type'] == 'model.started':
            model_started.append(payload['step'])
        elif event['type'] == 'model.completed':
            model_completed.append((payload['step'], payload['usage']))
            action = model_response(payload['action'], payload['usage'])['action']
            planned_calls.extend(action.get('calls', []))
    completed = run['result']['status'] == 'completed'
    if completed and (starts.keys() != finishes.keys() or not model_started or
                      model_started != list(range(len(model_started))) or
                      model_started != [step for step, _ in model_completed] or
                      planned_calls != started_calls or action.get('answer') != run['result']['answer'] or
                      run['result']['usage'] != [usage for _, usage in model_completed]):
        raise AttemptError('incomplete_operation_trace')


def validate_run(run, sid, rid, aid):
    if (run['session_id'], run['id'], run['request_id'], run['config']['execution']) != (sid, rid, aid, 'mounted'):
        raise AttemptError('unexpected_run_identity')
    if run['result'] is not None and (
            run['status'] != run['result']['status'] or run['cleanup_state'] != run['result']['cleanup_state']):
        raise AttemptError('inconsistent_run_result')


def run_attempt(client, *, plan_path, suite_digest, task_id, repeat, grant, export, output,
                provider_label, runtime_revision):
    export = Path(export).absolute()
    if export.resolve() != export:
        raise AttemptError('export_path_must_not_follow_links')
    output = Path(output).resolve()
    if output.is_relative_to(export):
        raise AttemptError('evidence_inside_export')
    aid = uuid.uuid4().hex
    folder = output / aid
    folder.mkdir(mode=0o700, parents=True)
    sync_dir(output)
    sync_dir(output.parent)
    receipt = folder / 'attempt.json'
    evidence = {'version': 1, 'mode': 'mounted_v2', 'attempt_id': aid, 'task_id': task_id,
                'repeat': repeat, 'suite_sha256': suite_digest, 'phase': 'preflight',
                'outcome': 'infra_error', 'grade_ready': False, 'close': 'not_requested',
                'operator_assertions': {'provider_label': provider_label, 'runtime_revision': runtime_revision}}
    persist(receipt, evidence)
    sid = rid = None
    events = []
    terminal = confirmed_snapshot = False
    started = time.monotonic()
    event_path = folder / 'events.json'
    try:
        key(grant)
        task, message, fixture = load_frozen_task(plan_path, suite_digest, task_id, repeat)
        evidence['task_sha256'] = task['sha256']
        with directory(export) as fd:
            if inventory(fd) != fixture:
                raise AttemptError('initial_fixture_mismatch')
            evidence['initial_fixture'] = {'directories': fixture[0], 'files_sha256': fingerprints(fixture[1])}
            status, health = client.request('GET', '/v0/health', timeout=5)
            if status != 200 or health != {'status': 'ok', 'version': 'v2', 'execution': 'mounted', 'client_mount': True} or (
                    health['client_mount'] is not True):
                raise AttemptError('mounted_health_unconfirmed')
            evidence.update(phase='session_request', runtime_health=health)
            persist(receipt, evidence)
            session = client.v2('POST', '', {'request_id': aid, 'grant': grant}, timeout=15)
            key(session['id'])
            sid = session['id']
            evidence['session_id'] = sid
            if (session['status'], session['active_run'], session['cursor']) != ('open', None, 0):
                raise AttemptError('session_not_fresh')
            evidence['phase'] = 'run_request'
            persist(receipt, evidence)
            run = client.v2('POST', f'/{sid}/runs', {'request_id': aid, 'message': message}, timeout=15)
            key(run['id'])
            rid = run['id']
            evidence.update(phase='waiting', run_id=rid)
            persist(receipt, evidence)
            validate_run(run, sid, rid, aid)
            run = client.wait_run(sid, rid, timeout=75)
            validate_run(run, sid, rid, aid)
            terminal = run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}
            evidence.update(phase='trace', result=run['result'], runtime_config=run['config'])
            persist(receipt, evidence)
            collect_trace(client, sid, rid, events, event_path)
            session = client.v2('GET', f'/{sid}', timeout=5)
            validate_trace(events, run, session['cursor'])
            if (run['cleanup_state'], session['id'], session['status'], session['active_run']) != (
                    'confirmed', sid, 'open', None):
                raise AttemptError('stop_or_lease_unconfirmed')
            if run['status'] != 'completed':
                if run['result']['reason'] in HARNESS_FAILURES:
                    evidence['outcome'] = 'fail'
                raise AttemptError('run_not_completed')
            evidence['phase'] = 'snapshot'
            persist(receipt, evidence)
            if identity(os.stat(export, follow_symlinks=False)) != identity(os.fstat(fd)):
                raise AttemptError('export_path_changed')
            dirs, files = inventory(fd)
            store_snapshot(folder, dirs, files)
            evidence['snapshot'] = {'relative_path': 'snapshot', 'directories': dirs,
                                    'files_sha256': fingerprints(files)}
            confirmed_snapshot = True
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        evidence['reason'] = str(exc) if isinstance(exc, AttemptError) else type(exc).__name__
    finally:
        if sid is not None:
            if rid is not None and not terminal:
                try:
                    cancelled = client.v2('POST', f'/{sid}/runs/{rid}/cancel', {}, timeout=15)
                    validate_run(cancelled, sid, rid, aid)
                    evidence['cancel_cleanup'] = cancelled['cleanup_state']
                    collect_trace(client, sid, rid, events, event_path)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                    evidence.setdefault('cancel_cleanup', 'unconfirmed')
            try:
                closed = client.v2('POST', f'/{sid}/close', {}, timeout=15)
                if (closed['id'], closed['status']) != (sid, 'closed'):
                    raise AttemptError('close_unconfirmed')
                evidence['close'] = 'confirmed'
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                evidence.update(outcome='infra_error', close='unconfirmed', close_reason='close_unconfirmed')
        elif evidence['phase'] == 'session_request':
            evidence['close'] = 'session_id_unknown'
        if confirmed_snapshot and evidence['close'] == 'confirmed':
            evidence.update(phase='finished', outcome='collected', grade_ready=True)
        if event_path.is_file():
            evidence['events'] = {'relative_path': 'events.json', 'count': len(events),
                                  'sha256': hashlib.sha256(event_path.read_bytes()).hexdigest()}
        evidence['elapsed_seconds'] = round(time.monotonic() - started, 3)
        persist(receipt, evidence)
    return folder, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--suite-sha256', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--repeat', type=int, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--grant', default=os.environ.get('MOUNT_GRANT'), help='Export grant, defaults to MOUNT_GRANT')
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider-label', required=True)
    parser.add_argument('--runtime-revision', required=True)
    args = parser.parse_args(argv)
    token = os.environ.get('PROBE_TOKEN')
    if not token:
        parser.error('Set PROBE_TOKEN for the existing mounted server')
    if args.grant is None:
        parser.error('Set MOUNT_GRANT or pass --grant for the existing mounted export')
    if not re.fullmatch(r'[0-9a-f]{64}', args.suite_sha256):
        parser.error('Provide the expected SHA-256 of the frozen suite')
    try:
        key(args.grant)
        key(args.task.replace('.', '-'))
        for label in (args.provider_label, args.runtime_revision):
            if not re.fullmatch(r'[A-Za-z0-9_.:/@+-]{1,128}', label) or token in label or args.grant == label:
                raise ValueError('Use non-secret provider and revision labels')
        client = MountedClient(args.url, token, args.grant)
        folder, evidence = run_attempt(client, plan_path=args.plan, suite_digest=args.suite_sha256,
                                       task_id=args.task, repeat=args.repeat, grant=args.grant,
                                       export=args.export, output=args.output, provider_label=args.provider_label,
                                       runtime_revision=args.runtime_revision)
    except (OSError, ValueError, TypeError, AttemptError) as exc:
        parser.exit(2, f'Attempt could not be recorded ({type(exc).__name__}).\n')
    print(json.dumps({'outcome': evidence['outcome'], 'evidence': str(folder / 'attempt.json')}))
    return {'collected': 0, 'fail': 1, 'infra_error': 2}[evidence['outcome']]


if __name__ == '__main__':
    raise SystemExit(main())
