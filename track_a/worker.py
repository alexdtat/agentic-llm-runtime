"""Executed inside the sandbox; emits exactly one terminal JSON result."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main():
    request = json.load(sys.stdin)
    marker = Path('/tmp/probe-runs') / sys.argv[1] / 'cancel'
    marker.parent.mkdir(parents=True, exist_ok=True)
    result = {'status':'failed','error_kind':None,'exit_code':None,'stdout':'','stderr':''}
    process = None
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(request['argv'],cwd='/work',stdout=stdout,stderr=stderr,
                                       start_new_session=True)
            deadline = time.monotonic()+request['timeout_seconds']
            while process.poll() is None:
                if marker.exists() or time.monotonic()>deadline:
                    result['status'] = 'cancelled' if marker.exists() else 'timed_out'
                    result['error_kind'] = 'cancelled' if marker.exists() else 'timeout'
                    os.killpg(process.pid,signal.SIGTERM)
                    try: process.wait(timeout=.5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL);process.wait()
                    break
                time.sleep(.02)
            result['exit_code'] = process.wait()
            if result['error_kind'] is None:
                result['status'] = 'complete' if process.returncode==0 else 'failed'
                result['error_kind'] = None if process.returncode==0 else 'command'
        except FileNotFoundError as exc:
            result.update(error_kind='command',exit_code=127,stderr=str(exc))
        except Exception as exc:
            result.update(error_kind='sandbox',stderr=str(exc))
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
        stdout.seek(0);stderr.seek(0)
        result['stdout'] = stdout.read(65536).decode(errors='replace')
        result['stderr'] += stderr.read(65536).decode(errors='replace')
    print(json.dumps(result))


if __name__=='__main__': main()
