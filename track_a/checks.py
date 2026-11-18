"""Container-native test entry point; no host venv, Docker socket or real LLM."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from common import ROOT, new_run, save_json


def main():
    out = new_run('runtime-cpu-v1')
    tests = ['test_runtime_cli', 'test_runtime_state', 'test_baf_checkers']
    if os.environ.get('RUNTIME_URL'):
        tests.append('test_runtime_integration')
    result = subprocess.run([sys.executable, '-m', 'unittest', *tests, '-v'],
                            capture_output=True, text=True, timeout=180)
    log = result.stdout + result.stderr
    (out/'tests.log').write_text(log)
    print(log, end='')
    summary = {'passed': result.returncode == 0, 'model_calls': 0, 'tests': tests,
               'scope': 'CPU infrastructure and historical action replay; not a quality experiment',
               'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for pattern in ['*.py', 'track_a/*.py'] for p in ROOT.glob(pattern)}}
    save_json(out/'summary.json', summary)
    print(json.dumps({'result': str(out), 'passed': summary['passed']}))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
