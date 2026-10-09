# lokkju/skills: Agent Notes

## Layout

- `.claude-plugin/marketplace.json`: the marketplace manifest, named `lokkju`. Local plugins use a
  `./plugins/<name>` source; a plugin that lives in its own repo uses a `github` source.
- `plugins/session-decisions/`: the in-session decision queue (a function-hooks module, a skill,
  `/decisions`). TypeScript, no dependencies; tests run with
  `claude plugin test plugins/session-decisions`. Its README covers type-checking and
  re-recording the demo.
- `plugins/forge-release/`: release-PR setup across forges. So far only `scripts/github-app-create`
  (GitHub App manifest flow); no skill yet. Standard-library Python plus `openssl`; tests run with
  `uv run --no-project --with pytest pytest plugins/forge-release/tests`.

`npx skills add lokkju/skills` finds every `SKILL.md` under `plugins/*/skills/`, so a skill here
is installable for any agent as well as through the marketplace.

## Adding a plugin

1. Create `plugins/<name>/.claude-plugin/plugin.json` (name, description, version, author,
   repository, license) and put skills in `plugins/<name>/skills/<skill>/SKILL.md`. Each skill's
   frontmatter `name` must match its directory name.
2. Add the entry to `.claude-plugin/marketplace.json` and the table in `README.md`.

## Versioning

Plugins carry a SemVer `version` in `plugin.json`. There is no release tooling; bump the version
by hand in the same commit that changes the plugin's content (patch for fixes and wording, minor
for new or changed behavior, major for removals or renames). Marketplace entries don't carry
versions.

## Validation

Run these before committing:

```bash
claude plugin validate --strict .
claude plugin validate --strict plugins/<name>
claude plugin test plugins/session-decisions
uv run --no-project --with pytest pytest plugins/forge-release/tests
```

`validate` doesn't check that a skill's frontmatter `name` matches its directory; check that by
eye.

## Commits

Conventional Commits. No AI attribution in commits, pull requests or docs.
