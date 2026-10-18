# release-plz (Cargo packages)

The one exception to yeet. [release-plz](https://release-plz.dev) knows Cargo: it bumps every
`Cargo.toml` and `Cargo.lock` in a workspace together, runs cargo-semver-checks to catch an API
break that the commit types didn't declare, writes changelogs with git-cliff, and publishes to
crates.io. yeet would leave `Cargo.lock` behind (it records each workspace package's own
version) and can't publish or semver-check.

## Setup

`release-plz.toml` at the workspace root:

```toml
[workspace]
changelog_update = true
git_release_enable = true
git_tag_enable = true
publish = true          # false for a crate that isn't on crates.io
semver_check = true
allow_dirty = false

# Crates that must share one version (one re-exports the other):
[[package]]
name = "foo-core"
version_group = "foo"

[[package]]
name = "foo"
version_group = "foo"
```

`.github/workflows/release-plz.yml` runs two jobs on every push to main: `release-pr` (opens or
refreshes the release PR; git only, no registry credential) and `release` (publishes and tags
once a release PR has merged; a no-op otherwise).

- **Token.** Give both jobs the release App's token (the same `create-github-app-token` step as
  `templates/yeet.yml`) instead of `secrets.GITHUB_TOKEN`; with `GITHUB_TOKEN` the release PR's
  checks don't run and the release starts no other workflow.
- **crates.io trusted publishing.** `rust-lang/crates-io-auth-action` trades the run's OIDC
  token for a short-lived crates.io token (`id-token: write` on the `release` job). Put the job
  in an environment restricted to `main`, and register the workflow file and environment as the
  crate's trusted publisher. A publisher can only be registered for a crate that already
  exists, so the first publish of each crate is by hand.
- **No approval gate** on that environment: the job runs on every push to main and is usually a
  no-op, so a reviewer would be trained to click through. Merging the release PR is the gate.
- `templates/release-check.sh` reads `.yeet.yaml`, so it doesn't apply as is. The PR-title check
  (`templates/pr-title.yml`) does.

## Troubleshooting

- **semver-checks fails the release PR:** the change is breaking. Mark the commit (`feat!:` or a
  `BREAKING CHANGE:` footer), or undo the break.
- **Publish fails for the first release of a crate:** trusted publishing isn't registered yet,
  because the crate doesn't exist. Publish once by hand with a token, then register.
- **Tags:** release-plz tags `<crate>-v<version>` in a workspace and `v<version>` for a single
  crate by default (`git_tag_name` changes it); a deploy's `TAG_PATTERN` must match.
