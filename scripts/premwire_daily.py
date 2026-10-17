"""Finite public SHADOW launcher. No tasks, credentials or publication.

The private operator launcher and its supervisor are intentionally not exported.
This wrapper exposes only the reviewed public CLI's offline/shadow interface.
"""
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    from premwire.cli import main as public_main
    sys.argv[1:1] = ['--root', str(root), 'daily-digest']
    return public_main()


if __name__ == '__main__':
    raise SystemExit(main())
