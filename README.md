# lokkju/skills

These are the Claude Code plugins and agent skills I run on my own machines every day. I keep them here so the rest of you can use them too.

## What's in it

**[session-decisions](plugins/session-decisions/README.md)** keeps a queue of everything your assistant is waiting on you for. A long reply can bury the one question that matters three screens up; here every question becomes a numbered card instead, `D1` for a decision and `A1` for something only you can do. Each card carries a recommendation, which you accept with one click or answer in your own words, and the footer shows a count of what's still open. After compaction, the assistant is reminded of every item it hasn't settled. Its README has a recording of it in a real session.

**[forge-release](plugins/forge-release/README.md)** sets up release PRs across forges: Conventional Commits feed one standing release PR, and merging it tags and publishes. Its skill picks the tool ([yeet](https://github.com/monkescience/yeet) for everything but Cargo packages, which get release-plz), then walks a repo through setup, migration from release-please, troubleshooting, and the publish or deploy a release should trigger, with templates to copy. Its script, `github-app-create`, registers the private GitHub App the release runs as in two browser clicks and stores its credentials with `gh`, `pulumi` or `sops`. With `--public --user-token` it makes an App you can install across your organizations and stores a user access token for it instead.

**[forge-tracker](plugins/forge-tracker/README.md)** keeps durable work on the forge's issue tracker, and the tracker honest enough to plan from. `/catchup` tells you, when you come back to a project, what's waiting on you (mentions, review requests, needs-human issues), what merged, which rulings were made, how the board moved and whether CI is healthy, all since your last catchup. `/next` recommends what to work on: the person-only items in your queue ordered by how much agent work each unblocks, the agent-ready items grouped into lanes that shouldn't touch the same files, and what's blocked, closing with Claude's pick of what to merge, take and start first, each with its reason. `/groom` finds where the tracker drifted (issues a merged PR closes but that are still open, tier labels and milestones that disagree, stale blocked labels and links, blocking written only as prose in an issue body, rulings left waiting, merged branches) and fixes only what you approve, through the repo's write-wrapper when it has one. `forge-tracker link add 18 --blocked-by 17` records blocking as the native link `/next` reads, and the `forge-tracker` skill tells any agent filing or linking issues to do that, never a "Blocked by" sentence, and to label person-only work when filing it. Every read fails on its own and says so; nothing that failed is shown as empty. GitHub only for now; GitLab and Forgejo and the rest of the working rules come next.

**[term-recorder](plugins/term-recorder/README.md)** lets an assistant see a terminal program the way you do. It runs the program on a private tmux server, drives it from a step file of keystrokes, waits and clicks, and hands back plain-text screen snaps, an asciicast or a GIF. It works on Claude Code itself, which is how I check what a plugin's dialog really looks like.

**[session-history](https://github.com/lokkju/claude-session-index)** indexes, searches and reports on your Claude Code sessions. It lives in its own repo, lokkju/claude-session-index, along with the `claude-session-index` CLI it drives; the marketplace just lists it. It used to be called `claude-session-index` too, until Claude Code reserved plugin names that start with `claude-`. If you installed it under that name, run `claude plugin uninstall claude-session-index@lokkju`, then `claude plugin marketplace update lokkju` and `claude plugin install session-history@lokkju`.

**delegation-stats** (`scripts/delegation-stats.py`, not a plugin) measures how much your Claude Code sessions hand work to subagents. Point it at a project and a date range, `uv run scripts/delegation-stats.py ~/projects/infra --since 2026-10-01 --until 2026-10-14`, and it prints a row per session: prompts that made tool calls, how many of those called Agent, the longest run of reads with no Agent call, how many runs reached 10 reads, median and peak main-thread context, and what the subagents spent in tokens. `--json` adds the per-prompt numbers. It reads the transcripts under `~/.claude/projects/` and prints numbers only, never what you or Claude wrote.

## Install

As a Claude Code marketplace, which gets you each plugin whole, hooks, commands and scripts included:

```bash
claude plugin marketplace add lokkju/skills
claude plugin install session-decisions@lokkju
```

Restart Claude Code afterwards.

If you only want the skills, for any agent that [`npx skills`](https://github.com/vercel-labs/skills) supports:

```bash
npx skills add lokkju/skills
```

session-decisions won't work that way. Its skill drives a hook, the cards and two tools of its own, and only the Claude Code plugin provides those, so install it through the marketplace.

## License

[PolyForm Shield 1.0.0](LICENSE)
