"""Prepare an isolated, synthetic export. No Docker or host mount changes."""
import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


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


def prepare(client):
    if os.getuid() != 1000:
        raise ValueError('The diagnostic exporter expects client UID 1000')
    if client is not None:
        client = client.resolve(strict=True)
        if not client.is_dir() or client == Path('/'):
            raise ValueError('Choose a specific project directory')
        if '$' in str(client):
            raise ValueError('Export path cannot contain $')
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix='agentic-mounted-runtime.', dir='/tmp'))
    if client is None:
        client = root / 'client'
        for relative in ('.git/probe', 'nested/repo/.git/probe', 'nested/worktree/.git'):
            canary = client / relative
            canary.parent.mkdir(parents=True, exist_ok=True)
            canary.write_text('protected-git-canary\n')
        (client / 'client.txt').write_text('client-initial\n')
        (root / 'outside.txt').write_text('outside-canary\n')
    (root / 'mount/work').mkdir(parents=True)
    (root / 'mount').chmod(0o755)
    (root / 'mount/work').chmod(0o755)

    empty_dir = root / 'empty-dir'
    empty_file = root / 'empty-file'
    empty_dir.mkdir(mode=0o555)
    empty_file.touch(mode=0o444)
    volumes = [bind(client, '/probe/work')]
    for relative, directory in protected_paths(client):
        if '$' in str(relative):
            raise ValueError('Protected paths cannot contain $')
        volumes.append(bind(empty_dir if directory else empty_file,
                            Path('/probe/work') / relative, readonly=True))
    # A second Compose file adds only the authorized workspace and its masks.
    (root / 'workspace.json').write_text(json.dumps({'services': {'exporter': {'volumes': volumes}}}))

    exporter, broker = root / 'keys/exporter', root / 'keys/broker'
    for directory, filename in [(exporter, 'host'), (broker, 'client')]:
        directory.mkdir(parents=True)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(directory / filename)],
                       check=True, timeout=3)
    (exporter / 'client.pub').write_bytes((broker / 'client.pub').read_bytes())
    host_key = subprocess.check_output(['ssh-keygen', '-y', '-f', str(exporter / 'host')],
                                       text=True, timeout=3)
    (broker / 'known_hosts').write_text('[exporter]:2222 ' + host_key)
    (root / 'marker').write_text('agentic-mounted-runtime\n')
    print(root)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', type=Path, help='Optional trusted project directory. Default makes synthetic files.')
    prepare(parser.parse_args().client)
