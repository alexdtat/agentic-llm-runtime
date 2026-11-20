"""Operator-only mounted behavior checks on one fresh, synthetic Linux stack."""
import contextlib
import errno
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from live_failure import LOCAL, Probe, ProbeDeadline, process_identity, require, run_probe, service
from checkpoint2_commands import CPU, HANG, MEMORY, NETWORK, PIDS, TIMEOUT
from track_a import mounted_executor
from track_a.application import Application
from track_a.model_proxy import ModelProxy


CASES = ('timeout', 'network', 'cpu', 'pids', 'memory', 'sshfs_hang')


class Blocked(RuntimeError):
    pass


class ScenarioProxy(ModelProxy):
    def __init__(self):
        super().__init__(SimpleNamespace(backend='mock'))
        self.scripts = {}
        self.requests = []

    def mock(self, messages):
        message = messages[-1]['content']
        self.requests.append(message)
        if message.startswith('Tool observations: '):
            return ModelProxy.mock(messages)
        return {'calls': [{'id': message, 'tool': 'RunCommand', 'args': {
            'command': 'exec python3 -c ' + shlex.quote(self.scripts[message]),
        }}]}


def check_network(observed, host_namespace, before, after, mock_requests):
    health = (200, {'status': 'ok', 'backend': 'mock'})
    require(before == after == health, 'Mock health check failed')
    require(mock_requests == ['network', 'observations'], 'Application model round trip missing')
    namespace = observed['namespace']
    require(namespace.startswith('net:[') and namespace != host_namespace,
            'Sandbox did not report a separate network namespace')
    require(observed['interfaces'] == ['lo'] and observed['errno'] == errno.ECONNREFUSED,
            'Expected TCP refusal on the isolated loopback interface')


def check_resource(name, observed):
    if name == 'cpu':
        require(all(observed['after'][key] > observed['before'][key]
                    for key in ('nr_throttled', 'throttled_usec')), 'CPU throttling counters did not advance')
    elif name == 'pids':
        require(observed['current'] == 64 and observed['errno'] == errno.EAGAIN and
                observed['initial'] + observed['children'] == observed['current'] and
                observed['after'] > observed['before'], 'Missing cgroup PID limit refusal')
        require(observed['reaped'] == observed['initial'], 'PID usage did not return to baseline')
    else:
        require(name == 'memory', 'Unknown resource')
        require(observed['child_exit'] == -9 and
                all(observed['after'][key] > observed['before'][key]
                    for key in ('max', 'oom', 'oom_kill')), 'Missing cgroup OOM kill')


class CheckpointProbe(Probe):
    def save_report(self):
        path = self.root / 'checkpoint2-scenarios.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.report, indent=2) + '\n')
        temporary.replace(path)

    def exercise(self):
        self.report = {name: {'status': 'not_run', 'cleanup': {'status': 'not_acquired'}} for name in CASES}
        self.save_report()
        self.setup()
        (self.root / 'client/checkpoint2-cold-read.txt').write_text('synthetic\n')
        # Keep loopback RPCs local even with proxy settings inherited from the operator.
        os.environ['no_proxy'] = '127.0.0.1'
        owner = secrets.token_hex(24)
        self.executor = mounted_executor.MountedExecutor(
            str(self.root / 'mount/work'), 'checkpoint2', self.scope,
            'agentic-sandbox:cpu-v1', self.root / 'checkpoint2-execution')
        self.proxy = ScenarioProxy()
        self.workers = {}
        original = mounted_executor.docker

        def observe(*args, **kwargs):
            if args[:2] != ('exec', '-i'):
                return original(*args, **kwargs)
            history = self.workers.setdefault(args[2], [])
            observation = {'status': 'dispatched'}
            history.append(observation)
            started = time.monotonic()
            try:
                result = original(*args, **kwargs)
                observation.update(status='returned', returncode=result.returncode,
                                   stdout=result.stdout, stderr=result.stderr)
                return result
            except subprocess.TimeoutExpired:
                observation.update(status='docker_exec_timeout')
                raise
            finally:
                observation['seconds'] = round(time.monotonic() - started, 3)
                self.log.write(json.dumps({'runner': args[2], 'worker': observation}) + '\n')
                self.log.flush()

        mounted_executor.docker = observe
        try:
            with contextlib.ExitStack() as services:
                self.backend = services.enter_context(service(self.executor, secrets.token_hex(24)))
                self.model = services.enter_context(service(self.proxy, secrets.token_hex(24)))
                app = Application(self.backend, self.model, {'local': owner},
                                  self.root / 'checkpoint2-app', mount_owner='local')
                self.api = services.enter_context(service(app, owner))
                for name in CASES:
                    self.run_case(name, getattr(self, 'check_' + name))
        finally:
            mounted_executor.docker = original
        passed = all(row['status'] == 'passed' and row['cleanup']['status'] == 'confirmed'
                     for row in self.report.values())
        return {'status': 'passed' if passed else 'failed', 'scenarios': self.report}

    def run_case(self, name, check):
        self.row = self.report[name]
        self.sid = self.rid = self.runner = None
        self.runner_pids = {}
        self.name = name
        self.row.update(status='running', cleanup={'status': 'pending'})
        self.save_report()
        try:
            self.sid = self.api.v2('POST', '', {'request_id': name, 'grant': self.executor.grant})['id']
            self.row['session'] = self.sid
            check()
            self.row['status'] = 'passed'
        except (KeyboardInterrupt, ProbeDeadline) as error:
            self.row.update(status='aborted', error=type(error).__name__)
            raise
        except Blocked as error:
            self.row.update(status='blocked', error=str(error))
        except Exception as error:
            self.row.update(status='failed', error=f'{type(error).__name__}: {error}')
        finally:
            try:
                if self.sid is not None:
                    closed = self.api.v2('POST', f'/{self.sid}/close', {}, timeout=12)
                    require(closed['status'] == 'closed', 'Session close was not confirmed')
                    self.row['cleanup'] = {'status': 'confirmed', 'session': 'closed', **self.absence()}
                else:
                    self.row['cleanup'] = {'status': 'unconfirmed', 'error': 'No session ID returned by provision'}
            except Exception as error:
                self.row['cleanup'] = {'status': 'failed', 'error': str(error)}
            finally:
                if self.runner:
                    self.row['workers'] = self.workers.get(self.runner, [])
                self.save_report()

    def begin(self, script):
        self.proxy.scripts[self.name] = script
        run = self.api.v2('POST', f'/{self.sid}/runs', {'request_id': self.name, 'message': self.name})
        self.rid = run['id']
        self.runner = f'agentic-mounted-{self.sid}-{self.rid}'
        self.row.update(run=self.rid, runner=self.runner)

    def absence(self):
        containers = self.command([*LOCAL, 'ps', '-aq', '--filter',
                                   'label=agentic.scope=mounted-' + self.scope]).stdout.split()
        processes = [pid for pid, identity in self.runner_pids.items() if process_identity(pid) == identity]
        evidence = {'containers': containers, 'processes': processes}
        require(not containers and not processes, f'Run resources remain: {evidence}')
        return evidence

    def result(self, status, reason=None, *, timeout=12):
        result = self.api.wait_run(self.sid, self.rid, timeout)['result']
        self.row['run_result'] = result
        require((result['status'], result['reason']) == (status, reason), 'Unexpected Application result')
        require(result['cleanup_state'] == 'confirmed', 'Application stop was not confirmed')
        self.row['product_stop'] = self.absence()

    def command_pid(self, script):
        self.runner_pids = self.remember_container(self.runner)
        for pid in self.runner_pids:
            try:
                args = Path('/proc', pid, 'cmdline').read_bytes().split(b'\0')
            except FileNotFoundError:
                continue
            if args == [b'python3', b'-c', script.encode(), b'']:
                return pid
        raise RuntimeError('Command process was not observed in the run container')

    def check_timeout(self):
        self.begin(TIMEOUT)
        marker = self.root / 'client/checkpoint2-timeout.txt'
        deadline = time.monotonic() + 4
        while not marker.exists() or not marker.stat().st_size:
            require(time.monotonic() < deadline, 'Timed command never wrote its marker')
            time.sleep(.03)
        self.row['command_pid'] = self.command_pid(TIMEOUT)
        self.result('interrupted', 'outcome_unknown')

        operations = self.executor.store.journals[self.sid].load()['operations']
        require(len(operations) == 1, 'Expected exactly one durable operation')
        op_id, operation = next(iter(operations.items()))
        require(operation['result'] is None, 'Operation unexpectedly has a confirmed result')
        base = f'/mounted/{self.sid}/'
        request = operation['request']
        replay = {}
        for label, path, body in (
            ('lookup', 'operation', {'generation': request['generation'], 'op_id': op_id}),
            ('same_operation', 'exec', request),
            ('new_operation', 'exec', {**request, 'op_id': 'checkpoint2-retry'}),
        ):
            replay[label] = self.backend.request('POST', base + path, body, timeout=3)[0]
        self.row['replay_http'] = replay
        require(set(replay.values()) == {409}, 'Unknown operation was resolved or accepted for replay')

        history = self.workers.get(self.runner, [])
        require(len(history) == 1 and history[0].get('returncode') == 0,
                'Expected exactly one worker dispatch with a worker response')
        worker = json.loads(history[0]['stdout'])
        require(worker['status'] == 'timed_out' and worker['error_kind'] == 'timeout' and
                worker['stdout'] == 'MARKER_SYNCED\n', 'Worker did not time out after syncing the marker')
        self.row['marker_bytes'] = marker.stat().st_size
        require(marker.read_bytes() == b't08-once\n', 'Marker contents differ from a single append')

    def tool_output(self):
        status, events = self.api.events(self.sid, 0)
        require(status == 200, 'Cannot read Application events')
        results = [event['payload']['result'] for event in events
                   if event['type'] == 'tool.result' and event['run_id'] == self.rid]
        require(len(results) == 1, 'Expected exactly one Application tool result')
        result = results[0]
        self.row['tool_result'] = result
        require(result['exit_code'] == 0 and result['truncated'] is False and
                result['error_kind'] is None, 'Synthetic command failed or output was truncated')
        return [json.loads(line) for line in result['stdout'].splitlines()]

    def check_network(self):
        host_namespace = os.readlink('/proc/self/ns/net')
        port = urlsplit(self.model.base).port
        request_offset = len(self.proxy.requests)
        before = self.model.request('GET', '/v0/health', timeout=2)
        self.begin(f'PORT = {port}\n' + NETWORK)
        self.result('completed')
        observed, = self.tool_output()
        after = self.model.request('GET', '/v0/health', timeout=2)
        requests = ['observations' if message.startswith('Tool observations: ') else message
                    for message in self.proxy.requests[request_offset:]]
        self.row['network'] = dict(address=self.model.base, host_namespace=host_namespace,
                                   sandbox=observed, health_before=before, health_after=after,
                                   mock_requests=requests)
        check_network(observed, host_namespace, before, after, requests)

    def check_resource_run(self, script):
        self.begin(script)
        self.result('completed')
        limits, observed = self.tool_output()
        self.row.update(cgroup_limits=limits, counters=observed)
        check_resource(self.name, observed)

    def check_cpu(self):
        self.check_resource_run(CPU)

    def check_pids(self):
        self.check_resource_run(PIDS)

    def check_memory(self):
        self.check_resource_run(MEMORY)

    def sandbox_tmp(self, *args, check=True):
        return self.command([*LOCAL, 'exec', '--workdir', '/tmp', self.runner, *args], seconds=2, check=check)

    def broker_state(self, broker):
        return self.command([*LOCAL, 'inspect', broker, '--format',
                             '{{.State.Running}} {{.State.Paused}}'], seconds=3).stdout.strip()

    def resume_broker(self, broker):
        self.command([*LOCAL, 'unpause', broker], seconds=3, check=False)
        state = self.broker_state(broker)
        self.row['broker_state_after'] = state
        require(state == 'true false', 'SSHFS broker resume was not confirmed')
        self.broker_to_resume = None

    @contextlib.contextmanager
    def paused_broker(self, broker):
        require(self.broker_state(broker) == 'true false', 'Expected a running, unpaused SSHFS broker')
        self.broker_to_resume = broker
        try:
            try:
                self.command([*LOCAL, 'pause', broker], seconds=3)
                require(self.broker_state(broker) == 'true true', 'Docker did not confirm the pause')
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                raise Blocked('Local Docker could not pause the owned SSHFS broker') from error
            yield
        finally:
            self.resume_broker(broker)

    def cleanup(self):
        try:
            broker = getattr(self, 'broker_to_resume', None)
            if broker is not None:
                self.resume_broker(broker)
                self.save_report()
        finally:
            evidence = super().cleanup()
        return {'status': 'confirmed', **evidence}

    def wait_for_fuse(self, pid):
        deadline = time.monotonic() + 1.5
        samples = []
        self.row['fuse_wchan_samples'] = samples
        while time.monotonic() < deadline:
            require(process_identity(pid) == self.runner_pids[pid], 'I/O process exited before confirmation')
            probe = self.command(['sudo', '-n', 'cat', f'/proc/{pid}/wchan'], seconds=1, check=False)
            if probe.returncode:
                raise Blocked('Need readable /proc/<sandbox host PID>/wchan (sudo -n cat)')
            require(process_identity(pid) == self.runner_pids[pid], 'I/O process changed during observation')
            wchan = probe.stdout.strip()
            if 'fuse' in wchan or wchan in {'request_wait_answer', 'wait_answer_interruptible'}:
                samples.append(wchan)
                if len(samples) == 2:
                    return
            else:
                samples.clear()
                self.row['last_other_wchan'] = wchan
            time.sleep(.10)
        raise Blocked('Cannot observe stable FUSE wait via host /proc/<pid>/wchan; check kernel symbols and ptrace policy')

    def check_sshfs_hang(self):
        broker = self.compose_container('broker')
        self.row['broker'] = broker
        self.begin(HANG)
        deadline = time.monotonic() + 3
        while self.sandbox_tmp('test', '-f', '/tmp/t08-io-ready', check=False).returncode:
            require(time.monotonic() < deadline, 'Command did not reach the I/O barrier')
            time.sleep(.03)
        pid = self.command_pid(HANG)
        self.row['command_pid'] = pid
        with self.paused_broker(broker):
            self.sandbox_tmp('touch', '/tmp/t08-io-go')
            self.wait_for_fuse(pid)
            started = time.monotonic()
            try:
                stopped = self.api.v2('POST', f'/{self.sid}/runs/{self.rid}/cancel', {}, timeout=4)
                self.row['cancel_result'] = stopped['result']
                self.result('cancelled', 'requested', timeout=1)
            finally:
                self.row['cancel_seconds'] = round(time.monotonic() - started, 3)


def main():
    return run_probe(CheckpointProbe, 'checkpoint2', exercise_seconds=165)


if __name__ == '__main__':
    sys.exit(main())
