"""forge-tracker's command line. See `forge-tracker --help`."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Optional, TextIO

from . import catchup, config, detect, groom, labelsetup, state, workspace, writes
from . import next as next_
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
    # The unrecorded transport and the token, for the write layer only.
    http: Optional[Http] = None
    token: Optional[str] = None


def open_forge(
    repo_dir: str,
    http: Optional[Http],
    run: Callable[..., Any],
    record: bool = False,
    environ: Mapping[str, str] = os.environ,
) -> Context:
    loaded = config.load(repo_dir)
    remote, forge = detect.resolve(repo_dir, loaded.forge, run)
    cfg = labelsetup.with_decline(loaded, remote, environ)
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
    return Context(repo_dir, remote, cfg, forge, adapter, recorder, inner, token)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    raise TypeError(f"{type(value).__name__} isn't JSON")


@dataclass
class CatchupRun:
    """One repo's catchup report, and the marker to write once it has printed (None to keep)."""

    report: dict[str, Any]
    marker_file: Optional[str] = None
    marker: Optional[dict[str, Any]] = None


def catchup_one(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> CatchupRun:
    """Read and build one repo's report. `args.since` is already known to parse."""
    ident = attempt(ctx.adapter.identity)
    login = ident.data.login if ident.ok else None

    marker_file: Optional[str] = None
    if args.since:
        window = catchup.Window(catchup.parse_since(args.since, now), now, "since")
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
    if not (moving and marker_file):
        return CatchupRun(report)
    payload = {
        "end": iso(now),
        "host": ctx.remote.host,
        "project": ctx.remote.project,
        "user": login,
    }
    return CatchupRun(report, marker_file, payload)


def _print_then_mark(text: str, runs: list[CatchupRun], out: TextIO) -> None:
    """The new markers are written beside the old ones first and renamed into place only after
    the report has printed; a failed print discards them and the old markers stay."""
    pending: list[state.PendingWrite] = []
    try:
        for r in runs:
            if r.marker_file and r.marker:
                pending.append(state.PendingWrite(r.marker_file, r.marker))
        out.write(text)
        out.flush()
    except BaseException:
        for p in pending:
            p.discard()
        raise
    for p in pending:
        p.commit()


def _since_ok(args: argparse.Namespace, now: datetime) -> bool:
    if args.since:
        try:
            catchup.parse_since(args.since, now)
        except ValueError as error:
            print(f"forge-tracker: {error}", file=sys.stderr)
            return False
    return True


def cmd_catchup(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    out: TextIO,
    tz: Optional[tzinfo],
) -> int:
    if not _since_ok(args, now):
        return EXIT_USAGE
    result = catchup_one(args, ctx, now, environ, run)
    report = result.report
    text = (
        json.dumps(report, indent=1, default=_json_default) + "\n"
        if args.json
        else catchup.render(report, tz)
    )
    _print_then_mark(text, [result], out)
    return EXIT_OK if report["ok"] else EXIT_FAILED


def cmd_catchup_workspace(
    args: argparse.Namespace,
    ws: workspace.Workspace,
    http: Optional[Http],
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    out: TextIO,
    tz: Optional[tzinfo],
) -> int:
    """Catch up on every repo in the workspace, each with its own config, window and marker, and
    print one report. A repo that can't be read is a FAILED line, and exit 1, like a failed read;
    the other repos' markers still move."""
    if not _since_ok(args, now):
        return EXIT_USAGE

    def one(path: str) -> tuple[workspace.Entry, Optional[CatchupRun]]:
        try:
            ctx = open_forge(path, http, run, environ=environ)
        except (ConfigError, ForgeError) as error:
            return workspace.Entry(path, error=str(error)), None
        result = catchup_one(args, ctx, now, environ, run)
        return workspace.Entry(path, report=result.report), result

    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(one, ws.repos))
    marker = "since" if args.since else "keep" if args.no_mark else "move"
    report = workspace.build(ws, [entry for entry, _ in done], marker)
    text = (
        json.dumps(report, indent=1, default=_json_default) + "\n"
        if args.json
        else workspace.render(report, tz)
    )
    _print_then_mark(text, [r for _, r in done if r is not None], out)
    return EXIT_OK if report["ok"] else EXIT_FAILED


def _workspace_at(
    path: str, run: Callable[..., Any], explicit: bool
) -> Optional[workspace.Workspace]:
    """The workspace at `path` when it is one: named by --workspace, or a folder that isn't a git
    repo and holds some (or lists them in `repos:`). None otherwise."""
    if not explicit and workspace.is_repo(path, run):
        return None
    ws = workspace.discover(path, run)
    return ws if ws.repos or explicit else None


def _run_workspace(
    args: argparse.Namespace,
    path: str,
    explicit: bool,
    http: Optional[Http],
    run: Callable[..., Any],
    now: datetime,
    environ: Mapping[str, str],
    out: TextIO,
    tz: Optional[tzinfo],
) -> Optional[int]:
    """Run catchup over the workspace at `path`, or for any other command say that it reads one
    repo. None when `path` isn't a workspace, so the caller's own error stands."""
    try:
        ws = _workspace_at(path, run, explicit)
    except ConfigError as error:
        print(f"forge-tracker: {error}", file=sys.stderr)
        return EXIT_USAGE
    if ws is None:
        return None
    if not ws.repos:
        print(
            f"forge-tracker: {path} holds no git repo with an origin remote, and its "
            f"{config.PATH} lists none in repos:",
            file=sys.stderr,
        )
        return EXIT_USAGE
    names = ", ".join(os.path.relpath(p, path) for p in ws.repos)
    if args.command != "catchup":
        print(
            f"forge-tracker: {path} isn't a git repo; it's a workspace of {len(ws.repos)} "
            f"({names}). `{args.command}` reads one repo: pass --repo <dir>. "
            "Only catchup reads a whole workspace.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.record:
        print("forge-tracker: --record takes one repo, not a workspace", file=sys.stderr)
        return EXIT_USAGE
    return cmd_catchup_workspace(args, ws, http, now, environ, run, out, tz)


def cmd_next(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    out: TextIO,
    tz: Optional[tzinfo],
) -> int:
    """`next` (the report) and `next facts` (the facts and classification, with issue bodies in
    --json, for ranking). Both read the fingerprint and reuse cached facts while it holds."""
    directory = next_.cache_dir(ctx.remote, environ)
    facts, origin = next_.load_facts(
        ctx.adapter, ctx.remote, ctx.forge, ctx.repo_dir, directory, now, refresh=args.refresh
    )
    facts_only = args.action == "facts"
    slate = None if facts_only else state.read_json(os.path.join(directory, "slate.json"))
    report = next_.build(
        facts,
        origin,
        ctx.config,
        slate,
        rerank=args.rerank,
        pickups=next_.recorded_pickups(ctx.remote, environ),
        bodies=facts_only and args.json,
        facts_only=facts_only,
    )
    if args.json:
        out.write(json.dumps(report, indent=1, default=_json_default) + "\n")
    else:
        out.write(next_.render(report, now, tz))
    out.flush()
    return EXIT_OK if report["ok"] else EXIT_FAILED


def cmd_save_slate(
    repo_dir: str,
    run: Callable[..., Any],
    now: datetime,
    environ: Mapping[str, str],
    stdin: TextIO,
    out: TextIO,
) -> int:
    """Store Claude's slate beside the cached facts. Reads no forge, so it needs no token."""
    try:
        cfg = config.load(repo_dir)
        remote, _, _ = detect.locate(repo_dir, cfg.forge, run)
    except ConfigError as error:
        print(f"forge-tracker: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        payload = json.loads(stdin.read())
    except ValueError as error:
        print(f"forge-tracker: the slate on stdin isn't JSON: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        slate = next_.save_slate(payload, next_.cache_dir(remote, environ), cfg, repo_dir, now)
    except next_.SlateError as error:
        print(f"forge-tracker: slate refused: {error}", file=sys.stderr)
        return EXIT_USAGE
    lanes = len(slate["lanes"])
    recommended = ", a recommendation" if slate.get("recommend") else ""
    out.write(
        f"slate saved: {len(slate['queue'])} in your queue, {lanes} lane{'s' if lanes != 1 else ''}"
        f"{recommended} (facts {slate['fingerprint'][:12]})\n"
    )
    return EXIT_OK


def cmd_groom(args: argparse.Namespace, ctx: Context, now: datetime, out: TextIO) -> int:
    """Find tracker drift and print the proposed fixes with their evidence. Writes nothing."""
    reads = groom.gather(ctx.adapter, ctx.config, now)
    report = groom.build(reads, ctx.config, ctx.remote, ctx.forge, now)
    if args.json:
        out.write(json.dumps(report, indent=1, default=_json_default) + "\n")
    else:
        out.write(groom.render(report))
    out.flush()
    return EXIT_OK if report["ok"] else EXIT_FAILED


def make_writer(
    ctx: Context, now: datetime, environ: Mapping[str, str], run: Callable[..., Any]
) -> writes.Writer:
    """The write layer for this repo: the write-wrapper when the config sets one, and otherwise
    GitHub's REST API through the write transport, which nothing but writes.py builds on."""
    forge = None
    if not ctx.config.write_wrapper:
        api_base, _ = github.endpoints(ctx.remote.host, ctx.config.api_base)
        transport = writes.WriteTransport(
            api_base=api_base,
            repo_path=ctx.adapter.repo_path,
            http=ctx.http,  # type: ignore[arg-type]
            token=ctx.token,
            extra_headers=github.headers(),
        )
        forge = writes.GitHubWrites(ctx.adapter.t, transport, ctx.remote.project)
    return writes.Writer(
        project=ctx.remote.project,
        repo_dir=ctx.repo_dir,
        log_path=writes.log_path(ctx.remote.key, environ),
        forge=forge,
        wrapper=ctx.config.write_wrapper,
        run=run,
        now=lambda: now,
        existing_labels=lambda: [label.name for label in ctx.adapter.labels()],
        issue_ref=lambda number, project: ctx.adapter.issue_ref(number, project),
        blocked_by=ctx.adapter.blocked_by,
    )


def _stdin_json(stdin: TextIO, what: str) -> tuple[Any, Optional[str]]:
    try:
        return json.loads(stdin.read()), None
    except ValueError as error:
        return None, f"forge-tracker: the {what} on stdin isn't JSON: {error}"


def cmd_groom_apply(
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    stdin: TextIO,
    out: TextIO,
) -> int:
    """Write the proposals the user approved ({"approved": [ids]} on stdin), one at a time,
    after re-running the checks: an id that isn't a current proposal refuses the whole list."""
    payload, problem = _stdin_json(stdin, "approval")
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    reads = groom.gather(ctx.adapter, ctx.config, now)
    report = groom.build(reads, ctx.config, ctx.remote, ctx.forge, now)
    try:
        chosen = groom.approved(payload, report)
    except groom.ApplyError as error:
        print(f"forge-tracker: nothing written: {error}", file=sys.stderr)
        return EXIT_USAGE
    writer = make_writer(ctx, now, environ, run)
    ok = True
    for proposal in chosen:
        result = writer.apply(proposal["op"], proposal["payload"])
        ok = ok and result.ok
        out.write(writes.render(result, proposal["id"]) + "\n")
        out.flush()
    return EXIT_OK if ok else EXIT_FAILED


def cmd_write(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    stdin: TextIO,
    out: TextIO,
) -> int:
    """One named write operation, its JSON payload on stdin. Exits 2 when it's refused."""
    payload, problem = _stdin_json(stdin, "payload")
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_USAGE
    result = make_writer(ctx, now, environ, run).apply(args.op, payload)
    out.write(writes.render(result) + "\n")
    if result.status == "refused":
        return EXIT_USAGE
    return EXIT_OK if result.ok else EXIT_FAILED


_ISSUE_ARG = re.compile(r"#?(\d+)")
_BLOCKER_ARG = re.compile(r"(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(\d+)")


def link_payload(blocked: str, blocker: str) -> Optional[dict[str, Any]]:
    """The link-add or link-remove payload for `<blocked> --blocked-by <blocker>`, where the
    blocked issue is `18` or `#18` and the blocker also may be `owner/repo#17`; None when either
    doesn't parse."""
    a, b = _ISSUE_ARG.fullmatch(blocked.strip()), _BLOCKER_ARG.fullmatch(blocker.strip())
    if not a or not b:
        return None
    payload: dict[str, Any] = {"issue": int(a.group(1)), "blocker": int(b.group(2))}
    if b.group(1):
        payload["blocker_project"] = b.group(1)
    return payload


def cmd_link(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    out: TextIO,
) -> int:
    """`link add|remove <blocked> --blocked-by <blocker>`: the link-add or link-remove write
    with the payload built from the arguments. Exits 2 when it's refused."""
    payload = link_payload(args.issue, args.blocked_by)
    if payload is None:
        print(
            "forge-tracker: link takes an issue number (18 or #18) and --blocked-by an issue "
            "(17, #17 or owner/repo#17)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    result = make_writer(ctx, now, environ, run).apply(f"link-{args.action}", payload)
    out.write(writes.render(result) + "\n")
    if result.status == "refused":
        return EXIT_USAGE
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_labels_suggest(args: argparse.Namespace, ctx: Context, now: datetime, out: TextIO) -> int:
    """Propose a config from the repo's labels. Reads the label list; writes nothing."""
    read = attempt(ctx.adapter.labels)
    if not read.ok:
        out.write(f"labels: FAILED ({read.error})\n")
        return EXIT_FAILED
    data = labelsetup.report(read.data, ctx.config, ctx.remote, now)
    if args.json:
        out.write(json.dumps(data, indent=1) + "\n")
    else:
        out.write(labelsetup.render(data))
    return EXIT_OK


def cmd_labels_create(
    args: argparse.Namespace,
    ctx: Context,
    now: datetime,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    out: TextIO,
) -> int:
    """Create the labels the user approved, by name, through the write layer. Exits 2, having
    written nothing, when any name doesn't fit."""
    read = attempt(ctx.adapter.labels)
    if not read.ok:
        out.write(f"labels: FAILED ({read.error}); nothing created\n")
        return EXIT_FAILED
    try:
        plan = labelsetup.plan_create(args.names, read.data, ctx.config)
    except labelsetup.CreateError as error:
        print(f"forge-tracker: nothing created: {error}", file=sys.stderr)
        return EXIT_USAGE
    data = labelsetup.create(plan, make_writer(ctx, now, environ, run).apply)
    out.write(json.dumps(data, indent=1) + "\n" if args.json else labelsetup.render_create(data))
    return EXIT_OK if data["ok"] else EXIT_FAILED


def cmd_labels_decline(
    repo_dir: str,
    run: Callable[..., Any],
    now: datetime,
    environ: Mapping[str, str],
    out: TextIO,
) -> int:
    """Remember that the user declined setup here. Reads no forge, so it needs no token."""
    try:
        remote, _, _ = detect.locate(repo_dir, config.load(repo_dir).forge, run)
    except ConfigError as error:
        print(f"forge-tracker: {error}", file=sys.stderr)
        return EXIT_USAGE
    path = labelsetup.record_decline(remote, environ, now)
    out.write(
        f"setup declined for {remote.project}: the reports still say what's unset, and the "
        f"commands won't offer setup again (delete {path} to undo)\n"
    )
    return EXIT_OK


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
        ("closing references", attempt(a.closing_refs).map(len)),
        (
            "next fingerprint",
            attempt(a.fingerprint_inputs).map(lambda f: f"{len(f['branch_heads'])} branch heads"),
        ),
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
        description=(
            "Read a repo's forge (GitHub so far) and report on it. Every command only reads, "
            "except `write`, `link`, `groom apply` and `labels create`, which run named write "
            "operations."
        ),
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
    c.add_argument(
        "--workspace",
        metavar="DIR",
        help=(
            "catch up on every repo in DIR (its repos: list, else its child repos) in one "
            "report; a --repo that isn't a git repo but holds some is read this way too"
        ),
    )

    n = sub.add_parser(
        "next",
        help="what to work on: your queue, agent lanes, and what's blocked",
        description=(
            "What to work on: person-only items (your queue), agent-ready items and the lanes "
            "Claude grouped them into (the slate), and what's blocked. Read-only; it recommends "
            "and never claims, assigns, comments or branches. Exits 1 when any read failed."
        ),
    )
    n.add_argument(
        "action",
        nargs="?",
        choices=("facts", "save-slate"),
        help=(
            "facts: the facts and classification only (--json adds issue bodies, for ranking); "
            "save-slate: store a slate (JSON on stdin) against the cached facts"
        ),
    )
    n.add_argument("--rerank", action="store_true", help="ask for a new slate even if one holds")
    n.add_argument(
        "--refresh", action="store_true", help="re-read the facts even if the fingerprint holds"
    )
    n.add_argument("--json", action="store_true", help="print the report as JSON")

    lb = sub.add_parser(
        "labels",
        help="propose a config from the repo's labels, create approved ones, or decline setup",
        description=(
            f"suggest: read the repo's labels and print a proposed {config.PATH} (labels, tiers, "
            "ruling_pattern), plus a create command for each label it lacks. It writes nothing. "
            "create: create the labels named (a default name such as needs-human, a role such "
            "as needs_human, or role=name), each through the label-create write operation, and "
            "print the config lines that replace their ~. "
            "decline: remember, in the local cache, that the user declined setup here, so the "
            "commands stop offering it."
        ),
    )
    lb.add_argument("action", choices=("suggest", "create", "decline"))
    lb.add_argument("names", nargs="*", help="create: the approved labels")
    lb.add_argument("--json", action="store_true", help="print the proposal or result as JSON")

    g = sub.add_parser(
        "groom",
        help="find tracker drift and propose fixes; apply only the approved ones",
        description=(
            "Find where the tracker drifted (merged but open issues, tier labels and milestones, "
            "blocked labels and links, rulings left waiting, stale branches, conflicted or stale "
            "PRs, likely duplicates, placeholder due dates) and print the proposals with their "
            "evidence, the unclear bucket and a list for you. It writes nothing. `groom apply` "
            'reads {"approved": [ids]} on stdin, re-runs the checks, and writes each approved '
            "proposal that is still current. Exits 1 when any read or write failed."
        ),
    )
    g.add_argument(
        "action", nargs="?", choices=("apply",), help="apply: write the approved proposals"
    )
    g.add_argument("--json", action="store_true", help="print the report as JSON")

    w = sub.add_parser(
        "write",
        help="run one named write operation (JSON payload on stdin)",
        description=(
            "Run one named write operation with its JSON payload on stdin, through the "
            "write-wrapper when the config sets one. Exits 2 when it's refused."
        ),
    )
    w.add_argument("op", choices=writes.OPS)

    lk = sub.add_parser(
        "link",
        help="record that an issue is blocked by another, as a native link (or remove one)",
        description=(
            "Add or remove a native blocking link: `link add 18 --blocked-by 17` records that "
            "#18 waits on #17 in the forge's own relationship data, which /next and /groom read; "
            "prose such as 'Blocked by #17' in an issue body is invisible to both. It runs the "
            "link-add or link-remove write operation, through the write-wrapper when the config "
            "sets one, and is refused, writing nothing, for a link that already exists (add) or "
            "doesn't (remove), a self-link, or a pull request or closed issue on either side "
            "(add). Exits 2 when it's refused."
        ),
    )
    lk.add_argument("action", choices=("add", "remove"))
    lk.add_argument("issue", help="the blocked issue: 18 or #18")
    lk.add_argument(
        "--blocked-by",
        required=True,
        metavar="ISSUE",
        help="the blocker: 17, #17, or owner/repo#17 in another project",
    )

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
    stdin: Optional[TextIO] = None,
) -> int:
    args = parser().parse_args(argv)
    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    now = now or datetime.now(timezone.utc)
    if args.command == "next" and args.action == "save-slate":
        return cmd_save_slate(
            os.path.abspath(args.repo), run, now, environ, stdin or sys.stdin, out
        )
    if args.command == "labels" and args.action != "create" and args.names:
        print(f"forge-tracker: labels {args.action} takes no names", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "labels" and args.action == "decline":
        return cmd_labels_decline(os.path.abspath(args.repo), run, now, environ, out)
    writing = (
        args.command in ("write", "link")
        or (args.command == "groom" and args.action == "apply")
        or (args.command == "labels" and args.action == "create")
    )
    if writing and args.record:
        print("forge-tracker: --record is for reads; it doesn't record writes", file=sys.stderr)
        return EXIT_USAGE
    explicit = args.command == "catchup" and args.workspace is not None
    if explicit:
        return _run_workspace(
            args, os.path.abspath(args.workspace), True, http, run, now, environ, out, tz
        )
    try:
        ctx = open_forge(
            os.path.abspath(args.repo), http, run, record=bool(args.record), environ=environ
        )
    except (ConfigError, ForgeError) as error:
        if isinstance(error, ConfigError):
            # A folder of repos rather than a repo: catchup reads them all; the others say how
            # to name one.
            code = _run_workspace(
                args, os.path.abspath(args.repo), False, http, run, now, environ, out, tz
            )
            if code is not None:
                return code
        print(f"forge-tracker: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.command == "catchup":
            return cmd_catchup(args, ctx, now, environ, run, out, tz)
        if args.command == "next":
            return cmd_next(args, ctx, now, environ, out, tz)
        if args.command == "labels" and args.action == "create":
            return cmd_labels_create(args, ctx, now, environ, run, out)
        if args.command == "labels":
            return cmd_labels_suggest(args, ctx, now, out)
        if args.command == "groom" and args.action == "apply":
            return cmd_groom_apply(ctx, now, environ, run, stdin or sys.stdin, out)
        if args.command == "groom":
            return cmd_groom(args, ctx, now, out)
        if args.command == "write":
            return cmd_write(args, ctx, now, environ, run, stdin or sys.stdin, out)
        if args.command == "link":
            return cmd_link(args, ctx, now, environ, run, out)
        return cmd_smoke(ctx, now, out)
    except BrokenPipeError:
        return EXIT_FAILED
    finally:
        if ctx.recorder is not None:
            with open(args.record, "w", encoding="utf-8") as handle:
                # The subcommand and its options only: --repo and --record are local paths.
                command = argv[argv.index(args.command) :]
                handle.write(ctx.recorder.dump(recorded_at=iso(now), argv=command))
