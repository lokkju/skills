# lokkju/skills

These are the Claude Code plugins and agent skills I run on my own machines every day. I keep them here so the rest of you can use them too.

## What's in it

**[session-decisions](plugins/session-decisions/README.md)** keeps a queue of everything your assistant is waiting on you for. A long reply can bury the one question that matters three screens up; here every question becomes a numbered card instead, `D1` for a decision and `A1` for something only you can do. Each card carries a recommendation, which you accept with one click or answer in your own words, and the footer shows a count of what's still open. After compaction, the assistant is reminded of every item it hasn't settled. Its README has a recording of it in a real session.

**[forge-release](plugins/forge-release/README.md)** sets up release PRs across forges. Right now it's one script, `github-app-create`, which registers a private GitHub App for release tooling (yeet, release-plz, release-please) in two browser clicks and stores its credentials with `gh`, `pulumi` or `sops`. With `--public --user-token` it makes an App you can install across your organizations and stores a user access token for it instead. The skill that walks a repo through the whole setup comes next.

**[claude-session-index](https://github.com/lokkju/claude-session-index)** indexes, searches and reports on your Claude Code sessions. It lives in its own repo; the marketplace just lists it.

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
