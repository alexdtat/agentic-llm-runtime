"""Rehearse one mounted B6.1 mock attempt on the Linux Docker host."""
import argparse
import contextlib
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from live_failure import LOCAL, STACK, preflight, run
from prepare import bind, prepare
from recover_readonly import SOCKET, mount_state, private_directory, require, text
from smoke import service
from track_a import baf_attempt, baf_grade
from track_a.application import Application
from track_a.baf_plan import build_plan
from track_a.baf_smoke_mock import ScriptedModel
from track_a.mounted_executor import MountedExecutor

TASK = 'B6.1'
OLD_EVIDENCE = Path('/tmp/agentic-mounted-ro-evidence.bOIRZf6e')
OLD_ROOT = Path('/tmp/agentic-mounted-runtime.lg7k_hkd')
# Full IDs from the local read-only inventory in agentic-b6-mock-evidence.dm73jgoh.
OLD_CONTAINERS = {
    '2ba8b2b72528becb96e00bf070fd3dd4dfc3f313b98f6467bb7f4bdfb1e65d2e',
    'a15c3e43d7b471dd43c4e5b4c54cebab6076ffd1cdb255593fb593d0027c151b',
}
OLD_NETWORK = '14efe5e977dc15ad471fbb0c21cf144d8bc37ec0012ba5e288975d0f44c75c1c'
OLD_RW_ROOT = Path('/tmp/agentic-mounted-runtime.rp38r6sd')
OLD_RW_EVIDENCE = Path('/tmp/agentic-b6-mock-evidence.w6f7n3um')
RECOVERY_ROOT = Path('/tmp/agentic-mounted-runtime.5vl_3_sm')
RECOVERY_EVIDENCE = Path('/tmp/agentic-b6-mock-evidence.zj224gf6')
RECOVERY_ATTEMPT = '48cda88ba1fb4b68b7bab03cc77aea1a'
NO_MOUNTS = ('none', 'none')


def scope_for(root):
    return 'mount-v2-' + hashlib.sha256(os.fsencode(root)).hexdigest()[:12]


def directory_present(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    require(stat.S_ISDIR(info.st_mode), f'Unexpected directory path {path}')
    return True


def propagation_tags(row):
    fields = row.split()
    return {field for field in fields[6:fields.index('-')] if field.startswith(('shared:', 'master:'))}


def rw_mount_graph(root, mountinfo, save, *, sshfs_source='ubuntu@exporter:/work'):
    """Classify propagation replicas without traversing the mounted paths."""
    rows = {line.split()[0]: line for line in mountinfo.splitlines() if line}
    entries = {ident: line.split() for ident, line in rows.items()}
    mount, work = str(root / 'mount'), str(root / 'mount/work')
    nested = {ident: entry for ident, entry in entries.items()
              if entry[4] == str(root) or entry[4].startswith(str(root) + '/')}
    relevant = set(nested)
    for ident in nested:
        while ident in entries:
            parent = entries[ident][1]
            if parent in relevant:
                break
            relevant.add(parent)
            ident = parent
    relevant.update(ident for ident, entry in entries.items() if Path(entry[4]) in root.parents)
    save(''.join(line + '\n' for ident, line in rows.items() if ident in relevant))
    binds = {ident: entry for ident, entry in nested.items() if entry[4] == mount}
    fuses = {ident: entry for ident, entry in nested.items() if entry[4] == work}
    require(len(nested) == len(binds) + len(fuses), 'Unexpected mount below the synthetic RW root')

    def filesystem(entry):
        return entry[2], entry[3], tuple(entry[entry.index('-') + 1:])

    def replicas(group, kind):
        require(len({filesystem(entry) for entry in group.values()}) <= 1,
                f'Unrelated {kind} filesystems in the mount graph')
        if len(group) > 1:
            peers = [{field.split(':')[1] for field in entry[6:entry.index('-')]
                      if field.startswith(('shared:', 'master:'))} for entry in group.values()]
            require(set.intersection(*peers), f'Unrelated {kind} propagation groups')

    covering = set()
    for entry in binds.values():
        base = entries[entry[1]]
        relative = posixpath.relpath(mount, base[4])
        require(relative != '..' and not relative.startswith('../') and
                entry[2] == base[2] and entry[3] == posixpath.normpath(posixpath.join(base[3], relative))
                and filesystem(entry)[2] == filesystem(base)[2],
                'Unexpected RW self-bind source')
        covering.add(entry[1])
    require(not fuses or binds, 'RW SSHFS lacks an owned self-bind')
    for entry in fuses.values():
        fstype, source, options = filesystem(entry)[2]
        require(entry[1] in binds.keys() | covering and entry[3] == '/' and
                (fstype, source) == ('fuse.sshfs', sshfs_source) and
                {'rw', 'user_id=0', 'group_id=0', 'default_permissions', 'allow_other'} <= set(options.split(',')),
                'Unexpected RW SSHFS source or parent')
    replicas(binds, 'self-bind')
    replicas(fuses, 'SSHFS')
    return {ident: rows[ident] for ident in binds}, {ident: rows[ident] for ident in fuses}


class Rehearsal:
    def __init__(self, evidence, log):
        self.evidence, self.log = evidence, log
        self.root = None
        self.stack_attempted = False
        self.mounts = ({}, {})
        self.summary = {
            'mode': 'mounted_mock_rehearsal', 'task': TASK, 'repeat': 1,
            'evidence': str(evidence), 'root': None, 'status': 'running',
            'stages': {stage: {'status': 'not_run', 'exit_code': None} for stage in
                       ('preflight', 'readonly', 'previous_rw', 'setup', 'attempt', 'grade', 'cleanup')},
        }

    def save(self):
        baf_attempt.persist(self.evidence / 'summary.json', self.summary)

    def stage(self, name, action):
        stage = self.summary['stages'][name]
        stage.update(status='running')
        self.save()
        try:
            status, code = action()
        except (Exception, KeyboardInterrupt) as error:
            stage.update(status='failed', exit_code=1, reason=str(error) or type(error).__name__)
            return False
        else:
            stage.update(status=status, exit_code=code)
            return code == 0
        finally:
            self.save()

    def command(self, args, **kwargs):
        try:
            return run(args, log=self.log, **kwargs)
        except subprocess.TimeoutExpired as error:
            def partial_output(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''

            self.log.write(json.dumps(dict(command=list(map(str, args)), returncode=None,
                                           timed_out_seconds=error.timeout, stdout=partial_output(error.stdout),
                                           stderr=partial_output(error.stderr))) + '\n')
            self.log.flush()
            raise

    def scoped_resources(self, root):
        scope = scope_for(root)
        resources = {}
        for label in ('com.docker.compose.project=' + scope, 'agentic.scope=mounted-' + scope):
            resources[label] = {
                'containers': self.command([*LOCAL, 'ps', '-aq', '--no-trunc', '--filter',
                                            'label=' + label]).stdout.split(),
                'networks': self.command([*LOCAL, 'network', 'ls', '-q', '--no-trunc', '--filter',
                                          'label=' + label]).stdout.split(),
            }
        return resources

    def verify_empty(self, root):
        resources = self.scoped_resources(root)
        require(not any(ids for entry in resources.values() for ids in entry.values()),
                f'Docker resources remain for {scope_for(root)}')
        return resources

    def inspect_fields(self, kind, ident, fields, members):
        # Project identity and member keys only. The Docker log never contains Config.Env.
        template = '\n'.join('{{json ' + field + '}}' for field in fields)
        template += '{{range $id, $unused := ' + members + '}}{{println}}{{json $id}}{{end}}'
        result = self.command([*LOCAL, kind, 'inspect', '--format', template, ident])
        return [json.loads(line) for line in result.stdout.splitlines()]

    def recover_orphaned_readonly(self):
        scope = scope_for(OLD_ROOT)
        resources = self.scoped_resources(OLD_ROOT)
        project = resources['com.docker.compose.project=' + scope]
        runtime = resources['agentic.scope=mounted-' + scope]
        containers, networks = set(project['containers']), set(project['networks'])
        require(containers <= OLD_CONTAINERS and networks <= {OLD_NETWORK} and
                not runtime['containers'] and not runtime['networks'],
                'Old RO scope contains an unexpected Docker resource')

        network_name = scope + '_file-channel'
        services = set()
        for ident in sorted(containers):
            actual_id, actual_scope, service_name, *network_names = self.inspect_fields(
                'container', ident,
                ('.Id', '(index .Config.Labels "com.docker.compose.project")',
                 '(index .Config.Labels "com.docker.compose.service")'),
                '.NetworkSettings.Networks')
            require((actual_id, actual_scope) == (ident, scope) and
                    service_name in {'exporter', 'broker'} - services and
                    set(network_names) <= {network_name}, 'Unexpected old RO container identity or network')
            services.add(service_name)

        def inspect_network(expected_endpoints):
            actual_id, name, actual_scope, segment, *endpoints = self.inspect_fields(
                'network', OLD_NETWORK,
                ('.Id', '.Name', '(index .Labels "com.docker.compose.project")',
                 '(index .Labels "com.docker.compose.network")'), '.Containers')
            require((actual_id, name, actual_scope, segment) == (OLD_NETWORK, network_name, scope, 'file-channel')
                    and set(endpoints) <= expected_endpoints,
                    'Unexpected old RO network identity or endpoint')

        if networks:
            inspect_network(containers)
        require(not directory_present(OLD_ROOT) and mount_state(OLD_ROOT) == NO_MOUNTS,
                'Old RO root or mounts reappeared before cleanup')
        for ident in sorted(containers):
            self.command([*LOCAL, 'container', 'rm', '-f', ident])
        if networks:
            inspect_network(set())
            self.command([*LOCAL, 'network', 'rm', OLD_NETWORK])
        return bool(containers or networks)

    def readonly(self):
        stage = self.summary['stages']['readonly']
        stage.update(root=str(OLD_ROOT), scope=scope_for(OLD_ROOT), evidence=str(OLD_EVIDENCE))
        have_root = directory_present(OLD_ROOT)
        if have_root:
            require(directory_present(OLD_EVIDENCE), 'Old prepared root exists without recovery evidence')
            recovery = STACK.with_name('recover_readonly.py')
            args = [sys.executable, recovery, OLD_EVIDENCE]
            dry_run = self.command(args).stdout
            plan = json.loads(dry_run)
            require((plan['status'], plan['root'], plan['scope']) ==
                    ('dry_run', str(OLD_ROOT), scope_for(OLD_ROOT)), 'Unexpected RO recovery plan')
            stage['dry_run'] = plan
            applied = self.command([*args, '--apply', plan['self_bind_id'], plan['sshfs_id']], seconds=115)
            plan_result = json.loads(applied.stdout.splitlines()[-1])
            require((plan_result['status'], plan_result['root'], plan_result['scope']) ==
                    ('passed', str(OLD_ROOT), scope_for(OLD_ROOT)), 'Unexpected RO recovery result')
        stage['mount_ids'] = mount_state(OLD_ROOT)
        require(stage['mount_ids'] == NO_MOUNTS, 'Old RO mount remains or has unverifiable identity')
        recovered = have_root or self.recover_orphaned_readonly()
        stage['mount_ids'] = mount_state(OLD_ROOT)
        require(stage['mount_ids'] == NO_MOUNTS, 'Old RO mounts reappeared after cleanup')
        stage['docker'] = self.verify_empty(OLD_ROOT)
        return ('recovered' if recovered else 'confirmed_absent'), 0

    def privileged(self, *args):
        return self.command(['sudo', '-n', 'timeout', '-k', '2', '6', *args], seconds=10)

    def rw_graph(self, root, note):
        path = self.evidence / f'{note}.mountinfo'
        return rw_mount_graph(root, Path('/proc/self/mountinfo').read_text(), path.write_text)

    def cleanup_rw(self, root, expected, stage, *, stack_attempted, note):
        graph = partial(self.rw_graph, root)
        before = graph(note + '-before')
        require(all(current.items() <= known.items() for current, known in zip(before, expected)),
                'RW mount identity changed before cleanup')
        if stack_attempted:
            env = {**os.environ, 'PROBE_ROOT': str(root)}
            require(self.command([STACK, 'scope'], env=env).stdout.strip() == scope_for(root),
                    'Unexpected cleanup scope')
            result = self.command([STACK, 'cleanup'], env=env, seconds=70, check=False)
            stage['stack_exit_code'] = result.returncode
            require(result.returncode in (0, 3), 'Scoped RW Docker cleanup failed')
        stage['docker'] = self.verify_empty(root)
        self.finish_rw_mounts(root, before, stage, note,
                              residual_confirmed=stack_attempted and result.returncode == 3)

    def finish_rw_mounts(self, root, before, stage, note, *, residual_confirmed):
        def graph(suffix):
            return self.rw_graph(root, note + '-' + suffix)

        after = graph('post-docker')
        require(all(current.items() <= prior.items() for current, prior in zip(after, before)),
                'RW mount identity changed during Docker cleanup')
        binds, fuses = after
        if fuses:
            require(residual_confirmed and len(binds) == len(fuses) == 1
                    and next(iter(fuses.values())).split()[1] == next(iter(binds)),
                    'Residual SSHFS has multiple or shadowed paths; inspect mountinfo in evidence')
            require(graph('before-fuse-unmount') == after, 'RW mount graph changed before SSHFS unmount')
            self.privileged('umount', '-i', '-c', '--', root / 'mount/work')
            require(graph('after-fuse-unmount') == (binds, {}), 'RW mount graph changed after SSHFS unmount')
        if binds:
            require(len(binds) == 1, 'Multiple residual self-bind aliases; inspect mountinfo in evidence')
            require(graph('before-bind-unmount') == (binds, {}), 'RW mount graph changed before bind unmount')
            self.privileged('umount', '-i', '-c', '--', root / 'mount')
        final = graph('after')
        stage['mount_ids'] = [sorted(group) for group in final]
        require(final == ({}, {}), 'RW root still has mounts')

    def verify_previous_rw_scope(self, root):
        scope = scope_for(root)
        resources = self.scoped_resources(root)
        project = resources['com.docker.compose.project=' + scope]
        runtime = resources['agentic.scope=mounted-' + scope]
        containers, networks = project['containers'], project['networks']
        require(not any(runtime.values()) and len(containers) <= 2 and len(networks) <= 1 and
                all(re.fullmatch(r'[0-9a-f]{64}', ident) for ident in containers + networks),
                'Unexpected Docker inventory for the previous RW attempt')
        network_name = scope + '_file-channel'
        services = set()
        for ident in containers:
            actual_id, name, actual_scope, service_name, *connections = self.inspect_fields(
                'container', ident,
                ('.Id', '.Name', '(index .Config.Labels "com.docker.compose.project")',
                 '(index .Config.Labels "com.docker.compose.service")'), '.NetworkSettings.Networks')
            require((actual_id, actual_scope, name) == (ident, scope, f'/{scope}-{service_name}-1')
                    and service_name in {'broker', 'exporter'} - services and
                    set(connections) <= {network_name}, 'Unexpected previous RW Compose container')
            services.add(service_name)
        for ident in networks:
            actual_id, name, actual_scope, network, *endpoints = self.inspect_fields(
                'network', ident, ('.Id', '.Name', '(index .Labels "com.docker.compose.project")',
                                   '(index .Labels "com.docker.compose.network")'), '.Containers')
            require((actual_id, name, actual_scope, network) == (ident, network_name, scope, 'file-channel') and
                    set(endpoints) <= set(containers), 'Unexpected previous RW Compose network or endpoint')
        return resources

    def recover_completed_rw(self):
        root, evidence = RECOVERY_ROOT, RECOVERY_EVIDENCE
        scope = scope_for(root)
        stage = self.summary['stages']['cleanup']
        private_directory(evidence, r'/tmp/agentic-b6-mock-evidence\.[a-z0-9_]{8}')
        summary = json.loads(text(evidence / 'summary.json'))
        stages = summary['stages']
        attempt = evidence / 'attempts' / RECOVERY_ATTEMPT
        require((summary['mode'], summary['task'], summary['repeat'], summary['root'], summary['scope']) ==
                ('mounted_mock_rehearsal', TASK, 1, str(root), scope)
                and summary['status'] == 'failed' and stages['cleanup']['status'] == 'failed'
                and stages['setup']['status'] == 'passed' and stages['setup']['mount_ids'] == [['6384'], ['9516']]
                and stages['attempt']['receipt'] == str(attempt / 'attempt.json')
                and stages['attempt']['status'] == 'collected' and stages['grade']['status'] == 'pass'
                and summary['grade'] == str(evidence / 'grade.json'),
                'Source rehearsal does not match the completed B6.1 recovery')
        private_directory(attempt, str(evidence) + r'/attempts/' + RECOVERY_ATTEMPT)
        receipt = json.loads(text(attempt / 'attempt.json'))
        grade = json.loads(text(evidence / 'grade.json'))
        require(receipt['attempt_id'] == RECOVERY_ATTEMPT and receipt['task_id'] == TASK and
                receipt['grade_ready'] is True and receipt['close'] == 'confirmed' and
                receipt['result']['status'] == 'completed' and receipt['result']['cleanup_state'] == 'confirmed'
                and grade['outcome'] == 'pass' and grade['pass'] is True, 'Unexpected B6.1 receipt or grade')
        expected = rw_mount_graph(root, text(evidence / 'rw-cleanup-before.mountinfo'), lambda _: None)
        require([sorted(group) for group in expected] == [['6384'], ['9516']]
                and expected == rw_mount_graph(root, text(evidence / 'rw-after-stack.mountinfo'), lambda _: None),
                'Retained mount graph differs from the verified setup')
        before = self.rw_graph(root, 'recovery-before')
        require(all(current.items() <= known.items() for current, known in zip(before, expected)),
                'Live mount graph differs from the retained host graph')
        if not directory_present(root):
            require(before == ({}, {}), 'Recovery root is missing but mounts remain')
            stage['docker'] = self.verify_empty(root)
            return 'confirmed_absent', 0
        private_directory(root, r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}')
        require(text(root / 'marker').strip() == 'agentic-mounted-runtime' and
                text(root / 'access-mode').strip() == 'rw' and json.loads(text(root / 'workspace.json')) ==
                {'services': {'exporter': {'volumes': [bind(root / 'client', '/probe/work')]}}},
                'Recovery root does not match the prepared RW export')
        with baf_attempt.directory(root / 'client') as export:
            directories, files = baf_attempt.inventory(export)
        require(directories == receipt['snapshot']['directories'] and
                baf_attempt.fingerprints(files) == receipt['snapshot']['files_sha256'],
                'Recovery export differs from the confirmed B6.1 snapshot')
        project_label = 'com.docker.compose.project=' + scope
        stage['pre_cleanup_docker'] = inventory = self.verify_previous_rw_scope(root)
        current = self.rw_graph(root, 'recovery-before-docker')
        require(all(observed.items() <= prior.items() for observed, prior in zip(current, before)),
                'Mount graph changed before scoped Docker removal')
        timed_out = stage['docker_removals_timed_out'] = []
        for ident in inventory[project_label]['containers']:
            try:
                self.command([*LOCAL, 'container', 'rm', '-f', ident], seconds=8, check=False)
            except subprocess.TimeoutExpired:
                timed_out.append(ident)
        project = self.verify_previous_rw_scope(root)[project_label]
        require(not project['containers'] and set(project['networks']) <= set(inventory[project_label]['networks']),
                'Scoped containers remain or network identity changed')
        for ident in project['networks']:
            try:
                self.command([*LOCAL, 'network', 'rm', ident], seconds=8, check=False)
            except subprocess.TimeoutExpired:
                timed_out.append(ident)
        stage['docker'] = self.verify_empty(root)
        self.finish_rw_mounts(root, current, stage, 'recovery', residual_confirmed=True)
        shutil.rmtree(root)
        return 'recovered', 0

    def previous_rw(self):
        stage = self.summary['stages']['previous_rw']
        root, evidence = OLD_RW_ROOT, OLD_RW_EVIDENCE
        scope = scope_for(root)
        stage.update(root=str(root), scope=scope, evidence=str(evidence))
        before = self.rw_graph(root, 'previous-rw-observed')
        if not directory_present(root):
            require(before == ({}, {}), 'Previous RW root is missing but mounts remain')
            stage['docker'] = self.verify_empty(root)
            return 'confirmed_absent', 0
        private_directory(evidence, r'/tmp/agentic-b6-mock-evidence\.[a-z0-9_]{8}')
        summary = json.loads(text(evidence / 'summary.json'))
        stages = summary['stages']
        require((summary['mode'], summary['task'], summary['repeat'], summary['root'], summary['scope']) ==
                ('mounted_mock_rehearsal', TASK, 1, str(root), scope) and summary['status'] == 'failed' and
                stages['setup']['status'] == stages['cleanup']['status'] == 'failed' and
                stages['attempt']['status'] == 'not_run', 'Unexpected previous RW outcome')
        log = [json.loads(line) for line in text(evidence / 'commands.jsonl').splitlines()]
        commands = [entry['command'] for entry in log if entry['returncode'] == 0]
        previous_mount = str(root / 'mount')
        require(commands[-3:] == [
            ['sudo', '-n', 'timeout', '-k', '2', '6', 'mount', '--bind', '--', previous_mount, previous_mount],
            ['sudo', '-n', 'timeout', '-k', '2', '6', 'mount', '--make-rshared', '--', previous_mount],
            [str(STACK), 'up'],
        ] and str(log[-1]['stderr']).count('Healthy') == 2 and any(
            entry['command'] == [str(STACK), 'scope'] and entry['returncode'] == 0
            and entry['stdout'].strip() == scope for entry in log
        ), 'Previous RW setup commands were not confirmed')
        require(all(propagation_tags(row) for row in before[0].values()),
                'Previous RW self-bind lacks a propagation identity')
        private_directory(root, r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}')
        require(text(root / 'marker').strip() == 'agentic-mounted-runtime' and
                text(root / 'access-mode').strip() == 'rw' and
                json.loads(text(root / 'workspace.json')) ==
                {'services': {'exporter': {'volumes': [bind(root / 'client', '/probe/work')]}}},
                'Previous RW root was not the synthetic fixture export')
        plan = evidence / 'plan.json'
        _, _, fixture = baf_attempt.load_frozen_task(plan, json.loads(text(plan))['suite_sha256'], TASK, 1)
        with baf_attempt.directory(root / 'client') as export:
            require(baf_attempt.inventory(export) == fixture, 'Previous RW export differs from B6.1 fixture')
        stage['pre_cleanup_docker'] = self.verify_previous_rw_scope(root)
        self.cleanup_rw(root, before, stage, stack_attempted=True, note='previous-rw')
        shutil.rmtree(root)
        return 'recovered', 0

    def setup(self):
        plan = build_plan()
        self.plan = self.evidence / 'plan.json'
        self.suite_digest = plan['suite_sha256']
        baf_attempt.persist(self.plan, plan)
        _, fixture, _ = baf_grade.source_task(TASK)
        self.root = prepare(None, fixture=fixture)
        self.scope = scope_for(self.root)
        self.summary.update(root=str(self.root), scope=self.scope)
        self.save()
        os.environ['PROBE_ROOT'] = str(self.root)
        private_directory(self.root, r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}')
        require(self.command([STACK, 'scope']).stdout.strip() == self.scope, 'Unexpected stack scope')
        require(self.rw_graph(self.root, 'rw-before-bind') == ({}, {}), 'Fresh root already has mounts')
        self.verify_empty(self.root)
        mount = self.root / 'mount'
        try:
            self.privileged('mount', '--bind', '--', mount, mount)
        finally:
            self.mounts = self.rw_graph(self.root, 'rw-after-bind')
        require(len(self.mounts[0]) == 1 and not self.mounts[1], 'Expected one new self-bind only')
        bind_ids = set(self.mounts[0])
        self.privileged('mount', '--make-private', '--', mount)
        current = self.rw_graph(self.root, 'rw-private-bind')
        require(set(current[0]) == bind_ids and not current[1], 'Self-bind identity changed before promotion')
        self.mounts = current
        require(not propagation_tags(next(iter(current[0].values()))),
                'New self-bind did not detach from external propagation groups')
        self.privileged('mount', '--make-rshared', '--', mount)
        current = self.rw_graph(self.root, 'rw-shared-bind')
        require(set(current[0]) == bind_ids and not current[1], 'Self-bind identity changed after promotion')
        self.mounts = current
        tags = propagation_tags(next(iter(current[0].values())))
        require(len(tags) == 1 and next(iter(tags)).startswith('shared:'),
                'New self-bind did not acquire its own shared group')
        self.stack_attempted = True
        try:
            self.command([STACK, 'up'], seconds=45)
        finally:
            current = self.rw_graph(self.root, 'rw-after-stack')
            require(current[0] == self.mounts[0], 'Unexpected self-bind identity after stack setup')
            self.mounts = current
            self.summary['stages']['setup']['mount_ids'] = [sorted(group) for group in current]
        require(len(self.mounts[1]) == 1 and
                next(iter(self.mounts[1].values())).split()[1] in self.mounts[0] and
                'rw' in next(iter(self.mounts[1].values())).split()[5].split(','),
                'Expected one propagated writable SSHFS mount under the private peer group')
        return 'passed', 0

    def attempt(self):
        grant, owner = secrets.token_hex(24), secrets.token_hex(24)
        executor = MountedExecutor(str(self.root / 'mount/work'), grant, self.scope, baf_grade.IMAGE,
                                   self.root / 'execution-state')
        executor.recover()
        with contextlib.ExitStack() as services:
            backend = services.enter_context(service(executor, secrets.token_hex(24)))
            model = services.enter_context(service(ScriptedModel(TASK), secrets.token_hex(24)))
            backend = baf_attempt.MountedClient(backend.base, backend.token, grant)
            model = baf_attempt.MountedClient(model.base, model.token, grant)
            app = Application(backend, model, {'local': owner}, self.root / 'app-state', mount_owner='local')
            api = services.enter_context(service(app, owner))
            mounted = baf_attempt.MountedClient(api.base, owner, grant)
            self.folder, receipt = baf_attempt.run_attempt(
                mounted, plan_path=self.plan, suite_digest=self.suite_digest, task_id=TASK, repeat=1,
                grant=grant, export=self.root / 'client', output=self.evidence / 'attempts',
                provider_label='local/ScriptedModel', runtime_revision='working-checkout')
            self.summary['stages']['attempt'].update(receipt=str(self.folder / 'attempt.json'),
                                                      close=receipt['close'],
                                                      reason=receipt.get('reason'))
            if 'events' in receipt:
                self.summary['trace'] = str(self.folder / 'events.json')
            return receipt['outcome'], 0 if receipt['grade_ready'] else 1

    def grade(self):
        snapshot = self.folder / 'snapshot'
        events = baf_grade.read_trace(self.folder / 'events.json', snapshot)
        isolated = partial(baf_grade.IsolatedPython, scope='mounted-' + self.scope)
        grade = baf_grade.grade(TASK, snapshot, events, isolated_python=isolated)
        path = self.evidence / 'grade.json'
        baf_attempt.persist(path, grade)
        self.summary['grade'] = str(path)
        return grade['outcome'], {'pass': 0, 'fail': 1, 'infra_error': 2}[grade['outcome']]

    def cleanup(self):
        if self.root is None:
            return 'not_acquired', 0
        stage = self.summary['stages']['cleanup']
        self.cleanup_rw(self.root, self.mounts, stage, stack_attempted=self.stack_attempted, note='rw-cleanup')
        shutil.rmtree(self.root)
        return 'passed', 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-zj224gf6', action='store_true',
                        help='Recover only the completed rehearsal retained in zj224gf6')
    recovery = parser.parse_args().recover_zj224gf6
    os.umask(0o077)
    prefix = 'agentic-b6-mock-recovery.' if recovery else 'agentic-b6-mock-evidence.'
    evidence = Path(tempfile.mkdtemp(prefix=prefix, dir='/tmp'))
    print(f'Evidence directory: {evidence}', flush=True)

    def interrupted(signum, _frame):
        raise InterruptedError(f'Host received signal {signum}')

    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    for signum in signals:
        signal.signal(signum, interrupted)
    with (evidence / 'commands.jsonl').open('w') as log:
        rehearsal = Rehearsal(evidence, log)
        if recovery:
            rehearsal.summary.update(mode='mounted_mock_cleanup_recovery', root=str(RECOVERY_ROOT),
                                     scope=scope_for(RECOVERY_ROOT), source_evidence=str(RECOVERY_EVIDENCE))
            rehearsal.summary['stages'] = {name: rehearsal.summary['stages'][name]
                                           for name in ('preflight', 'cleanup')}

        def check_host():
            if recovery:
                require(not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST', SOCKET) == SOCKET,
                        'Recovery requires the local Docker socket')
                rehearsal.command(['sudo', '-n', '-v'])
                return 'passed', 0
            preflight()
            os.environ['DOCKER_HOST'] = SOCKET
            rehearsal.rw_graph(OLD_RW_ROOT, 'entry-previous-rw')
            return 'passed', 0

        if not rehearsal.stage('preflight', check_host):
            rehearsal.summary['status'] = 'blocked'
            result = 2
        elif recovery:
            success = rehearsal.stage('cleanup', rehearsal.recover_completed_rw)
            rehearsal.summary['status'] = 'passed' if success else 'failed'
            result = 0 if success else 1
        else:
            result = 1
            try:
                for name in ('readonly', 'previous_rw', 'setup', 'attempt', 'grade'):
                    if not rehearsal.stage(name, getattr(rehearsal, name)):
                        break
                else:
                    result = 0
            finally:
                for signum in signals:
                    signal.signal(signum, signal.SIG_IGN)
                if not rehearsal.stage('cleanup', rehearsal.cleanup):
                    result = 1
                rehearsal.summary['status'] = 'passed' if result == 0 else 'failed'
        rehearsal.save()
        print(json.dumps(rehearsal.summary))
        return result


if __name__ == '__main__':
    sys.exit(main())
