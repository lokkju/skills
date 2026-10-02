"""The GitHub API half: manifest conversion, App JWTs, and waiting for the installation."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Tuple

from .manifest import FlowError

API = "https://api.github.com"
USER_AGENT = "forge-release-github-app-create"

# (method, url, headers, body) -> (status, parsed JSON body)
Http = Callable[[str, str, Mapping[str, str], Optional[bytes]], Tuple[int, Any]]


def urllib_http(method: str, url: str, headers: Mapping[str, str],
                body: Optional[bytes] = None) -> Tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except urllib.error.URLError as error:
        raise FlowError(f"{method} {url} failed: {error.reason}") from None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _headers(token: Optional[str] = None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _check(status: int, body: Any, what: str) -> None:
    if not 200 <= status < 300:
        message = body.get("message") if isinstance(body, dict) else None
        raise FlowError(f"{what} failed: HTTP {status}" + (f" ({message})" if message else ""))


@dataclass(frozen=True)
class AppCredentials:
    app_id: int
    slug: str
    client_id: Optional[str]
    html_url: str
    pem: str = field(repr=False)

    @property
    def jwt_issuer(self) -> str:
        # GitHub accepts the client ID or the App ID as iss and recommends the client ID.
        return self.client_id or str(self.app_id)


def convert_manifest(code: str, http: Http) -> AppCredentials:
    status, body = http("POST", f"{API}/app-manifests/{code}/conversions", _headers(), None)
    _check(status, body, "manifest conversion")
    if not isinstance(body, dict) or not body.get("pem") or not body.get("slug"):
        raise FlowError("manifest conversion returned an unexpected response")
    return AppCredentials(app_id=int(body["id"]), slug=str(body["slug"]),
                          client_id=body.get("client_id"),
                          html_url=str(body.get("html_url") or f"https://github.com/apps/{body['slug']}"),
                          pem=str(body["pem"]))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def openssl_sign(data: bytes, pem: str, run: Callable[..., Any] = subprocess.run) -> bytes:
    """RS256: an RSA PKCS#1 v1.5 signature over SHA-256, made by `openssl dgst`."""
    fd, path = tempfile.mkstemp(prefix="github-app-", suffix=".pem")  # mode 0600
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(pem)
        try:
            result = run(["openssl", "dgst", "-sha256", "-sign", path], input=data, capture_output=True)
        except FileNotFoundError:
            raise FlowError("openssl isn't on PATH; it's needed to sign the App JWT") from None
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise FlowError(f"openssl couldn't sign the App JWT (exit {result.returncode})")
    return result.stdout


def make_jwt(issuer: str, pem: str, now: Optional[float] = None,
             sign: Callable[[bytes, str], bytes] = openssl_sign) -> str:
    now = int(time.time() if now is None else now)
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": issuer},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}"
    return f"{signing_input}.{_b64(sign(signing_input.encode(), pem))}"


def wait_for_installation(token: Callable[[], str], http: Http, timeout: float = 600,
                          interval: float = 5, clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> int:
    deadline = clock() + timeout
    while True:
        status, body = http("GET", f"{API}/app/installations", _headers(token()), None)
        _check(status, body, "listing the App's installations")
        if isinstance(body, list) and body:
            return int(body[0]["id"])
        if clock() + interval > deadline:
            raise FlowError(f"timed out after {timeout:g}s waiting for the App to be installed")
        sleep(interval)
