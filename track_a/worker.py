"""Trusted supervisor inside the sandbox, with bounded output and descendant cleanup.

Linux subreaper adoption covers double forks/setsid after their parent exits.
This shares UID/kernel with the command; it is not a hostile-code security boundary.
The executor destroys the whole sandbox on cancellation, timeout or worker failure.
"""
import ctypes
import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def reap_descendants():
    # Kill direct children, reap them, then repeat for newly adopted grandchildren.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
        if not children:
            return
        for child in children:
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        time.sleep(.01)
    raise RuntimeError('Descendant cleanup did not complete')


def main():
    request = json.load(sys.stdin)
    # PR_SET_CHILD_SUBREAPER, before creating any descendants.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError('Cannot become a child subreaper')
    result = dict(status='failed', error_kind=None, exit_code=None, stdout='', stderr='')
    process = None
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(request['argv'], cwd='/work', stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                result['exit_code'] = process.wait(timeout=request['timeout_seconds'])
                result.update(status='complete' if process.returncode == 0 else 'failed',
                              error_kind=None if process.returncode == 0 else 'command')
            except subprocess.TimeoutExpired:
                result.update(status='timed_out', error_kind='timeout')
        except FileNotFoundError:
            result.update(error_kind='command', exit_code=127, stderr='Executable not found')
        except Exception:
            result.update(error_kind='sandbox')
        finally:
            if process is not None:
                # Even if the group leader already exited, its descendants may remain.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            try:
                reap_descendants()
            except Exception:
                result.update(status='failed', error_kind='sandbox')
        stdout.seek(0)
        stderr.seek(0)
        limit = request.get('output_bytes', 65536)
        out, err = stdout.read(limit + 1), stderr.read(limit + 1)
        result['stdout'] = out[:limit].decode(errors='replace')
        result['stderr'] += err[:limit].decode(errors='replace')
        if 'output_bytes' in request:
            result['truncated'] = len(out) > limit or len(err) > limit
    print(json.dumps(result))


def file_tool(tool, path, data=''):
    parts = Path(path).parts
    parent = os.open('/work', os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            if tool == 'WriteFile':
                try:
                    os.mkdir(part, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        if tool == 'ReadFile':
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                content = stream.read(6145)
                if len(content) > 6144:
                    raise ValueError('ReadFile exceeds 6144 bytes')
                print(base64.b64encode(content).decode())
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(parts[-1], flags, 0o644, dir_fd=parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(base64.b64decode(data, validate=True))
    finally:
        os.close(parent)


if __name__ == '__main__':
    if len(sys.argv) > 1:
        file_tool(*sys.argv[1:])
    else:
        main()
