"""Exercise the mounted limits smoke with a real executor and offline Docker."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from track_a.mounted_executor import MountedExecutor


SCRIPT = Path(__file__).resolve().parent / 'prototypes/mounted-runtime/smoke_limits.py'
CANARY = 'MOUNT_LIMITS_CANARY'
TOKEN = 'synthetic-test-value'
PROC_FILES = {
    '/proc/self/cgroup': '0::/\n',
    '/sys/fs/cgroup/cpu.max': '80000 80000\n',
    '/sys/fs/cgroup/memory.max': '268435456\n',
    '/sys/fs/cgroup/memory.swap.max': '0\n',
    '/sys/fs/cgroup/pids.max': '64\n',
    '/proc/self/status': ('Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1000\t1000\t1000\n'
                          'CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nNoNewPrivs:\t1\n'),
    '/proc/self/mountinfo': ('21 1 0:11 / / ro - overlay overlay rw\n'
                             '22 21 0:12 / /tmp rw - tmpfs tmpfs rw,size=16384k\n'
                             '23 21 0:13 / /work rw - fuse.sshfs client:/work rw\n'),
}


class MountedLimitsSmoke(unittest.TestCase):
    def setUp(self):
        with patch.object(sys, 'path', [str(SCRIPT.parent), *sys.path]):
            self.check_limits = runpy.run_path(str(SCRIPT))['check_limits']
        self.executor = MountedExecutor('/synthetic/export', 'grant', 'test', 'image', None)
        self.addCleanup(self.executor.containers.journal.db.close)
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def run_smoke(self, *, files=PROC_FILES, host_canary=True, sandbox_canary=False,
                  release_unconfirmed=False):
        host_env = {CANARY: TOKEN} if host_canary else {}
        sandbox_env = {CANARY: TOKEN} if sandbox_canary else {}

        def docker(argv, *, input, **kwargs):
            self.assertEqual(argv[0], 'docker')
            args = tuple(argv[1:])
            stdout = ''
            if args[:2] == ('exec', '-i'):
                self.assertIn(CANARY, os.environ)
                request = json.loads(input)
                self.assertEqual(request['argv'][:2], ['/bin/sh', '-c'])
                self.assertNotIn(TOKEN, input)
                command = shlex.split(request['argv'][2])
                self.assertEqual(command[:3], ['exec', 'python3', '-c'])
                with (patch.dict(os.environ, sandbox_env, clear=True),
                      patch.object(Path, 'read_text', autospec=True,
                                   side_effect=lambda path: files[str(path)]),
                      patch('os.statvfs', return_value=SimpleNamespace(f_blocks=4096, f_frsize=4096)),
                      patch('os.lstat', side_effect=FileNotFoundError),
                      patch('socket.if_nameindex', return_value=[(1, 'lo')]),
                      contextlib.redirect_stdout(io.StringIO()) as observed):
                    exec(compile(command[3], '<RunCommand>', 'exec'), {})
                self.assertLessEqual(len(observed.getvalue()), request['output_bytes'])
                stdout = json.dumps(dict(exit_code=0, stdout=observed.getvalue(), stderr='',
                                         truncated=False, error_kind=None))
            else:
                self.assertIn(args[0], {'run', 'exec', 'rm'})
            return subprocess.CompletedProcess(args, 0, stdout, '')

        def route(method, path, body):
            result = original_route(method, path, body)
            if release_unconfirmed and path.endswith('/release'):
                return 200, {'cleanup_state': 'unknown'}
            return result

        original_route = self.executor.route
        with (patch.dict(os.environ, host_env, clear=True),
              patch.object(self.executor, 'check_mount'),
              patch.object(self.executor, 'route', side_effect=route) as routed,
              patch('track_a.executor.subprocess.run', side_effect=docker) as cli,
              contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr)):
            code = self.check_limits(self.executor)

        for journal in self.executor.store.journals.values():
            self.addCleanup(journal.db.close)
        self.assertNotIn(TOKEN, self.stdout.getvalue() + self.stderr.getvalue())
        if host_canary:
            paths = [call.args[1] for call in routed.call_args_list]
            base = paths[0].rsplit('/', 1)[0]
            self.assertEqual(paths, [base + '/' + action for action in
                                     ('provision', 'start', 'exec', 'release')])
            run = routed.call_args_list[1].args[2]
            invocation = routed.call_args_list[2].args[2]
            self.assertEqual(run, {name: invocation[name] for name in ('generation', 'run_id')})
            self.assertEqual(invocation['call']['tool'], 'RunCommand')
            sid = base.rsplit('/', 1)[1]
            self.assertTrue(self.executor.sessions[sid]['closed'])
            name = self.executor.name({'id': sid}, run['run_id'])
            launch, ready, command, removal = [tuple(call.args[0][1:]) for call in cli.call_args_list]
            self.assertEqual(launch[launch.index('--name') + 1], name)
            self.assertEqual(ready[:2], ('exec', name))
            self.assertEqual(command[:3], ('exec', '-i', name))
            self.assertEqual(removal, ('rm', '-f', name))
        else:
            routed.assert_not_called()
            cli.assert_not_called()
        return code

    def test_success_measures_limits_through_run_command_and_confirms_release(self):
        self.assertEqual(self.run_smoke(), 0)
        self.assertEqual(self.stderr.getvalue(), '')
        host, release, result = map(json.loads, self.stdout.getvalue().splitlines())
        self.assertIs(host['host_canary_present'], True)
        self.assertEqual(release['cleanup_state'], 'confirmed')
        self.assertEqual(result['status'], 'passed')
        self.assertIs(result['sandbox_canary_present'], False)

    def test_one_wrong_limit_fails_with_values_and_still_confirms_release(self):
        files = {**PROC_FILES, '/sys/fs/cgroup/pids.max': '63\n'}
        self.assertEqual(self.run_smoke(files=files), 1)
        self.assertEqual(self.stderr.getvalue(), 'pids.max: expected "64", actual "63"\n')
        self.assertIn('"cleanup_state": "confirmed"', self.stdout.getvalue())
        self.assertNotIn('"status": "passed"', self.stdout.getvalue())

    def test_leaked_canary_and_unconfirmed_release_are_both_reported(self):
        self.assertEqual(self.run_smoke(sandbox_canary=True, release_unconfirmed=True), 1)
        self.assertEqual(self.stderr.getvalue().splitlines(), [
            'sandbox_canary_present: expected false, actual true',
            'release cleanup_state: expected "confirmed", actual "unknown"',
        ])

    def test_missing_host_canary_stops_before_provision(self):
        self.assertEqual(self.run_smoke(host_canary=False), 1)
        self.assertEqual(self.stderr.getvalue(), 'host_canary_present: expected true, actual false\n')


if __name__ == '__main__':
    unittest.main()
