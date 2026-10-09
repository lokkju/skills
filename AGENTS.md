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

Install the pre-commit hooks once per clone:

```bash
uvx pre-commit install
```

They run JSON and YAML checks, end-of-file and trailing-whitespace fixes, `ruff check` and
`ruff format` (settings in `ruff.toml`), shellcheck, and `scripts/check-skill-names.py`, which
fails when a skill's frontmatter `name` differs from its directory (`validate` doesn't check
that). `uvx pre-commit run --all-files` runs them on everything.

CI (`.github/workflows/ci.yml`) runs on every pull request and on pushes to `main`:

- `plugins`: `claude plugin validate --strict` on the marketplace and each `plugins/*`, then
  `claude plugin test plugins/session-decisions`. Neither needs an Anthropic login or API key.
- `forge-release`: `ruff check`, `ruff format --check` and the pytest suite on Python 3.11 and
  3.14.
- `shell`: shellcheck on every `*.sh` file and every tracked file with an `sh` or `bash` shebang.
- `skill-names`: `scripts/check-skill-names.py`.

`.github/workflows/pr-title.yml` checks that the PR title is a Conventional Commit. To run the CI
checks by hand:

```bash
claude plugin validate --strict .
claude plugin validate --strict plugins/<name>
claude plugin test plugins/session-decisions
uvx ruff check plugins/forge-release && uvx ruff format --check plugins/forge-release
uv run --no-project --with pytest pytest plugins/forge-release/tests
uv run --no-project scripts/check-skill-names.py
```

## Commits

Conventional Commits. No AI attribution in commits, pull requests or docs.
