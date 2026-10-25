---
name: forge-release
description: Use when setting up, migrating or fixing a repo's release automation - release PRs or MRs driven by Conventional Commits, yeet (.yeet.yaml), moving off release-please, release-plz for Rust crates, a release GitHub App and its token, a release PR that won't open or refresh, tags or versions yeet doesn't pick up, or a deploy or publish workflow that should run when a release is published. Covers GitHub, GitLab and Azure DevOps.
---

# forge-release

One standing release PR (or MR) per repo, built from Conventional Commits. Merging it tags the
version and creates the forge release, and that release starts whatever publishes or deploys.
Versions and changelogs are written by the release tool only; never edit a `version` field or a
`CHANGELOG.md` entry by hand (edit release notes on the release branch before merging, which the
tool keeps).

Paths below are relative to this skill's directory. The App helper is
`${CLAUDE_SKILL_DIR}/scripts/github-app-create` (its README is the plugin's `README.md`).
`${CLAUDE_SKILL_DIR}` is the directory holding this SKILL.md. Claude Code fills it in; in an
agent that leaves it as written, put that directory's path in its place.

## Decide

Answer these in order; the first that matches decides.

1. **Is it a Cargo package (a Rust crate or workspace)?** Use **release-plz**, see
   `references/release-plz.md`. Reasons: release-plz publishes to crates.io and runs
   cargo-semver-checks before a release, which yeet doesn't do; and `Cargo.lock` records the
   package's own version, which yeet can't rewrite (it only edits marker lines and JSON
   pointers), so a yeet release PR would leave the lock stale and `cargo build --locked` would
   fail. Cargo has no dynamic-version escape like hatch-vcs.
2. **Is the forge something other than GitHub, GitLab or Azure DevOps** (Forgejo, Gitea,
   Codeberg, Bitbucket)? yeet doesn't support it. Stop and ask the user which tool to use; this
   skill has no default there.
3. **Everything else: yeet.** SemVer or CalVer, single package or monorepo, on GitHub, GitLab or
   Azure DevOps, self-hosted included.

Then pick the versioning:

- **SemVer** (`vMAJOR.MINOR.PATCH`) when others depend on a compatibility contract: libraries,
  packages, CLIs others script against, schemas or formats consumers pin.
- **CalVer** `YYYY.0M.0D.MICRO` in UTC when it's deployed or published as snapshots: sites,
  services, container images, datasets, plugins whose users just take the latest.

And where the repo is now:

| Repo today | Do |
|---|---|
| No release automation | [Set up](#set-up-yeet) |
| release-please, hand-made tags, or SemVer moving to CalVer | [Migrate](#migrate) |
| release-plz on a Cargo package | Leave it. Fix it with `references/release-plz.md` |
| yeet, misbehaving | [Troubleshoot](#troubleshoot) |
| Releases work; a publish or deploy should follow them | [Release-triggered deploys](#release-triggered-deploys) |

A repo on release-please today moves to yeet when someone is working in it anyway, not as a
standalone chore.

## Set up yeet

Full detail, including GitLab and Azure DevOps, is in `references/yeet-setup.md`. On GitHub:

1. **Repo settings.** Merge commits on with GitHub's default merge message
   (`merge_commit_title=MERGE_MESSAGE`, `merge_commit_message=PR_TITLE`); squash and rebase off.
   Every PR commit lands and yeet reads each one; it skips the default "Merge pull request #N
   from <branch>" subject, and only the PR's own commits release. A repo managed as code
   (Pulumi's `github.Repository`, Terraform's `github_repository`) sets these in that code, not
   the web UI. Rulesets and branch protection must let the App force-push `yeet/release-*`.
2. **Release App.** One App per owner, reused by every repo of that owner. If none exists, run
   `github-app-create` (contents and pull requests write are its defaults, which is what yeet
   needs); it stores the client ID as variable `YEET_APP_ID` and the key as secret
   `YEET_APP_PRIVATE_KEY` on each `--repo`. If it exists, add the repo in the App's settings
   (Install App, then Configure; the API refuses a gh OAuth token) and store the two values on
   the repo, from wherever the owner keeps them (`--to pulumi` or `--to sops` in
   `github-app-create` terms). The token must come from the App: a PR or release made with
   `GITHUB_TOKEN` starts no workflows.
3. **`.yeet.yaml`.** Copy `templates/yeet.semver.yaml` or `templates/yeet.calver.yaml`, fill in
   the target, its `version_files` and `exclude_paths`. Keep `telemetry.enabled: false`.
4. **Workflow.** Copy `templates/yeet.yml` to `.github/workflows/yeet.yml` unchanged.
5. **Commit guard.** Copy `templates/release-check.yml` and `templates/release-check.sh`
   (`.github/workflows/`, `.github/scripts/`, mode 0755). Add `templates/pr-title.yml` only for a
   repo that keeps merge commits titled with the PR title. Create the `no-release` label.
6. **Dry run** before merging: `GITHUB_TOKEN=$(gh auth token) yeet release --dry-run --verbose`
   from a clone whose HEAD is the remote's release branch, pointing `--config` at the new file.
   It must report the current version you expect.
7. **Publish or deploy**, if the repo has either: [below](#release-triggered-deploys).

Python with uv: the package must take its version from the tag (hatch-vcs,
`dynamic = ["version"]`). A static `version` in `pyproject.toml` ends up in `uv.lock`, a yeet bump
leaves the lock stale, and `uv sync --locked` fails on the release PR.

## Migrate

`references/migration.md` covers release-please (config and marker mapping, the pre-1.0 bump
settings that keep its behaviour, its publish job), repos with hand-made tags, and moving a
SemVer repo to CalVer. The order is always: finish or close open release PRs, make the newest
tag represent what's on main, swap the config and workflow in one PR labeled `no-release`, dry
run, merge.

## Troubleshoot

`references/troubleshooting.md` goes symptom by symptom: the token step fails (App not
installed, wrong variable), the release PR doesn't open or doesn't refresh, tags yeet doesn't
see, version files left alone, changelog lines doubled or missing (every PR listed twice when
the merge subject is the PR title), a merged release PR with no tag, and a release that started no workflow. Start with
`yeet release --dry-run --verbose`; it says which tag it continued from and why each commit was
counted or ignored.

## Release-triggered deploys

`references/deploys.md` and `templates/deploy.yml`. Publish and deploy jobs run on
`release: published` in their own workflow (yeet sets no step outputs to chain on), plus a
`workflow_dispatch` that takes an existing tag for recovery. The guards: deploy only a tag
reachable from main, only the newest release-shaped tag, and create nothing (no tags, no
releases) in the deploy workflow.
