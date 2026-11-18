"""Control-plane regressions for the mounted executor, without Docker or FUSE."""
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from track_a.http_api import Conflict
from track_a.mounted_executor import MountedExecutor

PREPARE = Path(__file__).parent / 'prototypes/mounted-runtime/prepare.py'


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


if __name__ == '__main__':
    unittest.main()
