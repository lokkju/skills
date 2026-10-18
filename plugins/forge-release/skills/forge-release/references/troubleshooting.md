# Troubleshooting yeet

Start with `yeet release --dry-run --verbose` from a clean clone at `origin/main`. It prints the
tag each target continued from (`boundary_ref`), its `current_version`, and why each commit
matched or was ignored. yeet's own `docs/troubleshooting.md` covers labels and auto-merge.

## The token step fails

| Error | Cause and fix |
|---|---|
| Not installed, 404 on the installation | The App isn't installed on this repo. Add the repo to the installation in the App's settings (a gh OAuth token can't do it through the API). |
| `client-id` or `private-key` is empty | The client ID is stored as a secret instead of a variable, or under another name than `yeet.yml` reads, or the repo was never given the values. A fork or Dependabot run doesn't get secrets either; `yeet.yml` only runs on pushes to main. |
| A JWT or key error | The private key is from another App, or was regenerated. Store the current key. |
| Permission denied for `contents` or `pull_requests` | The App lacks the permission the workflow asks for. Raise it in the App's settings; the owner then has to accept the new permissions on the installation. |

## The release PR doesn't open

- **Nothing releasable.** Only `feat`, `fix`, `perf` and breaking changes release (or what
  `bump_types` lists). `chore`, `docs`, `ci`, `build` and `refactor` don't.
- **The commits don't touch the target.** A `path` target only counts commits under its `path`
  minus `exclude_paths`.
- **The commit isn't parsed.** A body or footer not separated from the subject by a blank line
  makes yeet ignore the commit. `--verbose` says so.
- **The run didn't happen or was skipped.** `yeet.yml` runs on pushes to main only. yeet skips a
  run when a newer push to the branch has overtaken it (the newer run does the work), and
  refuses a shallow, pull request or tag checkout (`fetch-depth: 0`).
- **The branch push was refused.** A ruleset or branch protection matching `yeet/release-*`
  blocks the App's push or force-push. Add the App as a bypass actor or exclude that pattern.

## The release PR opens but its checks don't run

It was opened with `GITHUB_TOKEN`, not the App token. Check that the yeet step's
`GITHUB_TOKEN` is `steps.token.outputs.token`.

## The release PR doesn't refresh

- **Several PRs carry `autorelease: pending`.** yeet finds the release by label. Close or
  relabel the stale ones (an old release-please PR is the usual one).
- **The labels were removed or renamed.** Rerun yeet; it relabels the PR. If the label names
  were changed in `.yeet.yaml` while a release was open, restore the old names, finish that
  release, then rename.
- **The new commits don't release** (see above): yeet refreshes with releasable commits only.
- **Hand edits vanished.** A refresh rebuilds the generated sections. Text above the first
  section, your own `###` sections and a closing paragraph survive; edits inside the generated
  sections don't.

## Tags don't match

- **Wrong prefix.** `boundary_ref` isn't the newest tag, or yeet plans a first release: the
  `tag_prefix` doesn't match the tags as written (`v` against `1.2.3`, `api-v` against
  `api/v1.2.3`).
- **A bare CalVer tag** (`v2026.10.18`, no counter) is invisible under `YYYY.0M.0D.MICRO`. Tag
  the same commit `.1`.
- **A prerelease tag** (`v1.3.0-beta.1`) is ignored by stable releases, by design.

## Version files weren't updated

- The file isn't in the target's `version_files`; yeet touches nothing else.
- The marker is missing or in a comment style yeet doesn't recognise (`#`, `//`, `/* */`, `--`,
  `;`, `<!-- -->`). Every listed file must contain one, or the run fails.
- JSON needs `format: json` and a `json_pointer`; markers don't work there.
- The file is under `.github/workflows/`, which needs workflows write on the App and in the
  token step.
- A lockfile (`uv.lock`, `Cargo.lock`) went stale: yeet can't manage those. Use hatch-vcs for uv;
  Cargo belongs on release-plz.

## Changelog lines doubled, missing or wrong

yeet reads the commit messages that land on the release branch, never PR descriptions.

- **Every PR listed twice:** the merge commit's subject is the PR title (the repo's default
  message for merge commits is "Pull request title", `merge_commit_title=PR_TITLE`). That
  subject is a conventional commit, and yeet lists it, with the PR number, beside each of the
  PR's own commits. Set the default merge message back (`merge_commit_title=MERGE_MESSAGE`,
  `merge_commit_message=PR_TITLE`): yeet skips "Merge pull request #N from <branch>" and only
  the PR's commits release. In a repo managed as code, change it there (Pulumi's
  `github.Repository` `merge_commit_title` and `merge_commit_message`), or the next apply undoes
  a web UI fix. Released entries stay as they are; edit the open release PR's entry if it has
  duplicates. Under the PR-title setting, a PR whose commits are all `chore` but whose title is
  `feat` released through the title alone; with the default subject it doesn't release, and
  release-check fails it before the merge.
- **Merge commits (default subject):** each of the PR's commits is a line and the merge commit
  is skipped.
- **Squash** (the setup turns it off): the squash commit is the only one. GitHub's default squash message for a
  multi-commit PR is the PR title plus a bullet list of the commits, which yeet doesn't read as
  separate changes; set the squash default to the PR title and description, and put
  `BREAKING CHANGE:` footers or a `BEGIN_COMMIT_OVERRIDE` block in the description so they reach
  the commit.
- **A line under the wrong heading:** the commit's type decides it. Fix the release notes by
  editing the entry on the release branch; fix the history with a `BEGIN_COMMIT_OVERRIDE` in the
  next commit.

## The release PR merged but nothing was tagged

The tag is created by the next `yeet.yml` run on main, the one on the release PR's merge
commit. Check that run: it may have been skipped because another push overtook it (the later
run tags it), failed at the token step, or never started (the workflow file changed in a way
that broke it). Rerun it with `workflow_dispatch`.

## The release exists but started nothing

The `release: published` workflow didn't run: the release was created with `GITHUB_TOKEN`
instead of the App token, or the workflow file isn't on the default branch. A guard job that
refused is working as meant; see `deploys.md`.

## yeet calls the wrong API

Requests to `/api/v3/...` on github.com mean the runner exports `GITHUB_URL`; set
`GITHUB_URL: ""` in the yeet step.
