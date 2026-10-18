# lokkju/skills: Agent Notes

## Layout

- `.claude-plugin/marketplace.json`: the marketplace manifest, named `lokkju`. Local plugins use a
  `./plugins/<name>` source; a plugin that lives in its own repo uses a `github` source (so far
  only `session-history`, from lokkju/claude-session-index). Third-party plugin names can't start
  with `claude-`, `anthropic-`, `anthropics-` or `cc-plugin-`; `claude plugin validate --strict`
  rejects them.
- `plugins/session-decisions/`: the in-session decision queue (a function-hooks module, a skill,
  `/decisions`). TypeScript, no dependencies; tests run with
  `claude plugin test plugins/session-decisions`. Its README covers type-checking and
  re-recording the demo.
- `plugins/forge-release/`: release-PR setup across forges. So far only `scripts/github-app-create`
  (GitHub App manifest flow); no skill yet. Standard-library Python plus `openssl`; tests run with
  `uv run --no-project --with pytest pytest plugins/forge-release/tests`.
- `plugins/forge-tracker/`: issue-tracker working rules and reports (design in
  `docs/specs/forge-tracker.md`). So far the GitHub layer, `/catchup`, `/next` and `/groom`:
  a standard-library Python CLI (`scripts/forge-tracker`) and `commands/`. Only
  `scripts/forge_tracker/writes.py` writes to a forge. Tests run with
  `uv run --no-project --with pytest pytest plugins/forge-tracker/tests` over recorded fixtures,
  no network.
- `plugins/term-recorder/`: drives a terminal program from a step file on a private tmux server
  and captures it (text snaps, asciicast, GIF via agg). A standard-library Python CLI
  (`scripts/term-record`), a skill whose `allowed-tools` pre-approves only that script, and
  example step files for Claude Code. Tests run with
  `uv run --no-project --with pytest pytest plugins/term-recorder/tests` and need tmux.
- `scripts/`: repo scripts that aren't part of any plugin. `check-skill-names.py` (below) and
  `delegation-stats.py`, a standard-library `uv run` script that reads a project's Claude Code
  transcripts under `~/.claude/projects/` and prints delegation numbers per session: prompts
  with tool calls, the share with an Agent call, read runs, main-thread context and subagent
  tokens (lokkju/infra#284). It prints numbers only, never transcript content, and exits 2
  naming the file and line on a record shape it doesn't know, which is the cue to update it for
  a new Claude Code version. Tests run with `uv run --no-project --with pytest pytest
  scripts/tests` over synthetic transcripts; never commit real ones.

`npx skills add lokkju/skills` finds every `SKILL.md` under `plugins/*/skills/`, so a skill here
is installable for any agent as well as through the marketplace.

## Adding a plugin

1. Create `plugins/<name>/.claude-plugin/plugin.json` (name, description, version, author,
   repository, license) and put skills in `plugins/<name>/skills/<skill>/SKILL.md`. Each skill's
   frontmatter `name` must match its directory name.
2. Add the entry to `.claude-plugin/marketplace.json` and the table in `README.md`.
3. Add a target to `.yeet.yaml` shaped like the existing ones: `tag_prefix: <name>--v`, the
   `plugin.json` version file, its own `CHANGELOG.md`, and `exclude_paths` for whatever it has
   that doesn't ship behavior.

## Versioning

[yeet](https://github.com/monkescience/yeet) owns plugin versions and changelogs (`.yeet.yaml`,
`.github/workflows/yeet.yml`). Each plugin is its own target with a CalVer version
(`YYYY.0M.0D.MICRO`, UTC), a `<plugin>--v<version>` tag and its own
`plugins/<plugin>/CHANGELOG.md`. Don't edit `version` in `plugin.json` or a `CHANGELOG.md` by
hand; yeet writes both. Marketplace entries carry no version.

Conventional Commit types decide what releases: `feat`, `fix`, `perf` and any breaking change
(`type!:`) release the plugins whose files they touch; other types don't. yeet reads the commit
messages that land on `main`. PRs merge with merge commits under GitHub's default merge message
("Merge pull request #N from <branch>"), which yeet skips, so a PR releases by its own commits;
squash and rebase merging are off. A merge commit titled with the PR title would count too and
list each PR twice in the changelog, as forge-tracker 2026.10.18.1's entry does. README.md, `docs/`, `tests/` and type-checking config don't count toward
a release (`exclude_paths` in `.yeet.yaml`).

A PR that changes a plugin's releasable files needs a `feat`, `fix` or `perf` commit, or the
`no-release` label when the change shouldn't reach users; `release-check` fails it otherwise.

After a releasable merge, yeet opens or refreshes one release PR (`yeet/release-main`) covering
every plugin with pending changes. Merging that PR ships the release: yeet tags each plugin and
creates its GitHub Release from the changelog entry. To edit release notes, edit the changelog on
the release branch before merging.

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
- `forge-release`, `forge-tracker` and `term-recorder`: `ruff check`, `ruff format --check` and
  the pytest suite on Python 3.11 and 3.14 (tmux is installed for term-recorder's).
- `scripts`: the same ruff checks and `scripts/tests` on Python 3.11 and 3.14.
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
uvx ruff check plugins/forge-tracker && uvx ruff format --check plugins/forge-tracker
uv run --no-project --with pytest pytest plugins/forge-tracker/tests
uvx ruff check plugins/term-recorder && uvx ruff format --check plugins/term-recorder
uv run --no-project --with pytest pytest plugins/term-recorder/tests
uvx ruff check scripts && uvx ruff format --check scripts
uv run --no-project --with pytest pytest scripts/tests
uv run --no-project scripts/check-skill-names.py
```

## Commits

Conventional Commits. No AI attribution in commits, pull requests or docs.
