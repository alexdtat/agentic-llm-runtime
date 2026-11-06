"""Isolated SSHFS diagnostic fixture; never exports an existing user folder."""
import json
import time
import uuid

from common import IMAGE, ROOT


class Fixture:
    def __init__(self, commands):
        self.commands = commands
        token = uuid.uuid4().hex[:10]
        self.network = "course-probe-net-" + token
        self.client = "course-probe-client-" + token
        self.sandbox = "course-probe-sandbox-" + token
        self.folder = ROOT / "runtime" / ("a-audit-" + token)
        self.shared = self.folder / "client-workdir"
        self.keys = self.folder / "keys"

    def __enter__(self):
        try:
            self.shared.mkdir(parents=True)
            self.keys.mkdir()
            self.commands.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.keys / "id")])
            config = self.folder / "sshd_config"
            config.write_text("\n".join([
                "Port 2222", "HostKey /etc/ssh/ssh_host_ed25519_key",
                "AuthorizedKeysFile /keys/id.pub", "PermitRootLogin prohibit-password",
                "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no",
                "StrictModes no", "Subsystem sftp internal-sftp", ""]))
            self.commands.docker("network", "create", "--internal", self.network)
            common = ["--runtime", "runc", "--init", "--cpus", "1", "--memory", "512m", "--pids-limit", "128"]
            self.commands.docker("run", "-d", "--name", self.client, *common,
                                 "--network", self.network, "--network-alias", "client", "--mount",
                                 f"type=bind,src={self.shared},dst=/client-workdir", "--mount",
                                 f"type=bind,src={self.keys},dst=/keys,readonly", "--mount",
                                 f"type=bind,src={config},dst=/probe-sshd.conf,readonly", IMAGE,
                                 "sh", "-c", "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -f /probe-sshd.conf")
            self.commands.docker("run", "-d", "--name", self.sandbox, *common,
                                 "--network", self.network, "--device", "/dev/fuse",
                                 "--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined",
                                 "--mount", f"type=bind,src={self.keys},dst=/keys,readonly", IMAGE)
            self.ssh_options = ["-p", "2222", "-i", "/keys/id", "-o", "StrictHostKeyChecking=no",
                                "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=3"]
            self.mount("/work")
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def mount(self, path, readonly=False):
        self.commands.docker("exec", self.sandbox, "mkdir", "-p", path)
        argv = ["exec", self.sandbox, "sshfs", "root@client:/client-workdir", path,
                "-p", "2222", "-o", "IdentityFile=/keys/id", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "reconnect",
                "-o", "ServerAliveInterval=1", "-o", "ServerAliveCountMax=2",
                "-o", "ConnectTimeout=3", "-o", "dir_cache=no", "-o", "direct_io"]
        if readonly:
            argv += ["-o", "ro"]
        event = self.commands.docker(*argv, check=False)
        if event["returncode"]:
            time.sleep(.5)
            self.commands.docker(*argv)

    def python(self, code, *, timeout=15, check=True, container=None):
        return self.commands.docker("exec", container or self.sandbox, "python3", "-c", code,
                                    timeout=timeout, check=check)

    def disconnect(self):
        state = json.loads(self.commands.docker("inspect", self.client)["stdout"])[0]
        self.ip = state["NetworkSettings"]["Networks"][self.network]["IPAddress"]
        self.commands.docker("network", "disconnect", self.network, self.client)

    def reconnect(self):
        self.commands.docker("network", "connect", "--alias", "client", "--ip", self.ip,
                             self.network, self.client)

    def __exit__(self, *args):
        for name in [self.sandbox, self.client]:
            self.commands.docker("rm", "-f", name, check=False)
        self.commands.docker("network", "rm", self.network, check=False)
