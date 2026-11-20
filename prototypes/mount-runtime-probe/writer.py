"""Start one detached child, then let the initial exec process exit."""
import os
import subprocess
import sys

source = """
import time
from pathlib import Path

for tick in range(200):
    Path('/work/heartbeat.txt').write_text(str(tick))
    time.sleep(0.1)
"""
child = subprocess.Popen(
    [sys.executable, "-c", f"exec({source!r})", sys.argv[1]],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
if os.getsid(child.pid) != child.pid or os.getpgid(child.pid) != child.pid:
    raise RuntimeError("Writer is not in a separate session/process group")
print(child.pid)
