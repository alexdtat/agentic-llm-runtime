"""Compatibility entry point. Prefer ./scripts/check.sh: all checks run in containers."""
import argparse
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('track', choices=['A'])
    parser.add_argument('--infrastructure', action='store_true', help='Included in the container suite')
    parser.parse_args()
    return subprocess.call([str(Path(__file__).parent/'scripts/check.sh')])


if __name__ == '__main__':
    raise SystemExit(main())
