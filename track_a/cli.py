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
    sub.add_parser('new', help='Create an isolated workdir and print its session ID')
    execute = sub.add_parser('exec', help='Start a sandbox command; print its run ID')
    execute.add_argument('session_id')
    execute.add_argument('--timeout', type=float, default=3)
    execute.add_argument('command', nargs=argparse.REMAINDER)
    watch = sub.add_parser('watch', help='Poll events and print the terminal result')
    watch.add_argument('run_id')
    watch.add_argument('--timeout', type=float, default=20)
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
        elif args.action == 'new':
            print(request('POST', '/v0/sessions', {'request_id': uuid.uuid4().hex})['id'])
        elif args.action == 'exec':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                parser.error('Provide a command after the session ID')
            value = request('POST', f'/v0/sessions/{args.session_id}/runs',
                            {'request_id': uuid.uuid4().hex, 'argv': command,
                             'timeout_seconds': args.timeout})
            print(value['id'])
        elif args.action == 'watch':
            seen = set()
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                value = request('GET', '/v0/runs/' + args.run_id)
                for event in value['events']:
                    if event['sequence'] not in seen:
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                        seen.add(event['sequence'])
                if value['result'] is not None:
                    return 0 if value['result']['status'] == 'complete' else 1
                time.sleep(.1)
            raise TimeoutError('Watching timed out; run remains available for watch or cancel')
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
