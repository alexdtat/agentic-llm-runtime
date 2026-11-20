"""Private enrollment and per-run authentication for the two-host rehearsal."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
from contextlib import contextmanager


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def config_root(role):
    return Path.home() / '.config' / 'agentic-two-host' / role


def private_directory(path):
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'Expected an owned private configuration directory')


def read_file(path, *, private, maximum=4096):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and 0 < info.st_size <= maximum
                and (not private or not info.st_mode & 0o077), 'Expected a bounded, owned configuration file')
        data = stream.read(maximum + 1)
        require(len(data) == info.st_size and os.fstat(stream.fileno()).st_mtime_ns == info.st_mtime_ns,
                'Configuration file changed during read')
    return data


def read_json(path, *, private):
    try:
        return json.loads(read_file(path, private=private))
    except (OSError, ValueError):
        raise RuntimeError('Could not read the pairing configuration') from None


def is_hex(value, length):
    return isinstance(value, str) and len(value) == length and re.fullmatch('[a-f0-9]+', value) is not None


def enrollment(source):
    original = read_json(source, private=False)
    require(isinstance(original, dict) and all(is_hex(original.get(key), 48) for key in ('token', 'grant'))
            and isinstance(original.get('desktop'), str) and original['desktop'],
            'Expected a previously transferred two-host credentials file')
    seed = bytes.fromhex(original['token']) + bytes.fromhex(original['grant'])
    return {'seed': hashlib.sha256(seed).hexdigest(), 'desktop': original['desktop']}


def credentials(seed, tag):
    require(is_hex(seed, 64) and is_hex(tag, 20), 'Invalid pairing seed or public run tag')
    key = bytes.fromhex(seed)
    return tuple(hmac.digest(key, f'agentic-two-host/v1/{label}/{tag}'.encode(), 'sha256').hex()[:48]
                 for label in ('token', 'grant'))


def challenge_proof(seed, tag, nonce):
    require(is_hex(seed, 64) and is_hex(tag, 20) and is_hex(nonce, 32), 'Invalid pairing challenge')
    return hmac.digest(bytes.fromhex(seed), f'agentic-two-host/v1/health/{tag}/{nonce}'.encode(), 'sha256').hex()


@contextmanager
def new_configuration(role):
    root = config_root(role)
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(root.parent)
    root.mkdir(mode=0o700)
    try:
        yield root
    except BaseException:
        shutil.rmtree(root)
        raise


def write_file(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_configuration(root, config):
    write_file(root / 'paired.json', json.dumps(config).encode() + b'\n')


def load(role):
    root = config_root(role)
    private_directory(root)
    config = read_json(root / 'paired.json', private=True)
    require(isinstance(config, dict) and is_hex(config.get('seed'), 64)
            and isinstance(config.get('desktop'), str) and config['desktop'],
            'Invalid paired configuration')
    return config
