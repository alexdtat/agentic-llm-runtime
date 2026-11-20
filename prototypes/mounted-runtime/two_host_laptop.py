"""Confirm the two-host mounted rehearsal from the synthetic laptop export."""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener, install_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from track_a.client import Client
import two_host_pairing as pairing
from two_host_transport import ensure_laptop_transport

URL = 'http://127.0.0.1:24180'
HEALTH = {'status': 'ok', 'version': 'v2', 'execution': 'mounted', 'client_mount': True}
ORIGINAL_EXPORT = Path('/tmp/chp2-client.PqzPUzbq/work')


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def enrolled_export(export):
    require(re.fullmatch(r'/tmp/chp2-[a-zA-Z0-9_.-]+/(?:work|client)', str(export))
            and export.resolve(strict=True) == export and export.is_dir() and export.stat().st_uid == os.getuid(),
            'Use the existing owned /tmp/chp2-…/work synthetic export')


def enroll(source, export, tunnel_key, gateway_known_hosts):
    export = export.absolute()
    enrolled_export(export)
    destinations = {
        'exporter-host': (export.parent / 'host', True), 'broker.pub': (export.parent / 'broker.pub', False),
        'tunnel-key': (tunnel_key, True), 'gateway_known_hosts': (gateway_known_hosts, False),
    }
    sources = [(name, pairing.read_file(path, private=private)) for name, (path, private) in destinations.items()]
    config = pairing.enrollment(source)
    require(config['desktop'] != socket.gethostname(), 'Use the laptop to enroll the laptop role')
    with pairing.new_configuration('laptop') as root:
        (root / 'work').mkdir()
        for name, data in sources:
            pairing.write_file(root / name, data)
        pairing.write_configuration(root, {**config, 'previous_export': str(export)})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_challenge(client, seed):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        nonce = secrets.token_hex(16)
        try:
            status, value = client.request('GET', '/v0/paired-health/' + nonce, timeout=3)
            tag = value.get('paired_tag')
            if status == 200 and pairing.is_hex(tag, 20) and {key: value.get(key) for key in HEALTH} == HEALTH:
                proof = value.get('paired_proof')
                require(pairing.is_hex(proof, 64) and hmac.compare_digest(
                    proof, pairing.challenge_proof(seed, tag, nonce)), 'Paired health authentication failed')
                return tag
        except (OSError, ValueError):
            pass
        time.sleep(.4)
    raise TimeoutError('Paired mounted Application did not become ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--paired', action='store_true')
    modes.add_argument('--pair-from', type=Path, metavar='PATH')
    parser.add_argument('--export-dir', type=Path, default=ORIGINAL_EXPORT, help='Existing exporter workdir at enrollment')
    parser.add_argument('--tunnel-key', type=Path, default=Path.home() / '.ssh/chp2-thinkpad-tunnel')
    parser.add_argument('--gateway-known-hosts', type=Path, default=Path.home() / 'usb/known_hosts')
    parser.add_argument('export', type=Path, nargs='?')
    parser.add_argument('credentials', type=Path, nargs='?')
    args = parser.parse_args()
    if not (args.paired or args.pair_from or (args.export and args.credentials)):
        parser.error('Supply --paired, --pair-from, or the export and credentials paths')
    os.umask(0o077)
    os.environ['no_proxy'] = '127.0.0.1'
    outcome = {'host': socket.gethostname(), 'status': 'failed', 'runs': []}
    session = client = None
    try:
        if args.pair_from:
            enroll(args.pair_from, args.export_dir, args.tunnel_key, args.gateway_known_hosts)
            outcome.update(status='passed', mode='enrollment')
            return 0
        if args.paired:
            config = pairing.load('laptop')
            install_opener(build_opener(ProxyHandler({}), NoRedirect()))
            export = ensure_laptop_transport(config['previous_export'])
            if str(export) == config['previous_export']:
                enrolled_export(export)
            else:
                pairing.private_directory(export)
            tag = wait_challenge(Client(URL, ''), config['seed'])
            config['token'], config['grant'] = pairing.credentials(config['seed'], tag)
        else:
            export, credentials = args.export.absolute(), args.credentials.absolute()
            enrolled_export(export)
            require(credentials.resolve(strict=True) == credentials and not credentials.is_relative_to(export),
                    'Keep credentials outside the export')
            metadata = credentials.lstat()
            require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                    and metadata.st_mode & 0o077 == 0 and metadata.st_size < 1024, 'Use a private credentials file')
            config = json.loads(credentials.read_text())
            tag = config['tag']
        require(re.fullmatch(r'[0-9a-f]{20}', tag) and config['desktop'] != outcome['host'],
                'Expected a fresh tag and a different desktop hostname')
        client = Client(URL, config['token'])
        if not args.paired:
            require(client.request('GET', '/v0/health', timeout=4) == (200, HEALTH),
                    'Forwarded Application health check failed')
        local = export / f'client-{tag}.txt'
        with local.open('x') as initial:
            initial.write('initial\n')
        session = client.v2('POST', '', {'request_id': tag, 'grant': config['grant']})['id']
        outcome.update(session=session, tag=tag, desktop=config['desktop'])
        for phase in ('write', 'read'):
            run = client.v2('POST', f'/{session}/runs',
                            {'request_id': phase, 'message': f'{phase}-{tag}'})['id']
            outcome['runs'].append(run)
            finished = client.wait_run(session, run, timeout=35)
            result = finished['result']
            require((finished['id'], finished['session_id'], result['status'], result['cleanup_state']) ==
                    (run, session, 'completed', 'confirmed'),
                    f'{phase} run did not complete with confirmed stop')
            if phase == 'write':
                written = export / f'agent-{tag}.txt'
                require(written.read_bytes() == f'agent:{tag}\n'.encode(), 'Laptop did not observe agent write')
                outcome['observed_agent_write'] = True
                local.write_bytes(f'client:{tag}\n'.encode())
                outcome['local_edit'] = True
        outcome['status'] = 'passed'
    except (OSError, ValueError, RuntimeError, KeyError, KeyboardInterrupt, subprocess.SubprocessError) as error:
        outcome['reason'] = type(error).__name__ if isinstance(error, (OSError, subprocess.SubprocessError)) else str(error)
    finally:
        if session:
            try:
                outcome['closed'] = client.v2('POST', f'/{session}/close', {}, timeout=12)['status'] == 'closed'
            except (OSError, ValueError, RuntimeError, KeyError, KeyboardInterrupt):
                outcome['closed'] = False
            if not outcome['closed']:
                outcome['status'] = 'failed'
        print(json.dumps(outcome), flush=True)
    return 0 if outcome['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
