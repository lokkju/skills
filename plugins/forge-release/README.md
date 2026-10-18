# forge-release

Release-PR setup across forges: Conventional Commits feed one standing release PR or MR, and
merging it tags and publishes. The plugin has a skill and one helper script.

## The forge-release skill

`skills/forge-release/SKILL.md` walks a repo through its release automation. It starts with a
decision tree: new repos use [yeet](https://github.com/monkescience/yeet), for SemVer and CalVer
alike, on GitHub, GitLab or Azure DevOps. Cargo packages are the exception and use
[release-plz](https://release-plz.dev), which publishes to crates.io, runs cargo-semver-checks
and keeps `Cargo.lock` in step, none of which yeet does. On a forge yeet doesn't support, the
skill stops and asks.

Then the procedures, with a reference file each under `skills/forge-release/references/`:

- **Setup** (`yeet-setup.md`): repo settings, the release App through `github-app-create` and
  where its credentials go, `.yeet.yaml`, the workflow, the release-check guard on Conventional
  Commits, and the dry run.
- **Migration** (`migration.md`): from release-please (config and marker mapping, the pre-1.0
  bump settings that keep its behaviour, moving the publish job), from hand-made tags, and from
  SemVer to CalVer.
- **Troubleshooting** (`troubleshooting.md`): the token step, a release PR that won't open or
  refresh, tags yeet doesn't see, version files left alone, what yeet reads under merge and
  squash, and a merged release PR with no tag.
- **Release-triggered deploys** (`deploys.md`): publishing and deploying on
  `release: published`, with guards that deploy only the newest release tag on main and create
  nothing.
- **release-plz** (`release-plz.md`): the Cargo setup with crates.io trusted publishing.

`skills/forge-release/templates/` has the files to copy: `yeet.yml`, a SemVer and a CalVer
`.yeet.yaml`, `release-check.yml` with its script, `pr-title.yml` and `deploy.yml`.

## github-app-create

Release tools like yeet and release-please push tags and open PRs with a GitHub App token, so a
`release: published` workflow can run off them (the default `GITHUB_TOKEN` can't trigger other
workflows). `scripts/github-app-create` registers that App (or, with `--user-token`, an App whose user access
token acts as you) through GitHub's
[App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
and stores its credentials. You click twice in the browser: Create GitHub App, then Install
(and a third time, Authorize, with `--user-token`).

```bash
plugins/forge-release/scripts/github-app-create --org acme --name acme-release \
  --repo acme/site --repo acme/api
```

What it does:

1. Starts a server on `127.0.0.1` on a free port (or the one `--port` names) and opens a page that posts the manifest to
   GitHub (the org's `settings/apps/new`, or your account's with `--user`). The manifest asks for
   a private App (public with `--public`) with no webhook and the permissions you give; the
   default is `contents=write` and `pull_requests=write`, which is what yeet needs. If no browser
   opens, the script prints the local URL; `--no-browser` skips the attempt and just prints it.
2. GitHub redirects back with a one-time code. The script checks the `state` it sent, then
   trades the code for the App's ID, client ID and private key (no auth needed for that call).
3. Stores the client ID and key (see below).
4. Opens the App's install page and polls `GET /app/installations` with an App JWT every five
   seconds until the installation shows up, then prints its ID (and stores it in Pulumi mode).

The private key is never printed. If storing it fails, the script writes it to
`./<slug>.private-key.pem` (mode 0600) so it isn't lost, or to `./<slug>.private-key.<n>.pem`
(the first free `<n>`) if a file by that name is already there, and says which file it used.

### Where the credentials go

| `--to` | Client ID | Private key | Installation ID |
|---|---|---|---|
| `github` (default) | `gh variable set YEET_APP_ID -R <repo>` per `--repo` | `gh secret set YEET_APP_PRIVATE_KEY -R <repo>`, value on stdin | printed |
| `pulumi --stack <stack> [--cwd <dir>]` | `pulumi config set --secret yeetAppId` | `pulumi config set --secret yeetAppPrivateKey`, value on stdin | `yeetAppInstallationId` |
| `sops --sops-file <path> [--sops-key <name>]` | printed (JSON) | entry in a SOPS-encrypted Kubernetes Secret file, default key `<slug>.pem` | printed (JSON) |
| `stdout [--key-file <path>]` | JSON on stdout | file, mode 0600 (default `./<slug>.private-key.pem`, or the first free `./<slug>.private-key.<n>.pem`) | JSON on stdout |

Rename the stored values with `--client-id-var`, `--key-secret` and `--installation-id-var`.
Pulumi keys are the camelCase form of those names (`YEET_APP_ID` becomes `yeetAppId`); a name
with a namespace, like `infra:releaseAppId`, is used as is. `--homepage` sets the App's homepage
URL (default: the org's GitHub page), and `--timeout` (default 600 seconds) bounds each browser
step. `--help` lists everything.

The value stored as `YEET_APP_ID` is the client ID, not the numeric App ID: GitHub accepts
either to identify the App when signing a JWT and recommends the client ID. The numeric ID is
printed (and is in the `--to stdout` JSON) if a tool insists on it.

### Storing the key in a SOPS Secret file

`--to sops` encrypts the key straight into a Kubernetes Secret file in a git repo, so the
plaintext never reaches disk. For a GARM runner pool:

```bash
plugins/forge-release/scripts/github-app-create --org acme --name acme-garm \
  --permission administration=write --permission organization_self_hosted_runners=write \
  --to sops --sops-file ~/infra/homelab/platform/garm/app-keys.sops.yaml \
  --secret-name garm-app-keys --namespace garm
```

The key lands under `stringData` as `<slug>.pem`; `--sops-key` picks another name. If the file
uses `data` instead, the value is base64-encoded there. `--secret-name` and `--namespace` only
matter when the file doesn't exist yet (the script refuses to start without them in that case).
An existing entry with the same key is left alone unless you pass `--replace`.

The script stays standard library only, so it doesn't parse YAML. sops does the editing:

- An existing file is decrypted once to JSON (in memory, to check for the key and see whether it
  uses `stringData` or `data`). The script then copies the still-encrypted file to a temp file in
  the same directory, runs `sops set --value-stdin` on the copy (the key travels on stdin, never in
  argv), and renames the copy over the original.
- A new file is built as a JSON Secret document and piped to
  `sops --encrypt --filename-override <path> --input-type json --output-type yaml /dev/stdin`, so
  the repo's `.sops.yaml` creation rule for that path applies. The result is written to a temp
  file and renamed.
- sops runs with the file's directory as its working directory, so it finds `.sops.yaml` by its
  normal upward search, and your usual key setup (for age, `SOPS_AGE_KEY_FILE`) must work there.
- Afterwards the file is decrypted again and the entry compared with the key in memory; a
  mismatch is an error. Nothing prints the key.

stdout gets the same JSON as `--to stdout` minus `private_key_file`, plus `sops_file` and
`sops_key`. If storing fails, the usual `./<slug>.private-key.pem` rescue file is written.

### Webhooks

The App never gets a webhook, so there's no flag for it. GitHub rejects a manifest whose
`hook_attributes` has no `url`, even with `active: false`; the manifest leaves the block out, and
GitHub then creates the App with its webhook off.

### Public Apps

A private App can only be installed on the account that owns it. To install one App on your
account and on organizations you belong to, pass `--public`, which sets the manifest's
`public: true`. Public means any GitHub account can install it, not only yours. Someone else's
install gives them nothing of yours, but it does mean an installation of your App can belong to
an account you don't know: anything that mints installation tokens with the App's private key
should check each installation's owner (`account.login` in `GET /app/installations`) before
using it.

After the first install the script prints
`https://github.com/apps/<slug>/installations/new`. Open it once per further account or
organization. An organization that requires approval for Apps sends its owners a request instead
of installing.

### User access tokens (`--user-token`)

Some consumers want a token that acts as you, limited to the App's permissions: a GitHub App
user access token (`ghu_...`). It reaches every repository that both you and an installation of
the App can reach, so one token covers your account and every organization the App is installed
on, including installations added later, without authorizing again.

```bash
plugins/forge-release/scripts/github-app-create --user --name acme-readonly --public \
  --permission metadata=read --permission contents=read --permission issues=read \
  --permission pull_requests=read --permission actions=read \
  --user-token --opt-out-expiry \
  --to sops --sops-file ~/infra/homelab/platform/mcp/github-token.sops.yaml --replace
```

`--user-token` needs `--to sops` (the token is never printed). After the install it:

1. Lists the local listener's `/callback` as the App's `callback_urls` in the manifest, so the
   authorization can come back to the same port. Use a fixed `--port` on a remote host, as below.
2. Sends you to `https://github.com/login/oauth/authorize` with the App's client ID, a fresh
   `state` and a PKCE challenge. You hit Authorize.
3. Checks the `state`, then trades the code at `https://github.com/login/oauth/access_token`
   with the client ID and the client secret from the manifest conversion.
4. Encrypts the token into the Secret file under `--sops-key` (default `token`).

The manifest's `request_oauth_on_install` isn't used: GitHub doesn't document whether the
`state` survives that redirect, and the separate authorization step checks it.

The App's private key isn't stored in this mode; it's only used in memory to watch for the
installation. Generate a new one in the App's settings if you ever want installation tokens. If
the installation times out, the script still asks for the authorization and stores the token
(it reaches installations made later), then exits 1.

**Expiry.** User access tokens expire after 8 hours by default, with a refresh token good for
6 months. The manifest has no field to turn that off; only the App's settings page does
(Optional features, "User-to-server token expiration", Opt-out), and only tokens issued after the
opt-out last. `--opt-out-expiry` pauses between the install and the authorization, prints the
settings URL, and waits for Enter, so the token it then gets doesn't expire. Without it the
script also stores `refresh_token`, `client_id` and `client_secret` in the same Secret, which is
what a refresh job needs (`POST https://github.com/login/oauth/access_token` with
`grant_type=refresh_token`; each refresh returns a new token and refresh token and voids the
old ones). Those three key names are reserved, so `--sops-key` can't use them.

An existing entry under any of those keys is refused unless you pass `--replace`. stdout gets
the usual `--to sops` JSON plus `install_url`, `sops_keys` (what was written),
`token_expires_in` and `refresh_token_expires_in` (both `null` for a token that doesn't
expire).

### Requirements

- `python3` 3.9 or newer; standard library only.
- `openssl` on `PATH`. The script signs the App JWT (RS256) with `openssl dgst -sha256 -sign`
  instead of depending on a crypto package.
- `gh`, logged in with admin access to each `--repo`, for `--to github`; `pulumi`, logged in to
  the stack's backend, for `--to pulumi`; `sops` (3.13 or newer, for `set --value-stdin`) with a
  key that can decrypt and encrypt the file, for `--to sops`.
- A browser that can reach the script's `127.0.0.1` port: a local one, or see the next section.

### Running on a remote host

The callback server listens on `127.0.0.1` only, so a browser on another machine can't reach it.
Pick a port, forward it from the machine with the browser, then run the script on the host:

```bash
# on the laptop
ssh -L 8765:127.0.0.1:8765 ml-01
# on ml-01, in that session
plugins/forge-release/scripts/github-app-create --org acme --name acme-release \
  --repo acme/site --port 8765 --no-browser
```

Open the printed `http://127.0.0.1:8765/` on the laptop. GitHub redirects the browser back to
that same address, which the tunnel carries to the script, both after creating the App and,
with `--user-token`, after authorizing it. `--no-browser` also prints the install
page URL instead of opening it, and a hint with the `ssh -L` command for the port in use. If the
port is taken, the script exits with a usage error naming it.

## Development

```bash
uv run --no-project --with pytest pytest plugins/forge-release/tests -q
claude plugin validate --strict plugins/forge-release
```

The tests mock every network and subprocess call except one `openssl` signing round trip, which
is skipped where `openssl` isn't installed.
