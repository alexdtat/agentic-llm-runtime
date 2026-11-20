"""Measure limits inside one run of an already prepared mounted RW stack."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from smoke_readonly import LOCAL, STACK
from track_a.mounted_executor import MountedExecutor


CANARY = 'MOUNT_LIMITS_CANARY'
SANDBOX_SCRIPT = """
import json
import os
import socket
from pathlib import Path


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def present(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


root_cgroup = '0::/' in read('/proc/self/cgroup').splitlines()
observed = {'cgroup_v2_root': root_cgroup}
for name in ('cpu.max', 'memory.max', 'memory.swap.max', 'pids.max'):
    observed[name] = (read('/sys/fs/cgroup/' + name) or None) if root_cgroup else None

status = dict(line.split(':', 1) for line in read('/proc/self/status').splitlines())
for name in ('Uid', 'Gid'):
    observed[name] = status.get(name, '').split()
for name in ('CapEff', 'CapBnd', 'NoNewPrivs'):
    observed[name] = status.get(name, '').strip() or None

mounts = {}
for line in read('/proc/self/mountinfo').splitlines():
    mount, filesystem = line.split(' - ', 1)
    target, options = mount.split()[4:6]
    fstype, _, superoptions = filesystem.split()
    if target in ('/', '/tmp', '/work'):
        flags = set(options.split(',')) | set(superoptions.split(','))
        mounts[target] = (fstype, 'ro' if 'ro' in flags else 'rw')

observed['/ mode'] = mounts.get('/', (None, None))[1]
observed['/tmp type'] = mounts.get('/tmp', (None, None))[0]
observed['/work type'], observed['/work mode'] = mounts.get('/work', (None, None))
try:
    space = os.statvfs('/tmp')
except OSError:
    observed['/tmp bytes'] = None
else:
    observed['/tmp bytes'] = space.f_blocks * space.f_frsize

observed['interfaces'] = sorted(name for _, name in socket.if_nameindex())
observed['docker_socket_present'] = any(present(path) for path in
                                        ('/var/run/docker.sock', '/run/docker.sock'))
observed['sandbox_canary_present'] = 'MOUNT_LIMITS_CANARY' in os.environ
print(json.dumps(observed))
"""
EXPECTED = {
    'cgroup_v2_root': True,
    'memory.max': '268435456',
    'memory.swap.max': '0',
    'pids.max': '64',
    'Uid': ['1000'] * 4,
    'Gid': ['1000'] * 4,
    'CapEff': '0000000000000000',
    'CapBnd': '0000000000000000',
    'NoNewPrivs': '1',
    '/ mode': 'ro',
    '/tmp type': 'tmpfs',
    '/tmp bytes': 16 * 1024 * 1024,
    '/work type': 'fuse.sshfs',
    '/work mode': 'rw',
    'interfaces': ['lo'],
    'docker_socket_present': False,
    'sandbox_canary_present': False,
}


class CheckFailed(RuntimeError):
    pass


def mismatch(name, expected, actual):
    return f'{name}: expected {json.dumps(expected)}, actual {json.dumps(actual)}'


def require_equal(name, expected, actual):
    if actual != expected:
        raise CheckFailed(mismatch(name, expected, actual))


def request(executor, base, action, body):
    try:
        status, result = executor.route('POST', base + '/' + action, body)
    except Exception as error:
        # Do not echo transport errors or raw tool output.
        raise CheckFailed(mismatch(action, 'successful response', type(error).__name__)) from None
    require_equal(action + ' HTTP status', 200, status)
    return result


def verify(observed):
    differences = [mismatch(name, expected, observed.get(name))
                   for name, expected in EXPECTED.items() if observed.get(name) != expected]
    cpu = observed.get('cpu.max')
    quota_period = cpu.split() if isinstance(cpu, str) else []
    if (len(quota_period) != 2 or not all(value.isdecimal() for value in quota_period)
            or int(quota_period[0]) != int(quota_period[1]) or int(quota_period[0]) == 0):
        differences.append(mismatch('cpu.max', 'positive quota equal to period (1 CPU)', cpu))
    if differences:
        raise CheckFailed('\n'.join(differences))


def check_limits(executor):
    canary_present = bool(os.environ.get(CANARY))
    print(json.dumps({'host_canary_present': canary_present}), flush=True)
    if not canary_present:
        print(mismatch('host_canary_present', True, False), file=sys.stderr)
        return 1

    base = '/mounted/limits-' + uuid4().hex
    passed = False
    try:
        lease = request(executor, base, 'provision', {'grant': executor.grant})
        require_equal('provision closed', False, lease.get('closed'))
        run = {'generation': lease['generation'], 'run_id': 'limits'}
        request(executor, base, 'start', run)
        command = 'exec python3 -c ' + shlex.quote(SANDBOX_SCRIPT)
        call = {'id': 'measure', 'tool': 'RunCommand', 'args': {'command': command}}
        result = request(executor, base, 'exec', {**run, 'op_id': 'measure', 'call': call})
        require_equal('RunCommand exit code', 0, result.get('exit_code'))
        require_equal('RunCommand output truncated', False, result.get('truncated'))
        try:
            observed = json.loads(result['stdout'])
        except (ValueError, TypeError):
            observed = None
        if not isinstance(observed, dict):
            raise CheckFailed(mismatch('RunCommand output', 'JSON object', 'invalid'))
        verify(observed)
        passed = True
    except CheckFailed as error:
        print(error, file=sys.stderr)
    finally:
        try:
            release = request(executor, base, 'release', {})
            require_equal('release cleanup_state', 'confirmed', release.get('cleanup_state'))
            print(json.dumps({'cleanup_state': 'confirmed'}))
        except CheckFailed as error:
            print(error, file=sys.stderr)
            passed = False
    if passed:
        print(json.dumps({'status': 'passed', **{name: observed[name] for name in EXPECTED},
                          'cpu.max': observed['cpu.max']}))
    return 0 if passed else 1


def main():
    try:
        if os.environ.get('DOCKER_CONTEXT') or os.environ.get('DOCKER_HOST', LOCAL) != LOCAL:
            raise CheckFailed('Use the local Docker socket and unset DOCKER_CONTEXT')
        root = Path(os.environ['PROBE_ROOT'])
        scope = subprocess.check_output([str(STACK), 'scope'], text=True, timeout=5).strip()
        require_equal('stack access mode', 'rw', (root / 'access-mode').read_text().strip())
        os.environ['DOCKER_HOST'] = LOCAL
        executor = MountedExecutor(str(root / 'mount/work'), 'mounted-limits', scope,
                                   'agentic-sandbox:cpu-v1', root / 'limits-state')
    except CheckFailed as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print('Cannot configure mounted smoke: ' + type(error).__name__, file=sys.stderr)
        return 1
    return check_limits(executor)


if __name__ == '__main__':
    raise SystemExit(main())
