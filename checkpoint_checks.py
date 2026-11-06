"""Reproducible CPU checks for either checkpoint package. Never calls an LLM."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from common import ROOT, new_run, save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('track', choices=['A', 'B'])
    parser.add_argument('--infrastructure', action='store_true', help='Also run temporary Docker containers')
    args = parser.parse_args()
    out = new_run('checkpoint-' + args.track.lower())
    local_python = ROOT / 'runtime/arena-venv/bin/python'
    python = str(local_python) if local_python.exists() else sys.executable
    tests = ['test_runtime_cli', 'test_baf_checkers'] if args.track == 'A' else [
        'test_svg_checker', 'test_action_adapter_v2', 'test_metric_proposal', 'test_metric_documents']
    commands = [[python, '-m', 'unittest', *tests, '-v']]
    if args.infrastructure:
        if args.track == 'A':
            commands += [[python, 'a_transport_smoke.py'], [python, 'baf_runner.py', '--replay-from',
                'results/baf-suite-20261106T015237658406Z']]
        else:
            commands += [[python, 'b_arena_contract_smoke.py']]
    records = []
    for index, command in enumerate(commands):
        try:
            run = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=240)
            log = run.stdout + run.stderr
            code = run.returncode
        except subprocess.TimeoutExpired:
            log, code = 'Check exceeded 240 seconds; inspect child resources before retrying.\n', 124
        (out / f'check-{index+1}.log').write_text(log)
        records.append({'argv': command, 'returncode': code, 'log': f'check-{index+1}.log'})
        if code:
            break
    source_hashes = {}
    for pattern in ['*.py', 'track_a/**/*.py' if args.track == 'A' else 'track_b/**/*.py']:
        for source in ROOT.glob(pattern):
            source_hashes[str(source.relative_to(ROOT))] = hashlib.sha256(source.read_bytes()).hexdigest()
    result = {'track': args.track, 'model_calls': 0, 'checks': records,
              'passed': all(record['returncode'] == 0 for record in records),
              'source_sha256': source_hashes,
              'scope': 'Technical CPU checks; not a new quality experiment or a campaign'}
    save_json(out / 'summary.json', result)
    print(json.dumps({'result': str(out), 'passed': result['passed'], 'model_calls': 0}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
