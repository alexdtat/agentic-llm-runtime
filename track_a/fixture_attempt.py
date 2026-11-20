"""Offline attempt lifecycle over the normal v2 API, using a data-only checker."""
import argparse
import json
import os
import uuid
from pathlib import Path

from track_a.client import Client
from track_a.contracts import fields, fixture_files


def attempt(client, task, output):
    fields(task, 'message', 'fixture', 'expected')
    initial = fixture_files(task['fixture'])
    expected = fixture_files(task['expected'])
    aid = uuid.uuid4().hex
    folder = Path(output) / aid
    folder.mkdir(parents=True)
    evidence = {'id': aid, 'mode': 'fixture', 'outcome': 'infra_error'}
    sid = None
    try:
        session = client.v2('POST', '', {'request_id': aid, 'files': initial})
        sid = session['id']
        run = client.v2('POST', f'/{sid}/runs', {'request_id': aid, 'message': task['message']})
        run = client.wait_run(sid, run['id'])
        evidence['run'] = run
        if run['cleanup_state'] != 'confirmed':
            evidence['reason'] = 'cleanup_unconfirmed'
        elif run['result']['status'] == 'completed':
            files = client.v2('POST', f"/{sid}/runs/{run['id']}/collect", {})['files']
            evidence['files'] = files
            evidence['checker'] = {'pass': files == expected}
            evidence['outcome'] = 'pass' if files == expected else 'fail'
        else:
            evidence['reason'] = run['result']['reason']
            if evidence['reason'] in {'context_limit', 'step_limit', 'duplicate_tool_call_id'}:
                evidence['outcome'] = 'fail'
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        evidence['reason'] = type(exc).__name__
    finally:
        if sid:
            try:
                client.v2('POST', f'/{sid}/close', {})
            except (OSError, RuntimeError, ValueError):
                evidence.update(outcome='infra_error', close='unconfirmed')
        (folder / 'attempt.json').write_text(json.dumps(evidence, indent=2) + '\n')
    return evidence


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    client = Client(os.environ.get('RUNTIME_URL', 'http://127.0.0.1:8080'), os.environ['PROBE_TOKEN'])
    result = attempt(client, json.loads(args.task.read_text()), args.output)
    print(json.dumps({'id': result['id'], 'outcome': result['outcome']}))
    raise SystemExit({'pass': 0, 'fail': 1, 'infra_error': 2}[result['outcome']])
