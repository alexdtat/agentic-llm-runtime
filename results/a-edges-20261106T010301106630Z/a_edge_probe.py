"""Additional technical diagnostics; no existing experiment is modified."""
import json
import shutil
import time
import traceback

from a_fixture import Fixture
from common import Commands, ROOT, new_run, save_json


def main():
    out = new_run("a-edges")
    for name in ["a_edge_probe.py", "a_fixture.py", "common.py"]:
        shutil.copyfile(ROOT / name, out / name)
    commands = Commands(out)
    result = {"scope":"technical_failure_modes", "model_calls":0, "gpu_used":False,
              "limitations":["Single physical host; no production hardening",
                             "One deterministic interleaving, not a concurrency benchmark"]}
    start = time.monotonic()
    try:
        with Fixture(commands) as f:
            f.mount("/work_ro", readonly=True)
            event = f.python("""import json,errno
try:
 open('/work_ro/forbidden.txt','w').write('must-not-exist')
 print(json.dumps({'write_succeeded':True}))
except OSError as e:
 print(json.dumps({'write_succeeded':False,'errno':e.errno,'error':str(e)}))
""")
            result["readonly"] = json.loads(event["stdout"])
            result["readonly"]["client_file_created"] = (f.shared / "forbidden.txt").exists()
            # Two independent actors read the same old value, then each writes its increment.
            counter = f.shared / "counter.txt"
            counter.write_text("0")
            client_snapshot = int(counter.read_text())
            sandbox_snapshot = int(f.python("print(open('/work/counter.txt').read())")["stdout"])
            f.python(f"open('/work/counter.txt','w').write('{sandbox_snapshot+1}')")
            counter.write_text(str(client_snapshot+1))
            result["concurrent_edit_interleaving"] = {
                "client_read":client_snapshot, "sandbox_read":sandbox_snapshot,
                "final_value":int(counter.read_text()), "logical_increments":2,
                "interpretation":"Last-writer-wins; SSHFS does not merge application updates or report this conflict"}
            # This is a fake .git directory in the generated fixture, never the user's repo.
            (f.shared / ".git").mkdir()
            (f.shared / ".git/config").write_text("fictional-git-canary")
            visible = f.python("print(open('/work/.git/config').read())")
            result["git_metadata_visible"] = visible["stdout"].strip() == "fictional-git-canary"
            # The transport credential is overpowered in this diagnostic setup.
            f.python("open('/tmp/outside-workdir-canary','w').write('fictional-client-outside-canary')",
                     container=f.client)
            outside = commands.docker("exec", f.sandbox, "ssh", *f.ssh_options,
                                       "root@client", "cat /tmp/outside-workdir-canary", check=False)
            result["credential_can_read_outside_export"] = (
                outside["returncode"] == 0 and "fictional-client-outside-canary" in outside["stdout"])
            writer = """import json,os,time
from pathlib import Path
state=Path('/tmp/writer-state.json')
def save(v):
 t=state.with_suffix('.tmp');t.write_text(json.dumps(v));t.replace(state)
r={'phase':'opening','acked_before':0,'acked_after':0}
try:
 with open('/work/interrupted.txt','wb',buffering=0) as stream:
  r['acked_before']=stream.write(b'acknowledged-before-outage\\n');os.fsync(stream.fileno())
  r['phase']='ready';save(r)
  deadline=time.monotonic()+10
  while not Path('/tmp/write-go').exists():
   if time.monotonic()>deadline:raise TimeoutError('no disconnect signal')
   time.sleep(.02)
  try:
   r['acked_after']=stream.write(b'after-disconnect\\n');os.fsync(stream.fileno())
  except OSError as e:r['write_error']={'errno':e.errno,'message':str(e)}
 r['phase']='complete'
except Exception as e:r['phase']='error';r['error']=str(e)
save(r)
"""
            commands.docker("exec", "-d", f.sandbox, "python3", "-c", writer)
            state = None
            for _ in range(30):
                event = f.python("from pathlib import Path; p=Path('/tmp/writer-state.json'); print(p.read_text() if p.exists() else '{}')")
                state = json.loads(event["stdout"])
                if state.get("phase") == "ready":
                    break
                time.sleep(.1)
            if not state or state.get("phase") != "ready":
                raise RuntimeError("Writer did not reach its pre-disconnect barrier")
            before = (f.shared / "interrupted.txt").read_bytes()
            f.disconnect()
            f.python("from pathlib import Path; Path('/tmp/write-go').touch()")
            for _ in range(40):
                event = f.python("print(open('/tmp/writer-state.json').read())")
                state = json.loads(event["stdout"])
                if state.get("phase") in {"complete","error"}:
                    break
                time.sleep(.1)
            after = (f.shared / "interrupted.txt").read_bytes()
            result["interrupted_write"] = {"writer":state,
                "client_before":before.decode(), "client_after":after.decode(),
                "acknowledged_prefix_preserved":after.startswith(before),
                "writer_finished":state.get("phase") in {"complete","error"}}
            f.reconnect()
        result["completed"] = True
    except Exception:
        result["error"] = traceback.format_exc()
        result["completed"] = False
    result["seconds"] = time.monotonic()-start
    save_json(out / "summary.json", result)
    print(json.dumps({"result":str(out), **result},ensure_ascii=False,indent=2))
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
