"""Entry point:  python report.py report|baseline|check|publish --run runs/<id> ..."""

import sys

from evalharness.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    sys.exit(
        main(
            argv
            if argv and argv[0] in {"report", "baseline", "check", "publish"}
            else ["report", *argv]
        )
    )
