"""Fault injection at the Docker CLI, findmnt, and private transport boundaries."""
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from track_a.application import Application, RunError
from track_a.http_api import Conflict
from track_a.model_proxy import ModelProxy
from track_a.mounted_executor import MOUNT_CHECK, READONLY_CHECK, MountedExecutor, main

RUN_PROCESS = subprocess.run
SCOPE = {'generation': 1, 'run_id': 'run'}
PAYLOAD = 'VGhpcyBpcyBhIHRlc3QgcGF5bG9hZC4='
WRITE = {'id': 'write', 'tool': 'WriteFile', 'args': {'path': 'result.txt', 'data': PAYLOAD}}
COMMAND = {'id': 'command', 'tool': 'RunCommand', 'args': {'command': 'printf mounted'}}
INTENT = {**SCOPE, 'op_id': 'op', 'call': COMMAND}
LOOKUP = {'generation': 1, 'op_id': 'op'}
NAME = 'agentic-mounted-session-run'


def completed(returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def tool_success(argv):
    stdout = PAYLOAD + '\n' if 'ReadFile' in argv else 'mounted' if argv[0] == '/bin/sh' else ''
    return completed(stdout=json.dumps(dict(exit_code=0, stdout=stdout, stderr='',
                                            truncated=False, error_kind=None)))


def mounted(root, access_mode='rw'):
    return MountedExecutor('/mnt/test-export', 'test-grant', 'test', 'sandbox', root,
                           access_mode=access_mode)


def post(executor, action, body):
    return executor.route('POST', '/mounted/session/' + action, body)


class FakeSystem:
    def __init__(self):
        self.mount = completed(stdout='fuse.sshfs\n')
        self.ready = completed()
        self.start_result = completed()
        self.collect_result = completed(stdout='{}')
        self.tool = tool_success
        self.calls = []
        self.tools = []
        self.collect_paths = []
        self.running = set()
        self.removal_fails = False
        self.listing_fails = False

    def run(self, args, *, input=None, **kwargs):
        if args[0] == 'findmnt':
            self.calls.append(('findmnt', *args[1:]))
            return self.mount
        if args[0] != 'docker':
            raise AssertionError(f'Unexpected subprocess {args[0]}')
        self.calls.append(tuple(args[1:]))
        operation = args[1]
        if operation == 'run':
            name = args[args.index('--name') + 1]
            if name in self.running:
                raise AssertionError(f'Duplicate sandbox {name}')
            result = self.start_result if args[2] == '-d' else self.collect_result
            if args[2] == '-i':
                self.collect_paths.append(json.loads(input))
            if args[2] == '-i' or result.returncode == 0:
                self.running.add(name)
            return result
        if operation == 'exec':
            if args[2] != '-i':
                return self.ready
            if args[3] not in self.running:
                raise AssertionError(f'No sandbox {args[3]}')
            argv = json.loads(input)['argv']
            self.tools.append(argv)
            return self.tool(argv)
        if operation == 'rm':
            name = args[3]
            if self.removal_fails or name not in self.running:
                return completed(1)
            self.running.remove(name)
            return completed()
        if operation == 'ps':
            if self.listing_fails:
                return completed(1)
            match = args[args.index('--filter') + 1]
            if match == 'label=agentic.scope=mounted-test':
                names = self.running
            elif match.startswith('name=^/'):
                names = {match[len('name=^/'):-1]} & self.running
            else:
                raise AssertionError(f'Unexpected filter {match}')
            return completed(stdout=''.join(name + '\n' for name in sorted(names)))
        raise AssertionError(f'Unexpected Docker operation {operation}')


def crash_after_dispatch(root):
    executor = mounted(root)
    system = FakeSystem()

    def die(argv):
        # Stand in for a client-visible side effect while the real intent remains unresolved.
        with (Path(root) / 'side-effect').open('ab') as handle:
            handle.write(b'dispatched\n')
            handle.flush()
            os.fsync(handle.fileno())
        os._exit(73)

    system.tool = die
    with patch('subprocess.run', side_effect=system.run):
        post(executor, 'provision', {'grant': 'test-grant'})
        post(executor, 'start', SCOPE)
        post(executor, 'exec', INTENT)


class InProcessClient:
    """Model the private HTTP boundary without opening sockets."""
    def __init__(self, service):
        self.service = service
        self.requests = []
        self.drop_exec_reply = False

    def request(self, method, path, body, timeout):
        self.requests.append((path.rsplit('/', 1)[-1], body))
        try:
            response = self.service.route(method, path, body)
        except Conflict:
            return 409, {}
        except RuntimeError:
            return 503, {}
        if self.drop_exec_reply and path.endswith('/exec'):
            raise TimeoutError('response lost after dispatch')
        return response


class MountedFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.system = FakeSystem()
        subprocess_patch = patch('subprocess.run', side_effect=self.system.run)
        subprocess_patch.start()
        self.addCleanup(subprocess_patch.stop)

    def executor(self, root=None, *, access_mode='rw'):
        executor = mounted(root or self.root / 'executor', access_mode)
        self.addCleanup(executor.containers.journal.db.close)
        self.addCleanup(self.close_journals, executor)
        return executor

    @staticmethod
    def close_journals(service):
        for journal in service.store.journals.values():
            journal.db.close()

    def provision(self, executor):
        self.assertEqual(post(executor, 'provision', {'grant': 'test-grant'}),
                         (200, {'generation': 1, 'closed': False}))

    def started(self):
        executor = self.executor()
        self.provision(executor)
        self.assertEqual(post(executor, 'start', SCOPE), (200, {}))
        return executor

    def test_readonly_tools_and_collect_keep_known_outcomes(self):
        self.system.mount = completed(stdout='fuse.sshfs ro,relatime\n')
        executor = self.executor(access_mode='ro')
        self.provision(executor)
        post(executor, 'start', SCOPE)
        start = next(call for call in self.system.calls if call[:2] == ('run', '-d'))
        self.assertTrue(start[start.index('--mount') + 1].endswith(',readonly'))
        self.assertIn(READONLY_CHECK, start[-1])

        write = {**INTENT, 'call': WRITE}
        denied = post(executor, 'exec', write)
        self.assertEqual(denied, (200, dict(exit_code=1, stdout='', stderr='Read-only workspace',
                                            truncated=False, error_kind='command_failure')))
        self.assertEqual(post(executor, 'exec', write), denied)
        self.assertEqual(post(executor, 'operation', LOOKUP), denied)
        with self.assertRaisesRegex(Conflict, 'different payload'):
            post(executor, 'exec', INTENT)
        self.assertEqual(self.system.tools, [])

        read = {'id': 'read', 'tool': 'ReadFile', 'args': {'path': 'client.txt'}}
        outcome = post(executor, 'exec', {**INTENT, 'op_id': 'read-op', 'call': read})
        self.assertEqual(outcome[1]['data'], PAYLOAD)
        self.system.tool = lambda argv: completed(stdout=json.dumps(
            dict(exit_code=1, stdout='', stderr='Read-only file system',
                 truncated=False, error_kind='command')))
        command = post(executor, 'exec', {**INTENT, 'op_id': 'shell-op'})
        self.assertEqual(command[1]['error_kind'], 'command_failure')
        self.assertEqual(self.system.tools[-1], ['/bin/sh', '-c', 'printf mounted'])

        post(executor, 'stop', SCOPE)
        self.system.collect_result = completed(stdout=json.dumps({'client.txt': PAYLOAD}))
        self.assertEqual(post(executor, 'collect', SCOPE), (200, {'files': {'client.txt': PAYLOAD}}))
        self.assertEqual(self.system.collect_paths, [['client.txt']])
        collector = next(call for call in self.system.calls if call[:2] == ('run', '-i'))
        self.assertTrue(collector[collector.index('--mount') + 1].endswith(',readonly'))
        self.assertFalse(self.system.running)

    def test_readonly_mount_requires_every_exact_host_view_to_be_readonly(self):
        executor = self.executor(access_mode='ro')
        for output in ('fuse.sshfs\n', 'fuse.sshfs rw,relatime\n', 'fuse.sshfs ro,rw\n',
                       'fuse.sshfs ro,relatime\nfuse.sshfs rw,relatime\n', 'ext4 ro\n'):
            with self.subTest(output=output):
                self.system.mount = completed(stdout=output)
                self.assertEqual(executor.health()[0], 503)
                with self.assertRaises(RuntimeError):
                    post(executor, 'provision', {'grant': 'test-grant'})
        self.system.mount = completed(stdout='fuse.sshfs ro,relatime\nfuse.sshfs ro,nosuid\n')
        self.assertEqual(executor.health()[0], 200)
        self.provision(executor)
        self.assertFalse(self.system.running)

    def test_grant_mode_cannot_be_overridden_or_upgraded_after_restart(self):
        self.system.mount = completed(stdout='fuse.sshfs ro,relatime\n')
        executor = self.executor(access_mode='ro')
        with self.assertRaises(TypeError):
            post(executor, 'provision', {'grant': 'test-grant', 'access_mode': 'rw'})
        app = Application(None, None, {'owner': 'token'}, mount_owner='owner')
        with self.assertRaises(TypeError):
            app.create('owner', {'request_id': 'override', 'grant': 'test-grant', 'access_mode': 'rw'})
        self.provision(executor)
        post(executor, 'start', SCOPE)
        write = {**INTENT, 'call': WRITE}
        denied = post(executor, 'exec', write)

        restored = self.executor()
        restored.recover()
        with self.assertRaisesRegex(Conflict, 'access mode changed'):
            post(restored, 'provision', {'grant': 'test-grant'})
        with self.assertRaisesRegex(Conflict, 'access mode changed'):
            post(restored, 'start', {**SCOPE, 'run_id': 'next-run'})
        self.assertEqual(post(restored, 'operation', LOOKUP), denied)
        self.assertEqual(post(restored, 'exec', write), denied)
        self.assertEqual(post(restored, 'release', {})[1]['cleanup_state'], 'confirmed')
        self.assertFalse(self.system.tools)

    def test_legacy_journal_without_access_mode_still_starts_readwrite(self):
        executor = self.executor()
        self.provision(executor)
        legacy = executor.sessions['session']
        del legacy['access_mode']
        executor.store.save(legacy)
        self.system.mount = completed(stdout='fuse.sshfs\nfuse.sshfs\n')
        restored = self.executor()
        restored.recover()
        self.provision(restored)
        post(restored, 'start', SCOPE)
        start = next(call for call in self.system.calls if call[:2] == ('run', '-d'))
        self.assertNotIn(',readonly', start[start.index('--mount') + 1])
        self.assertEqual(post(restored, 'exec', {**INTENT, 'call': WRITE})[1]['exit_code'], 0)
        post(restored, 'stop', SCOPE)

    def test_mount_loss_at_start_keeps_failed_sandbox_unusable(self):
        cases = (
            ('findmnt absent', 'mount', completed(1), 'SSHFS mount is unavailable'),
            ('underlying directory', 'mount', completed(stdout='ext4\n'), 'SSHFS mount is unavailable'),
            ('docker run', 'start_result', completed(125), 'Cannot start mounted sandbox'),
            ('in-container probe', 'ready', completed(1), 'Sandbox did not confirm'),
        )
        for index, (label, field, value, message) in enumerate(cases):
            with self.subTest(label=label):
                executor = self.executor(self.root / str(index))
                self.provision(executor)
                with patch.object(self.system, field, value):
                    with self.assertRaisesRegex(RuntimeError, message):
                        post(executor, 'start', SCOPE)
                    with self.assertRaisesRegex(Conflict, 'inactive'):
                        post(executor, 'exec', INTENT)
                    self.assertEqual(post(executor, 'stop', SCOPE)[1]['cleanup_state'], 'confirmed')
                self.assertFalse(self.system.running)
        self.assertEqual(self.system.tools, [])

    def test_command_mount_failure_ignores_partial_worker_stdout_and_blocks_replay(self):
        executor = self.started()
        self.system.tool = lambda argv: completed(1, tool_success(argv).stdout, 'Transport endpoint is not connected')
        with self.assertRaisesRegex(RuntimeError, 'No confirmed worker result'):
            post(executor, 'exec', INTENT)
        command = next(call for call in self.system.calls if call[:2] == ('exec', '-i'))
        self.assertTrue(command[-1].startswith(MOUNT_CHECK))
        self.assertTrue(command[-1].endswith('\nmain()\nos.stat("/work")\n'))
        self.assertEqual(self.system.tools, [['/bin/sh', '-c', 'printf mounted']])
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'operation', LOOKUP)
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'exec', INTENT)
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'exec', {**INTENT, 'op_id': 'new-op'})
        self.assertEqual(len(self.system.tools), 1)

    def test_known_result_is_durable_and_duplicate_exec_returns_it(self):
        executor = self.started()
        expected = post(executor, 'exec', INTENT)
        self.assertEqual(post(executor, 'exec', INTENT), expected)
        with self.assertRaisesRegex(Conflict, 'different payload'):
            post(executor, 'exec', {**INTENT, 'call': WRITE})
        restored = self.executor()
        restored.recover()
        self.assertEqual(post(restored, 'operation', LOOKUP), expected)
        self.assertEqual(post(restored, 'exec', INTENT), expected)
        self.assertEqual(len(self.system.tools), 1)

    def test_crash_after_dispatch_reconciles_container_and_does_not_replay(self):
        root = self.root / 'crashed'
        child = RUN_PROCESS(
            [sys.executable, '-c',
             'from tests.test_mounted_failures import crash_after_dispatch; import sys; crash_after_dispatch(sys.argv[1])',
             str(root)], capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(child.returncode, 73, child.stderr)
        effect = root / 'side-effect'
        self.assertEqual(effect.read_bytes(), b'dispatched\n')
        executor = self.executor(root)
        session = executor.sessions['session']
        self.assertEqual(session['active'], 'run')
        self.assertEqual(session['operations'], {'op': {'request': INTENT, 'result': None}})
        self.system.running.add(NAME)
        # An old journal also has active intent and no process-local readiness.
        with self.assertRaisesRegex(Conflict, 'inactive'):
            post(executor, 'exec', {**INTENT, 'op_id': 'another'})
        executor.recover()
        self.assertFalse(self.system.running)
        self.assertEqual(executor.store.journals['session'].load()['stopped'], ['run'])
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'operation', LOOKUP)
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'exec', INTENT)
        with self.assertRaisesRegex(Conflict, 'Cannot start run'):
            post(executor, 'start', SCOPE)
        self.assertEqual(effect.read_bytes(), b'dispatched\n')
        self.assertEqual(self.system.tools, [])

    def test_stop_release_and_recovery_refuse_to_confirm_failed_removal(self):
        executor = self.started()
        self.system.removal_fails = True
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            post(executor, 'stop', SCOPE)
        with self.assertRaisesRegex(Conflict, 'inactive'):
            post(executor, 'exec', INTENT)
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            post(executor, 'release', {})
        saved = executor.store.journals['session'].load()
        self.assertEqual(saved['active'], 'run')
        self.assertEqual(saved['stopped'], [])
        self.assertFalse(saved['closed'])

        restored = self.executor()
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            restored.recover()
        self.system.listing_fails = True
        with self.assertRaisesRegex(RuntimeError, 'Cannot reconcile'):
            restored.recover()
        self.assertEqual(restored.store.journals['session'].load(), saved)
        # An rm error plus a failed absence check must also fail closed.
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            post(restored, 'stop', SCOPE)
        self.system.removal_fails = self.system.listing_fails = False
        restored.recover()
        self.assertEqual(post(restored, 'stop', SCOPE)[1]['cleanup_state'], 'confirmed')
        self.assertFalse(self.system.running)

    def test_collect_removes_container_on_failure_and_persists_unconfirmed_cleanup(self):
        executor = self.started()
        post(executor, 'exec', {**INTENT, 'call': WRITE})
        post(executor, 'stop', SCOPE)
        collector = NAME + '-collect'
        self.system.collect_result = completed(1)
        with self.assertRaisesRegex(RuntimeError, 'Cannot collect'):
            post(executor, 'collect', SCOPE)
        self.assertIn(('rm', '-f', collector), self.system.calls)
        self.assertFalse(self.system.running)
        self.assertNotIn('collector', executor.store.journals['session'].load())

        self.system.removal_fails = True
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            post(executor, 'collect', SCOPE)
        self.assertEqual(executor.store.journals['session'].load().get('collector'), collector)
        starts = [call for call in self.system.calls if call[0] == 'run']
        for action, body in (('collect', SCOPE), ('start', {**SCOPE, 'run_id': 'new'}), ('release', {})):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, 'removal failed'):
                post(executor, action, body)
        self.assertEqual([call for call in self.system.calls if call[0] == 'run'], starts)
        restored = self.executor()
        with self.assertRaisesRegex(RuntimeError, 'removal failed'):
            restored.recover()
        self.assertEqual(restored.store.journals['session'].load()['collector'], collector)
        self.system.removal_fails = False
        restored.recover()
        self.assertFalse(self.system.running)
        self.assertNotIn('collector', restored.store.journals['session'].load())

    def test_startup_with_lost_mount_keeps_recovery_operations_available(self):
        executor = self.started()
        self.system.tool = lambda argv: completed(1)
        with self.assertRaisesRegex(RuntimeError, 'No confirmed worker result'):
            post(executor, 'exec', INTENT)
        self.system.mount = completed(1)
        served = []

        def serve_recovered(backend, port):
            served.append(port)
            self.addCleanup(backend.containers.journal.db.close)
            self.addCleanup(self.close_journals, backend)
            self.assertFalse(self.system.running)
            self.assertEqual(backend.health(), (503, {
                'status': 'mount_unavailable', 'backend': 'mounted', 'client_mount': False,
            }))
            with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
                post(backend, 'operation', LOOKUP)
            self.assertEqual(post(backend, 'stop', SCOPE)[1]['cleanup_state'], 'confirmed')
            with self.assertRaisesRegex(RuntimeError, 'SSHFS mount is unavailable'):
                post(backend, 'start', {**SCOPE, 'run_id': 'next-run'})
            self.assertEqual(post(backend, 'release', {})[1]['cleanup_state'], 'confirmed')
            self.assertEqual(len(self.system.tools), 1)

        environment = {
            'DOCKER_HOST': 'unix:///var/run/docker.sock', 'SSHFS_MOUNT': '/mnt/test-export',
            'MOUNT_GRANT': 'test-grant', 'RUNTIME_SCOPE': 'test', 'SANDBOX_IMAGE': 'sandbox',
            'STATE_DIR': str(self.root / 'executor'), 'PORT': '8081',
        }
        with patch.dict(os.environ, environment, clear=True), patch(
                'track_a.mounted_executor.serve', side_effect=serve_recovered):
            main()
        self.assertEqual(served, [8081])

    def test_collect_timeout_and_mount_failure_attempt_cleanup(self):
        executor = self.started()
        post(executor, 'exec', {**INTENT, 'call': WRITE})
        post(executor, 'stop', SCOPE)
        collector = NAME + '-collect'
        original = self.system.run

        def timeout_after_launch(args, **kwargs):
            result = original(args, **kwargs)
            if args[:3] == ['docker', 'run', '-i']:
                self.assertEqual(executor.store.journals['session'].load()['collector'], collector)
                raise subprocess.TimeoutExpired(args, 6)
            return result

        with patch('subprocess.run', side_effect=timeout_after_launch):
            with self.assertRaises(subprocess.TimeoutExpired):
                post(executor, 'collect', SCOPE)
        self.assertFalse(self.system.running)
        self.assertNotIn('collector', executor.store.journals['session'].load())
        self.system.mount = completed(1)
        with self.assertRaisesRegex(RuntimeError, 'SSHFS mount is unavailable'):
            post(executor, 'collect', SCOPE)
        self.assertEqual(self.system.calls[-2][0], 'rm')
        self.assertEqual(self.system.calls[-1][0], 'ps')
        self.assertNotIn('collector', executor.store.journals['session'].load())

    def test_release_purges_payload_and_remains_idempotent_after_restart(self):
        executor = self.started()
        post(executor, 'exec', {**INTENT, 'call': WRITE})
        journal = self.root / 'executor' / 'session.sqlite'
        self.assertIn(PAYLOAD.encode(), journal.read_bytes())
        release = (200, {'cleanup_state': 'confirmed'})
        self.assertEqual(post(executor, 'release', {}), release)
        self.assertEqual(post(executor, 'release', {}), release)
        self.assertFalse(self.system.running)
        self.assertNotIn(PAYLOAD.encode(), journal.read_bytes())

        restored = self.executor()
        restored.recover()
        self.assertEqual(restored.sessions['session'], {'id': 'session', 'generation': 1, 'closed': True})
        self.assertEqual(post(restored, 'release', {}), release)
        self.assertEqual(post(restored, 'operation', LOOKUP), (410, {'error': 'released'}))
        self.assertEqual(post(restored, 'exec', INTENT), (410, {'error': 'released'}))

    def test_no_late_result_after_stop(self):
        self.check_late_result_after_stop(removal_fails=False)

    def test_no_late_result_after_failed_stop(self):
        self.check_late_result_after_stop(removal_fails=True)

    def check_late_result_after_stop(self, *, removal_fails):
        executor = self.started()
        entered, resume = threading.Event(), threading.Event()
        errors = []

        def blocked_tool(argv):
            entered.set()
            if not resume.wait(2):
                raise AssertionError('Test never unblocked Docker')
            return tool_success(argv)

        def execute():
            try:
                post(executor, 'exec', INTENT)
            except Exception as exc:
                errors.append(exc)

        self.system.tool = blocked_tool
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.system.removal_fails = removal_fails
            if removal_fails:
                with self.assertRaisesRegex(RuntimeError, 'removal failed'):
                    post(executor, 'stop', SCOPE)
            else:
                self.assertEqual(post(executor, 'stop', SCOPE)[1]['cleanup_state'], 'confirmed')
        finally:
            resume.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([str(exc) for exc in errors], ['Run stopped during execution'])
        self.assertIsInstance(errors[0], Conflict)
        self.assertIsNone(executor.store.journals['session'].load()['operations']['op']['result'])
        with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
            post(executor, 'operation', LOOKUP)
        self.system.removal_fails = False
        post(executor, 'stop', SCOPE)

    def application(self):
        executor = self.executor()
        transport = InProcessClient(executor)
        proxy = InProcessClient(ModelProxy(SimpleNamespace(backend='mock')))
        app = Application(transport, proxy, {'owner': 'test-token'},
                          self.root / 'app', mount_owner='owner')
        self.addCleanup(self.close_journals, app)
        _, session = app.route('owner', 'POST', '/v2/sessions',
                               {'request_id': 'session', 'grant': 'test-grant'}, {})
        return app, executor, transport, f"/v2/sessions/{session['id']}"

    def run_app(self, app, path):
        body = {'request_id': 'run', 'message': 'Use the mock tools'}
        _, run = app.route('owner', 'POST', path + '/runs', body, {})
        run_path = path + '/runs/' + run['id']
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, run = app.route('owner', 'GET', run_path, {}, {})
            if run['result'] is not None:
                return run_path, run
            time.sleep(.01)
        self.fail('Application did not reach a terminal result')

    def test_application_recovers_known_lost_replies_by_lookup_only(self):
        app, executor, transport, path = self.application()
        transport.drop_exec_reply = True
        _, run = self.run_app(app, path)
        self.assertEqual((run['status'], run['cleanup_state']), ('completed', 'confirmed'))
        wire_ops = [(action, body['op_id']) for action, body in transport.requests
                    if action in {'exec', 'operation'}]
        self.assertEqual(len(wire_ops), 4)
        self.assertEqual([action for action, _ in wire_ops], ['exec', 'operation', 'exec', 'operation'])
        self.assertEqual(wire_ops[0][1], wire_ops[1][1])
        self.assertEqual(wire_ops[2][1], wire_ops[3][1])
        self.assertNotEqual(wire_ops[0][1], wire_ops[2][1])
        self.assertEqual(len(self.system.tools), 2)
        self.assertFalse(self.system.running)
        session = executor.sessions[run['session_id']]
        self.assertEqual(set(session['operations']), {op for _, op in wire_ops})

    def test_application_interrupts_unknown_operation_and_quarantines_unconfirmed_stop(self):
        app, _, transport, path = self.application()
        self.system.tool = lambda argv: completed(1)
        self.system.removal_fails = True
        run_path, run = self.run_app(app, path)
        self.assertEqual(run['result'], dict(status='interrupted', reason='cleanup_unconfirmed',
                                             answer=None, usage=None, cleanup_state='quarantined'))
        actions = [action for action, _ in transport.requests]
        self.assertEqual(actions, ['provision', 'start', 'exec', 'operation', 'stop'])
        self.assertEqual(transport.requests[2][1]['op_id'], transport.requests[3][1]['op_id'])
        self.assertEqual(len(self.system.tools), 1)
        _, events = app.route('owner', 'GET', path + '/events', {}, {})
        self.assertEqual([event['type'] for event in events].count('run.result'), 1)
        self.assertFalse(any(event['type'] == 'tool.result' for event in events))
        with self.assertRaisesRegex(Conflict, 'unavailable'):
            app.route('owner', 'POST', path + '/runs', {'request_id': 'another', 'message': 'retry'}, {})
        with self.assertRaisesRegex(Conflict, 'cleanup unconfirmed'):
            app.route('owner', 'POST', run_path + '/collect', {}, {})
        with self.assertRaises(RunError):
            app.route('owner', 'POST', path + '/close', {}, {})
        self.system.removal_fails = False
        _, closed = app.route('owner', 'POST', path + '/close', {}, {})
        self.assertEqual(closed['status'], 'closed')
        self.assertFalse(self.system.running)
        self.assertEqual(len(self.system.tools), 1)

    def test_application_interrupts_unknown_operation_after_confirmed_cleanup(self):
        app, _, transport, path = self.application()
        self.system.tool = lambda argv: completed(1)
        _, run = self.run_app(app, path)
        self.assertEqual(run['result']['status'], 'interrupted')
        self.assertEqual(run['result']['reason'], 'outcome_unknown')
        self.assertEqual(run['cleanup_state'], 'confirmed')
        self.assertEqual([action for action, _ in transport.requests],
                         ['provision', 'start', 'exec', 'operation', 'stop'])
        self.assertEqual(len(self.system.tools), 1)
        self.assertFalse(self.system.running)


class LiveFailureRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'prototypes/mounted-runtime/live_failure.py'
        cls.runner = SimpleNamespace(**runpy.run_path(str(path)))

    def test_preflight_blocks_restricted_execution_before_commands(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                Path, 'read_text', return_value='NoNewPrivs:\t1\n'), patch(
                'subprocess.run') as command:
            with self.assertRaisesRegex(RuntimeError, 'NoNewPrivs'):
                self.runner.preflight()
        command.assert_not_called()

    def test_preflight_rejects_alternative_docker_routing(self):
        for environment in ({'DOCKER_CONTEXT': 'another'}, {'DOCKER_HOST': 'tcp://elsewhere:2375'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                with patch('subprocess.run') as command:
                    with self.assertRaisesRegex(RuntimeError, 'Preflight'):
                        self.runner.preflight()
                command.assert_not_called()

    def test_io_evidence_requires_post_cut_io_failure(self):
        evidence = self.runner.io_evidence({'stdout': 'IO_BEGIN\nIO_ERRNO=107\n', 'error_kind': 'command'})
        self.assertEqual(evidence, {'errno': 107, 'io_timed_out': False})
        self.assertTrue(self.runner.io_evidence(
            {'stdout': 'IO_BEGIN\n', 'error_kind': 'timeout'})['io_timed_out'])
        for worker in (
                {'stdout': '', 'error_kind': 'timeout'},
                {'stdout': 'IO_BEGIN\nIO_END\n', 'error_kind': 'timeout'},
                {'stdout': 'IO_BEGIN\n', 'stderr': 'HANDSHAKE_TIMEOUT', 'error_kind': 'timeout'}):
            with self.subTest(worker=worker), self.assertRaisesRegex(RuntimeError, 'Inconclusive'):
                self.runner.io_evidence(worker)

    def test_refused_scope_or_mount_acquisition_never_runs_cleanup_commands(self):
        for mounts, containers in (([None, None], ['existing-container']), ([None, 'fuse.sshfs'], [])):
            with self.subTest(mounts=mounts), tempfile.TemporaryDirectory() as root:
                probe = self.runner.Probe.__new__(self.runner.Probe)
                probe.root = Path(root)
                probe.bind_attempted = probe.stack_attempted = False
                probe.mount_type = Mock(side_effect=mounts)
                probe.containers = Mock(return_value=containers)
                probe.command = Mock()
                with self.assertRaisesRegex(RuntimeError, 'already mounted|existing project'):
                    probe.exercise()
                self.assertEqual(probe.cleanup(), {'status': 'not_acquired'})
                probe.command.assert_not_called()

    def test_prepare_detaches_the_owned_self_bind_before_making_it_shared(self):
        probe = self.runner.Probe.__new__(self.runner.Probe)
        probe.root, probe.scope = Path('/tmp/synthetic-probe'), 'test-scope'
        probe.mount_type = Mock(return_value=None)
        probe.containers = Mock(return_value=[])
        probe.command = Mock(return_value=completed(stdout=''))
        probe.privileged = Mock()
        probe.remember_processes = Mock()

        probe.setup()

        mount = probe.root / 'mount'
        self.assertEqual([call.args for call in probe.privileged.call_args_list],
                         [('mount', '--bind', mount, mount), ('mount', '--make-private', mount),
                          ('mount', '--make-rshared', mount)])
        self.assertTrue(probe.bind_attempted and probe.stack_attempted)

    def test_diagnostics_failure_still_attempts_owned_stack_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            probe = self.runner.Probe.__new__(self.runner.Probe)
            probe.root, probe.scope = Path(root), 'test-scope'
            probe.bind_attempted = probe.stack_attempted = True
            probe.identities, probe.compose = {}, ['compose']
            probe.command = Mock(side_effect=[
                subprocess.TimeoutExpired('logs', 6), completed(), completed(stdout='')])
            probe.containers = Mock(return_value=[])
            probe.mount_type = Mock(return_value=None)
            self.assertEqual(probe.cleanup()['stack_cleanup_exit'], 0)
            self.assertEqual(probe.command.call_args_list[1].args[0], [self.runner.STACK, 'cleanup'])


if __name__ == '__main__':
    unittest.main()
