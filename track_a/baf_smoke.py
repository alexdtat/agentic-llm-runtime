"""Run one v2 Application attempt per course BAF through a synthetic offline model."""
import argparse
import ast
import base64
import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from track_a.application import Application
from track_a.baf_smoke_mock import SCRIPTS, STATS_COMMAND, ScriptedModel, SmokeExecutor
from track_a.client import Client
from track_a.contracts import fields, fixture_files
from track_a.http_api import Conflict

TASK_ROOT = Path(__file__).resolve().parent
CATALOG = TASK_ROOT / 'baf-v2' / 'smoke.json'
MODE = 'synthetic_mock_fixture'
FAILED_HARNESS_REASONS = {'context_limit', 'step_limit', 'duplicate_tool_call_id'}


class LocalClient(Client):
    """Exercise ordinary v2 routing and client polling without opening sockets."""

    def __init__(self, application, owner=None):
        self.app, self.owner = application, owner

    def request(self, method, path, payload=None, timeout=20):
        url = urlsplit(path)
        body = {} if payload is None else json.loads(json.dumps(payload))
        try:
            if method == 'GET' and url.path == '/v0/health':
                status, value = self.app.health()
            elif self.owner is None:
                status, value = self.app.route(method, url.path, body)
            else:
                status, value = self.app.route(self.owner, method, url.path, body,
                                               parse_qs(url.query, keep_blank_values=True))
        except KeyError:
            return 404, {'error': 'not_found'}
        except Conflict:
            return 409, {'error': 'conflict'}
        except (TypeError, ValueError):
            return 400, {'error': 'invalid_request'}
        return status, json.loads(json.dumps(value, allow_nan=False))

    def events(self, session_id, after, timeout=5):
        return self.request('GET', f'/v2/sessions/{session_id}/events?after={after}', timeout=timeout)


def read_catalog():
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    entries = catalog['tasks']
    for entry in entries:
        fields(entry, 'id', 'path')
        if not isinstance(entry['path'], str):
            raise TypeError('Invalid task path')
    if catalog['version'] != 1 or [entry['id'] for entry in entries] != list(SCRIPTS):
        raise ValueError('Expected one ordered simple task per BAF, B1.1 through B8.1')
    return entries


def load_task(entry):
    directory = (TASK_ROOT / entry['path']).resolve(strict=True)
    if not any(directory.is_relative_to(TASK_ROOT / root) for root in ('baf', 'baf-v2')):
        raise ValueError('Task is outside BAF inventory')
    task = json.loads((directory / 'task.json').read_text(encoding='utf-8'))
    if task['id'] != entry['id'] or task['level'] != 'simple' or len(task['messages']) != 1:
        raise ValueError('Invalid smoke task metadata')
    root = directory / 'fixture'
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Missing fixture directory')
    initial = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Fixture symlinks are unsupported')
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > 6144:
            raise ValueError('Unsupported fixture file')
        relative = path.relative_to(root).as_posix()
        initial[relative] = base64.b64encode(path.read_bytes()).decode()
    return task, fixture_files(initial)


def collect_events(client, sid):
    events = []
    after = 0
    while True:
        status, page = client.events(sid, after)
        if status != 200:
            raise RuntimeError('Event retrieval failed')
        for event in page:
            after += 1
            if event['sequence'] != after:
                raise RuntimeError('Event sequence gap')
            events.append(event)
        if len(page) < 32:
            return events


def artifact_ok(task_id, body):
    if not body.strip():
        return False
    try:
        if task_id == 'B3.1':
            return isinstance(json.loads(body), dict)
        if task_id == 'B6.1':
            return any(isinstance(node, ast.FunctionDef) and node.name == 'slugify'
                       for node in ast.parse(body).body)
    except (ValueError, SyntaxError):
        return False
    return True


def check_mechanics(task, initial, files, events, rid):
    target = task['answer_file']
    case = task['id']
    starts = {event['payload']['op_id']: event['payload']['call']
              for event in events if event['type'] == 'tool.started'}
    results = {event['payload']['op_id']: event['payload']['result']
               for event in events if event['type'] == 'tool.result'}
    read_paths = {call['args']['path'] for call in starts.values() if call['tool'] == 'ReadFile'}
    commands = {call['args']['command'] for call in starts.values() if call['tool'] == 'RunCommand'}
    write_paths = [call['args']['path'] for call in starts.values() if call['tool'] == 'WriteFile']
    required_reads = {
        'B1.1': {'config/app.yaml'}, 'B2.1': {'util/date_tools.py'}, 'B3.1': {'spec.md'},
        'B4.1': {'README.md'}, 'B5.1': set(), 'B6.1': {'spec.md'},
        'B7.1': set(initial), 'B8.1': {path for path in initial if path.endswith('.csv')},
    }
    required_commands = {'B2.1': {'fixture:list'}, 'B5.1': {STATS_COMMAND}, 'B8.1': {'fixture:list'}}
    body = base64.b64decode(files.get(target, ''), validate=True)
    return {
        'one_run_result': sum(event['type'] == 'run.result' for event in events) == 1
        and all(event['run_id'] == rid for event in events),
        'tool_results': bool(starts) and list(starts) == list(results)
        and all(result['exit_code'] == 0 and not result['truncated'] for result in results.values()),
        'requested_tools': required_reads[case] <= read_paths
        and required_commands.get(case, set()) == commands and write_paths == [target],
        'artifact_written': files.get(target) != initial.get(target) and artifact_ok(case, body),
        'remaining_fixture_preserved': {path: data for path, data in files.items() if path != target}
        == {path: data for path, data in initial.items() if path != target},
    }


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def run_case(entry, output, model_factory=ScriptedModel):
    aid = uuid.uuid4().hex
    folder = output / aid
    folder.mkdir()
    evidence = {'id': aid, 'task': entry['id'], 'mode': MODE, 'outcome': 'infra_error',
                'fixture_path': entry['path'] + '/fixture'}
    executor = SmokeExecutor()
    app = None
    sid = None
    try:
        task, initial = load_task(entry)
        app = Application(LocalClient(executor), LocalClient(model_factory(task['id'])),
                          {'smoke': 'local-smoke'})
        client = LocalClient(app, 'smoke')
        sid = client.v2('POST', '', {'request_id': aid, 'files': initial})['id']
        evidence['session_id'] = sid
        rid = client.v2('POST', f'/{sid}/runs', {'request_id': aid, 'message': task['messages'][0]})['id']
        evidence['run_id'] = rid
        run = client.wait_run(sid, rid, timeout=5)
        evidence['run'] = run['result']
        events = collect_events(client, sid)
        write_json(folder / 'events.json', events)
        if run['cleanup_state'] != 'confirmed':
            evidence['reason'] = 'cleanup_unconfirmed'
        elif run['status'] != 'completed':
            evidence['reason'] = run['result']['reason']
            if evidence['reason'] in FAILED_HARNESS_REASONS:
                evidence['outcome'] = 'fail'
        else:
            files = client.v2('POST', f'/{sid}/runs/{rid}/collect', {})['files']
            write_json(folder / 'files.json', files)
            checks = check_mechanics(task, initial, files, events, rid)
            evidence['checks'] = checks
            evidence['outcome'] = 'pass' if all(checks.values()) else 'fail'
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        evidence['reason'] = type(exc).__name__
    finally:
        if sid:
            try:
                closed = client.v2('POST', f'/{sid}/close', {})
                if closed['status'] != 'closed' or client.events(sid, 0)[0] != 410:
                    raise RuntimeError('Close not confirmed')
                evidence['close'] = 'confirmed'
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                evidence.update(outcome='infra_error', close='unconfirmed', reason='close_unconfirmed')
        if app:
            app.shutdown()
            for journal in app.store.journals.values():
                journal.db.close()
        for journal in executor.store.journals.values():
            journal.db.close()
        write_json(folder / 'attempt.json', evidence)
    return evidence


def run_suite(output):
    evidence_root = Path(output) / uuid.uuid4().hex
    evidence_root.mkdir(parents=True)
    summary = {'mode': MODE, 'quality_evaluation': False, 'repetitions': 1}
    try:
        entries = read_catalog()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        summary.update(outcome='infra_error', reason='catalog_' + type(exc).__name__, attempts=[])
    else:
        attempts = [run_case(entry, evidence_root) for entry in entries]
        summary['attempts'] = [{'task': attempt['task'], 'id': attempt['id'], 'outcome': attempt['outcome'],
                                'evidence': f"{attempt['id']}/attempt.json"} for attempt in attempts]
        counts = {status: sum(attempt['outcome'] == status for attempt in attempts)
                  for status in ('pass', 'fail', 'infra_error')}
        summary['counts'] = counts
        summary['outcome'] = 'infra_error' if counts['infra_error'] else 'fail' if counts['fail'] else 'pass'
    write_json(evidence_root / 'summary.json', summary)
    return evidence_root, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Parent directory for unique evidence runs')
    args = parser.parse_args()
    output, summary = run_suite(args.output)
    print(json.dumps({'evidence': str(output / 'summary.json'), **summary}, ensure_ascii=False))
    return {'pass': 0, 'fail': 1, 'infra_error': 2}[summary['outcome']]


if __name__ == '__main__':
    raise SystemExit(main())
