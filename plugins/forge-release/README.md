# forge-release

Release-PR setup across forges: Conventional Commits feed one standing release PR or MR, and
merging it tags and publishes. The setup skill comes later; this release has one helper script.

## github-app-create

Release tools like yeet and release-please push tags and open PRs with a GitHub App token, so a
`release: published` workflow can run off them (the default `GITHUB_TOKEN` can't trigger other
workflows). `scripts/github-app-create` registers that App through GitHub's
[App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
and stores its credentials. You click twice in the browser: Create GitHub App, then Install.

```bash
plugins/forge-release/scripts/github-app-create --org acme --name acme-release \
  --repo acme/site --repo acme/api
```

What it does:

1. Starts a server on `127.0.0.1` on a free port and opens a page that posts the manifest to
   GitHub (the org's `settings/apps/new`, or your account's with `--user`). The manifest asks for
   a private App with webhooks off and the permissions you give; the default is
   `contents=write` and `pull_requests=write`, which is what yeet needs. If no browser opens, the
   script prints the local URL.
2. GitHub redirects back with a one-time code. The script checks the `state` it sent, then
   trades the code for the App's ID, client ID and private key (no auth needed for that call).
3. Stores the client ID and key (see below).
4. Opens the App's install page and polls `GET /app/installations` with an App JWT every five
   seconds until the installation shows up, then prints its ID (and stores it in Pulumi mode).

The private key is never printed. If storing it fails, the script writes it to
`./<slug>.private-key.pem` (mode 0600) so it isn't lost.

### Where the credentials go

| `--to` | Client ID | Private key | Installation ID |
|---|---|---|---|
| `github` (default) | `gh variable set YEET_APP_ID -R <repo>` per `--repo` | `gh secret set YEET_APP_PRIVATE_KEY -R <repo>`, value on stdin | printed |
| `pulumi --stack <stack> [--cwd <dir>]` | `pulumi config set --secret yeetAppId` | `pulumi config set --secret yeetAppPrivateKey`, value on stdin | `yeetAppInstallationId` |
| `stdout [--key-file <path>]` | JSON on stdout | file, mode 0600 (default `./<slug>.private-key.pem`) | JSON on stdout |

Rename the stored values with `--client-id-var`, `--key-secret` and `--installation-id-var`.
Pulumi keys are the camelCase form of those names (`YEET_APP_ID` becomes `yeetAppId`); a name
with a namespace, like `infra:releaseAppId`, is used as is. `--homepage` sets the App's homepage
URL (default: the org's GitHub page), and `--timeout` (default 600 seconds) bounds each browser
step. `--help` lists everything.

The value stored as `YEET_APP_ID` is the client ID, not the numeric App ID: GitHub accepts
either to identify the App when signing a JWT and recommends the client ID. The numeric ID is
printed (and is in the `--to stdout` JSON) if a tool insists on it.

### Requirements

- `python3` 3.9 or newer; standard library only.
- `openssl` on `PATH`. The script signs the App JWT (RS256) with `openssl dgst -sha256 -sign`
  instead of depending on a crypto package.
- `gh`, logged in with admin access to each `--repo`, for `--to github`; `pulumi`, logged in to
  the stack's backend, for `--to pulumi`.
- A local browser. On a headless host, open the printed URLs on a machine that can reach the
  host's `127.0.0.1` port (an SSH tunnel will do).

## Development

```bash
uv run --no-project --with pytest pytest plugins/forge-release/tests -q
claude plugin validate --strict plugins/forge-release
```

The tests mock every network and subprocess call except one `openssl` signing round trip, which
is skipped where `openssl` isn't installed.
