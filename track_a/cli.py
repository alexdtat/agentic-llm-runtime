"""Terminal client for the v0 runtime; commands execute only on the server."""
import argparse
import json
import os
import sys
import time
import uuid

from track_a.client import Client


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('RUNTIME_URL', 'http://127.0.0.1:8080'))
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('health')
    fixture = sub.add_parser('fixture-new', help='Create a v2 offline fixture session')
    fixture.add_argument('--request-id')
    mounted = sub.add_parser('mount-new', help='Create a v2 session with a configured export grant')
    mounted.add_argument('grant')
    mounted.add_argument('--request-id')
    send = sub.add_parser('run', help='Submit a message to a v2 session')
    send.add_argument('session_id')
    send.add_argument('message')
    send.add_argument('--request-id')
    events = sub.add_parser('events', help='Read an SSE batch from a v2 session')
    events.add_argument('session_id')
    events.add_argument('--after', type=int, default=0)
    for action in ('run-get', 'run-cancel', 'run-collect'):
        command = sub.add_parser(action)
        command.add_argument('session_id')
        command.add_argument('run_id')
    session_close = sub.add_parser('session-close')
    session_close.add_argument('session_id')
    new = sub.add_parser('new', help='Create an isolated workdir and print its session ID')
    new.add_argument('--request-id', default=None)
    task = sub.add_parser('task', help='Submit a message to the server-side agent')
    task.add_argument('session_id')
    task.add_argument('message')
    task.add_argument('--request-id', default=None)
    task_watch = sub.add_parser('task-watch')
    task_watch.add_argument('run_id')
    task_watch.add_argument('--timeout', type=float, default=75)
    task_watch.add_argument('--after', type=int, default=-1)
    task_cancel = sub.add_parser('task-cancel')
    task_cancel.add_argument('run_id')
    execute = sub.add_parser('exec', help='Start a sandbox command; print its run ID')
    execute.add_argument('session_id')
    execute.add_argument('--timeout', type=float, default=3)
    execute.add_argument('command', nargs=argparse.REMAINDER)
    watch = sub.add_parser('watch', help='Poll events and print the terminal result')
    watch.add_argument('run_id')
    watch.add_argument('--timeout', type=float, default=20)
    watch.add_argument('--after', type=int, default=-1)
    cancel = sub.add_parser('cancel')
    cancel.add_argument('run_id')
    close = sub.add_parser('close', help='Remove this session and its temporary workdir')
    close.add_argument('session_id')
    args = parser.parse_args(argv)
    token = os.environ.get('PROBE_TOKEN', '')
    if args.action != 'health' and not token:
        parser.error('Set PROBE_TOKEN in the environment')
    client = Client(args.url.rstrip('/'), token)

    def request(method, path, payload=None):
        status, value = client.request(method, path, payload)
        if status != 200:
            raise RuntimeError(f'HTTP {status}: {value}')
        return value

    try:
        if args.action == 'health':
            print(json.dumps(request('GET', '/v0/health')))
        elif args.action == 'fixture-new':
            value = client.v2('POST', '', {'request_id': args.request_id or uuid.uuid4().hex, 'files': {}})
            print(value['id'])
        elif args.action == 'mount-new':
            value = client.v2('POST', '', {'request_id': args.request_id or uuid.uuid4().hex,
                                          'grant': args.grant})
            print(value['id'])
        elif args.action == 'run':
            value = client.v2('POST', f'/{args.session_id}/runs',
                              {'request_id': args.request_id or uuid.uuid4().hex, 'message': args.message})
            print(value['id'])
        elif args.action == 'events':
            status, value = client.events(args.session_id, args.after)
            if status != 200:
                raise RuntimeError(f'HTTP {status}: {value}')
            for event in value:
                print(json.dumps(event, ensure_ascii=False))
        elif args.action in {'run-get', 'run-cancel', 'run-collect'}:
            path = f'/{args.session_id}/runs/{args.run_id}'
            if args.action == 'run-get':
                value = client.v2('GET', path)
            else:
                value = client.v2('POST', path + '/' + args.action.removeprefix('run-'), {})
            print(json.dumps(value, ensure_ascii=False))
        elif args.action == 'session-close':
            print(json.dumps(client.v2('POST', f'/{args.session_id}/close', {})))
        elif args.action == 'new':
            print(request('POST', '/v0/sessions', {'request_id': args.request_id or uuid.uuid4().hex})['id'])
        elif args.action == 'task':
            print(request('POST', f'/v1/sessions/{args.session_id}/tasks',
                          {'request_id': args.request_id or uuid.uuid4().hex, 'message': args.message})['id'])
        elif args.action == 'exec':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                parser.error('Provide a command after the session ID')
            value = request('POST', f'/v0/sessions/{args.session_id}/runs',
                            {'request_id': uuid.uuid4().hex, 'argv': command,
                             'timeout_seconds': args.timeout})
            print(value['id'])
        elif args.action in {'watch', 'task-watch'}:
            seen = set()
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                prefix = '/v1/tasks/' if args.action == 'task-watch' else '/v0/runs/'
                value = request('GET', prefix + args.run_id)
                for event in value['events']:
                    if event['sequence'] > args.after and event['sequence'] not in seen:
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        seen.add(event['sequence'])
                if value['result'] is not None:
                    return 0 if value['result']['status'] == 'complete' else 1
                time.sleep(.1)
            raise TimeoutError('Watching timed out; run remains available for watch or cancel')
        elif args.action == 'task-cancel':
            print(json.dumps(request('POST', '/v1/tasks/' + args.run_id + '/cancel', {})))
        elif args.action == 'cancel':
            print(json.dumps(request('POST', '/v0/runs/' + args.run_id + '/cancel', {})))
        elif args.action == 'close':
            print(json.dumps(request('DELETE', '/v0/sessions/' + args.session_id)))
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
