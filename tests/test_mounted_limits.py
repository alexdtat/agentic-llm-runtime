"""Offline checks of mounted Docker and worker arguments."""
import json
import subprocess
import unittest
from unittest.mock import patch

from track_a.mounted_executor import MountedExecutor


class MountedLimitContract(unittest.TestCase):
    def test_mounted_sandbox_and_collector_receive_the_same_resource_policy(self):
        executor = MountedExecutor("/synthetic/export", "grant", "scope", "image", None)
        expected = {
            "--runtime": "runc",
            "--cpus": "1",
            "--memory": "256m",
            "--memory-swap": "256m",
            "--pids-limit": "64",
            "--tmpfs": "/tmp:rw,size=16m,mode=1777",
            "--network": "none",
            "--user": "1000:1000",
            "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges",
        }
        with patch.object(executor, "check_mount"), patch("track_a.mounted_executor.docker") as docker:
            for detach, readonly in ((True, False), (False, True)):
                with self.subTest(detach=detach, readonly=readonly):
                    executor.run_container("test", "true", detach=detach, readonly=readonly)
                    args = docker.call_args.args
                    self.assertEqual(args[-2:], ("image", "true"))
                    options = args[2:-2]
                    for name, value in expected.items():
                        self.assertEqual(options.count(name), 1)
                        self.assertEqual(options[options.index(name) + 1], value)
                    self.assertIn("--read-only", options)
                    forbidden = {"--privileged", "--cap-add", "--env", "-e", "--env-file", "--volume", "-v"}
                    self.assertTrue(forbidden.isdisjoint(option.split("=", 1)[0] for option in options))
                    self.assertEqual(options.count("--mount"), 1)

    def test_mounted_command_passes_a_bounded_timeout_to_worker_and_docker(self):
        outcome = dict(exit_code=0, stdout="", stderr="", truncated=False, error_kind=None)
        with patch("track_a.mounted_executor.docker", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(outcome), ""
        )) as docker:
            actual = MountedExecutor.execute_tool("test", {"tool": "RunCommand", "args": {"command": "true"}})
        request = json.loads(docker.call_args.kwargs["input"])
        self.assertEqual(request["timeout_seconds"], 6)
        self.assertEqual(docker.call_args.kwargs["timeout"], 8)
        self.assertEqual(actual, outcome)


if __name__ == "__main__":
    unittest.main()
