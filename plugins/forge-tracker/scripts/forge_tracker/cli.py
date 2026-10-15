"""forge-tracker's command line. See `forge-tracker --help`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Optional, TextIO

from . import catchup, config, detect, state
from .forges import github
from .model import ConfigError, ForgeError, attempt, iso
from .transport import Http, ReadTransport, RecordingHttp, gh_token, urllib_http

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


@dataclass
class Context:
    repo_dir: str
    remote: detect.Remote
    config: config.Config
    forge: str
    adapter: Any
    recorder: Optional[RecordingHttp]


def open_forge(
    repo_dir: str,
    http: Optional[Http],
    run: Callable[..., Any],
    record: bool = False,
) -> Context:
    remote = detect.parse_remote(detect.origin_url(repo_dir, run))
    cfg = config.load(repo_dir)
    forge = detect.detect_forge(remote, cfg.forge)
    token = gh_token(remote.host, run)
    inner = http or urllib_http(cfg.ca_file)
    recorder = RecordingHttp(inner, [token]) if record else None
    api_base, graphql_url = github.endpoints(remote.host, cfg.api_base)
    transport = ReadTransport(
        api_base=api_base,
        http=recorder or inner,
        token=token,
        graphql_url=graphql_url,
        extra_headers=github.headers(),
    )
    adapter = github.GitHub(transport, remote.project)
    return Context(repo_dir, remote, cfg, forge, adapter, recorder)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"{type(value).__name__} isn't JSON")


def cmd_catchup(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    out: TextIO,
    tz: Optional[tzinfo],
) -> int:
    ident = attempt(ctx.adapter.identity)
    login = ident.data.login if ident.ok else None

    marker_file: Optional[str] = None
    if args.since:
        try:
            start = catchup.parse_since(args.since, now)
        except ValueError as error:
            print(f"forge-tracker: {error}", file=sys.stderr)
            return EXIT_USAGE
        window = catchup.Window(start, now, "since")
    elif login is None:
        window = catchup.Window(now - catchup.FIRST_WINDOW, now, "no-identity")
    else:
        marker_file = catchup.marker_path(ctx.remote, login, environ)
        window = catchup.window_from_marker(state.read_json(marker_file), now)

    reads = catchup.gather(ctx.adapter, ctx.config, login, window, ctx.repo_dir, run)
    moving = marker_file is not None and not args.no_mark
    report = catchup.build(
        reads,
        ctx.config,
        ctx.remote,
        ctx.forge,
        login,
        ident.error,
        window,
        show_all=args.all,
        marker="move" if moving else "keep",
    )
    if not report["ok"] and moving:
        moving = False
        report["marker"] = "held"

    text = (
        json.dumps(report, indent=1, default=_json_default) + "\n"
        if args.json
        else catchup.render(report, tz)
    )
    # The new marker is written beside the old one first and renamed into place only after the
    # report has printed; a failed print discards it and the old marker stays.
    pending = None
    if moving and marker_file:
        pending = state.PendingWrite(
            marker_file,
            {
                "end": iso(now),
                "host": ctx.remote.host,
                "project": ctx.remote.project,
                "user": login,
            },
        )
    try:
        out.write(text)
        out.flush()
    except BaseException:
        if pending:
            pending.discard()
        raise
    if pending:
        pending.commit()
    return EXIT_OK if report["ok"] else EXIT_FAILED


def cmd_smoke(ctx: Context, now: datetime, out: TextIO) -> int:
    """Every read once against the live forge, printing counts and any FAILED part."""
    a = ctx.adapter
    since = now - catchup.FIRST_WINDOW
    ident = attempt(a.identity)
    repo = attempt(a.repo)
    branch = repo.data.default_branch if repo.ok else None
    changes = attempt(a.open_changes)
    checks: list[tuple[str, Any]] = [
        ("identity", ident.map(lambda u: f"{u.login} (bot: {'yes' if u.is_bot else 'no'})")),
        ("repo", repo.map(lambda r: f"default branch {r.default_branch}")),
        ("open issues", attempt(a.open_issues).map(len)),
        ("issues updated in 7 days", attempt(lambda: a.issues_since(since)).map(len)),
        ("comments in 7 days", attempt(lambda: a.comments_since(since)).map(len)),
        (
            "mentions in 7 days",
            attempt(lambda: a.mentions_since(ident.data.login, since)).map(len)
            if ident.ok
            else ident,
        ),
        (
            "open changes",
            changes.map(
                lambda cs: (
                    f"{len(cs)} (conflicts: "
                    + ", ".join(
                        f"{v} {sum(c.conflicts == v for c in cs)}" for v in ("yes", "no", "unknown")
                    )
                    + ")"
                )
            ),
        ),
        ("merged in 7 days", attempt(lambda: a.merged_since(since)).map(len)),
        ("blocking links", attempt(a.blocking_links).map(len)),
    ]
    if branch:
        checks += [
            (
                "push run",
                attempt(lambda: a.latest_run(branch, "push")).map(
                    lambda r: "none" if r is None else f"{r.id} {r.conclusion or r.status}"
                ),
            ),
            (
                "schedule run",
                attempt(lambda: a.latest_run(branch, "schedule")).map(
                    lambda r: "none" if r is None else f"{r.id} {r.conclusion or r.status}"
                ),
            ),
            ("passed on retry", attempt(lambda: a.passed_on_retry(branch, since)).map(len)),
            (
                "branches",
                attempt(lambda: a.branches(branch, changes.data or [])).map(len)
                if changes.ok
                else changes,
            ),
        ]
    ok = True
    out.write(f"{ctx.remote.project} on {ctx.remote.host} ({ctx.forge})\n")
    for name, read in checks:
        if read.ok:
            out.write(f"  {name}: {read.data}\n")
        else:
            ok = False
            out.write(f"  {name}: FAILED ({read.error})\n")
    return EXIT_OK if ok else EXIT_FAILED


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forge-tracker",
        description="Read a repo's forge (GitHub so far) and report on it. Read-only.",
    )
    p.add_argument("--repo", default=".", help="the git checkout to report on (default: .)")
    p.add_argument(
        "--record",
        metavar="FILE",
        help="also write every API exchange, scrubbed, to FILE (for test fixtures)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser(
        "catchup",
        help="what changed and what waits on you since your last catchup",
        description=(
            "What changed and what's waiting on you since your last successful catchup "
            "(7 days the first time). Exits 1 when any read failed; the marker then stays."
        ),
    )
    c.add_argument("--since", help="look back 3d, 12h, 2w, 90m or to an ISO time; marker untouched")
    c.add_argument(
        "--no-mark",
        action="store_true",
        help="read the marker but don't move it (agents always pass this)",
    )
    c.add_argument(
        "--all", action="store_true", help="list the standing backlog in full, not as counts"
    )
    c.add_argument("--json", action="store_true", help="print the report as JSON")

    sub.add_parser("smoke", help="run every read once and print counts (a live check)")
    return p


def main(
    argv: list[str],
    *,
    http: Optional[Http] = None,
    run: Callable[..., Any] = subprocess.run,
    now: Optional[datetime] = None,
    environ: Optional[Mapping[str, str]] = None,
    out: Optional[TextIO] = None,
    tz: Optional[tzinfo] = None,
) -> int:
    args = parser().parse_args(argv)
    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    now = now or datetime.now(timezone.utc)
    try:
        ctx = open_forge(os.path.abspath(args.repo), http, run, record=bool(args.record))
    except (ConfigError, ForgeError) as error:
        print(f"forge-tracker: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.command == "catchup":
            return cmd_catchup(args, ctx, now, environ, run, out, tz)
        return cmd_smoke(ctx, now, out)
    except BrokenPipeError:
        return EXIT_FAILED
    finally:
        if ctx.recorder is not None:
            with open(args.record, "w", encoding="utf-8") as handle:
                # The subcommand and its options only: --repo and --record are local paths.
                command = argv[argv.index(args.command) :]
                handle.write(ctx.recorder.dump(recorded_at=iso(now), argv=command))
