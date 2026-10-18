# Release-triggered publishes and deploys

yeet creates the tag and the forge release and stops. Anything that ships the release (a PyPI
or npm publish, a container image, a site deploy) runs in its own workflow on
`release: published`. That works because the release comes from the App token; one created with
`GITHUB_TOKEN` starts no workflow, which is why release-please setups publish in the same job.
yeet sets no step outputs, so there's nothing to chain on in `yeet.yml` itself.

Start from `templates/deploy.yml`.

## The guards

All of them live in a `guard` job the deploy job `needs`:

- **Release-shaped tags only.** `TAG_PATTERN` is the exact shape yeet writes (`^v[0-9]+\.[0-9]+\.[0-9]+$`
  for SemVer, `^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$` for CalVer, with the component prefix in
  a monorepo). The newest-tag check filters on it too, so a stray `v*` tag (a test tag, an old
  bare CalVer tag) can neither deploy nor block deploys.
- **From main only.** The tag's commit must be an ancestor of `origin/main`. A release made from
  another branch, or a dispatch with a tag that isn't on main, is refused.
- **The newest release only.** `sort -V` over the release-shaped tags; anything but the newest
  is refused. A re-run of an old deploy, or a slow run overtaken by a newer release, can't roll
  production back. Rolling back on purpose is a new release (a `revert:` commit, or a `fix`).
- **Creates nothing.** No tags, no releases, no commits from the deploy workflow; those belong
  to yeet. Uploading build artifacts to the existing release (`gh release upload "$TAG"`) is
  fine.

`workflow_dispatch` with a `tag` input is the recovery path for a deploy or publish that failed
for an outside reason (an outage, a bad credential). It rebuilds from the existing tag under the
same guards. Re-running the original run replays its workflow file from that commit, including
whatever was broken in it.

## Concurrency

Give the deploy its own concurrency group, used by no other workflow, with
`cancel-in-progress: false`. GitHub keeps one pending run per group and cancels the older pending
one; within the deploy that's harmless (the newer tag would win the guard anyway), but a group
shared with another workflow lets that workflow's run cancel a queued deploy, or the other way
round.

## Publishing packages

- **PyPI Trusted Publishing** names the workflow file and, optionally, the environment. Keep the
  publish job in the file the publisher names; moving it means changing the publisher on PyPI
  first. The job needs `id-token: write` and checks out the tag with `fetch-depth: 0` so
  hatch-vcs sees it.
- **npm and crates.io trusted publishing** work the same way: the registry trusts
  a specific workflow file, so don't rename it in passing.
- Gate the publish on full CI from the tag (`uses: ./.github/workflows/test.yml`), so a release
  whose tag fails CI doesn't ship.

## Deploying from a host

A deploy that runs on the target host (a self-hosted runner, a pull-based script) fetches tags
itself. On a private repo, give git the gh credentials for that one command:

```bash
git -c credential.helper='!gh auth git-credential' fetch --tags origin
```

The same guards apply there: refuse a tag that isn't on `origin/main` or isn't the newest
release-shaped tag.

## GitLab

A tag pipeline runs for the tag yeet creates. Put the deploy job behind
`rules: - if: $CI_COMMIT_TAG =~ /^v[0-9]+\.[0-9]+\.[0-9]+$/` and do the same ancestor and newest
checks in its script.
