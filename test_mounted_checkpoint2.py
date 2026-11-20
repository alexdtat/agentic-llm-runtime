"""Offline tests for the checkpoint operator rehearsal and evidence gates."""
import errno
import json
import runpy
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_mounted_failures import FakeSystem, InProcessClient, completed
from track_a.application import Application
from track_a.contracts import tool_call

SCRIPT = Path(__file__).parent / 'prototypes/mounted-runtime/live_checkpoint2.py'
with patch.object(sys, 'path', [str(SCRIPT.parent), *sys.path]):
    runner = SimpleNamespace(**runpy.run_path(str(SCRIPT)))


class LocalApplicationClient:
    """The same v2 routes without a listening socket."""
    def __init__(self, app):
        self.app = app

    def v2(self, method, path, body=None, timeout=15):
        status, response = self.app.route('local', method, '/v2/sessions' + path, body, {})
        if status != 200:
            raise RuntimeError(response)
        return response

    def wait_run(self, sid, rid, timeout):
        return self.v2('GET', f'/{sid}/runs/{rid}')


class CheckpointTests(unittest.TestCase):
    def test_mock_commands_fit_mounted_tool_contract(self):
        proxy = runner.ScenarioProxy()
        scripts = (runner.TIMEOUT, 'PORT = 12345\n' + runner.NETWORK, runner.CPU, runner.PIDS,
                   runner.MEMORY, runner.HANG)
        for name, body in zip(runner.CASES, scripts, strict=True):
            with self.subTest(name=name):
                proxy.scripts[name] = body
                status, reply = proxy.route('POST', '/generate',
                                            {'messages': [{'role': 'user', 'content': name}]})
                self.assertEqual(status, 200)
                call, = reply['action']['calls']
                tool_call(call)
                self.assertEqual(shlex.split(call['args']['command']), ['exec', 'python3', '-c', body])
                compile(body, name, 'exec')

    def test_timeout_rehearsal_uses_real_application_and_executor_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'client').mkdir()
            probe = runner.CheckpointProbe.__new__(runner.CheckpointProbe)
            probe.root, probe.scope = root, 'test'
            probe.report = {'timeout': {'cleanup': {'status': 'not_acquired'}}}
            probe.executor = runner.mounted_executor.MountedExecutor(
                '/fake/sshfs', 'checkpoint2', 'test', 'sandbox', root / 'executor')
            probe.backend = InProcessClient(probe.executor)
            probe.proxy = runner.ScenarioProxy()
            probe.workers = {}
            app = Application(probe.backend, InProcessClient(probe.proxy), {'local': 'token'},
                              root / 'app', mount_owner='local')
            probe.api = LocalApplicationClient(app)
            self.addCleanup(probe.executor.containers.journal.db.close)
            def close_journals():
                for store in (app.store, probe.executor.store):
                    for journal in store.journals.values():
                        journal.db.close()
            self.addCleanup(close_journals)
            system = FakeSystem()

            def fake_worker(argv):
                self.assertEqual(argv[:2], ['/bin/sh', '-c'])
                with (root / 'client/checkpoint2-timeout.txt').open('ab') as marker:
                    marker.write(b't08-once\n')
                raw = json.dumps(dict(status='timed_out', error_kind='timeout',
                                      stdout='MARKER_SYNCED\n', stderr='', exit_code=None, truncated=False))
                probe.workers[probe.executor.ready] = [{'returncode': 0, 'stdout': raw}]
                return completed(stdout=raw)

            system.tool = fake_worker
            probe.command_pid = Mock(return_value='123')
            probe.command = Mock(side_effect=lambda args: completed(stdout='\n'.join(system.running)))
            # Run Application's asynchronous target synchronously, keeping its exception path intact.
            class ImmediateThread:
                def __init__(self, target, args, **kwargs):
                    self.target, self.args = target, args

                def start(self):
                    self.target(*self.args)

            with patch('subprocess.run', side_effect=system.run), patch(
                    'track_a.application.threading.Thread', ImmediateThread):
                probe.run_case('timeout', probe.check_timeout)

            row = json.loads((root / 'checkpoint2-scenarios.json').read_text())['timeout']
            self.assertEqual(row['status'], 'passed', row)
            self.assertEqual(row['run_result']['reason'], 'outcome_unknown')
            self.assertEqual(set(row['replay_http'].values()), {409})
            self.assertEqual(row['cleanup']['status'], 'confirmed')
            self.assertEqual(len(system.tools), 1)
            self.assertFalse(system.running)

    def test_same_namespace_or_down_proxy_cannot_pass(self):
        health = (200, {'status': 'ok', 'backend': 'mock'})
        observed = {'errno': errno.ECONNREFUSED, 'namespace': 'net:[2]', 'interfaces': ['lo']}
        requests = ['network', 'observations']
        runner.check_network(observed, 'net:[1]', health, health, requests)
        for evidence, inode, after, calls in (
                (observed, 'net:[2]', health, requests),
                (observed, 'net:[1]', (503, {}), requests),
                (observed, 'net:[1]', health, []),
                ({**observed, 'errno': 0}, 'net:[1]', health, requests)):
            with self.subTest(evidence=evidence, inode=inode, after=after, calls=calls):
                with self.assertRaises(RuntimeError):
                    runner.check_network(evidence, inode, health, after, calls)

    def test_resource_rejections_require_counter_deltas(self):
        cpu = {'nr_throttled': 0, 'throttled_usec': 0}
        memory = {'max': 0, 'oom': 0, 'oom_kill': 0}
        for name, positive, negative in (
                ('cpu', {'before': cpu, 'after': dict.fromkeys(cpu, 1)}, {'after': cpu}),
                ('pids', dict(initial=4, children=60, current=64, errno=errno.EAGAIN, before=0, after=1, reaped=4),
                 {'after': 0}),
                ('memory', dict(child_exit=-9, before=memory, after=dict.fromkeys(memory, 1)),
                 {'after': memory})):
            with self.subTest(name=name):
                runner.check_resource(name, positive)
                with self.assertRaises(RuntimeError):
                    runner.check_resource(name, {**positive, **negative})

    def test_failed_check_still_closes_its_session(self):
        probe = runner.CheckpointProbe.__new__(runner.CheckpointProbe)
        probe.report = {'cpu': {}}
        probe.save_report = Mock()
        probe.executor = SimpleNamespace(grant='checkpoint2')
        probe.api = SimpleNamespace(v2=Mock(side_effect=[{'id': 'sid'}, {'status': 'closed'}]))
        probe.absence = Mock(return_value={'containers': [], 'processes': []})
        probe.run_case('cpu', Mock(side_effect=RuntimeError('no throttling')))
        self.assertEqual(probe.report['cpu']['status'], 'failed')
        self.assertEqual(probe.report['cpu']['cleanup']['status'], 'confirmed')
        probe.api.v2.assert_called_with('POST', '/sid/close', {}, timeout=12)

    def test_hang_cancels_while_broker_is_paused_after_two_fuse_samples(self):
        probe = runner.CheckpointProbe.__new__(runner.CheckpointProbe)
        probe.row, probe.runner_pids = {}, {'123': 'same-starttime'}
        probe.sid, probe.rid = 'sid', 'rid'
        probe.compose_container = Mock(return_value='owned-broker')
        probe.begin = Mock()
        probe.sandbox_tmp = Mock(return_value=completed())
        probe.command_pid = Mock(return_value='123')
        probe.command = Mock(side_effect=[completed(), completed(stdout='request_wait_answer'),
                                          completed(stdout='request_wait_answer'), completed()])
        probe.broker_state = Mock(side_effect=['true false', 'true true', 'true false'])
        def cancel(*args, **kwargs):
            self.assertEqual(probe.command.call_count, 3)
            self.assertEqual(probe.broker_to_resume, 'owned-broker')
            return {'result': {'status': 'cancelled'}}

        probe.api = SimpleNamespace(v2=Mock(side_effect=cancel))
        probe.result = Mock()

        with patch('live_failure.time.sleep'), patch.dict(
                probe.wait_for_fuse.__globals__, {'process_identity': lambda pid: 'same-starttime'}):
            probe.check_sshfs_hang()

        probe.api.v2.assert_called_once_with('POST', '/sid/runs/rid/cancel', {}, timeout=4)
        probe.result.assert_called_once_with('cancelled', 'requested', timeout=1)
        self.assertEqual(probe.row['fuse_wchan_samples'], ['request_wait_answer'] * 2)
        self.assertEqual([call.args[0][-2:] for call in probe.command.call_args_list],
                         [['pause', 'owned-broker'], ['cat', '/proc/123/wchan'],
                          ['cat', '/proc/123/wchan'], ['unpause', 'owned-broker']])

    def test_paused_broker_is_resumed_when_fuse_cannot_be_observed(self):
        probe = runner.CheckpointProbe.__new__(runner.CheckpointProbe)
        probe.row = {}
        probe.command = Mock(return_value=completed())
        probe.broker_state = Mock(side_effect=['true false', 'true true', 'true false'])
        with self.assertRaises(runner.Blocked):
            with probe.paused_broker('owned-broker'):
                raise runner.Blocked('wchan unavailable')
        self.assertEqual([call.args[0][-2:] for call in probe.command.call_args_list],
                         [['pause', 'owned-broker'], ['unpause', 'owned-broker']])
        self.assertEqual(probe.row['broker_state_after'], 'true false')
        self.assertIsNone(probe.broker_to_resume)


if __name__ == '__main__':
    unittest.main()
