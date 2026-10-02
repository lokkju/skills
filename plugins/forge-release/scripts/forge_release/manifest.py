"""The browser half of GitHub's App manifest flow: the manifest, the form, the local callback."""

from __future__ import annotations

import html
import json
import queue
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Mapping, Optional

CALLBACK_PATH = "/callback"


class FlowError(Exception):
    """The manifest flow can't continue; the message says why."""


def build_manifest(name: str, homepage: str, permissions: Mapping[str, str],
                   redirect_url: str) -> dict:
    return {
        "name": name,
        "url": homepage,
        "hook_attributes": {"active": False},
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": dict(permissions),
    }


def form_action(org: Optional[str], state: str) -> str:
    query = urllib.parse.urlencode({"state": state})
    if org is None:
        return f"https://github.com/settings/apps/new?{query}"
    return f"https://github.com/organizations/{urllib.parse.quote(org, safe='')}/settings/apps/new?{query}"


def form_page(action: str, manifest: dict) -> str:
    value = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>Create GitHub App</title></head>\n"
        "<body>\n"
        f"<form id=\"manifest\" method=\"post\" action=\"{html.escape(action, quote=True)}\">\n"
        f"<input type=\"hidden\" name=\"manifest\" value=\"{value}\">\n"
        "<p>Sending the App manifest to GitHub...</p>\n"
        "<noscript><button type=\"submit\">Continue to GitHub</button></noscript>\n"
        "</form>\n"
        "<script>document.getElementById(\"manifest\").submit();</script>\n"
        "</body></html>\n"
    )


def new_state() -> str:
    return secrets.token_urlsafe(32)


def check_callback(query: Mapping[str, List[str]], expected_state: str) -> str:
    state = (query.get("state") or [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise FlowError("the callback's state doesn't match this run; refusing the code")
    code = (query.get("code") or [""])[0]
    if not code:
        raise FlowError("the callback has no code")
    return code


class CallbackServer:
    """A one-shot HTTP server on 127.0.0.1: serves the form at /, takes the redirect at /callback."""

    def __init__(self) -> None:
        self._page = ""
        self._queries: "queue.Queue[Dict[str, List[str]]]" = queue.Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/":
                    self._reply(200, owner._page)
                elif parsed.path == CALLBACK_PATH:
                    owner._queries.put(urllib.parse.parse_qs(parsed.query))
                    self._reply(200, "<!doctype html><p>GitHub App created. "
                                     "You can close this tab and go back to the terminal.</p>")
                else:
                    self._reply(404, "not found")

            def _reply(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def start_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/"

    @property
    def redirect_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{CALLBACK_PATH}"

    def set_page(self, page: str) -> None:
        self._page = page

    def wait(self, timeout: float) -> Dict[str, List[str]]:
        try:
            return self._queries.get(timeout=timeout)
        except queue.Empty:
            raise FlowError(f"timed out after {timeout:g}s waiting for GitHub's redirect") from None

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
