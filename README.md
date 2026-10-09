# lokkju/skills

Claude Code plugins and agent skills I use every day and maintain in the open.

[![session-decisions queueing and settling decisions in a Claude Code session](plugins/session-decisions/docs/demo.gif)](plugins/session-decisions/README.md)

## What's here

| Plugin | What it does |
| --- | --- |
| [session-decisions](plugins/session-decisions/README.md) | A queue for everything your assistant is waiting on you for. Each question becomes a numbered card (`D1` for a decision, `A1` for something only you can do) in the transcript, a sidebar and the footer, with a recommendation you can accept in one click. Shown above. |
| [forge-release](plugins/forge-release/README.md) | Release-PR setup across forges. So far, `github-app-create`: it registers a private GitHub App for release tooling (yeet, release-plz, release-please) in two browser clicks and stores its credentials with `gh`, `pulumi` or `sops`. |
| [claude-session-index](https://github.com/lokkju/claude-session-index) | Index, search, and report on Claude Code sessions. Listed here; it lives in its own repo. |

## Install

As a Claude Code marketplace, which gets you the plugins whole (hooks, commands and scripts included):

```bash
claude plugin marketplace add lokkju/skills
claude plugin install session-decisions@lokkju
```

The skills alone, for any agent [`npx skills`](https://github.com/vercel-labs/skills) supports:

```bash
npx skills add lokkju/skills
```

session-decisions needs its plugin: its skill drives a hook, a sidebar and two fallback tools that only the Claude Code plugin provides. Install it through the marketplace.

## License

[PolyForm Shield 1.0.0](LICENSE)
