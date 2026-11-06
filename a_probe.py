"""Actual SSHFS I/O, network interruption and process-tree cancellation.

No external ports, host SSH configuration, Docker socket mounts or GPUs.
FUSE permissions apply only to the diagnostic sandbox container.
"""
import json
import time
import uuid

from common import Commands, IMAGE, ROOT, new_run, save_json


def main():
    out = new_run("a")
    commands = Commands(out)
    token = uuid.uuid4().hex[:10]
    network = "course-probe-net-" + token
    client = "course-probe-client-" + token
    sandbox = "course-probe-sandbox-" + token
    work = ROOT / "runtime" / ("a-" + token)
    work.mkdir(parents=True)
    shared = work / "client-workdir"
    shared.mkdir()
    keys = work / "keys"
    keys.mkdir()
    result = {"probe": "A", "kind": "technical_diagnostic", "checks": {},
              "model_calls": 0, "gpu_used": False, "errors": [],
              "limitations": ["Same physical host, isolated container network namespaces",
                              "Diagnostic sandbox has SYS_ADMIN/FUSE/AppArmor exception",
                              "No desktop client, LLM harness, full hardening or BAF runner"]}
    started = time.monotonic()

    def dex(code, timeout=15, check=True):
        return commands.docker("exec", sandbox, "python3", "-c", code,
                               timeout=timeout, check=check)

    def remote_read(name):
        return dex(f"from pathlib import Path; print(Path('/work/{name}').read_text())")

    try:
        commands.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keys / "id")])
        config = work / "sshd_config"
        config.write_text("\n".join([
            "Port 2222", "ListenAddress 0.0.0.0", "HostKey /etc/ssh/ssh_host_ed25519_key",
            "AuthorizedKeysFile /keys/id.pub", "PermitRootLogin prohibit-password",
            "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no",
            "StrictModes no", "PidFile /run/probe-sshd.pid", "Subsystem sftp internal-sftp", ""]))
        commands.docker("network", "create", "--internal", network)
        common = ["--runtime", "runc", "--init", "--cpus", "1", "--memory", "512m",
                  "--pids-limit", "128", "--label", "course-feasibility=" + token]
        commands.docker("run", "-d", "--name", client, *common, "--network", network,
                        "--network-alias", "client", "--mount",
                        f"type=bind,src={shared},dst=/client-workdir", "--mount",
                        f"type=bind,src={keys},dst=/keys,readonly", "--mount",
                        f"type=bind,src={config},dst=/probe-sshd.conf,readonly", IMAGE,
                        "sh", "-c", "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -f /probe-sshd.conf")
        commands.docker("run", "-d", "--name", sandbox, *common, "--network", network,
                        "--device", "/dev/fuse", "--cap-add", "SYS_ADMIN",
                        "--security-opt", "apparmor=unconfined", "--mount",
                        f"type=bind,src={keys},dst=/keys,readonly", IMAGE)
        mount = ["docker", "exec", sandbox, "sh", "-c",
                 "mkdir -p /work && sshfs root@client:/client-workdir /work -p 2222 "
                 "-o IdentityFile=/keys/id -o StrictHostKeyChecking=no "
                 "-o UserKnownHostsFile=/dev/null -o reconnect "
                 "-o ServerAliveInterval=1 -o ServerAliveCountMax=2 "
                 "-o ConnectTimeout=3 -o dir_cache=no -o direct_io"]
        mounted = commands.run(mount, check=False)
        if mounted["returncode"] != 0:
            # Readiness retry only; the mounting mechanism and options are unchanged.
            time.sleep(1)
            commands.run(mount)
        info = commands.docker("exec", sandbox, "findmnt", "--json", "/work")
        result["mount"] = json.loads(info["stdout"])
        result["checks"]["real_fuse_mount"] = "fuse.sshfs" in info["stdout"]
        (shared / "client.txt").write_text("from-client-v1")
        result["checks"]["client_to_sandbox"] = remote_read("client.txt")["stdout"].strip() == "from-client-v1"
        dex("from pathlib import Path; Path('/work/sandbox.txt').write_text('from-sandbox-v1')")
        result["checks"]["sandbox_to_client"] = (shared / "sandbox.txt").read_text() == "from-sandbox-v1"
        (shared / "client.txt").write_text("from-client-v2")
        result["checks"]["client_edit_visible"] = remote_read("client.txt")["stdout"].strip() == "from-client-v2"
        dex("from pathlib import Path; Path('/work/sandbox.txt').write_text('from-sandbox-v2')")
        result["checks"]["sandbox_edit_visible"] = (shared / "sandbox.txt").read_text() == "from-sandbox-v2"
        cancel = dex("""import subprocess,os,signal,time,json
p=subprocess.Popen(['sh','-c','sleep 60 & wait'],start_new_session=True)
time.sleep(.2)
children=subprocess.check_output(['pgrep','-P',str(p.pid)],text=True).split()
os.killpg(p.pid,signal.SIGTERM)
p.wait(timeout=3)
time.sleep(.2)
alive=[]
for pid in children:
 try:
  state=open('/proc/'+pid+'/stat').read().split()[2]
  if state!='Z': alive.append(pid)
 except FileNotFoundError: pass
print(json.dumps({'parent_exit':p.returncode,'children':children,'running_children':alive}))
""")
        result["cancellation"] = json.loads(cancel["stdout"])
        result["checks"]["cancel_process_tree"] = not result["cancellation"]["running_children"]
        state_before = json.loads(commands.docker("inspect", client)["stdout"])[0]
        client_ip = state_before["NetworkSettings"]["Networks"][network]["IPAddress"]
        commands.docker("network", "disconnect", network, client)
        # A never-read path avoids mistaking a cached page for a live connection.
        (shared / "during-outage.txt").write_text("created-during-outage")
        failed = dex("from pathlib import Path; print(Path('/work/during-outage.txt').read_text())",
                     timeout=8, check=False)
        result["outage"] = {k: failed[k] for k in ["seconds", "returncode", "timed_out", "stderr"]}
        result["checks"]["outage_does_not_return_stale_new_file"] = failed["returncode"] != 0
        # Reconnection gets the same explicit IP; sshfs may have cached name resolution.
        client_state = commands.docker("inspect", client)
        result["disconnected_container_state"] = json.loads(client_state["stdout"])[0]["State"]["Status"]
        commands.docker("network", "connect", "--alias", "client", "--ip", client_ip,
                        network, client)
        recovered = None
        # Recovery polling is bounded and every attempt is recorded.
        for _ in range(5):
            recovered = dex("from pathlib import Path; print(Path('/work/during-outage.txt').read_text())",
                            timeout=5, check=False)
            if recovered["returncode"] == 0:
                break
            time.sleep(.3)
        result["checks"]["automatic_reconnect"] = recovered["returncode"] == 0
        if recovered["returncode"] != 0:
            commands.docker("exec", sandbox, "fusermount3", "-uz", "/work", check=False)
            commands.run(mount)
            recovered = remote_read("during-outage.txt")
        result["checks"]["recovery_with_remount_if_needed"] = recovered["stdout"].strip() == "created-during-outage"
        commands.docker("exec", sandbox, "fusermount3", "-u", "/work")
        released = commands.docker("exec", sandbox, "mountpoint", "-q", "/work", check=False)
        result["checks"]["mount_released"] = released["returncode"] != 0
    except Exception as exc:
        result["errors"].append(str(exc))
    finally:
        for name in [sandbox, client]:
            commands.docker("rm", "-f", name, check=False)
        commands.docker("network", "rm", network, check=False)
        # Only the ephemeral key created in this function is removed; public logs retain no key.
        # Keep the private key under ignored runtime for explicit user-controlled cleanup.
        result["seconds"] = time.monotonic() - started
        result["passed"] = bool(result["checks"]) and all(result["checks"].values()) and not result["errors"]
        save_json(out / "summary.json", result)
        print(json.dumps({"result": str(out), **result}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
