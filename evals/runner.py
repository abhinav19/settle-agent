"""Entry point:  python runner.py run|estimate|dataset ...   (see `python runner.py -h`)"""

import sys

from evalharness.cli import main

if __name__ == "__main__":
    sys.exit(main())
