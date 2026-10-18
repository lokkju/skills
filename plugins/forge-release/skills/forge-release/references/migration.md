# Migrating to yeet

Every migration has the same order:

1. Finish or close every open release PR from the old tool. yeet only adopts branches named
   `yeet/release-*`, so an old tool's PR goes stale the moment its workflow is gone.
2. Make the newest tag represent what's on main. yeet takes the current version from the newest
   tag matching `tag_prefix`; there's no manifest.
3. In one PR: remove the old tool's workflow and config, add `.yeet.yaml`, `yeet.yml` and the
   release-check files. Its commits are `ci:` or `build:`, and it gets the `no-release` label.
4. Dry run from a clone at `origin/main` with `--config` pointing at the PR's `.yeet.yaml`
   (`yeet-setup.md`, "The dry run"). Every target's `current_version` must equal its newest
   existing tag.
5. Give the repo the App credentials before merging; merged without them, `yeet.yml` fails at
   the token step and nothing else breaks.

## From release-please

Remove the release-please workflow (or job), `release-please-config.json` and
`.release-please-manifest.json`.

| release-please | yeet |
|---|---|
| `packages.<path>` | `targets.<name>` with `type: path`, `path: <path>` |
| `include-v-in-tag`, `include-component-in-tag`, `tag-separator` | `tag_prefix` per target, matching the existing tags exactly (`v`, `api-v`) |
| `bump-minor-pre-major: true` | `pre_major_breaking_bumps_minor: true` (yeet's default); release-please's default, `false`, is `pre_major_breaking_bumps_minor: false` |
| `bump-patch-for-minor-pre-major: false` (its default) | `pre_major_features_bump_patch: false`; yeet's default is `true`, so without this a feat after the migration bumps patch where it used to bump minor |
| `changelog-path` | `changelog.file` |
| `changelog-sections` | `changelog.include` plus `changelog.sections` |
| `extra-files` and the `release-type`'s own files | `version_files`, listed one by one (yeet has no release types) |
| `x-release-please-version`, `-major`, `-minor`, `-patch` | `x-yeet-version`, `-major`, `-minor`, `-patch` |
| `x-release-please-start-version` ... `x-release-please-end` | `x-yeet-start-version` ... `x-yeet-end` |
| `release-as` in config | a `Release-As: X.Y.Z` commit footer |
| `prerelease` | `release.channels` (SemVer only) |
| `separate-pull-requests` | `release.pull_request_mode: independent` |
| `label`, `release-label` | the default `autorelease: pending` and `autorelease: tagged` are the same names |

Things that change underneath:

- **Version files release-please never touched.** A `release-type` bumps its own files and
  nothing else, so a second manifest (a `plugin.json`, a chart) may have sat at an old version
  through many releases. List it in `version_files`; the first yeet release brings it up to
  the package version.
- **Python.** `release-type: python` bumped `pyproject.toml`. If the package has a static
  version and uses uv, move it to hatch-vcs in this PR, or the first release PR fails
  `uv sync --locked` (`yeet-setup.md`, lockfiles).
- **Publishing.** release-please runs publish steps in the same job, gated on its
  `release_created` output, because a release made with `GITHUB_TOKEN` can't start another
  workflow. yeet sets no step outputs. Move the publish into a workflow on `release: published`
  (yeet's release comes from the App token, so it starts it), and keep a `workflow_dispatch`
  with a `tag` input as the recovery path. If PyPI Trusted Publishing or crates.io trusted
  publishing names the workflow file, keep the publish job in that file, or change the
  publisher first; the same goes for the deployment environment it names. See `deploys.md`.
- **Changelog.** Old entries stay as they are. yeet keeps release-please's heading shape and
  section names; a breaking entry shows the commit description with the footer text as a note.
- **Commit overrides** (`BEGIN_COMMIT_OVERRIDE`) still work, but only from the final commit
  message, never from a PR body.

## From hand-made tags

- Pick the `tag_prefix` the existing tags use. If they're inconsistent (`1.2.0` and `v1.3.0`),
  create one tag in the new shape on the commit of the newest release, and let yeet continue
  from it.
- A CalVer tag without the counter (`v2026.10.18`) doesn't match `YYYY.0M.0D.MICRO`; yeet
  doesn't see it. Tag the same commit `v2026.10.18.1` (and delete the bare one once nothing
  points at it).
- With no matching tag at all, the dry run shows what yeet would treat as the first release;
  if that isn't right, create the starting tag by hand on main before enabling the workflow.

## SemVer to CalVer

For something that's deployed or taken as the latest snapshot, not depended on through a
compatibility promise. Change the target's `tag_prefix` too (`v` to `<name>--v`, say), so the old
SemVer tags don't match and yeet starts the new series cleanly; the first release of the day is
`.1`. Say it in the first entry: edit the changelog on the release branch before merging and
add a line above the generated sections.

## release-plz, or a repo staying put

A Cargo package stays on release-plz (`release-plz.md`). A repo on release-please moves when
someone is working in it anyway; until then, fix it in place with release-please's own docs.
