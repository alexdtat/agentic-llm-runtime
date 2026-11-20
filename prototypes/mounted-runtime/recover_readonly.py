"""Recover an interrupted RO smoke cleanup. The default invocation only reports a plan."""
import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

STACK = Path(__file__).resolve().with_name('stack.sh')
SOCKET = 'unix:///var/run/docker.sock'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_directory(path, pattern):
    require(re.fullmatch(pattern, str(path)), 'Unexpected diagnostic path')
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and path.resolve() == path,
            'Diagnostic directory must be private and owned by the current operator')


def text(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size <= 65536,
            f'Unexpected diagnostic metadata at {path}')
    return path.read_text()


def prepared_root(evidence):
    private_directory(evidence, r'/tmp/agentic-mounted-ro-evidence\.[a-zA-Z0-9]{8}')
    summary = json.loads(text(evidence / 'summary.json'))
    smoke = json.loads(text(evidence / 'smoke.stdout.log'))
    require(summary['status'] == 'failed' and summary['phase'] == 'smoke'
            and summary['probe_code'] == summary['check_code'] == 0 and summary['cleanup_code']
            and smoke['status'] == 'passed', 'Expected evidence of a successful smoke with failed cleanup')
    root = Path(summary['root'])
    private_directory(root, r'/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}')
    require(text(root / 'marker').strip() == 'agentic-mounted-runtime'
            and text(root / 'access-mode').strip() == 'ro', 'Expected a prepared RO stack')
    workspace = json.loads(text(root / 'workspace.json'))
    volumes = workspace['services']['exporter']['volumes']
    source = [volume for volume in volumes if volume['target'] == '/probe/work']
    require(len(source) == 1 and source[0]['source'] == str(root / 'client')
            and source[0]['read_only'] is True, 'Expected the synthetic RO source')
    return root


def mount_state(root, mountinfo=None, *, access_mode='ro', shared=True):
    # Never stat, canonicalize or open /work during recovery.
    if mountinfo is None:
        mountinfo = Path('/proc/self/mountinfo').read_text()
    entries = [line.split() for line in mountinfo.splitlines()]
    parent_path = str(root / 'mount')
    child_path = str(root / 'mount/work')
    nested = [entry for entry in entries if entry[4] == str(root) or entry[4].startswith(str(root) + '/')]
    parent = [entry for entry in nested if entry[4] == parent_path]
    child = [entry for entry in nested if entry[4] == child_path]
    require(len(parent) <= 1 and len(child) <= 1 and len(nested) == len(parent) + len(child),
            'Unexpected or stacked mounts in the synthetic directory')
    if parent:
        entry = parent[0]
        covering = [item for item in entries if item[0] == entry[1]]
        require(len(covering) == 1 and entry[2] == covering[0][2],
                'Self-bind source device does not match its containing mount')
        base = covering[0]
        relative = posixpath.relpath(parent_path, base[4])
        require(not relative.startswith('../') and entry[3] == posixpath.normpath(
            posixpath.join(base[3], relative)) and
            (not shared or any(field.startswith('shared:') for field in entry[6:])),
            'Mount does not match the expected self-bind')
    if child:
        entry = child[0]
        require(parent and entry[1] == parent[0][0] and access_mode in entry[5].split(',')
                and entry[entry.index('-') + 1:][:2] == ['fuse.sshfs', 'ubuntu@exporter:/work'],
                'Unexpected child mount under the self-bind')
    return (parent[0][0] if parent else 'none', child[0][0] if child else 'none')


def command(args, seconds, env, log):
    log.write('$ ' + ' '.join(map(str, args)) + '\n')
    log.flush()
    result = subprocess.run(['timeout', '-k', '2', str(seconds), *args], env=env,
                            stdout=log, stderr=log)
    log.write(f'exit={result.returncode}\n')
    log.flush()
    return result.returncode


def recover(root, expected, log, *, access_mode='ro', shared=True, verify_docker=None):
    def state():
        current = mount_state(root, access_mode=access_mode, shared=shared)
        log.write(f'mount_ids={current}\n')
        log.flush()
        return current

    require(state() == expected, 'Mount IDs changed since the dry run')
    require(not os.environ.get('DOCKER_CONTEXT') and os.environ.get('DOCKER_HOST', SOCKET) == SOCKET,
            'Use the local Docker socket')
    env = {**os.environ, 'DOCKER_HOST': SOCKET, 'PROBE_ROOT': str(root)}
    require(command(['sudo', '-n', '-v'], 6, env, log) == 0, 'Run sudo -v before applying recovery')
    result = command([str(STACK), 'cleanup'], 65, env, log)
    require(result in (0, 3), 'Scoped Docker cleanup failed')
    if verify_docker is not None:
        verify_docker()
    bind, child = state()
    require(bind == expected[0] and child in ('none', expected[1]), 'Mount IDs changed during cleanup')
    if child != 'none':
        require(result == 3, 'Docker cleanup did not confirm the residual SSHFS mount')
        require(command(['sudo', '-n', 'umount', '-i', '-c', '--', str(root / 'mount/work')],
                        8, env, log) == 0, 'Residual SSHFS unmount failed')
    require(state() == (bind, 'none'), 'SSHFS removal or self-bind identity could not be verified')
    if bind != 'none':
        require(command(['sudo', '-n', 'umount', '-i', '-c', '--', str(root / 'mount')],
                        8, env, log) == 0, 'Self-bind unmount failed')
    require(state() == ('none', 'none'), 'Mounts remain after recovery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--apply', nargs=2, metavar=('SELF_BIND_ID', 'SSHFS_ID'),
                        help='Require both mount IDs printed by the dry run')
    args = parser.parse_args()
    try:
        root = prepared_root(args.evidence)
        ids = mount_state(root)
        scope = 'mount-v2-' + hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
        if args.apply is None:
            print(json.dumps(dict(status='dry_run', root=str(root), scope=scope,
                                  self_bind_id=ids[0], sshfs_id=ids[1])))
            return 0
        with tempfile.NamedTemporaryFile(mode='w', prefix='recovery.', suffix='.log',
                                         dir=args.evidence, delete=False) as log:
            print(f'Recovery log: {log.name}', flush=True)
            try:
                recover(root, tuple(args.apply), log)
            except Exception as error:
                log.write(f'status=failed error={error}\n')
                raise
            log.write('status=passed\n')
        print(json.dumps(dict(status='passed', root=str(root), scope=scope)))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f'Recovery stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
