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
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Dict, Optional, Sequence

from . import api, manifest, store
from .manifest import FlowError

DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
ACCESS = ("read", "write", "admin")
POLL_INTERVAL = 5.0


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
    stdout: IO[str] = field(default_factory=lambda: sys.stdout)
    stderr: IO[str] = field(default_factory=lambda: sys.stderr)


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: expected an integer 1-65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {port}: expected 1-65535")
    return port


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="github-app-create",
        description="Create a private GitHub App through GitHub's App manifest flow (two clicks in "
                    "the browser: create, then install) and store its client ID, private key and "
                    "installation ID. The private key is never printed.")
    owner = p.add_mutually_exclusive_group(required=True)
    owner.add_argument("--org", help="organization that will own the App")
    owner.add_argument("--user", action="store_true", help="create the App on your personal account")
    p.add_argument("--name", required=True, help="App name (unique across GitHub, at most 34 characters)")
    p.add_argument("--permission", action="append", default=[], metavar="NAME=ACCESS",
                   help="repository or organization permission, repeatable "
                        "(default: contents=write pull_requests=write)")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/REPO",
                   help="repository the credentials are for, repeatable; required with --to github")
    p.add_argument("--homepage",
                   help="App homepage URL (default: the org's, or the first repo's, GitHub page)")
    p.add_argument("--client-id-var", default="YEET_APP_ID",
                   help="name for the stored client ID (default: %(default)s)")
    p.add_argument("--key-secret", default="YEET_APP_PRIVATE_KEY",
                   help="name for the stored private key (default: %(default)s)")
    p.add_argument("--installation-id-var", default="YEET_APP_INSTALLATION_ID",
                   help="name for the stored installation ID, pulumi mode only (default: %(default)s)")
    p.add_argument("--to", choices=("github", "pulumi", "stdout", "sops"), default="github",
                   help="github: Actions variable and secret on each --repo via gh; pulumi: secret "
                        "config via pulumi; stdout: JSON on stdout, key in --key-file; sops: key into a "
                        "SOPS-encrypted Kubernetes Secret file, never written in plaintext (default: github)")
    p.add_argument("--stack", help="Pulumi stack (required with --to pulumi)")
    p.add_argument("--cwd", default=".", help="Pulumi project directory (default: current directory)")
    p.add_argument("--key-file", help="where --to stdout writes the key, mode 0600 "
                                      "(default: ./<slug>.private-key.pem, or "
                                      "./<slug>.private-key.<n>.pem if that exists)")
    p.add_argument("--sops-file", metavar="PATH",
                   help="SOPS-encrypted Secret file for --to sops (required); edited in place if it "
                        "exists, created if not")
    p.add_argument("--sops-key", metavar="NAME",
                   help="key name inside the Secret's stringData (or data) (default: <slug>.pem)")
    p.add_argument("--secret-name", help="metadata.name of a new Secret file (needed if --sops-file is new)")
    p.add_argument("--namespace", help="metadata.namespace of a new Secret file (needed if --sops-file is new)")
    p.add_argument("--replace", action="store_true",
                   help="with --to sops, overwrite an existing entry under the same key")
    p.add_argument("--timeout", type=float, default=600,
                   help="seconds to wait for each browser step (default: %(default)g)")
    p.add_argument("--port", type=_port, default=0, metavar="N",
                   help="listen for GitHub's redirect on 127.0.0.1:N instead of a random free port; "
                        "on a remote host, forward it first: ssh -L N:127.0.0.1:N <host>")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open a browser; print the URLs to open yourself")
    return p


def _permissions(parser: argparse.ArgumentParser, raw: Sequence[str]) -> Dict[str, str]:
    if not raw:
        return dict(DEFAULT_PERMISSIONS)
    perms: Dict[str, str] = {}
    for item in raw:
        name, sep, access = item.partition("=")
        if not sep or not re.fullmatch(r"[a-z_]+", name) or access not in ACCESS:
            parser.error(f"--permission {item}: expected NAME=ACCESS with ACCESS one of {', '.join(ACCESS)}")
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
            parser.error(f"--sops-file {args.sops_file} doesn't exist; creating it needs "
                         "--secret-name and --namespace")
    if args.to == "stdout" and args.key_file and os.path.exists(args.key_file):
        parser.error(f"--key-file {args.key_file} already exists; not overwriting it")
    if not args.homepage:
        args.homepage = ("https://github.com/" + args.org if args.org
                         else "https://github.com/" + args.repo[0] if args.repo else "https://github.com")
    return args


def _say(deps: Deps, text: str) -> None:
    print(text, file=deps.stderr, flush=True)


def _port_of(server: Any) -> int:
    return int(server.start_url.rsplit(":", 1)[1].rstrip("/"))


def _create(args: argparse.Namespace, deps: Deps) -> api.AppCredentials:
    state = deps.new_state()
    server = deps.server_factory(args.port)
    try:
        app_manifest = manifest.build_manifest(args.name, args.homepage, args.permissions,
                                               server.redirect_url)
        server.set_page(manifest.form_page(manifest.form_action(args.org, state), app_manifest))
        if args.no_browser:
            _say(deps, f"Open {server.start_url} in your browser.")
            _say(deps, f"On another machine? Forward the port first: "
                       f"ssh -L {_port_of(server)}:127.0.0.1:{_port_of(server)} {socket.gethostname()}")
        else:
            _say(deps, f"Opening {server.start_url} in your browser; open it yourself if nothing appears.")
        _say(deps, "On GitHub, check the name and hit Create GitHub App.")
        if not args.no_browser:
            deps.open_browser(server.start_url)
        code = manifest.check_callback(server.wait(args.timeout), state)
    finally:
        server.close()
    app = api.convert_manifest(code, deps.http)
    _say(deps, f"Created {app.html_url} (App ID {app.app_id}, client ID {app.client_id}).")
    return app


def _store_credentials(args: argparse.Namespace, deps: Deps, app: api.AppCredentials) -> None:
    client_id = app.client_id or str(app.app_id)
    if args.to == "github":
        store.run_commands(store.github_commands(client_id, app.pem, args.repo, args.client_id_var,
                                                 args.key_secret), deps.run)
        _say(deps, f"Set variable {args.client_id_var} and secret {args.key_secret} on "
                   + ", ".join(args.repo) + ".")
    elif args.to == "pulumi":
        store.run_commands(store.pulumi_commands(
            [(store.pulumi_key(args.client_id_var), client_id),
             (store.pulumi_key(args.key_secret), app.pem)], args.stack, args.cwd), deps.run)
        _say(deps, f"Set Pulumi secrets {store.pulumi_key(args.client_id_var)} and "
                   f"{store.pulumi_key(args.key_secret)} on stack {args.stack}.")
    elif args.to == "sops":
        store.sops_store_key(args.sops_file, args.sops_key, app.pem, replace=args.replace,
                             secret_name=args.secret_name, namespace=args.namespace, run=deps.run)
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
        _say(deps, f"Couldn't save the private key either ({error}); generate a new one at "
                   f"{app.html_url} under the App's settings.")
        return
    _say(deps, f"Saved the private key to {path} (mode 0600) so it isn't lost; store it by hand, "
               "then delete the file.")


def _install(args: argparse.Namespace, deps: Deps, app: api.AppCredentials) -> int:
    url = f"https://github.com/apps/{app.slug}/installations/new"
    if args.no_browser:
        _say(deps, f"Open {url}; pick the repositories and hit Install.")
    else:
        _say(deps, f"Opening {url}; pick the repositories and hit Install.")
        deps.open_browser(url)
    return api.wait_for_installation(
        lambda: api.make_jwt(app.jwt_issuer, app.pem, sign=deps.sign), deps.http,
        timeout=args.timeout, interval=POLL_INTERVAL, clock=deps.clock, sleep=deps.sleep)


def main(argv: Sequence[str], deps: Optional[Deps] = None) -> int:
    deps = deps or Deps()
    args = _args(argv)
    try:
        app = _create(args, deps)
    except manifest.PortInUseError as error:
        _parser().error(str(error))
    except FlowError as error:
        _say(deps, f"error: {error}")
        return 1
    if args.to == "sops" and not args.sops_key:
        args.sops_key = f"{app.slug}.pem"
    try:
        _store_credentials(args, deps, app)
    except (FlowError, OSError) as error:
        _say(deps, f"error: storing the credentials failed: {error}")
        _rescue_key(deps, app)
        return 1

    installation_id: Optional[int] = None
    status = 0
    try:
        installation_id = _install(args, deps, app)
        _say(deps, f"Installed: installation ID {installation_id}.")
    except FlowError as error:
        _say(deps, f"error: {error}. The credentials are stored; install the App at "
                   f"https://github.com/apps/{app.slug}/installations/new.")
        status = 1
    if installation_id is not None and args.to == "pulumi":
        key = store.pulumi_key(args.installation_id_var)
        try:
            store.run_commands(store.pulumi_commands([(key, str(installation_id))], args.stack,
                                                     args.cwd), deps.run)
            _say(deps, f"Set Pulumi secret {key} on stack {args.stack}.")
        except FlowError as error:
            _say(deps, f"error: storing installation ID {installation_id} failed: {error}")
            status = 1

    if args.to == "stdout":
        print(json.dumps({"app_id": app.app_id, "slug": app.slug, "client_id": app.client_id,
                          "installation_id": installation_id, "private_key_file": args.key_file,
                          "html_url": app.html_url}, indent=2), file=deps.stdout)
    elif args.to == "sops":
        print(json.dumps({"app_id": app.app_id, "slug": app.slug, "client_id": app.client_id,
                          "installation_id": installation_id, "html_url": app.html_url,
                          "sops_file": args.sops_file, "sops_key": args.sops_key}, indent=2),
              file=deps.stdout)
    elif installation_id is not None:
        print(f"{app.slug}: App ID {app.app_id}, client ID {app.client_id}, "
              f"installation ID {installation_id}", file=deps.stdout)
    return status
