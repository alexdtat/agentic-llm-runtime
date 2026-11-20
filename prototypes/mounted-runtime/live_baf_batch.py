"""Run the full mounted BAF against one live local OpenAI-compatible model."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from live_b6_mock import Rehearsal
from live_failure import LOCAL, REPOSITORY, preflight, require, run
from track_a import baf_attempt, baf_grade
from track_a.baf_plan import build_plan, read_json
from track_a.model import Model, NoRedirect
from track_a.model_proxy import ModelProxy
from vision_client import local_api_key

DEFAULT_BASE = 'http://127.0.0.1:18020/v1'
MAX_CATALOG = 1024 * 1024
SOURCE_ROOTS = ('track_a', 'prototypes/mounted-runtime', 'prototypes/mount-runtime-probe')


def loopback(url):
    address = urlsplit(url)
    require(not any(ch.isspace() or ch == '\\' for ch in url) and address.scheme == 'http'
            and address.hostname and address.username is None and address.password is None
            and address.port and address.path in ('/v1', '/v1/') and not address.query and not address.fragment
            and ipaddress.ip_address(address.hostname).is_loopback, 'Use an explicit HTTP loopback /v1 endpoint')
    return url.rstrip('/')


def discover_model(base, selected):
    opener = build_opener(ProxyHandler({}), NoRedirect)
    key = os.environ.get('MODEL_API_KEY') or (local_api_key() if base == DEFAULT_BASE else '')
    request = Request(base + '/models', headers={'Authorization': 'Bearer ' + key} if key else {})
    try:
        with opener.open(request, timeout=6) as response:
            raw = response.read(MAX_CATALOG + 1)
        require(len(raw) <= MAX_CATALOG, 'Model catalog exceeds the limit')
        models = sorted({item['id'] for item in json.loads(raw)['data']})
    except HTTPError as error:
        error.close()
        raise RuntimeError(f'Local model catalog returned HTTP {error.code}') from None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError('Could not verify the local model catalog: ' + type(error).__name__) from None
    require(models and all(isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_.:/@+-]{1,256}', model)
                           and (not key or key not in model) for model in models), 'Invalid local model catalog')
    if selected is None:
        require(len(models) == 1, 'Specify --model when the local catalog lists multiple model IDs')
        selected = models[0]
    require(selected in models, 'Requested model ID is absent from the local catalog')
    return selected, key, opener


class ObservedModel(Model):
    def __init__(self, base, model, key, opener):
        self.backend, self.base, self.name, self.key, self.opener = 'openai-compatible', base, model, key, opener
        self.calls = 0
        self.failure = None

    def generate(self, messages, timeout):
        self.calls += 1
        try:
            content, usage = super().generate(messages, timeout)
            if isinstance(content, str) and self.key and self.key in content:
                raise ValueError('Provider reflected API credential')
            return content, usage
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            self.failure = {'kind': type(error).__name__}
            if isinstance(error, HTTPError):
                self.failure['http_status'] = error.code
                error.close()
            raise


class ObservedProxy(ModelProxy):
    def __init__(self, model):
        super().__init__(model)
        self.invalid_action = False

    def route(self, method, path, body):
        before = self.model.calls
        try:
            return super().route(method, path, body)
        except (ValueError, TypeError, AttributeError, KeyError):
            if self.model.calls > before and not self.model.failure:
                self.invalid_action = True
            raise


def attempt_failure(receipt, proxy):
    if proxy.model.failure:
        return 'infra_error', 'provider_request_failed'
    result = receipt.get('result') or {}
    if receipt['close'] == 'confirmed':
        terminal = result.get('cleanup_state') == 'confirmed' and receipt.get('events')
        if terminal and (receipt['outcome'] == 'fail' or
                         result.get('reason') == 'upstream_rejected' and proxy.invalid_action):
            return 'fail', 'invalid_model_action' if proxy.invalid_action else result['reason']
        if terminal and result.get('reason') == 'deadline_exceeded':
            return 'fail', 'deadline_exceeded'
        if receipt['phase'] == 'snapshot' and receipt.get('reason') in {
            'unsupported_snapshot_entry', 'snapshot_depth_limit', 'snapshot_entry_limit', 'snapshot_byte_limit',
        }:
            return 'fail', receipt['reason']
    return 'infra_error', receipt.get('reason') or 'attempt_not_collected'


def runtime_identity():
    revision = run(['git', 'rev-parse', '--verify', 'HEAD']).stdout.strip()
    require(re.fullmatch(r'[0-9a-f]{40,64}', revision), 'Cannot pin the runtime Git revision')
    paths = [REPOSITORY / 'common.py', REPOSITORY / 'vision_client.py']
    for root in SOURCE_ROOTS:
        paths.extend(path for path in (REPOSITORY / root).iterdir()
                     if path.suffix in {'.py', '.sh', '.yaml', '.conf'} and path.is_file())
    fingerprint = hashlib.sha256()
    for path in sorted(paths):
        fingerprint.update(str(path.relative_to(REPOSITORY)).encode() + b'\0')
        fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
    return revision, fingerprint.hexdigest()


@contextmanager
def batch_lock():
    path = f'/tmp/agentic-baf-batch.{os.getuid()}.lock'
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077, 'Unsafe local batch lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A BAF batch is already running for this operator') from None
        yield


class Case(Rehearsal):
    def __init__(self, evidence, log, identity, plan, attempt, model_access):
        super().__init__(evidence, log)
        self.plan = plan
        self.suite_digest = identity['suite_sha256']
        self.task_id, self.repeat = attempt['task_id'], attempt['repeat']
        self.proxy = ObservedProxy(ObservedModel(*model_access))
        self.identity = identity
        self.summary.update(mode='mounted_live_baf', task=self.task_id, repeat=self.repeat, identity=identity,
                            stages={name: self.summary['stages'][name]
                                    for name in ('setup', 'attempt', 'grade', 'cleanup')})

    def cleanup(self):
        if self.root is None:
            return 'not_acquired', 0
        root, stage = self.root, self.summary['stages']['cleanup']
        before = self.rw_graph(root, 'cleanup-before')
        require(all(active.items() <= expected.items() for active, expected in zip(before, self.mounts)),
                'Mount identities changed before cleanup')
        if self.stack_attempted:
            label = 'com.docker.compose.project=' + self.scope
            runtime_label = 'agentic.scope=mounted-' + self.scope
            runtime = self.scoped_resources(root)[runtime_label]
            require(not runtime['networks'], 'Unexpected network in the runner scope')
            for ident in runtime['containers']:
                require(re.fullmatch(r'[a-f0-9]{64}', ident), 'Unexpected runner ID')
                actual, name, scope = self.inspect_fields(
                    'container', ident, ('.Id', '.Name', '(index .Config.Labels "agentic.scope")'))
                require(actual == ident and scope == 'mounted-' + self.scope and re.fullmatch(
                    r'/agentic-(?:mounted-[a-f0-9]{32}-[a-f0-9]{32}(?:-collect)?|baf-grade-[a-f0-9]{32})', name),
                    'Unexpected runner identity')
                self.remove_container(ident)
            owned = self.verify_previous_rw_scope(root)[label]
            for ident in owned['containers']:
                self.remove_container(ident)
            current = self.verify_previous_rw_scope(root)[label]
            require(not current['containers'] and set(current['networks']) <= set(owned['networks']),
                    'Compose resources changed during cleanup')
            for ident in current['networks']:
                self.command([*LOCAL, 'network', 'rm', ident], seconds=9, check=False)
        stage['docker'] = self.verify_empty(root)
        self.finish_rw_mounts(root, before, stage, 'cleanup', residual_confirmed=self.stack_attempted)
        shutil.rmtree(root)
        return 'passed', 0

    def remove_container(self, ident):
        try:
            self.command([*LOCAL, 'container', 'rm', '-f', ident], seconds=9, check=False)
        except subprocess.TimeoutExpired:
            # The caller verifies Docker inventory before attempting an unmount.
            pass

    def execute_case(self):
        outcome = 'infra_error'
        self.save()
        try:
            _, fixture, _ = baf_grade.source_task(self.task_id)
            if self.stage('setup', lambda: self.setup_fixture(fixture)):
                if self.stage('attempt', lambda: self.attempt(task_id=self.task_id, repeat=self.repeat,
                                                             proxy=self.proxy, provider_label=self.identity['model'],
                                                             runtime_revision=self.identity['git_revision'])):
                    self.stage('grade', lambda: self.grade(task_id=self.task_id))
                    verdict = self.summary['stages']['grade']['status']
                    if verdict in ('pass', 'fail'):
                        outcome = verdict
                elif hasattr(self, 'receipt'):
                    outcome, reason = attempt_failure(self.receipt, self.proxy)
                    self.summary['reason'] = reason
                    baf_attempt.persist(self.evidence / 'grade.json', baf_grade.verdict(outcome, reason))
        finally:
            if not self.stage('cleanup', self.cleanup):
                outcome = 'infra_error'
            if outcome in ('pass', 'fail') and not self.proxy.model.calls:
                outcome = 'infra_error'
                self.summary['reason'] = 'no_live_provider_request'
            self.summary.update(status=outcome, provider_calls=self.proxy.model.calls,
                                provider_failure=self.proxy.model.failure)
            self.save()
        return outcome


def existing_case(folder, attempt, identity):
    summary = read_json(folder / 'summary.json')
    require((summary.get('task'), summary.get('repeat'), summary.get('identity')) ==
            (attempt['task_id'], attempt['repeat'], identity)
            and summary.get('mode') == 'mounted_live_baf'
            and summary.get('status') in ('pass', 'fail')
            and type(summary.get('provider_calls')) is int and summary['provider_calls'] > 0
            and summary['stages']['cleanup'].get('status') == 'passed',
            'Attempt is reserved with no confirmed result; automatic replay is disabled')
    require(read_json(folder / 'grade.json').get('outcome') == summary['status'], 'Recorded grade changed')
    return summary['status']


def run_batch(output, model_access):
    plan = build_plan()
    revision, fingerprint = runtime_identity()
    identity = {'suite_sha256': plan['suite_sha256'], 'git_revision': revision, 'runtime_sha256': fingerprint,
                'model': model_access[1], 'model_endpoint': model_access[0], 'profile': 'mounted_v2_live'}
    artifacts = {'plan.json': plan, 'identity.json': identity}
    for name, value in artifacts.items():
        path = output / name
        if path.exists():
            require(read_json(path) == value, 'Frozen plan, runtime or model identity changed')
        else:
            baf_attempt.persist(path, value)
    progress = {'status': 'running', 'total': len(plan['attempts']), 'attempts': []}
    progress_path = output / 'progress.json'
    baf_attempt.persist(progress_path, progress)
    try:
        for attempt in plan['attempts']:
            folder = output / f"{attempt['repeat']:02d}-{attempt['task_id']}"
            if folder.exists():
                outcome = existing_case(folder, attempt, identity)
            else:
                require(runtime_identity() == (revision, fingerprint), 'Runtime source changed during the batch')
                run(['sudo', '-n', '-v'])
                folder.mkdir(mode=0o700)
                baf_attempt.sync_dir(output)
                with (folder / 'commands.jsonl').open('w') as log:
                    case = Case(folder, log, identity, output / 'plan.json', attempt, model_access)
                    outcome = case.execute_case()
            progress['attempts'].append({**attempt, 'outcome': outcome})
            baf_attempt.persist(progress_path, progress)
            print(f"{len(progress['attempts'])}/{progress['total']} {attempt['task_id']} "
                  f"repeat {attempt['repeat']} {outcome}", flush=True)
            require(outcome != 'infra_error', 'Infrastructure failure; inspect ' + str(folder / 'summary.json'))
        progress['status'] = 'complete'
        return 0
    except (Exception, KeyboardInterrupt) as error:
        progress.update(status='stopped', reason=str(error) or type(error).__name__)
        print(progress['reason'], file=sys.stderr)
        return 2
    finally:
        progress['passes'] = sum(row['outcome'] == 'pass' for row in progress['attempts'])
        progress['failures'] = sum(row['outcome'] == 'fail' for row in progress['attempts'])
        baf_attempt.persist(progress_path, progress)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPOSITORY / 'results/baf-mounted-live')
    parser.add_argument('--model-base', default=DEFAULT_BASE)
    parser.add_argument('--model')
    args = parser.parse_args()
    os.umask(0o077)

    def stop(*_):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        with batch_lock():
            base = loopback(args.model_base)
            preflight()
            os.environ['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
            run([sys.executable, '-I', '-B', '-c', 'import yaml; assert yaml.__version__ == "6.0.3"'])
            model, key, opener = discover_model(base, args.model)
            output = args.output.absolute()
            output.mkdir(parents=True, exist_ok=True)
            info = output.lstat()
            require(output.resolve() == output and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                    and not info.st_mode & 0o077, 'Use a private owned output directory')
            print(f'Provider: {base} model {model}\nEvidence: {output}', flush=True)
            return run_batch(output, (base, model, key, opener))
    except (Exception, KeyboardInterrupt) as error:
        print(str(error) or type(error).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
