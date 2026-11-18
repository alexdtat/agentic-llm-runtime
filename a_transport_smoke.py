"""Compatibility entry point for the current container-native CPU suite."""
import subprocess
from pathlib import Path


def main():
    return subprocess.call([str(Path(__file__).parent/'scripts/check.sh')])


if __name__ == '__main__':
    raise SystemExit(main())
