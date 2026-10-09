# lokkju/skills

These are the Claude Code plugins and agent skills I run on my own machines every day. I keep them here so the rest of you can use them too.

[![A Claude Code session queues its decisions while planning a SQLite to Postgres move; Accept all answers them in one message, and the sidebar drops to the two actions still waiting](plugins/session-decisions/docs/demo.gif)](plugins/session-decisions/README.md)

That's session-decisions in a real session. I asked Claude to plan moving a small Flask service from SQLite to Postgres without writing any code. It queued its questions as cards in the transcript and the sidebar, each with the answer it recommends. One click on Accept all put an answer line for every recommendation in my prompt, and one Enter settled them. What's left are the two things only I can do: tell it where the database lives, and hand over a sample of the data.

## What's in it

**[session-decisions](plugins/session-decisions/README.md)** keeps a queue of everything your assistant is waiting on you for. A long reply can bury the one question that matters three screens up; here every question becomes a numbered card instead, `D1` for a decision and `A1` for something only you can do. Each card carries a recommendation, which you accept with one click or answer in your own words, and the footer shows a count of what's still open. After compaction, the assistant is reminded of every item it hasn't settled.

**[forge-release](plugins/forge-release/README.md)** sets up release PRs across forges. Right now it's one script, `github-app-create`, which registers a private GitHub App for release tooling (yeet, release-plz, release-please) in two browser clicks and stores its credentials with `gh`, `pulumi` or `sops`. The skill that walks a repo through the whole setup comes next.

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
