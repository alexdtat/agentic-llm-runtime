"""Run the desktop side of the two-host synthetic mounted rehearsal."""
import argparse
import base64
import contextlib
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from live_b6_mock import Rehearsal, propagation_tags, rw_mount_graph, scope_for
from live_failure import IMAGE, LOCAL, REPOSITORY, preflight, require, service
from recover_readonly import SOCKET, private_directory, text
from track_a.application import Application
from track_a.baf_attempt import persist
from track_a.http_api import JsonHandler
from track_a.model_proxy import ModelProxy
from track_a.mounted_executor import MountedExecutor
import two_host_pairing as pairing
from two_host_transport import ensure_gateway, wait_pinned_export

KEYS = Path('/tmp/chp2-server.K0b92fOA')
BROKER = REPOSITORY / 'prototypes/mount-runtime-probe/broker.sh'
RESUME_EVIDENCE = Path('/tmp/agentic-two-host-evidence.xgz0sec3')
RESUME_ROOT = Path('/tmp/agentic-mounted-runtime.ri65tzys')
RESUME_TAG = '34577139723871c73f04'
READINESS_ERROR = 'Operator readiness was not confirmed'


def resume_credentials(path, desktop):
    private_directory(RESUME_EVIDENCE, r'/tmp/agentic-two-host-evidence\.[a-zA-Z0-9]{8}')
    summary = json.loads(text(RESUME_EVIDENCE / 'summary.json'))
    scope = scope_for(RESUME_ROOT)
    empty = {'containers': [], 'networks': []}
    require((summary['mode'], summary['status'], summary['desktop'], summary['evidence'],
             summary['root'], summary['scope'], summary['tag']) ==
            ('two_host_mounted_mock', 'failed', desktop, str(RESUME_EVIDENCE), str(RESUME_ROOT), scope, RESUME_TAG)
            and summary['stages'] == {
                'prepare': {'status': 'passed', 'exit_code': 0},
                'exchange': {'status': 'failed', 'exit_code': 1, 'reason': READINESS_ERROR},
                'cleanup': {'status': 'passed', 'exit_code': 0, 'mount_ids': [[], []], 'docker': {
                    'com.docker.compose.project=' + scope: empty, 'agentic.scope=mounted-' + scope: empty,
                }},
            }, 'Resume requires the exact previous pre-mount stop and confirmed cleanup')
    try:
        for name in ('before-bind.mountinfo', 'events.json'):
            require(not os.path.lexists(RESUME_EVIDENCE / name), 'Previous rehearsal advanced beyond readiness')
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 1024,
                    'Resume credentials must be a bounded regular file')
            payload = stream.read(1025)
            require(len(payload) == info.st_size and os.fstat(stream.fileno()).st_mtime_ns == info.st_mtime_ns,
                    'Resume credentials changed during read')
        config = json.loads(payload)
    except (OSError, ValueError):
        raise RuntimeError('Could not read the original USB credentials file') from None
    require(isinstance(config, dict) and config.keys() == {'tag', 'desktop', 'token', 'grant'} and
            (config['tag'], config['desktop']) == (RESUME_TAG, desktop) and
            all(isinstance(config[key], str) and re.fullmatch(r'[a-f0-9]{48}', config[key])
                for key in ('token', 'grant')),
            'Credentials do not match the previous desktop rehearsal')
    return config['tag'], config['token'], config['grant']


class TwoHostModel(ModelProxy):
    def __init__(self, tag):
        super().__init__(SimpleNamespace(backend='mock'))
        self.tag = tag

    def mock(self, messages):
        tag, message = self.tag, messages[-1]['content']
        if message == f'write-{tag}':
            path = f'agent-{tag}.txt'
            data = base64.b64encode(f'agent:{tag}\n'.encode()).decode()
            return {'calls': [
                {'id': 'write', 'tool': 'WriteFile', 'args': {'path': path, 'data': data}},
                {'id': 'readback', 'tool': 'ReadFile', 'args': {'path': path}},
            ]}
        if message == f'read-{tag}':
            return {'calls': [{'id': 'read-client', 'tool': 'ReadFile', 'args': {'path': f'client-{tag}.txt'}}]}
        return {'answer': 'Two-host mock finished.'}


class TwoHostApplication(Application):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_traces = {}

    def close(self, session):
        # Application retains only a tombstone after a confirmed release.
        trace = copy.deepcopy(session)
        super().close(session)
        trace['status'] = session['status']
        self.closed_traces[session['id']] = trace


class PairedHealthHandler(JsonHandler):
    def do_GET(self):
        prefix = '/v0/paired-health/'
        if not self.path.startswith(prefix):
            return super().do_GET()
        nonce = self.path[len(prefix):]
        if not pairing.is_hex(nonce, 32):
            return self.reply(400, {'error': 'invalid_challenge'})
        app = self.server.app
        status, value = app.health()
        if status == 200:
            value.update(paired_tag=app.paired_tag,
                         paired_proof=pairing.challenge_proof(app.paired_seed, app.paired_tag, nonce))
        return self.reply(status, value)


def verify_session(session, tag):
    require(session['status'] == 'closed' and session['request_id'] == tag, 'Laptop session did not close')
    runs = list(session['runs'].values())
    require(len(runs) == 2, 'Expected two runs in the same laptop session')
    events = session['events']
    require([event['sequence'] for event in events] == list(range(1, session['cursor'] + 1)),
            'Session event sequence is incomplete')
    checks = [
        ('write', [('WriteFile', f'agent-{tag}.txt', None), ('ReadFile', f'agent-{tag}.txt', f'agent:{tag}\n')]),
        ('read', [('ReadFile', f'client-{tag}.txt', f'client:{tag}\n')]),
    ]
    for run, (phase, expected) in zip(runs, checks):
        require((run['message'], run['status'], run['cleanup_state']) ==
                (f'{phase}-{tag}', 'completed', 'confirmed'), 'Unexpected run result')
        group = [event for event in events if event['run_id'] == run['id']]
        starts = [event['payload'] for event in group if event['type'] == 'tool.started']
        results = [event['payload'] for event in group if event['type'] == 'tool.result']
        require(len(starts) == len(results) == len(expected) and group[-1]['type'] == 'run.result',
                'Unexpected tool trace')
        for start, result, (tool, path, content) in zip(starts, results, expected):
            data = result['result']
            require((start['op_id'], start['call']['tool'], start['call']['args']['path'], data['exit_code']) ==
                    (result['op_id'], tool, path, 0) and not data['truncated'], 'Unexpected tool result')
            if content is not None:
                require(base64.b64decode(data['data'], validate=True) == content.encode(), 'Readback bytes differ')
    return [run['id'] for run in runs]


def broker_args(root, scope, keys=KEYS):
    return [*LOCAL, 'run', '-d', '--pull', 'never', '--runtime', 'runc', '--read-only',
            '--name', 'agentic-two-host-broker-' + scope, '--label', 'com.docker.compose.project=' + scope,
            '--user', '0:0', '--network', 'host', '--cpus', '.5', '--memory', '128m',
            '--memory-swap', '128m', '--pids-limit', '32', '--cap-drop', 'ALL',
            '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'SYS_ADMIN', '--device', '/dev/fuse:/dev/fuse:rw',
            '--security-opt', 'no-new-privileges', '--security-opt', 'apparmor:unconfined',
            '--tmpfs', '/tmp:size=1m,mode=1777', '--log-driver', 'local',
            '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1', '--log-opt', 'compress=false',
            '--env', 'SSHFS_HOST=127.0.0.1', '--env', 'SSHFS_PORT=24222',
            '--mount', f'type=bind,src={keys}/broker,dst=/keys/client,readonly',
            '--mount', f'type=bind,src={keys}/known_hosts,dst=/keys/known_hosts,readonly',
            '--mount', f'type=bind,src={BROKER},dst=/probe-config/broker.sh,readonly',
            '--mount', f'type=bind,src={root}/mount,dst=/probe-mount,bind-propagation=rshared',
            '--entrypoint', '/bin/sh', IMAGE, '/probe-config/broker.sh']


class TwoHost(Rehearsal):
    def __init__(self, evidence, log, *, resume=None, paired=False):
        super().__init__(evidence, log)
        self.resume = resume
        self.paired = paired
        self.keys = KEYS
        if resume is None:
            self.tag, self.token, self.grant = secrets.token_hex(10), secrets.token_hex(24), secrets.token_hex(24)
        self.desktop = socket.gethostname()
        self.summary = {'status': 'running', 'mode': 'two_host_mounted_mock', 'desktop': self.desktop,
                        'evidence': str(evidence), 'stages': {stage: {'status': 'not_run', 'exit_code': None}
                                                           for stage in ('prepare', 'exchange', 'cleanup')}}

    def rw_graph(self, root, note):
        return rw_mount_graph(root, Path('/proc/self/mountinfo').read_text(),
                              (self.evidence / f'{note}.mountinfo').write_text,
                              sshfs_source='ubuntu@127.0.0.1:/work')

    def prepare(self):
        if self.paired:
            config = pairing.load('desktop')
            require(config['desktop'] == self.desktop, 'Pairing belongs to another desktop')
            self.paired_seed = config['seed']
            self.token, self.grant = pairing.credentials(self.paired_seed, self.tag)
            self.keys = pairing.config_root('desktop') / 'broker-keys'
            pairing.private_directory(self.keys)
            self.summary['paired'] = True
        if self.resume is not None:
            self.tag, self.token, self.grant = resume_credentials(self.resume, self.desktop)
            self.summary['resumed_from'] = str(RESUME_EVIDENCE)
        preflight()
        os.environ.update(DOCKER_HOST=SOCKET, no_proxy='127.0.0.1')
        if not self.paired:
            private_directory(self.keys, r'/tmp/chp2-server\.[a-zA-Z0-9]{8}')
        private_key = self.keys / 'broker'
        require(private_key.is_file() and not private_key.is_symlink() and private_key.stat().st_uid == os.getuid()
                and private_key.stat().st_mode & 0o077 == 0, 'Expected the private desktop broker key')
        known_hosts = text(self.keys / 'known_hosts').splitlines()
        require(len(known_hosts) == 1 and
                re.fullmatch(r'\[127\.0\.0\.1\]:24222 ssh-ed25519 [A-Za-z0-9+/=]+(?: +[^\r\n]+)?', known_hosts[0]),
                'Expected the pinned laptop host key for port 24222')
        self.root = Path(tempfile.mkdtemp(prefix='agentic-mounted-runtime.', dir='/tmp'))
        self.scope = scope_for(self.root)
        self.summary.update(root=str(self.root), scope=self.scope, tag=self.tag)
        mount = self.root / 'mount'
        (mount / 'work').mkdir(parents=True)
        mount.chmod(0o755)
        if self.resume is not None or self.paired:
            return 'passed', 0
        bundle = self.evidence / 'laptop'
        (bundle / 'track_a').mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name('two_host_laptop.py'), bundle / 'laptop.py')
        for name in ('two_host_pairing.py', 'two_host_transport.py'):
            shutil.copyfile(Path(__file__).with_name(name), bundle / name)
        shutil.copyfile(REPOSITORY / 'track_a/client.py', bundle / 'track_a/client.py')
        self.credentials = self.root / 'credentials.json'
        persist(self.credentials, dict(tag=self.tag, token=self.token, grant=self.grant, desktop=self.desktop))
        print(f'Public laptop code: {bundle}\nPrivate credentials for trusted transfer: {self.credentials}', flush=True)
        return 'passed', 0

    def exchange(self):
        if self.resume is None and not self.paired:
            print('With both SSH forwards ready and the laptop bundle transferred, press Enter here.', flush=True)
            require(sys.stdin.isatty() and sys.stdin.readline(), READINESS_ERROR)
        subprocess.run(['sudo', '-v'], check=True)
        if self.paired:
            ensure_gateway(self.command)
            print('Gateway ready. Start laptop.py --paired within five minutes.', flush=True)
            wait_pinned_export(self.keys)
        mount = self.root / 'mount'
        require(self.rw_graph(self.root, 'before-bind') == ({}, {}), 'Fresh mount path is occupied')
        self.verify_empty(self.root)
        try:
            self.privileged('mount', '--bind', '--', mount, mount)
        finally:
            self.mounts = self.rw_graph(self.root, 'after-bind')
        require(len(self.mounts[0]) == 1 and not self.mounts[1], 'Expected one new self-bind')
        bind_ids = self.mounts[0].keys()
        self.privileged('mount', '--make-private', '--', mount)
        current = self.rw_graph(self.root, 'private-bind')
        require(current[0].keys() == bind_ids and not current[1], 'Self-bind identity changed')
        self.mounts = current
        require(not propagation_tags(next(iter(current[0].values()))), 'Self-bind is still in a peer group')
        self.privileged('mount', '--make-rshared', '--', mount)
        current = self.rw_graph(self.root, 'shared-bind')
        require(current[0].keys() == bind_ids and not current[1], 'Self-bind identity changed after promotion')
        self.mounts = current
        tags = propagation_tags(next(iter(current[0].values())))
        require(len(tags) == 1 and next(iter(tags)).startswith('shared:'), 'Self-bind lacks an isolated peer group')
        self.stack_attempted = True
        try:
            self.command(broker_args(self.root, self.scope, self.keys), seconds=12)
            deadline = time.monotonic() + 25
            while not self.observe_mount() and time.monotonic() < deadline:
                time.sleep(.2)
            require(self.mounts[1], 'Broker did not mount the laptop export')
        finally:
            self.observe_mount()
        executor = MountedExecutor(str(mount / 'work'), self.grant, self.scope, 'agentic-sandbox:cpu-v1',
                                   self.root / 'execution-state')
        executor.recover()
        with contextlib.ExitStack() as services:
            backend = services.enter_context(service(executor, secrets.token_hex(24)))
            model = services.enter_context(service(TwoHostModel(self.tag), secrets.token_hex(24)))
            app = TwoHostApplication(backend, model, {'laptop': self.token}, self.root / 'app-state',
                                     mount_owner='laptop')
            if self.paired:
                app.paired_tag, app.paired_seed = self.tag, self.paired_seed
            handler = PairedHealthHandler if self.paired else JsonHandler
            services.enter_context(service(app, self.token, port=24180, handler=handler))
            print('Desktop ready. Run laptop.py through the forwarded loopback now.', flush=True)
            return self.wait_for_laptop(app)

    def observe_mount(self):
        current = self.rw_graph(self.root, 'broker-mount')
        require(current[0] == self.mounts[0], 'Broker mount changed the self-bind')
        self.mounts = current
        if not current[1]:
            return False
        require(len(current[1]) == 1 and next(iter(current[1].values())).split()[1] in current[0]
                and 'rw' in next(iter(current[1].values())).split()[5].split(','),
                'Expected one writable SSHFS mount under the owned self-bind')
        return True

    def wait_for_laptop(self, app):
        stage = self.summary['stages']['exchange']
        deadline = time.monotonic() + 240

        def snapshot():
            with app.lock:
                sessions = [(sid, app.locks[sid]) for sid in app.sessions]
            require(len(sessions) <= 1, 'Expected only one laptop session')
            if sessions:
                sid, lock = sessions[0]
                with lock:
                    return copy.deepcopy(app.closed_traces.get(sid, app.sessions[sid]))

        try:
            while time.monotonic() < deadline:
                session = snapshot()
                if session and session['status'] == 'closed':
                    stage.update(session=session['id'], close='confirmed')
                    stage['runs'] = verify_session(session, self.tag)
                    return 'passed', 0
                time.sleep(.2)
            raise TimeoutError('Laptop did not complete within four minutes')
        finally:
            session = snapshot()
            if session:
                persist(self.evidence / 'events.json', session['events'])
                if session['status'] != 'closed':
                    status, closed = app.route('laptop', 'POST', f"/v2/sessions/{session['id']}/close", {}, {})
                    stage['close'] = 'operator_confirmed' if status == 200 and closed['status'] == 'closed' else 'failed'

    def cleanup(self):
        if self.root is None:
            return 'not_acquired', 0
        stage = self.summary['stages']['cleanup']
        (self.root / 'credentials.json').unlink(missing_ok=True)
        before = self.rw_graph(self.root, 'cleanup-before')
        require(all(active.items() <= expected.items() for active, expected in zip(before, self.mounts)),
                'Mount identities changed before cleanup')
        if self.stack_attempted:
            resources = self.scoped_resources(self.root)
            project = resources['com.docker.compose.project=' + self.scope]
            runtime = resources['agentic.scope=mounted-' + self.scope]
            require(not any(runtime.values()) and not project['networks'] and len(project['containers']) <= 1,
                    'Unexpected Docker resources before broker removal')
            for ident in project['containers']:
                name = self.command([*LOCAL, 'container', 'inspect', '--format', '{{.Name}}', ident]).stdout.strip()
                require(re.fullmatch(r'[a-f0-9]{64}', ident) and name == '/agentic-two-host-broker-' + self.scope,
                        'Unexpected broker container identity')
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.command([*LOCAL, 'container', 'logs', '--tail', '32', ident], seconds=3, check=False)
                self.command([*LOCAL, 'container', 'rm', '-f', ident], seconds=12)
        stage['docker'] = self.verify_empty(self.root)
        self.finish_rw_mounts(self.root, before, stage, 'cleanup', residual_confirmed=self.stack_attempted)
        shutil.rmtree(self.root)
        return 'passed', 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--resume-credentials', type=Path, metavar='PATH',
                        help='Reuse the original USB credentials after the confirmed xgz0sec3 pre-mount stop')
    modes.add_argument('--paired', action='store_true', help='Run an enrolled two-host pair')
    modes.add_argument('--pair-from', type=Path, metavar='PATH', help='Enroll from an existing credentials file')
    parser.add_argument('--broker-keys', type=Path, default=KEYS,
                        help='Original desktop broker-key directory for one-time enrollment')
    args = parser.parse_args()
    os.umask(0o077)
    if args.pair_from:
        config = pairing.enrollment(args.pair_from)
        require(config['desktop'] == socket.gethostname(), 'Credentials belong to another desktop')
        private_key = pairing.read_file(args.broker_keys / 'broker', private=True)
        pinned_hosts = pairing.read_file(args.broker_keys / 'known_hosts', private=False)
        with pairing.new_configuration('desktop') as root:
            keys = root / 'broker-keys'
            keys.mkdir()
            pairing.write_file(keys / 'broker', private_key)
            pairing.write_file(keys / 'known_hosts', pinned_hosts)
            pairing.write_configuration(root, config)
        print('Desktop enrollment saved.', flush=True)
        return 0
    evidence = Path(tempfile.mkdtemp(prefix='agentic-two-host-evidence.', dir='/tmp'))
    print(f'Evidence directory: {evidence}', flush=True)

    def interrupted(*_):
        raise KeyboardInterrupt()

    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        signal.signal(sig, interrupted)
    with (evidence / 'commands.jsonl').open('w') as commands:
        runner = TwoHost(evidence, commands, resume=args.resume_credentials, paired=args.paired)
        succeeded = False
        try:
            succeeded = runner.stage('prepare', runner.prepare) and runner.stage('exchange', runner.exchange)
        finally:
            for sig in signals:
                signal.signal(sig, signal.SIG_IGN)
            cleaned = runner.stage('cleanup', runner.cleanup)
            runner.summary['status'] = 'passed' if succeeded and cleaned else 'failed'
            runner.save()
            print(json.dumps(runner.summary), flush=True)
    return 0 if succeeded and cleaned else 1


if __name__ == '__main__':
    sys.exit(main())
