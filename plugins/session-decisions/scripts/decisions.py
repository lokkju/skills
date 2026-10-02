#!/usr/bin/env python3
"""Entry point for the session-decisions hooks, statusline segment and fallback ledger."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_decisions.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ, sys.stdin, sys.stdout, sys.stderr))
