"""Synthetic RunCommand payloads for the mounted checkpoint host rehearsal."""

TIMEOUT = r"""
import os
import time
with open('checkpoint2-timeout.txt', 'ab', buffering=0) as marker:
    marker.write(b't08-once\n')
    os.fsync(marker.fileno())
print('MARKER_SYNCED', flush=True)
time.sleep(9)
print('DEADLINE_MISSED', flush=True)
"""

NETWORK = """
import json
import os
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
    client.settimeout(1)
    error = client.connect_ex(('127.0.0.1', PORT))
print(json.dumps({'errno': error, 'namespace': os.readlink('/proc/self/ns/net'),
                  'interfaces': sorted(name for _, name in socket.if_nameindex())}))
"""

CGROUP = """
import json
from pathlib import Path
if '0::/' not in Path('/proc/self/cgroup').read_text().splitlines():
    raise RuntimeError('Expected the container at the cgroup v2 namespace root')
root = Path('/sys/fs/cgroup')
limits = {name: (root / name).read_text().strip()
          for name in ('cpu.max', 'pids.max', 'memory.max', 'memory.swap.max')}
quota, period = map(int, limits['cpu.max'].split())
if quota != period or quota <= 0 or (limits['pids.max'], limits['memory.max'],
                                     limits['memory.swap.max']) != ('64', '268435456', '0'):
    raise RuntimeError('Unexpected mounted cgroup limits: ' + repr(limits))
print(json.dumps(limits), flush=True)

def counters(name):
    return {key: int(value) for key, value in
            (line.split() for line in (root / name).read_text().splitlines())}
"""

CPU = CGROUP + """
import os
import time
assert len(os.sched_getaffinity(0)) >= 2, 'CPU probe requires two allowed logical CPUs'
before = counters('cpu.stat')
end = time.monotonic() + 2
pid = os.fork()
while time.monotonic() < end:
    pass
if pid == 0:
    os._exit(0)
assert os.waitpid(pid, 0)[1] == 0
after = counters('cpu.stat')
keys = ('nr_throttled', 'throttled_usec')
print(json.dumps({phase: {key: values[key] for key in keys}
                  for phase, values in [('before', before), ('after', after)]}))
"""

PIDS = CGROUP + """
import errno
import os
import signal
signal.alarm(4)
before = counters('pids.events')['max']
initial = int((root / 'pids.current').read_text())
assert 0 < initial < 64
children = []
reader, writer = os.pipe()
error = None
try:
    for _ in range(65 - initial):
        try:
            pid = os.fork()
        except OSError as failure:
            error = failure.errno
            break
        if pid == 0:
            os.close(writer)
            os.read(reader, 1)
            os._exit(0)
        children.append(pid)
    current = int((root / 'pids.current').read_text())
    after = counters('pids.events')['max']
finally:
    os.close(reader)
    os.close(writer)
    for pid in children:
        os.waitpid(pid, 0)
reaped = int((root / 'pids.current').read_text())
print(json.dumps(dict(initial=initial, current=current, children=len(children),
                      errno=error, before=before, after=after, reaped=reaped)))
"""

MEMORY = CGROUP + """
import os
import signal
import time
# Keep the trusted worker and the measuring parent out of the allocation loop.
assert (root / 'memory.oom.group').read_text().strip() == '0'
signal.alarm(5)
before = counters('memory.events')
pid = os.fork()
if pid == 0:
    signal.alarm(4)
    Path('/proc/self/oom_score_adj').write_text('1000')
    blocks = []
    for _ in range(272):
        block = bytearray(1024 * 1024)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
        time.sleep(.003)
    os._exit(0)
_, status = os.waitpid(pid, 0)
after = counters('memory.events')
keys = ('max', 'oom', 'oom_kill')
print(json.dumps(dict(
    child_exit=os.waitstatus_to_exitcode(status),
    before={key: before[key] for key in keys},
    after={key: after[key] for key in keys})))
"""

HANG = """
import time
from pathlib import Path
Path('/tmp/t08-io-ready').touch()
deadline = time.monotonic() + 5
while not Path('/tmp/t08-io-go').exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('I/O handshake timed out')
    time.sleep(.02)
# A previously untouched path forces a request to the paused FUSE daemon.
Path('checkpoint2-cold-read.txt').read_bytes()
Path('/tmp/t08-io-done').touch()
"""
