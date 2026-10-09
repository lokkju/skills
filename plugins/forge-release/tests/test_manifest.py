import html
import json
import re
import threading
import urllib.error
import urllib.request

import pytest
from forge_release import manifest

PERMS = {"contents": "write", "pull_requests": "write"}


def test_manifest_fields():
    m = manifest.build_manifest(
        "acme-release", "https://example.com/acme", PERMS, "http://127.0.0.1:5000/callback"
    )
    assert m == {
        "name": "acme-release",
        "url": "https://example.com/acme",
        "redirect_url": "http://127.0.0.1:5000/callback",
        "public": False,
        "default_permissions": PERMS,
    }


def test_manifest_has_no_webhook_block():
    # GitHub rejects a manifest whose hook_attributes lack a url ('"url" wasn't supplied'),
    # even with active: false. Leaving the block out creates the App with no webhook.
    m = manifest.build_manifest(
        "acme-release", "https://example.com/acme", PERMS, "http://127.0.0.1:5000/callback"
    )
    assert "hook_attributes" not in m


def test_form_action_org():
    assert (
        manifest.form_action("acme", "s3cr3t")
        == "https://github.com/organizations/acme/settings/apps/new?state=s3cr3t"
    )


def test_form_action_user():
    assert (
        manifest.form_action(None, "s3cr3t") == "https://github.com/settings/apps/new?state=s3cr3t"
    )


def test_form_action_quotes_org():
    assert (
        manifest.form_action("a b", "x")
        == "https://github.com/organizations/a%20b/settings/apps/new?state=x"
    )


def test_form_page_posts_manifest_and_autosubmits():
    m = manifest.build_manifest('n"<x>', "https://e.x", PERMS, "http://127.0.0.1:1/callback")
    page = manifest.form_page(manifest.form_action("acme", "st"), m)
    form = re.search(r'<form[^>]*method="post"[^>]*action="([^"]+)"', page)
    assert (
        form
        and html.unescape(form.group(1))
        == "https://github.com/organizations/acme/settings/apps/new?state=st"
    )
    value = re.search(r'<input type="hidden" name="manifest" value="([^"]*)"', page).group(1)
    assert json.loads(html.unescape(value)) == m
    assert ".submit()" in page


def test_check_callback_returns_code():
    assert manifest.check_callback({"code": ["abc"], "state": ["st"]}, "st") == "abc"


def test_check_callback_rejects_state_mismatch():
    with pytest.raises(manifest.FlowError, match="state"):
        manifest.check_callback({"code": ["abc"], "state": ["evil"]}, "st")


def test_check_callback_rejects_missing_code():
    with pytest.raises(manifest.FlowError, match="code"):
        manifest.check_callback({"state": ["st"]}, "st")


def test_new_state_is_unguessable():
    a, b = manifest.new_state(), manifest.new_state()
    assert a != b and len(a) >= 32


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_local_server_serves_form_then_returns_code():
    server = manifest.CallbackServer()
    try:
        m = manifest.build_manifest("n", "https://e.x", PERMS, server.redirect_url)
        server.set_page(manifest.form_page(manifest.form_action("acme", "st"), m))
        assert server.start_url.startswith("http://127.0.0.1:")
        status, body = _get(server.start_url)
        assert status == 200 and "manifest" in body
        t = threading.Thread(target=_get, args=(server.redirect_url + "?code=abc&state=st",))
        t.start()
        query = server.wait(timeout=5)
        t.join()
        assert manifest.check_callback(query, "st") == "abc"
    finally:
        server.close()


def test_local_server_wait_times_out():
    server = manifest.CallbackServer()
    try:
        with pytest.raises(manifest.FlowError, match="timed out"):
            server.wait(timeout=0.1)
    finally:
        server.close()


def test_local_server_binds_the_requested_port_on_loopback_only():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = manifest.CallbackServer(port)
    try:
        assert server.start_url == f"http://127.0.0.1:{port}/"
        assert server.redirect_url == f"http://127.0.0.1:{port}/callback"
        assert server._server.server_address == ("127.0.0.1", port)
    finally:
        server.close()


def test_local_server_busy_port_raises_flow_error_naming_it():
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(manifest.PortInUseError) as e:
            manifest.CallbackServer(port)
    assert str(port) in str(e.value) and isinstance(e.value, manifest.FlowError)
