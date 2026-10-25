"""github-app-create: register a GitHub App through the manifest flow and store its credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Optional

from . import api, manifest, store
from .manifest import FlowError

DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
ACCESS = ("read", "write", "admin")
POLL_INTERVAL = 5.0
# Secret keys for what an expiring user token needs to be refreshed.
REFRESH_KEYS = ("refresh_token", "client_id", "client_secret")


@dataclass
class Deps:
    server_factory: Callable[[int], Any] = manifest.CallbackServer
    open_browser: Callable[[str], Any] = webbrowser.open
    http: api.Http = api.urllib_http
    run: Callable[..., Any] = subprocess.run
    sign: Callable[[bytes, str], bytes] = api.openssl_sign
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    new_state: Callable[[], str] = manifest.new_state
    pkce_pair: Callable[[], tuple[str, str]] = manifest.pkce_pair
    read_line: Callable[[], str] = field(default_factory=lambda: sys.stdin.readline)
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid port {text!r}: expected an integer 1-65535"
        ) from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {port}: expected 1-65535")
    return port


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="github-app-create",
        description="Create a GitHub App (private unless --public) through GitHub's App manifest "
        "flow (two clicks in the browser: create, then install) and store its client ID, private "
        "key and installation ID, or with --user-token a user access token. The App has no "
        "webhook. Keys and tokens are never printed.",
    )
    owner = p.add_mutually_exclusive_group(required=True)
    owner.add_argument("--org", help="organization that will own the App")
    owner.add_argument(
        "--user", action="store_true", help="create the App on your personal account"
    )
    p.add_argument(
        "--name", required=True, help="App name (unique across GitHub, at most 34 characters)"
    )
    p.add_argument(
        "--permission",
        action="append",
        default=[],
        metavar="NAME=ACCESS",
        help="repository or organization permission, repeatable "
        "(default: contents=write pull_requests=write)",
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="let any account install the App (a private App installs only on its owner); "
        "anything that uses its installation tokens should check each installation's owner",
    )
    p.add_argument(
        "--user-token",
        action="store_true",
        help="after the install, authorize the App as you and store the user access token "
        "(ghu_...) instead of the private key; needs --to sops. The token reaches every "
        "installation you can access",
    )
    p.add_argument(
        "--opt-out-expiry",
        action="store_true",
        help="with --user-token, pause before authorizing so you can turn off user token "
        "expiration in the App's settings (the manifest can't); otherwise the token expires in "
        "8 hours and the refresh token, client ID and client secret are stored too",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="repository the credentials are for, repeatable; required with --to github",
    )
    p.add_argument(
        "--homepage", help="App homepage URL (default: the org's, or the first repo's, GitHub page)"
    )
    p.add_argument(
        "--client-id-var",
        default="YEET_APP_ID",
        help="name for the stored client ID (default: %(default)s)",
    )
    p.add_argument(
        "--key-secret",
        default="YEET_APP_PRIVATE_KEY",
        help="name for the stored private key (default: %(default)s)",
    )
    p.add_argument(
        "--installation-id-var",
        default="YEET_APP_INSTALLATION_ID",
        help="name for the stored installation ID, pulumi mode only (default: %(default)s)",
    )
    p.add_argument(
        "--to",
        choices=("github", "pulumi", "stdout", "sops"),
        default="github",
        help="github: Actions variable and secret on each --repo via gh; pulumi: secret "
        "config via pulumi; stdout: JSON on stdout, key in --key-file; sops: key into a "
        "SOPS-encrypted Kubernetes Secret file, never written in plaintext (default: github)",
    )
    p.add_argument("--stack", help="Pulumi stack (required with --to pulumi)")
    p.add_argument(
        "--cwd", default=".", help="Pulumi project directory (default: current directory)"
    )
    p.add_argument(
        "--key-file",
        help="where --to stdout writes the key, mode 0600 "
        "(default: ./<slug>.private-key.pem, or "
        "./<slug>.private-key.<n>.pem if that exists)",
    )
    p.add_argument(
        "--sops-file",
        metavar="PATH",
        help="SOPS-encrypted Secret file for --to sops (required); edited in place if it "
        "exists, created if not",
    )
    p.add_argument(
        "--sops-key",
        metavar="NAME",
        help="key name inside the Secret's stringData (or data) (default: <slug>.pem, or "
        "token with --user-token)",
    )
    p.add_argument(
        "--secret-name", help="metadata.name of a new Secret file (needed if --sops-file is new)"
    )
    p.add_argument(
        "--namespace", help="metadata.namespace of a new Secret file (needed if --sops-file is new)"
    )
    p.add_argument(
        "--replace",
        action="store_true",
        help="with --to sops, overwrite existing entries under the same keys",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to wait for each browser step (default: %(default)g)",
    )
    p.add_argument(
        "--port",
        type=_port,
        default=0,
        metavar="N",
        help="listen for GitHub's redirect on 127.0.0.1:N instead of a random free port; "
        "on a remote host, forward it first: ssh -L N:127.0.0.1:N <host>",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="don't open a browser; print the URLs to open yourself",
    )
    return p


def _permissions(parser: argparse.ArgumentParser, raw: Sequence[str]) -> dict[str, str]:
    if not raw:
        return dict(DEFAULT_PERMISSIONS)
    perms: dict[str, str] = {}
    for item in raw:
        name, sep, access = item.partition("=")
        if not sep or not re.fullmatch(r"[a-z_]+", name) or access not in ACCESS:
            parser.error(
                f"--permission {item}: expected NAME=ACCESS with ACCESS one of {', '.join(ACCESS)}"
            )
        perms[name] = access
    return perms


def _args(argv: Sequence[str]) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    args.permissions = _permissions(parser, args.permission)
    for repo in args.repo:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            parser.error(f"--repo {repo}: expected owner/repo")
    if args.to == "github" and not args.repo:
        parser.error("--to github needs at least one --repo")
    if args.to == "pulumi" and not args.stack:
        parser.error("--to pulumi needs --stack")
    if args.to == "sops":
        if not args.sops_file:
            parser.error("--to sops needs --sops-file")
        if not os.path.exists(args.sops_file) and not (args.secret_name and args.namespace):
            parser.error(
                f"--sops-file {args.sops_file} doesn't exist; creating it needs "
                "--secret-name and --namespace"
            )
    if args.user_token:
        if args.to != "sops":
            parser.error("--user-token needs --to sops (the token is never printed)")
        if args.sops_key in REFRESH_KEYS:
            parser.error(
                f"--sops-key {args.sops_key}: that name is kept for the refresh credentials"
            )
        args.sops_key = args.sops_key or "token"
    elif args.opt_out_expiry:
        parser.error("--opt-out-expiry only applies with --user-token")
    if args.to == "stdout" and args.key_file and os.path.exists(args.key_file):
        parser.error(f"--key-file {args.key_file} already exists; not overwriting it")
    if not args.homepage:
        args.homepage = (
            "https://github.com/" + args.org
            if args.org
            else "https://github.com/" + args.repo[0]
            if args.repo
            else "https://github.com"
        )
    return args


def _say(deps: Deps, text: str) -> None:
    print(text, file=deps.stderr, flush=True)


def _port_of(server: Any) -> int:
    return int(server.start_url.rsplit(":", 1)[1].rstrip("/"))


def _create(args: argparse.Namespace, deps: Deps, server: Any) -> api.AppCredentials:
    state = deps.new_state()
    app_manifest = manifest.build_manifest(
        args.name,
        args.homepage,
        args.permissions,
        server.redirect_url,
        public=args.public,
        # The user authorization comes back to this same listener, so the App must list it.
        callback_urls=[server.redirect_url] if args.user_token else None,
    )
    server.set_page(manifest.form_page(manifest.form_action(args.org, state), app_manifest))
    if args.no_browser:
        _say(deps, f"Open {server.start_url} in your browser.")
        _say(
            deps,
            f"On another machine? Forward the port first: "
            f"ssh -L {_port_of(server)}:127.0.0.1:{_port_of(server)} {socket.gethostname()}",
        )
    else:
        _say(
            deps,
            f"Opening {server.start_url} in your browser; open it yourself if nothing appears.",
        )
    _say(deps, "On GitHub, check the name and hit Create GitHub App.")
    if not args.no_browser:
        deps.open_browser(server.start_url)
    code = manifest.check_callback(server.wait(args.timeout), state)
    app = api.convert_manifest(code, deps.http)
    _say(deps, f"Created {app.html_url} (App ID {app.app_id}, client ID {app.client_id}).")
    if args.public:
        _say(
            deps,
            "The App is public: any account can install it. Anything that uses its "
            "installation tokens should check which account each installation belongs to.",
        )
    return app


def _settings_url(args: argparse.Namespace, app: api.AppCredentials) -> str:
    if args.org:
        return f"https://github.com/organizations/{args.org}/settings/apps/{app.slug}"
    return f"https://github.com/settings/apps/{app.slug}"


def _authorize(
    args: argparse.Namespace, deps: Deps, server: Any, app: api.AppCredentials
) -> api.UserToken:
    """GitHub's web application flow, back to the listener the manifest named as callback URL."""
    if not app.client_id or not app.client_secret:
        raise FlowError("the manifest conversion returned no client ID or client secret")
    if args.opt_out_expiry:
        _say(
            deps,
            f"Turn off token expiration now: open {_settings_url(args, app)}, go to Optional "
            'features, and next to "User-to-server token expiration" click Opt-out. '
            "Then press Enter here.",
        )
        deps.read_line()
    state = deps.new_state()
    verifier, challenge = deps.pkce_pair()
    url = manifest.authorize_url(app.client_id, server.redirect_url, state, challenge)
    if args.no_browser:
        _say(deps, f"Open {url} and hit Authorize.")
    else:
        _say(deps, f"Opening {url}; hit Authorize.")
        deps.open_browser(url)
    code = manifest.check_callback(server.wait(args.timeout), state)
    return api.exchange_user_code(
        app.client_id, app.client_secret, code, server.redirect_url, verifier, deps.http
    )


def _store_user_token(
    args: argparse.Namespace, deps: Deps, app: api.AppCredentials, token: api.UserToken
) -> list[str]:
    values = {args.sops_key: token.access_token}
    if token.refresh_token:
        values.update(
            refresh_token=token.refresh_token,
            client_id=app.client_id or "",
            client_secret=app.client_secret or "",
        )
    store.sops_store_values(
        args.sops_file,
        values,
        replace=args.replace,
        secret_name=args.secret_name,
        namespace=args.namespace,
        run=deps.run,
    )
    _say(deps, f"Encrypted {', '.join(values)} into {args.sops_file}.")
    if token.refresh_token:
        hours = (token.expires_in or 0) / 3600
        _say(
            deps,
            f"The token expires in {hours:g} hours. Refresh it before then with the stored "
            "refresh token, client ID and client secret (the refresh token lasts about 6 months, "
            "and each refresh returns a new pair). Only a token issued after opting out of "
            "expiration lasts; --opt-out-expiry pauses for that before authorizing.",
        )
    return list(values)


def _store_credentials(args: argparse.Namespace, deps: Deps, app: api.AppCredentials) -> None:
    client_id = app.client_id or str(app.app_id)
    if args.to == "github":
        store.run_commands(
            store.github_commands(
                client_id, app.pem, args.repo, args.client_id_var, args.key_secret
            ),
            deps.run,
        )
        _say(
            deps,
            f"Set variable {args.client_id_var} and secret {args.key_secret} on "
            + ", ".join(args.repo)
            + ".",
        )
    elif args.to == "pulumi":
        store.run_commands(
            store.pulumi_commands(
                [
                    (store.pulumi_key(args.client_id_var), client_id),
                    (store.pulumi_key(args.key_secret), app.pem),
                ],
                args.stack,
                args.cwd,
            ),
            deps.run,
        )
        _say(
            deps,
            f"Set Pulumi secrets {store.pulumi_key(args.client_id_var)} and "
            f"{store.pulumi_key(args.key_secret)} on stack {args.stack}.",
        )
    elif args.to == "sops":
        store.sops_store_key(
            args.sops_file,
            args.sops_key,
            app.pem,
            replace=args.replace,
            secret_name=args.secret_name,
            namespace=args.namespace,
            run=deps.run,
        )
        _say(deps, f"Encrypted the private key into {args.sops_file} as {args.sops_key}.")
    elif args.key_file:
        store.write_key_file(args.key_file, app.pem)
        _say(deps, f"Wrote the private key to {args.key_file} (mode 0600).")
    else:
        args.key_file = _write_fresh_key_file(app)
        _say(deps, f"Wrote the private key to {args.key_file} (mode 0600).")


def _write_fresh_key_file(app: api.AppCredentials) -> str:
    """Write the key to ./<slug>.private-key.pem, or the first free ./<slug>.private-key.<n>.pem.

    The App already exists on GitHub by now, so a file left over from an earlier run must not
    cost this run its only copy of the key."""
    for n in range(1000):
        path = f"{app.slug}.private-key.pem" if n == 0 else f"{app.slug}.private-key.{n}.pem"
        try:
            store.write_key_file(path, app.pem)
        except FlowError:
            continue
        return path
    raise FlowError(f"no free {app.slug}.private-key.<n>.pem name in the current directory")


def _rescue_key(deps: Deps, app: api.AppCredentials) -> None:
    try:
        path = _write_fresh_key_file(app)
    except (FlowError, OSError) as error:
        _say(
            deps,
            f"Couldn't save the private key either ({error}); generate a new one at "
            f"{app.html_url} under the App's settings.",
        )
        return
    _say(
        deps,
        f"Saved the private key to {path} (mode 0600) so it isn't lost; store it by hand, "
        "then delete the file.",
    )


def _install(args: argparse.Namespace, deps: Deps, app: api.AppCredentials) -> int:
    url = f"https://github.com/apps/{app.slug}/installations/new"
    if args.no_browser:
        _say(deps, f"Open {url}; pick the repositories and hit Install.")
    else:
        _say(deps, f"Opening {url}; pick the repositories and hit Install.")
        deps.open_browser(url)
    return api.wait_for_installation(
        lambda: api.make_jwt(app.jwt_issuer, app.pem, sign=deps.sign),
        deps.http,
        timeout=args.timeout,
        interval=POLL_INTERVAL,
        clock=deps.clock,
        sleep=deps.sleep,
    )


def main(argv: Sequence[str], deps: Optional[Deps] = None) -> int:
    deps = deps or Deps()
    args = _args(argv)
    try:
        server = deps.server_factory(args.port)
    except manifest.PortInUseError as error:
        _parser().error(str(error))
    try:
        return _run(args, deps, server)
    finally:
        server.close()


def _run(args: argparse.Namespace, deps: Deps, server: Any) -> int:
    try:
        app = _create(args, deps, server)
    except FlowError as error:
        _say(deps, f"error: {error}")
        return 1
    if args.to == "sops" and not args.sops_key:
        args.sops_key = f"{app.slug}.pem"
    if args.user_token:
        _say(
            deps,
            "The private key isn't stored with --user-token; generate one in the App's settings "
            "if you ever need installation tokens.",
        )
    else:
        try:
            _store_credentials(args, deps, app)
        except (FlowError, OSError) as error:
            _say(deps, f"error: storing the credentials failed: {error}")
            _rescue_key(deps, app)
            return 1

    install_url = f"https://github.com/apps/{app.slug}/installations/new"
    installation_id: Optional[int] = None
    status = 0
    try:
        installation_id = _install(args, deps, app)
        _say(deps, f"Installed: installation ID {installation_id}.")
    except FlowError as error:
        stored = "" if args.user_token else " The credentials are stored;"
        _say(deps, f"error: {error}.{stored} install the App at {install_url}.")
        status = 1
    if args.public:
        _say(
            deps,
            f"To install it on another account or organization, open {install_url} and pick "
            "it; an organization that requires approval sends its owners a request instead."
            + (
                " The user token reaches each new installation without authorizing again."
                if args.user_token
                else ""
            ),
        )
    if installation_id is not None and args.to == "pulumi":
        key = store.pulumi_key(args.installation_id_var)
        try:
            store.run_commands(
                store.pulumi_commands([(key, str(installation_id))], args.stack, args.cwd), deps.run
            )
            _say(deps, f"Set Pulumi secret {key} on stack {args.stack}.")
        except FlowError as error:
            _say(deps, f"error: storing installation ID {installation_id} failed: {error}")
            status = 1

    stored_keys: list[str] = []
    token: Optional[api.UserToken] = None
    if args.user_token:
        try:
            token = _authorize(args, deps, server, app)
            stored_keys = _store_user_token(args, deps, app, token)
        except (FlowError, OSError) as error:
            _say(
                deps,
                f"error: getting or storing the user token failed: {error}. The App exists at "
                f"{_settings_url(args, app)}; nothing secret was kept.",
            )
            status = 1

    if args.to == "stdout":
        print(
            json.dumps(
                {
                    "app_id": app.app_id,
                    "slug": app.slug,
                    "client_id": app.client_id,
                    "installation_id": installation_id,
                    "private_key_file": args.key_file,
                    "html_url": app.html_url,
                },
                indent=2,
            ),
            file=deps.stdout,
        )
    elif args.to == "sops":
        result: dict[str, Any] = {
            "app_id": app.app_id,
            "slug": app.slug,
            "client_id": app.client_id,
            "installation_id": installation_id,
            "html_url": app.html_url,
            "sops_file": args.sops_file,
            "sops_key": args.sops_key,
        }
        if args.user_token:
            result.update(
                install_url=install_url,
                sops_keys=stored_keys,
                token_expires_in=token.expires_in if token else None,
                refresh_token_expires_in=token.refresh_token_expires_in if token else None,
            )
        print(json.dumps(result, indent=2), file=deps.stdout)
    elif installation_id is not None:
        print(
            f"{app.slug}: App ID {app.app_id}, client ID {app.client_id}, "
            f"installation ID {installation_id}",
            file=deps.stdout,
        )
    return status
