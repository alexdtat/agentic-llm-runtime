"""Control-plane regressions for the mounted executor, without Docker or FUSE."""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from track_a.http_api import Conflict
from track_a.mounted_executor import MountedExecutor

PREPARE = Path(__file__).resolve().parents[1] / 'prototypes/mounted-runtime/prepare.py'
BROKER = Path(__file__).resolve().parents[1] / 'prototypes/mount-runtime-probe/broker.sh'
STACK = PREPARE.with_name('stack.sh')
RECOVERY = PREPARE.with_name('recover_readonly.py')


class MountedRuntimeTests(unittest.TestCase):
    def test_unknown_tool_outcome_is_durable_and_never_reexecuted(self):
        with tempfile.TemporaryDirectory() as root:
            executor = MountedExecutor('/mnt/export', 'grant', 'test', 'sandbox', root)
            self.addCleanup(executor.containers.journal.db.close)
            session, scope = '/mounted/session', {'generation': 1, 'run_id': 'run'}
            call = {'id': 'write', 'tool': 'WriteFile', 'args': {'path': 'a.txt', 'data': 'YQ=='}}
            intent = {**scope, 'op_id': 'op', 'call': call}
            ok = SimpleNamespace(returncode=0)
            with patch.object(executor, 'check_mount'), patch('track_a.mounted_executor.docker', return_value=ok):
                executor.route('POST', session + '/provision', {'grant': 'grant'})
                executor.route('POST', session + '/start', scope)
            self.addCleanup(executor.store.journals['session'].db.close)
            with patch.object(executor, 'execute_tool', side_effect=TimeoutError) as tool:
                with self.assertRaises(TimeoutError):
                    executor.route('POST', session + '/exec', intent)
                with self.assertRaises(Conflict):
                    executor.route('POST', session + '/exec', intent)
                tool.assert_called_once()

            recovered = MountedExecutor('/mnt/export', 'grant', 'test', 'sandbox', root)
            self.addCleanup(recovered.containers.journal.db.close)
            self.addCleanup(recovered.store.journals['session'].db.close)
            with patch.object(recovered.containers, 'recover'):
                recovered.recover()
            with self.assertRaisesRegex(Conflict, 'outcome is unknown'):
                recovered.route('POST', session + '/operation', {'generation': 1, 'op_id': 'op'})
            self.assertEqual(recovered.sessions['session']['stopped'], ['run'])

    def test_export_preparation_finds_git_and_rejects_links(self):
        paths = runpy.run_path(str(PREPARE))['protected_paths']
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root)
            work = parent / 'work'
            (work / 'nested/.git').mkdir(parents=True)
            (work / '.git').write_text('gitdir: ../secret\n')
            self.assertEqual(dict(paths(work)), {Path('.git'): False, Path('nested/.git'): True})

            secret = parent / 'secret'
            secret.write_text('outside')
            for link in (os.link, os.symlink):
                alias = work / 'alias'
                link(secret, alias)
                with self.assertRaisesRegex(ValueError, 'without links'):
                    list(paths(work))
                alias.unlink()

    def test_prepare_binds_readonly_export_and_sets_the_sftp_and_broker_mode(self):
        prepare = runpy.run_path(str(PREPARE))['prepare']

        def keygen(args, **kwargs):
            Path(args[-1] + '.pub').write_text('fixture-pubkey')

        for mode in ('rw', 'ro'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch('os.getuid', return_value=1000), patch('os.umask'), patch(
                        'tempfile.mkdtemp', return_value=directory), patch(
                        'subprocess.run', side_effect=keygen), patch(
                        'subprocess.check_output', return_value='fixture-host-key\n'), contextlib.redirect_stdout(
                        io.StringIO()):
                    prepare(None, mode)
                services = json.loads((root / 'workspace.json').read_text())['services']
                volumes = {item['target']: item for item in services['exporter']['volumes']}
                self.assertEqual((root / 'access-mode').read_text(), mode + '\n')
                self.assertEqual(volumes['/probe/work']['source'], str(root / 'client'))
                self.assertEqual(volumes['/probe/work']['read_only'], mode == 'ro')
                self.assertTrue(volumes['/probe/work/nested/repo/.git']['read_only'])
                if mode == 'ro':
                    config = volumes['/probe-config/sshd.conf']
                    self.assertTrue(config['read_only'])
                    self.assertIn('ForceCommand internal-sftp -R -d /work -P symlink,hardlink',
                                  Path(config['source']).read_text())
                    self.assertEqual(services['broker']['environment'], {'SSHFS_ACCESS_MODE': 'ro'})
                else:
                    self.assertNotIn('/probe-config/sshd.conf', volumes)
                    self.assertNotIn('broker', services)

    def test_bounded_export_configures_capacity_only_for_empty_synthetic_rw(self):
        setup = runpy.run_path(str(PREPARE))
        prepare = setup['prepare']
        for client, mode, fixture in ((Path('/project'), 'rw', None),
                                      (None, 'ro', None), (None, 'rw', Path('/fixture'))):
            with self.subTest(client=client, mode=mode, fixture=fixture), self.assertRaisesRegex(
                    ValueError, 'empty synthetic RW workspace'):
                prepare(client, mode, fixture=fixture, bounded_export=True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def keygen(args, **kwargs):
                Path(args[-1] + '.pub').write_text('fixture-pubkey')

            with patch('subprocess.run', side_effect=keygen), patch(
                    'subprocess.check_output', return_value='fixture-host-key\n'):
                setup['configure'](root, None, 'rw', None, bounded_export=True)
            services = json.loads((root / 'workspace.json').read_text())['services']
            self.assertEqual(services, {'exporter': {'volumes': [{
                'type': 'tmpfs', 'target': '/probe/work',
                'tmpfs': {'size': 128 * 1024, 'mode': 0o1777},
            }]}})
            self.assertFalse((root / 'client').exists())

    def test_broker_preserves_pinned_auth_and_applies_operator_mode_and_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory)
            for name in ('sftp', 'sshfs'):
                executable = bin_dir / name
                executable.write_text(f'#!/bin/sh\nprintf "%s\\n" {name} "$@"\n')
                executable.chmod(0o700)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH']}
            for key in ('SSHFS_ACCESS_MODE', 'SSHFS_HOST', 'SSHFS_PORT'):
                env.pop(key, None)
            args = ['/bin/sh', BROKER]
            rw = subprocess.check_output(args, env=env, text=True, timeout=3).splitlines()
            ro = subprocess.check_output(args, env={**env, 'SSHFS_ACCESS_MODE': 'ro'},
                                         text=True, timeout=3).splitlines()
            self.assertEqual(ro, rw[:-2] + ['-o', 'ro'] + rw[-2:])
            self.assertIn('ubuntu@exporter', rw)
            self.assertIn('ubuntu@exporter:/work', rw)
            self.assertEqual(rw.count('2222'), 2)
            self.assertIn('UserKnownHostsFile=/keys/known_hosts', rw)
            self.assertIn('StrictHostKeyChecking=yes', rw)
            self.assertIn('UserKnownHostsFile=/keys/known_hosts,StrictHostKeyChecking=yes,BatchMode=yes', rw)
            forwarded = subprocess.check_output(args, env={**env, 'SSHFS_HOST': '127.0.0.1', 'SSHFS_PORT': '24222'},
                                                text=True, timeout=3).splitlines()
            replacements = {'2222': '24222', 'ubuntu@exporter': 'ubuntu@127.0.0.1',
                            'ubuntu@exporter:/work': 'ubuntu@127.0.0.1:/work'}
            self.assertEqual(forwarded, [replacements.get(value, value) for value in rw])
            invalid = subprocess.run(args, env={**env, 'SSHFS_HOST': '127.0.0.1'},
                                     capture_output=True, text=True, timeout=3)
            self.assertEqual((invalid.returncode, invalid.stdout, invalid.stderr), (1, '', 'Invalid SSHFS endpoint\n'))

    def test_cleanup_does_not_probe_the_child_mountpoint_path(self):
        with tempfile.TemporaryDirectory(prefix='agentic-mounted-runtime.', dir='/tmp') as directory:
            root = Path(directory)
            (root / 'marker').write_text('agentic-mounted-runtime\n')
            (root / 'access-mode').write_text('ro\n')
            (root / 'mount').mkdir()
            # A path-based child guard would terminate cleanup before contacting the broker.
            (root / 'mount/work').symlink_to('missing')
            fake_docker = root / 'docker'
            fake_docker.write_text('#!/bin/sh\nexit 0\n')
            fake_docker.chmod(0o700)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'PROBE_ROOT': directory}
            output = subprocess.check_output(['/bin/sh', STACK, 'cleanup'], env=env, timeout=5, text=True)
            self.assertIn('Runtime containers, Compose resources and SSHFS mount removed', output)

    def test_cleanup_disconnects_broker_unmount_from_terminal_input(self):
        with tempfile.TemporaryDirectory(prefix='agentic-mounted-runtime.', dir='/tmp') as directory:
            root = Path(directory)
            (root / 'marker').write_text('agentic-mounted-runtime\n')
            (root / 'mount').mkdir()
            scripts = {
                'awk': '''if [ ! -f "$PROBE_ROOT/broker-unmounted" ]; then
    printf '%s\\n' fuse.sshfs
fi''',
                'docker': '''case "$*" in
    *"exec -T broker fusermount3 -u /probe-mount/work")
        [ "$(readlink /proc/self/fd/0)" = /dev/null ] || exit 1
        touch "$PROBE_ROOT/broker-unmounted"
        ;;
esac''',
            }
            for name, script in scripts.items():
                executable = root / name
                executable.write_text('#!/bin/sh\n' + script + '\n')
                executable.chmod(0o700)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'PROBE_ROOT': directory}
            result = subprocess.run(
                ['/bin/sh', STACK, 'cleanup'], env=env, input='terminal input\n',
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / 'broker-unmounted').exists())

    def test_recovery_validates_mount_source_from_mount_table(self):
        mount_state = runpy.run_path(str(RECOVERY))['mount_state']
        root = Path('/tmp/agentic-mounted-runtime.1234efgh')
        mounts = ('10 1 8:1 / / rw - ext4 /dev/sda1 rw\n'
                  f'11 10 8:1 {root}/mount {root}/mount rw shared:51 - ext4 /dev/sda1 rw\n'
                  f'12 11 0:5 / {root}/mount/work ro - fuse.sshfs ubuntu@exporter:/work ro\n')
        self.assertEqual(mount_state(root, mounts), ('11', '12'))
        for changed in (mounts.replace('ubuntu@exporter', 'ubuntu@other'),
                        mounts.replace('8:1 ' + str(root), '8:2 ' + str(root)),
                        mounts + f'13 12 0:6 / {root}/mount/work rw - tmpfs tmpfs rw\n'):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                mount_state(root, changed)

    def test_recovery_unmounts_only_after_scoped_cleanup_and_rechecks_ids(self):
        recover = runpy.run_path(str(RECOVERY))['recover']
        initial = ('11', '12')
        root = Path('/tmp/agentic-mounted-runtime.1234efgh')
        with patch.dict(recover.__globals__, {'mount_state': lambda _, **options: ('21', '12')}), patch.dict(
                os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock'}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'changed since the dry run'):
                recover(root, initial, io.StringIO())

        def apply(states, results, error=None):
            command = Mock(side_effect=results)
            with patch.dict(recover.__globals__, {'mount_state': lambda _, **options: next(states),
                                                  'command': command}):
                with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock'}, clear=True):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            recover(root, initial, io.StringIO())
                    else:
                        recover(root, initial, io.StringIO())
            return [call.args[0] for call in command.call_args_list]

        calls = apply(iter([initial]), [0, 1], 'Scoped Docker cleanup failed')
        self.assertEqual(len(calls), 2)
        calls = apply(iter([initial, ('11', '13')]), [0, 3], 'Mount IDs changed during cleanup')
        self.assertEqual(len(calls), 2)
        calls = apply(iter([initial, initial, ('11', 'none'), ('none', 'none')]), [0, 3, 0, 0])
        self.assertEqual([call[-1] for call in calls[1:]],
                         ['cleanup', str(root / 'mount/work'), str(root / 'mount')])

    def test_orphaned_readonly_scope_removes_only_verified_full_ids(self):
        script = PREPARE.with_name('live_b6_mock.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        Rehearsal = spec['Rehearsal']
        exporter = '2ba8b2b72528becb96e00bf070fd3dd4dfc3f313b98f6467bb7f4bdfb1e65d2e'
        broker = 'a15c3e43d7b471dd43c4e5b4c54cebab6076ffd1cdb255593fb593d0027c151b'
        network = '14efe5e977dc15ad471fbb0c21cf144d8bc37ec0012ba5e288975d0f44c75c1c'
        foreign = '0' * 64
        scope = 'mount-v2-1b3897db7a4a'
        local = ['docker', '--host', 'unix:///var/run/docker.sock']
        for initial, has_network, extra, error in (
            ([exporter, broker], True, [], None),
            ([broker], True, [], None),
            ([broker], False, [], None),
            ([], True, [], None),
            ([], False, [], None),
            ([exporter, foreign], True, [], 'unexpected Docker resource'),
            ([exporter], True, [foreign], 'Unexpected old RO network identity or endpoint'),
        ):
            with self.subTest(initial=initial, network=has_network, extra=extra):
                containers, networks = set(initial), {network} if has_network else set()
                removed = []
                rehearsal = Rehearsal(Path('/tmp/evidence'), io.StringIO())

                def command(args, **kwargs):
                    self.assertEqual(args[:3], local)
                    kind, action, *rest = args[3:]
                    if kind == 'ps' or (kind, action) == ('network', 'ls'):
                        self.assertIn('--no-trunc', args)
                        ids = containers if kind == 'ps' else networks
                        output = sorted(ids) if args[-1] == 'label=com.docker.compose.project=' + scope else []
                        return SimpleNamespace(stdout='\n'.join(output))
                    ident = rest[-1]
                    if action == 'inspect':
                        self.assertEqual(rest[0], '--format')
                        self.assertNotIn('Env', rest[1])
                        if kind == 'container':
                            self.assertIn(ident, containers)
                            fields = [ident, scope, {exporter: 'exporter', broker: 'broker'}[ident],
                                      scope + '_file-channel']
                        else:
                            self.assertEqual(ident, network)
                            fields = [ident, scope + '_file-channel', scope, 'file-channel',
                                      *sorted(containers), *extra]
                        return SimpleNamespace(stdout='\n'.join(map(json.dumps, fields)))
                    self.assertEqual(action, 'rm')
                    self.assertEqual(rest, ['-f', ident] if kind == 'container' else [ident])
                    (containers if kind == 'container' else networks).remove(ident)
                    removed.append((kind, ident))
                    return SimpleNamespace(stdout=ident)

                with patch.object(rehearsal, 'command', side_effect=command), patch.dict(
                        Rehearsal.readonly.__globals__,
                        {'directory_present': lambda path: False, 'mount_state': lambda path: ('none', 'none')}):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            rehearsal.readonly()
                        self.assertEqual(removed, [])
                    else:
                        status, exit_code = rehearsal.readonly()
                        self.assertEqual((status, exit_code),
                                         ('recovered' if initial or has_network else 'confirmed_absent', 0))
                        expected = [('container', ident) for ident in sorted(initial)]
                        if has_network:
                            expected.append(('network', network))
                        self.assertEqual(removed, expected)
                        self.assertFalse(containers or networks)

    def test_rw_mount_graph_recognizes_propagated_aliases_but_rejects_foreign_layers(self):
        script = PREPARE.with_name('live_b6_mock.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        root = spec['OLD_RW_ROOT']
        bind_source = f'{root.name}/mount'
        tmpfs = '- tmpfs tmpfs rw,nr_inodes=1048576,inode64,usrquota'
        sshfs = '- fuse.sshfs ubuntu@exporter:/work rw,user_id=0,group_id=0,default_permissions,allow_other'
        mountinfo = (
            '9740 6381 252:1 / / ro master:1 - ext4 /dev/sda1 rw\n'
            f'9796 9740 0:48 / /tmp ro master:230 {tmpfs}\n'
            f'9797 9796 0:48 /{bind_source} {root}/mount ro master:230 {tmpfs}\n'
            f'9798 9797 0:48 /{bind_source} {root}/mount ro master:230 {tmpfs}\n'
            f'9799 9798 0:150 / {root}/mount/work ro master:1273 {sshfs}\n'
            f'9800 9797 0:150 / {root}/mount/work rw master:1273 {sshfs}\n'
            f'9814 9796 0:48 / /tmp rw master:230 {tmpfs}\n'
            f'9815 9814 0:48 /{bind_source} {root}/mount rw master:230 {tmpfs}\n'
            f'9816 9815 0:48 /{bind_source} {root}/mount rw master:230 {tmpfs}\n'
            f'9817 9816 0:150 / {root}/mount/work rw master:1273 {sshfs}\n'
            f'9818 9815 0:150 / {root}/mount/work rw master:1273 {sshfs}\n'
        )
        saved = io.StringIO()
        binds, fuses = spec['rw_mount_graph'](root, mountinfo, saved.write)
        self.assertEqual(set(binds), {'9797', '9798', '9815', '9816'})
        self.assertEqual(set(fuses), {'9799', '9800', '9817', '9818'})
        self.assertEqual(saved.getvalue(), mountinfo)
        for invalid in (
            mountinfo.replace('9817 9816 0:150', '9817 9816 0:153'),
            mountinfo.replace('9817 9816', '9817 9799'),
            mountinfo.replace('9817 9816 0:150 / ' + str(root) + '/mount/work rw master:1273',
                              '9817 9816 0:150 / ' + str(root) + '/mount/work rw master:999'),
            mountinfo + f'9820 9814 0:7 / {root}/client rw - ext4 /dev/sdz rw\n',
        ):
            with self.subTest(invalid=invalid[-200:]), self.assertRaises(RuntimeError):
                spec['rw_mount_graph'](root, invalid, io.StringIO().write)

        # Scoped Docker cleanup may remove all child replicas. Retaining several
        # aliases at the same path must never select one by insertion order.
        rehearsal = spec['Rehearsal'](Path('/tmp/evidence'), io.StringIO())
        scopes = [SimpleNamespace(stdout=spec['scope_for'](root)), SimpleNamespace(returncode=0)]
        with patch.object(rehearsal, 'command', side_effect=scopes), patch.object(
                rehearsal, 'verify_empty', return_value={}), patch.object(
                rehearsal, 'rw_graph', side_effect=[(binds, fuses), (binds, {})]), patch.object(
                rehearsal, 'privileged') as privileged:
            with self.assertRaisesRegex(RuntimeError, 'Multiple residual self-bind aliases'):
                rehearsal.cleanup_rw(root, (binds, fuses), {}, stack_attempted=True, note='test')
            privileged.assert_not_called()

        single_bind = {'9816': binds['9816']}
        direct = ({'9816': binds['9816']}, {'9817': fuses['9817']})
        only_bind = (single_bind, {})
        with patch.object(rehearsal, 'command', side_effect=[
                SimpleNamespace(stdout=spec['scope_for'](root)), SimpleNamespace(returncode=3)]), patch.object(
                rehearsal, 'verify_empty', return_value={}), patch.object(
                rehearsal, 'rw_graph', side_effect=[direct, direct, direct, only_bind, only_bind, ({}, {})]), patch.object(
                rehearsal, 'privileged') as privileged:
            stage = {}
            rehearsal.cleanup_rw(root, direct, stage, stack_attempted=True, note='test')
            self.assertEqual(stage['mount_ids'], [[], []])
            self.assertEqual([call.args for call in privileged.call_args_list],
                             [('umount', '-i', '-c', '--', root / 'mount/work'),
                              ('umount', '-i', '-c', '--', root / 'mount')])

    def test_completed_rw_recovery_rejects_alias_ids_and_is_idempotent_if_absent(self):
        script = PREPARE.with_name('live_b6_mock.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        root, evidence = spec['RECOVERY_ROOT'], spec['RECOVERY_EVIDENCE']
        attempt = spec['RECOVERY_ATTEMPT']
        scope = spec['scope_for'](root)
        files = {
            'summary.json': dict(mode='mounted_mock_rehearsal', task='B6.1', repeat=1, root=str(root), scope=scope,
                                 status='failed', grade=str(evidence / 'grade.json'), stages={
                                     'cleanup': {'status': 'failed'},
                                     'setup': {'status': 'passed', 'mount_ids': [['6384'], ['9516']]},
                                     'attempt': {'status': 'collected', 'receipt': str(
                                         evidence / 'attempts' / attempt / 'attempt.json')},
                                     'grade': {'status': 'pass'},
                                 }),
            'attempt.json': dict(attempt_id=attempt, task_id='B6.1', grade_ready=True, close='confirmed',
                                 result={'status': 'completed', 'cleanup_state': 'confirmed'}),
            'grade.json': {'outcome': 'pass', 'pass': True},
        }
        bind = '6384 265 0:48 /' + root.name + '/mount ' + str(root / 'mount') + ' rw shared:842 - tmpfs tmpfs rw\n'
        fuse = (f'9516 6384 0:152 / {root}/mount/work rw shared:1274 - fuse.sshfs '
                'ubuntu@exporter:/work rw,user_id=0,group_id=0,default_permissions,allow_other\n')
        table = '265 46 0:48 / /tmp rw shared:230 - tmpfs tmpfs rw\n' + bind + fuse
        files['rw-cleanup-before.mountinfo'] = table
        files['rw-after-stack.mountinfo'] = table
        encode = lambda path: files[path.name] if path.suffix == '.mountinfo' else json.dumps(files[path.name])
        rehearsal = spec['Rehearsal'](Path('/tmp/evidence'), io.StringIO())
        with patch.dict(rehearsal.recover_completed_rw.__globals__,
                        {'private_directory': Mock(), 'text': encode, 'directory_present': lambda _: False}), patch.object(
                rehearsal, 'verify_empty', return_value={}) as verify, patch.object(
                rehearsal, 'rw_graph', side_effect=[({'9599': bind.replace('6384', '9599')}, {}), ({}, {})]), patch.object(
                rehearsal, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'differs from the retained host graph'):
                rehearsal.recover_completed_rw()
            self.assertEqual(rehearsal.recover_completed_rw(), ('confirmed_absent', 0))
            verify.assert_called_once_with(root)
            command.assert_not_called()

        files.update({'marker': 'agentic-mounted-runtime', 'access-mode': 'rw', 'workspace.json': {
            'services': {'exporter': {'volumes': [spec['bind'](root / 'client', '/probe/work')]}}}})
        encode = lambda path: (files[path.name] if path.suffix != '.json' else json.dumps(files[path.name]))
        files['attempt.json']['snapshot'] = {'directories': [], 'files_sha256': {}}
        graph = ({'6384': bind.strip()}, {'9516': fuse.strip()})
        ident = 'a' * 64
        project = {'com.docker.compose.project=' + scope: {'containers': [ident], 'networks': []}}
        timeout = subprocess.TimeoutExpired(['docker'], 8, output=b'partial stdout', stderr=b'partial stderr')
        with patch.dict(rehearsal.recover_completed_rw.__globals__, {
                'private_directory': Mock(), 'text': encode, 'directory_present': lambda _: True,
                'run': Mock(side_effect=timeout)}), patch.object(
                spec['baf_attempt'], 'directory', return_value=contextlib.nullcontext(0)), patch.object(
                spec['baf_attempt'], 'inventory', return_value=([], {})), patch.object(
                rehearsal, 'rw_graph', return_value=graph), patch.object(
                rehearsal, 'verify_previous_rw_scope', return_value=project), patch.object(
                rehearsal, 'finish_rw_mounts') as finish:
            with self.assertRaisesRegex(RuntimeError, 'Scoped containers remain'):
                rehearsal.recover_completed_rw()
            finish.assert_not_called()
            self.assertEqual(rehearsal.summary['stages']['cleanup']['docker_removals_timed_out'], [ident])
            partial_log = json.loads(rehearsal.log.getvalue())
            self.assertEqual((partial_log['stdout'], partial_log['stderr']), ('partial stdout', 'partial stderr'))

    def test_recovery_cli_never_runs_setup_attempt_or_grade(self):
        script = PREPARE.with_name('live_b6_mock.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        Rehearsal = spec['Rehearsal']
        with tempfile.TemporaryDirectory() as folder, patch(
                'tempfile.mkdtemp', return_value=folder), patch.object(
                sys, 'argv', [str(script), '--recover-zj224gf6']), patch('signal.signal'), patch.dict(
                os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}), patch.object(
                Rehearsal, 'command') as command, patch.object(
                Rehearsal, 'recover_completed_rw', return_value=('recovered', 0)) as recover, patch.object(
                Rehearsal, 'setup', side_effect=AssertionError('recovery may not launch setup')), patch.object(
                Rehearsal, 'attempt', side_effect=AssertionError('recovery may not launch an attempt')), patch.object(
                Rehearsal, 'grade', side_effect=AssertionError('recovery may not run a grade')), contextlib.redirect_stdout(
                io.StringIO()):
            self.assertEqual(spec['main'](), 0)
            result = json.loads((Path(folder) / 'summary.json').read_text())
        self.assertEqual(result['mode'], 'mounted_mock_cleanup_recovery')
        self.assertEqual(result['source_evidence'], str(spec['RECOVERY_EVIDENCE']))
        self.assertEqual(set(result['stages']), {'preflight', 'cleanup'})
        self.assertEqual(result['stages']['cleanup']['status'], 'recovered')
        command.assert_called_once_with(['sudo', '-n', '-v'])
        recover.assert_called_once()

    def test_two_host_laptop_uses_stock_v2_client_through_one_session(self):
        from track_a.client import Client

        spec = runpy.run_path(str(PREPARE.with_name('two_host_laptop.py')))
        tag = '1' * 20
        with tempfile.TemporaryDirectory(prefix='chp2-test.', dir='/tmp') as directory:
            export = Path(directory) / 'work'
            export.mkdir()
            credentials = Path(directory) / 'credentials.json'
            credentials.write_text(json.dumps(dict(tag=tag, token='fake-token', grant='fake-grant', desktop='desktop')))
            credentials.chmod(0o600)
            requests = []

            def request(client, method, path, payload=None, timeout=20):
                self.assertEqual((client.base, client.token), ('http://127.0.0.1:24180', 'fake-token'))
                requests.append((method, path, payload))
                if path == '/v0/health':
                    return 200, dict(status='ok', version='v2', execution='mounted', client_mount=True)
                if path == '/v2/sessions':
                    self.assertEqual(payload, {'request_id': tag, 'grant': 'fake-grant'})
                    return 200, {'id': 'session'}
                if path == '/v2/sessions/session/runs':
                    operation = payload['request_id']
                    self.assertEqual(payload['message'], f'{operation}-{tag}')
                    if operation == 'write':
                        (export / f'agent-{tag}.txt').write_text(f'agent:{tag}\n')
                    else:
                        self.assertEqual((export / f'client-{tag}.txt').read_text(), f'client:{tag}\n')
                    return 200, {'id': operation}
                if method == 'GET':
                    return 200, dict(id=path.rsplit('/', 1)[1], session_id='session',
                                     result={'status': 'completed', 'cleanup_state': 'confirmed'})
                self.assertEqual(path, '/v2/sessions/session/close')
                return 200, {'status': 'closed'}

            output = io.StringIO()
            with patch.object(Client, 'request', request), patch('socket.gethostname', return_value='laptop'), patch(
                    'os.umask'), patch.dict(os.environ, {}), patch.object(
                    sys, 'argv', ['laptop.py', str(export), str(credentials)]), contextlib.redirect_stdout(output):
                self.assertEqual(spec['main'](), 0)
            report = json.loads(output.getvalue())
            self.assertEqual((report['host'], report['session'], report['runs'], report['closed']),
                             ('laptop', 'session', ['write', 'read'], True))
            self.assertNotIn('fake-token', output.getvalue())
            self.assertNotIn('fake-grant', output.getvalue())
            self.assertEqual(requests[-1][:2], ('POST', '/v2/sessions/session/close'))

    def test_two_host_retains_tool_evidence_after_actual_application_close(self):
        import base64

        from track_a.baf_smoke import LocalClient
        from track_a.fixture_executor import FixtureExecutor

        script = PREPARE.with_name('live_two_host.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        tag = '2' * 20
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = MountedExecutor('/tmp/synthetic', 'grant', 'scope', 'image', root / 'executor')
            self.addCleanup(executor.containers.journal.db.close)
            app = spec['TwoHostApplication'](LocalClient(executor), LocalClient(spec['TwoHostModel'](tag)),
                                             {'laptop': 'token'}, root / 'application', mount_owner='laptop')
            client = LocalClient(app, 'laptop')
            success = SimpleNamespace(returncode=0)
            workspace = {}

            def execute(_name, call):
                return FixtureExecutor.apply(workspace, call)

            with patch.object(executor, 'check_mount'), patch.object(executor, 'run_container',
                    return_value=success), patch('track_a.mounted_executor.docker', return_value=success), patch.object(
                    executor, 'execute_tool', side_effect=execute), patch.object(executor.containers, 'remove'):
                session = client.v2('POST', '', {'grant': 'grant', 'request_id': tag})['id']
                self.addCleanup(app.store.journals[session].db.close)
                self.addCleanup(executor.store.journals[session].db.close)
                runs = []
                for phase in ('write', 'read'):
                    run = client.v2('POST', f'/{session}/runs',
                                    {'request_id': phase, 'message': f'{phase}-{tag}'})['id']
                    runs.append(run)
                    self.assertEqual(client.wait_run(session, run, 2)['status'], 'completed')
                    workspace[f'client-{tag}.txt'] = base64.b64encode(f'client:{tag}\n'.encode()).decode()
                self.assertEqual(client.v2('POST', f'/{session}/close', {})['status'], 'closed')
            self.assertNotIn('events', app.sessions[session])
            runner = spec['TwoHost'](root, io.StringIO())
            runner.tag = tag
            self.assertEqual(runner.wait_for_laptop(app), ('passed', 0))
            self.assertEqual(runner.summary['stages']['exchange']['runs'], runs)
            stored = json.loads((root / 'events.json').read_text())
            self.assertEqual(stored, app.closed_traces[session]['events'])
            self.assertEqual(stored[-1]['payload']['status'], 'completed')

    def test_two_host_resume_accepts_fat_credentials_only_for_the_confirmed_pre_mount_stop(self):
        script = PREPARE.with_name('live_two_host.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            spec = runpy.run_path(str(script))
        validate = spec['resume_credentials']
        tag, root, evidence = spec['RESUME_TAG'], spec['RESUME_ROOT'], spec['RESUME_EVIDENCE']
        scope = spec['scope_for'](root)
        empty = {'containers': [], 'networks': []}
        report = dict(status='failed', mode='two_host_mounted_mock', desktop='desktop', tag=tag,
                      root=str(root), evidence=str(evidence), scope=scope, stages={
                          'prepare': {'status': 'passed', 'exit_code': 0},
                          'exchange': {'status': 'failed', 'exit_code': 1, 'reason': spec['READINESS_ERROR']},
                          'cleanup': {'status': 'passed', 'exit_code': 0, 'mount_ids': [[], []], 'docker': {
                              'com.docker.compose.project=' + scope: empty,
                              'agentic.scope=mounted-' + scope: empty,
                          }},
                      })
        token, grant = 'a' * 48, 'b' * 48
        with tempfile.TemporaryDirectory() as folder, patch.dict(validate.__globals__, {
                'private_directory': Mock(), 'text': lambda _: json.dumps(report)}), patch(
                'os.path.lexists', return_value=False):
            credential = Path(folder) / 'credentials.json'
            credential.write_text(json.dumps(dict(tag=tag, desktop='desktop', token=token, grant=grant)))
            credential.chmod(0o755)
            self.assertEqual(validate(credential, 'desktop'), (tag, token, grant))
            alias = Path(folder) / 'alias'
            alias.symlink_to(credential)
            with self.assertRaisesRegex(RuntimeError, 'Could not read'):
                validate(alias, 'desktop')
            with self.assertRaisesRegex(RuntimeError, 'previous pre-mount stop'):
                validate(credential, 'other-host')
            report['stages']['cleanup']['mount_ids'][0].append('999')
            with self.assertRaisesRegex(RuntimeError, 'confirmed cleanup'):
                validate(credential, 'desktop')

    def test_paired_laptop_requires_fresh_health_proof_before_credential_derivation(self):
        script = PREPARE.with_name('live_two_host.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            server = runpy.run_path(str(script))
            laptop = runpy.run_path(str(script.with_name('two_host_laptop.py')))
        pairing = server['pairing']
        seed, tag = 'a' * 64, 'b' * 20
        handler = server['PairedHealthHandler'].__new__(server['PairedHealthHandler'])
        handler.server = SimpleNamespace(app=SimpleNamespace(
            paired_seed=seed, paired_tag=tag, health=lambda: (200, dict(laptop['HEALTH']))))
        handler.reply = Mock()
        client = Mock()

        def request(method, path, *, timeout):
            self.assertEqual(method, 'GET')
            handler.path = path
            handler.do_GET()
            return handler.reply.call_args.args

        client.request.side_effect = request
        self.assertEqual(laptop['wait_challenge'](client, seed), tag)
        self.assertEqual(set(handler.reply.call_args.args[1]) - laptop['HEALTH'].keys(),
                         {'paired_tag', 'paired_proof'})
        first = pairing.credentials(seed, tag)
        self.assertEqual(pairing.credentials(seed, tag), first)
        self.assertTrue(all(left != right for left, right in zip(first, pairing.credentials(seed, 'c' * 20))))
        self.assertNotEqual(*first)
        with self.assertRaisesRegex(RuntimeError, 'health authentication failed'):
            laptop['wait_challenge'](client, 'c' * 64)
        prior_reply = handler.reply.call_args.args
        client.request.side_effect = None
        client.request.return_value = prior_reply
        with self.assertRaisesRegex(RuntimeError, 'health authentication failed'):
            laptop['wait_challenge'](client, seed)

    def test_paired_transport_recovers_a_missing_forward_without_replacing_the_other(self):
        script = PREPARE.with_name('two_host_transport.py')
        with patch.object(sys, 'path', [str(script.parent), *sys.path]):
            transport = runpy.run_path(str(script))

        def existing_reverse(args, **kwargs):
            if '-R' in args:
                kwargs['stderr'].write(b'Error: remote port forwarding failed for listen port 24222\n')
                return SimpleNamespace(returncode=255)
            return SimpleNamespace(returncode=0)

        with patch('subprocess.run', side_effect=existing_reverse) as command, patch(
                'socket.create_connection', side_effect=ConnectionRefusedError):
            transport['laptop_forwards'](Path('/tmp/test-pair'))
        first, second = (entry.args[0] for entry in command.call_args_list)
        self.assertIn('-R', first)
        self.assertNotIn('-L', first)
        self.assertIn('-L', second)
        self.assertNotIn('-R', second)
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as command, patch(
                'socket.create_connection', return_value=contextlib.nullcontext()):
            transport['laptop_forwards'](Path('/tmp/test-pair'))
        command.assert_called_once()
        self.assertIn('-R', command.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
