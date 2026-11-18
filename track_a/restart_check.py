"""Crash-recovery integration phases, coordinated by scripts/check.sh."""
import json
import os
import sys
import time
from pathlib import Path
from track_a.client import Client


def main():
    client = Client(os.environ['RUNTIME_URL'], os.environ['PROBE_TOKEN'])
    def call(method, path, body=None):
        status, value = client.request(method, path, body)
        if status != 200:
            raise RuntimeError((status, value))
        return value
    path = Path('/results/restart-input.json')
    if sys.argv[1] == 'prepare':
        session = call('POST', '/v0/sessions', {'request_id': 'crash-session'})
        payload = {'request_id': 'crash-command', 'argv': ['sleep', '10'], 'timeout_seconds': 10}
        run = call('POST', f'/v0/sessions/{session["id"]}/runs', payload)
        task_session = call('POST', '/v0/sessions', {'request_id': 'crash-task-session'})
        task_payload = {'request_id': 'crash-task', 'message': 'mock:slow'}
        task = call('POST', f'/v1/sessions/{task_session["id"]}/tasks', task_payload)
        for _ in range(50):
            task = call('GET', '/v1/tasks/' + task['id'])
            if any(e['type'] == 'tool_started' for e in task['events']):
                break
            time.sleep(.01)
        else:
            raise RuntimeError('Task did not enter tool execution')
        if task['result'] is not None:
            raise RuntimeError('Crash preparation missed the active task')
        path.write_text(json.dumps(dict(session=session, run=run, payload=payload,
                                       task=task, task_session=task_session, task_payload=task_payload)))
        print('Prepared active command and task')
    else:
        saved = json.loads(path.read_text())
        run = call('GET', '/v0/runs/' + saved['run']['id'])
        task = call('GET', '/v1/tasks/' + saved['task']['id'])
        assert run['result']['error_kind'] == 'interrupted', run
        assert task['result']['error_kind'] == 'interrupted', task
        assert len([e for e in run['events'] if e['type'] == 'result']) == 1
        assert len([e for e in task['events'] if e['type'] == 'result']) == 1
        repeated = call('POST', f'/v0/sessions/{saved["session"]["id"]}/runs', saved['payload'])
        assert repeated['id'] == run['id'] and repeated['result'] == run['result']
        repeated = call('POST', f'/v1/sessions/{saved["task_session"]["id"]}/tasks', saved['task_payload'])
        assert repeated['id'] == task['id'] and repeated['result'] == task['result']
        for session in [saved['session'], saved['task_session']]:
            assert call('GET', '/v0/sessions/' + session['id'])['status'] == 'closed'
        Path('/results/restart-result.json').write_text(json.dumps({'passed': True, 'run': run, 'task': task}, indent=2))
        print('Crash recovery and durable idempotency passed')


if __name__ == '__main__':
    main()
