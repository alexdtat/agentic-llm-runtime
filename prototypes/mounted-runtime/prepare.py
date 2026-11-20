"""Prepare an isolated, synthetic export. No Docker or host mount changes."""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


BOUNDED_EXPORT_BYTES = 128 * 1024


def protected_paths(root):
    mountinfo = Path('/proc/self/mountinfo').read_text().splitlines()
    for line in mountinfo:
        escaped = line.split()[4]
        mount = Path(re.sub(r'\\([0-7]{3})', lambda word: chr(int(word[1], 8)), escaped))
        if root in mount.parents:
            raise ValueError('Nested mountpoints are not permitted in the export')

    def fail(error):
        raise error

    for directory, folders, files in os.walk(root, onerror=fail):
        parent = Path(directory)
        for name in folders + files:
            path = parent / name
            info = path.lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            is_single_file = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            if not (is_directory or is_single_file):
                raise ValueError('Export requires directories and regular files without links: ' + str(path))
            if name == '.git':
                if is_directory:
                    folders.remove(name)
                yield path.relative_to(root), is_directory


def bind(source, target, readonly=False):
    return {'type': 'bind', 'source': str(source), 'target': str(target),
            'read_only': readonly, 'bind': {'create_host_path': False}}


def prepare(client, access_mode='rw', *, fixture=None, bounded_export=False):
    if access_mode not in ('ro', 'rw'):
        raise ValueError('Export access mode must be ro or rw')
    if bounded_export and (client is not None or fixture is not None or access_mode != 'rw'):
        raise ValueError('A bounded export requires an empty synthetic RW workspace')
    if os.getuid() != 1000:
        raise ValueError('The diagnostic exporter expects client UID 1000')
    if client is not None and fixture is not None:
        raise ValueError('Fixture preparation requires a new export')
    if fixture is not None and access_mode != 'rw':
        raise ValueError('Fixture preparation requires a writable export')
    if client is not None:
        client = client.resolve(strict=True)
        if not client.is_dir() or client == Path('/'):
            raise ValueError('Choose a specific project directory')
        if '$' in str(client):
            raise ValueError('Export path cannot contain $')
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix='agentic-mounted-runtime.', dir='/tmp'))
    try:
        configure(root, client, access_mode, fixture, bounded_export=bounded_export)
    except BaseException:
        shutil.rmtree(root)
        raise
    return root


def configure(root, client, access_mode, fixture, *, bounded_export=False):
    if fixture is not None:
        client = root / 'client'
        if list(protected_paths(fixture)):
            raise ValueError('Fixture cannot contain protected paths')
        shutil.copytree(fixture, client)
    elif client is None and not bounded_export:
        client = root / 'client'
        for relative in ('.git/probe', 'nested/repo/.git/probe', 'nested/worktree/.git'):
            canary = client / relative
            canary.parent.mkdir(parents=True, exist_ok=True)
            canary.write_text('protected-git-canary\n')
        (client / 'client.txt').write_text('client-initial\n')
        if access_mode == 'ro':
            (client / 'upload.txt').write_text('distinct-upload-canary\n')
        (root / 'outside.txt').write_text('outside-canary\n')
    (root / 'mount/work').mkdir(parents=True)
    (root / 'mount').chmod(0o755)
    (root / 'mount/work').chmod(0o755)

    if bounded_export:
        volumes = [{'type': 'tmpfs', 'target': '/probe/work',
                    'tmpfs': {'size': BOUNDED_EXPORT_BYTES, 'mode': 0o1777}}]
    else:
        empty_dir = root / 'empty-dir'
        empty_file = root / 'empty-file'
        empty_dir.mkdir(mode=0o555)
        empty_file.touch(mode=0o444)
        volumes = [bind(client, '/probe/work', readonly=access_mode == 'ro')]
        for relative, directory in protected_paths(client):
            if '$' in str(relative):
                raise ValueError('Protected paths cannot contain $')
            volumes.append(bind(empty_dir if directory else empty_file,
                                Path('/probe/work') / relative, readonly=True))
    services = {'exporter': {'volumes': volumes}}
    if access_mode == 'ro':
        sshd = Path(__file__).with_name('sshd.conf').read_text()
        force_command = '\nForceCommand internal-sftp '
        if sshd.count(force_command) != 1:
            raise ValueError('Expected one forced SFTP command')
        config = root / 'sshd.conf'
        config.write_text(sshd.replace(force_command, force_command + '-R '))
        volumes.append(bind(config, '/probe-config/sshd.conf', readonly=True))
        services['broker'] = {'environment': {'SSHFS_ACCESS_MODE': access_mode}}
    (root / 'workspace.json').write_text(json.dumps({'services': services}))

    exporter, broker = root / 'keys/exporter', root / 'keys/broker'
    for directory, filename in [(exporter, 'host'), (broker, 'client')]:
        directory.mkdir(parents=True)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(directory / filename)],
                       check=True, timeout=3)
    (exporter / 'client.pub').write_bytes((broker / 'client.pub').read_bytes())
    host_key = subprocess.check_output(['ssh-keygen', '-y', '-f', str(exporter / 'host')],
                                       text=True, timeout=3)
    (broker / 'known_hosts').write_text('[exporter]:2222 ' + host_key)
    (root / 'access-mode').write_text(access_mode + '\n')
    (root / 'marker').write_text('agentic-mounted-runtime\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', type=Path, help='Optional trusted project directory. Default makes synthetic files.')
    parser.add_argument('--access-mode', choices=('ro', 'rw'), default='rw',
                        help='Trusted export access mode (default: rw).')
    parser.add_argument('--bounded-export', action='store_true',
                        help='Use an empty, size-limited tmpfs on the synthetic RW exporter.')
    args = parser.parse_args()
    print(prepare(args.client, args.access_mode, bounded_export=args.bounded_export))
