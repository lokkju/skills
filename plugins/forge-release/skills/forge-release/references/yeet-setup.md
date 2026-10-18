# Setting up yeet

yeet ([monkescience/yeet](https://github.com/monkescience/yeet), MIT, docs in its `docs/`) runs
on every push to the release branch. Each run moves the release one step: no open release PR
means it writes the changelog entry and version files on `yeet/release-<branch>` and opens a PR
labeled `autorelease: pending`; an open one gets refreshed with new commits; a merged one gets
its tag and forge release, and the label becomes `autorelease: tagged`. Its only state is git
tags, `CHANGELOG.md` and those two labels.

The templates pin yeet v0.17.0, the version these workflows have run. To upgrade, take the
digest of the new release's `yeet_linux_amd64.tar.gz`
(`gh release view vX.Y.Z -R monkescience/yeet --json assets --jq '.assets[] | select(.name == "yeet_linux_amd64.tar.gz") | .digest'`)
and change `YEET_VERSION`, `YEET_SHA256` and the schema URL in `.yeet.yaml` together.

## Repo settings (GitHub)

- **Merge method.** Merge commits on. yeet reads every commit that lands on the release branch,
  including the commits a merge commit brings in, so a PR's commits each become a changelog
  line. If the merge commit's title is the PR title (Settings, Pull Requests, "Default commit
  message": Pull request title), that title is a commit too, and a conventional one: yeet lists
  it as well, and `templates/pr-title.yml` should check it. With the default merge message
  ("Merge pull request #N from ...") yeet skips it. If squash stays enabled, set its default
  message to "Pull request title and description", since yeet reads the final commit only.
- **Branch rules.** The token must be able to push and force-push `yeet/release-*`, and create
  tags matching the tag prefix. A ruleset that covers all branches or tags needs the App as a
  bypass actor.
- **Labels.** yeet creates `autorelease: pending`, `autorelease: tagged` and `yeet`. Create
  `no-release` for release-check yourself.

## The release App and its credentials

The release has to start other workflows (the release PR's checks, a `release: published`
publish or deploy), and anything created with the workflow's `GITHUB_TOKEN` starts none. So yeet
runs with an installation token of a GitHub App, minted per run by
`actions/create-github-app-token` and scoped to the one repo with contents and pull requests
write. Add workflows write only if `version_files` include files under `.github/workflows/`.

One App per owner (organization or user) serves all of its repos. A private App can only be
installed on the account that owns it; an App meant for a user account and their organizations
needs `--public` (see the plugin README's "Public Apps" for what that exposes).

**No App yet.** Run the helper (see the plugin README for every flag):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/github-app-create --org ACME --name ACME-release --repo ACME/REPO
```

Two clicks in the browser: Create GitHub App, then Install (choose the repos). Where the
credentials go:

| Owner keeps repo secrets in | Run with | Result |
|---|---|---|
| Nothing central | `--to github` (default), one `--repo` per repo | variable `YEET_APP_ID` (the client ID), secret `YEET_APP_PRIVATE_KEY` on each repo |
| Pulumi | `--to pulumi --stack S` | stack config `yeetAppId`, `yeetAppPrivateKey`, `yeetAppInstallationId`; the stack pushes them to repos |
| A SOPS file in a GitOps repo | `--to sops --sops-file F` | key in the Secret file; client ID printed |

On a remote host, `--port 8765 --no-browser` and an `ssh -L 8765:127.0.0.1:8765` tunnel from
the machine with the browser.

**App exists.** Add the repo to the installation in the App's settings page (Install App, the
gear, Repository access). The installations API needs a classic token for that and refuses a gh
OAuth token with 403. Then store the client ID and key on the repo under the names the
workflow reads, the same way the owner's other repos get them. If the owner's names differ
(`RELEASE_APP_ID`, say), change the two `vars.`/`secrets.` references in `yeet.yml` rather than
storing a second copy.

The client ID goes in a variable, not a secret: `create-github-app-token` takes it as
`client-id`, and the workflow reads `vars.YEET_APP_ID`. A secret by that name reads as empty.

## `.yeet.yaml`

Start from `templates/yeet.semver.yaml` or `templates/yeet.calver.yaml`. What to fill in:

- **`tag_prefix`** matches the tags the repo has or should have: `v` for `v1.2.3`,
  `<name>--v` per component in a monorepo. yeet continues from the newest tag with that prefix.
- **`version_files`** lists every file that carries the version; yeet touches nothing else. A
  text file (TOML, YAML, Markdown) needs a marker comment on the line, `version = "1.2.3" #
  x-yeet-version`, or an `x-yeet-start-version` ... `x-yeet-end` block. JSON takes
  `format: json` and `json_pointer: /version`. Every listed file must contain a marker.
- **Lockfiles that record the package's own version** can't be listed: their tools rewrite them
  and drop comments. Python on uv: use hatch-vcs (`dynamic = ["version"]`,
  `[tool.hatch.version] source = "vcs"`) so neither `pyproject.toml` nor `uv.lock` has a version
  to bump. A Cargo package goes to release-plz instead (`release-plz.md`).
- **`exclude_paths`** drops what doesn't ship: the target's own `CHANGELOG.md` (always, so the
  release commit never counts), tests, docs, `.github`, lockfiles that only dependency bots
  touch. `release-check.sh` uses the same list.
- **SemVer before 1.0.0:** the template sets `pre_major_features_bump_patch: false`, so a feat
  bumps minor as SemVer reads it; yeet's default bumps patch.
- **CalVer:** `YYYY.0M.0D.MICRO` with `timezone: UTC`, so a dry run on a laptop and the CI run
  agree on the date.
- **Telemetry** is on by default. Keep `telemetry.enabled: false`; the workflow also sets
  `DO_NOT_TRACK=1`.
- A monorepo gets one `type: path` target per component. All of them share one release PR
  unless `release.pull_request_mode: independent`.

## The dry run

`yeet release --dry-run --verbose` with `GITHUB_TOKEN=$(gh auth token)`. It refuses unless the
local HEAD is the remote head of the release branch, so run it from a clean clone (or
worktree) at `origin/main` and pass the new config with `--config /path/to/new/.yeet.yaml`.
When the release branch itself is what you're testing, a scratch copy of the config with
`branch:` set to your branch works too. Check `current_version` and `boundary_ref` for each
target, and which commits matched.

## GitLab and Azure DevOps

The `.yeet.yaml` is the same. The pipeline examples are in yeet's `docs/ci.md`; the tokens in
`docs/authentication.md`:

- **GitLab:** a project or group access token with `api` and at least Developer, as a masked
  `GITLAB_TOKEN` CI/CD variable (protected only if the job only runs on protected branches).
  Protected branch and tag rules must let it push and force-push `yeet/release-*` and create the
  tags. Run the job on the default branch with `GIT_DEPTH: "0"`.
- **Azure DevOps:** the pipeline's build service identity needs Read, Contribute, Contribute to
  pull requests, Create branch, Create tag and Force push on the repo; map `$(System.AccessToken)`
  to `AZURE_DEVOPS_SYSTEM_ACCESSTOKEN`.
- `release-check.sh` is GitHub-only. On GitLab, the closest built-in is a push rule regex on
  commit messages (a Premium feature).

## Self-hosted runners

- No Docker daemon: the template already runs the release tarball, not `docker://`.
- A runner that exports `GITHUB_URL` (GARM does) makes yeet call `/api/v3/...` as if on GitHub
  Enterprise. Set `GITHUB_URL: ""` in the yeet step's `env`.
- Caches follow `XDG_CACHE_HOME`, which a runner may point somewhere other than `~/.cache`.
