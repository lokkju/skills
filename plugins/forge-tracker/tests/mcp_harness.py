"""Runs the MCP server on stdio against a recorded fixture, for test_server.py.

    python mcp_harness.py FIXTURE HOME

FIXTURE is a recording under fixtures/ (or `-` for none: every forge read then fails), HOME the
cache root. git and gh answer from FakeRun, as in the CLI tests; no network is reached.
"""

import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import FakeRun, ReplayHttp
from forge_tracker.model import parse_time
from forge_tracker.server import Env, serve


def offline(method, url, headers, body=None):
    raise AssertionError(f"no fixture: {method} {url}")


def main(fixture: str, home: str) -> int:
    http = ReplayHttp(fixture) if fixture != "-" else None
    now = parse_time(http.meta["recorded_at"]) if http else None
    run = FakeRun(origin="https://github.com/lokkju/skills.git")
    run.answer(("fetch",), stdout="")
    run.answer(("rev-list",), stdout="0123abcd\n")
    run.answer(
        ("diff",),
        stdout=(
            "+++ b/plugins/session-decisions/CHANGELOG.md\n"
            "+## 2026.10.15.1\n"
            "+- **session-decisions:** an entry\n"
        ),
    )
    real_run = run

    def logged(argv, **kwargs):
        print("run: " + " ".join(argv), file=sys.stderr, flush=True)
        return real_run(argv, **kwargs)

    env = Env(
        http=http or offline,
        run=logged,
        now=(lambda: now) if now else None,
        environ={"HOME": home},
        tz=timezone.utc,
    )
    return serve(sys.stdin, sys.stdout, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
