import io
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from forge_release import cli

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfakeSECRET\n-----END RSA PRIVATE KEY-----\n"
CONVERSION = {"id": 42, "slug": "acme-release", "client_id": "Iv23abc", "pem": PEM,
              "html_url": "https://github.com/apps/acme-release"}
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "github-app-create"


class FakeServer:
    def __init__(self, state="st"):
        self.state = state
        self.page = None
        self.closed = False
        self.start_url = "http://127.0.0.1:9999/"
        self.redirect_url = "http://127.0.0.1:9999/callback"

    def set_page(self, page):
        self.page = page

    def wait(self, timeout):
        return {"code": ["c0de"], "state": [self.state]}

    def close(self):
        self.closed = True


class World:
    def __init__(self, callback_state="st", installations=((200, [{"id": 777}]),), run_fail=None):
        self.server = FakeServer(callback_state)
        self.opened = []
        self.http_calls = []
        self.http_responses = [(201, CONVERSION), *installations]
        self.commands = []
        self.run_fail = run_fail
        self.t = 0.0
        self.out, self.err = io.StringIO(), io.StringIO()

    def http(self, method, url, headers, body=None):
        self.http_calls.append((method, url))
        return self.http_responses.pop(0)

    def run(self, argv, input=None, capture_output=None, text=None, cwd=None):
        self.commands.append((argv, input))
        code = 1 if self.run_fail and argv[:3] == self.run_fail else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    def factory(self, port=0):
        self.port_asked = port
        return self.server

    def deps(self):
        def sleep(s):
            self.t += s

        return cli.Deps(server_factory=self.factory, open_browser=self.opened.append,
                        http=self.http, run=self.run, sign=lambda data, pem: b"sig",
                        clock=lambda: self.t, sleep=sleep, new_state=lambda: "st",
                        stdout=self.out, stderr=self.err)

    def main(self, args):
        return cli.main(args, self.deps())

    def no_pem_leak(self):
        assert "MIIfake" not in self.out.getvalue() + self.err.getvalue()


def manifest_of(page):
    import html
    import re
    return json.loads(html.unescape(re.search(r'name="manifest" value="([^"]*)"', page).group(1)))


def test_github_mode_end_to_end():
    w = World()
    code = w.main(["--org", "acme", "--name", "acme-release", "--repo", "acme/site", "--repo", "acme/api"])
    assert code == 0
    assert w.commands == [
        (["gh", "variable", "set", "YEET_APP_ID", "-R", "acme/site", "--body", "Iv23abc"], None),
        (["gh", "secret", "set", "YEET_APP_PRIVATE_KEY", "-R", "acme/site"], PEM),
        (["gh", "variable", "set", "YEET_APP_ID", "-R", "acme/api", "--body", "Iv23abc"], None),
        (["gh", "secret", "set", "YEET_APP_PRIVATE_KEY", "-R", "acme/api"], PEM),
    ]
    m = manifest_of(w.server.page)
    assert m["default_permissions"] == {"contents": "write", "pull_requests": "write"}
    assert m["redirect_url"] == w.server.redirect_url and m["name"] == "acme-release"
    assert 'action="https://github.com/organizations/acme/settings/apps/new?state=st"' in w.server.page
    assert w.opened == [w.server.start_url, "https://github.com/apps/acme-release/installations/new"]
    assert w.http_calls[0] == ("POST", "https://api.github.com/app-manifests/c0de/conversions")
    assert w.http_calls[1] == ("GET", "https://api.github.com/app/installations")
    out = w.out.getvalue() + w.err.getvalue()
    assert w.server.start_url in out and "installations/new" in out and "777" in out
    assert w.server.closed
    w.no_pem_leak()


def test_user_mode_and_custom_permissions_and_homepage():
    w = World()
    assert w.main(["--user", "--name", "mine", "--repo", "me/r", "--permission", "contents=read",
                   "--permission", "issues=write", "--homepage", "https://example.com"]) == 0
    assert 'action="https://github.com/settings/apps/new?state=st"' in w.server.page
    m = manifest_of(w.server.page)
    assert m["default_permissions"] == {"contents": "read", "issues": "write"}
    assert m["url"] == "https://example.com"


def test_state_mismatch_is_rejected_before_conversion():
    w = World(callback_state="evil")
    assert w.main(["--org", "acme", "--name", "n", "--repo", "acme/site"]) == 1
    assert w.http_calls == [] and w.commands == []
    assert "state" in w.err.getvalue()


def test_pulumi_mode():
    w = World()
    assert w.main(["--org", "acme", "--name", "n", "--to", "pulumi", "--stack", "prod",
                   "--cwd", "/srv/infra"]) == 0
    base = ["pulumi", "config", "set", "--secret", "--stack", "prod", "--cwd", "/srv/infra"]
    assert w.commands == [
        (base + ["yeetAppId"], "Iv23abc"),
        (base + ["yeetAppPrivateKey"], PEM),
        (base + ["yeetAppInstallationId"], "777"),
    ]
    w.no_pem_leak()


def test_pulumi_mode_custom_names():
    w = World()
    assert w.main(["--org", "acme", "--name", "n", "--to", "pulumi", "--stack", "s",
                   "--client-id-var", "RELEASE_APP_ID", "--key-secret", "RELEASE_APP_KEY",
                   "--installation-id-var", "RELEASE_APP_INSTALLATION"]) == 0
    assert [argv[-1] for argv, _ in w.commands] == ["releaseAppId", "releaseAppKey",
                                                     "releaseAppInstallation"]
    assert w.commands[0][0][7] == "."


def test_stdout_mode_json_and_key_file(tmp_path):
    w = World()
    key = tmp_path / "app.pem"
    assert w.main(["--org", "acme", "--name", "n", "--to", "stdout", "--key-file", str(key)]) == 0
    assert json.loads(w.out.getvalue()) == {
        "app_id": 42, "slug": "acme-release", "client_id": "Iv23abc", "installation_id": 777,
        "private_key_file": str(key), "html_url": "https://github.com/apps/acme-release"}
    assert key.read_text() == PEM and stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert w.commands == []
    w.no_pem_leak()


def test_installation_timeout_reports_and_keeps_stored_credentials(tmp_path):
    w = World(installations=[(200, [])] * 500)
    key = tmp_path / "app.pem"
    assert w.main(["--org", "acme", "--name", "n", "--to", "stdout", "--key-file", str(key),
                   "--timeout", "20"]) == 1
    assert json.loads(w.out.getvalue())["installation_id"] is None
    assert "timed out" in w.err.getvalue() and "installations/new" in w.err.getvalue()
    assert key.exists()
    w.no_pem_leak()


def test_storage_failure_saves_key_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = World(run_fail=["gh", "secret", "set"])
    assert w.main(["--org", "acme", "--name", "n", "--repo", "acme/site"]) == 1
    saved = tmp_path / "acme-release.private-key.pem"
    assert saved.read_text() == PEM and stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    assert str(saved) in w.err.getvalue() or saved.name in w.err.getvalue()
    assert len(w.http_calls) == 1
    w.no_pem_leak()


@pytest.mark.parametrize("args,msg", [
    (["--org", "acme", "--name", "n"], "--repo"),
    (["--org", "acme", "--name", "n", "--to", "pulumi"], "--stack"),
    (["--org", "acme", "--name", "n", "--repo", "a/b", "--permission", "contents"], "contents"),
    (["--org", "acme", "--name", "n", "--repo", "a/b", "--permission", "contents=all"], "contents"),
    (["--org", "acme", "--name", "n", "--repo", "nope"], "owner/repo"),
    (["--org", "acme", "--user", "--name", "n", "--repo", "a/b"], "not allowed"),
])
def test_usage_errors(args, msg, capsys):
    w = World()
    with pytest.raises(SystemExit) as e:
        w.main(args)
    assert e.value.code == 2 and msg in capsys.readouterr().err
    assert w.opened == []


def test_script_help_runs():
    out = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--permission" in out.stdout


def test_existing_key_file_is_refused_before_the_browser(tmp_path, capsys):
    key = tmp_path / "app.pem"
    key.write_text("old")
    w = World()
    with pytest.raises(SystemExit) as e:
        w.main(["--org", "acme", "--name", "n", "--to", "stdout", "--key-file", str(key)])
    assert e.value.code == 2 and "exists" in capsys.readouterr().err
    assert w.opened == [] and key.read_text() == "old"


def test_pulumi_installation_store_failure_is_not_reported_as_timeout():
    w = World(run_fail=["pulumi", "config", "set"])
    w.run_fail = None
    original = w.run

    def run(argv, **k):
        if argv[-1] == "yeetAppInstallationId":
            w.commands.append((argv, k.get("input")))
            return subprocess.CompletedProcess(argv, 1, "", "no stack")
        return original(argv, **k)

    w.run = run
    assert w.main(["--org", "acme", "--name", "n", "--to", "pulumi", "--stack", "s"]) == 1
    err = w.err.getvalue()
    assert "no stack" in err and "777" in err and "install the App" not in err


def test_default_asks_for_an_ephemeral_port_and_opens_the_browser():
    w = World()
    assert w.main(["--org", "acme", "--name", "n", "--repo", "acme/site"]) == 0
    assert w.port_asked == 0
    assert w.opened[0] == w.server.start_url and "ssh -L" not in w.err.getvalue()


def test_port_is_passed_to_the_server():
    w = World()
    assert w.main(["--org", "acme", "--name", "n", "--repo", "acme/site", "--port", "8123"]) == 0
    assert w.port_asked == 8123


def test_no_browser_prints_urls_and_ssh_hint_without_opening(monkeypatch):
    monkeypatch.setattr(cli.socket, "gethostname", lambda: "ml-01")
    w = World()
    assert w.main(["--org", "acme", "--name", "n", "--repo", "acme/site", "--no-browser"]) == 0
    assert w.opened == []
    err = w.err.getvalue()
    assert w.server.start_url in err and "https://github.com/apps/acme-release/installations/new" in err
    assert "On another machine? Forward the port first: ssh -L 9999:127.0.0.1:9999 ml-01" in err


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc"])
def test_bad_port_is_a_usage_error(port, capsys):
    w = World()
    with pytest.raises(SystemExit) as e:
        w.main(["--org", "acme", "--name", "n", "--repo", "a/b", "--port", port])
    assert e.value.code == 2 and "port" in capsys.readouterr().err
    assert w.opened == []


def test_busy_port_is_a_usage_error_naming_the_port(capsys):
    import socket
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        w = World()
        w.factory = lambda p=0: cli.manifest.CallbackServer(p)
        with pytest.raises(SystemExit) as e:
            w.main(["--org", "acme", "--name", "n", "--repo", "a/b", "--port", str(port)])
    err = capsys.readouterr().err
    assert e.value.code == 2 and f"127.0.0.1:{port}" in err and "Traceback" not in err
    assert w.opened == [] and w.http_calls == []


class FakeSops:
    """Stands in for sops: "encrypted" files are ENC: plus the JSON document, kept on disk."""

    def __init__(self, break_readback=False):
        self.calls = []
        self.break_readback = break_readback

    def __call__(self, argv, input=None, capture_output=None, text=None, cwd=None):
        self.calls.append((argv, input, cwd))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")
        at = lambda name: Path(cwd or ".") / name  # like sops, relative paths resolve against cwd
        if argv[1] == "--decrypt":
            doc = json.loads(at(argv[-1]).read_text()[4:])
            if self.break_readback and "late.pem" in json.dumps(doc):
                doc["stringData"]["late.pem"] = "other"
            return ok(json.dumps(doc))
        if argv[1] == "--encrypt":
            assert argv[argv.index("--filename-override") + 1] and argv[-1] == "/dev/stdin"
            return ok("ENC:" + input)
        assert argv[1] == "set" and "--value-stdin" in argv
        path = at(argv[-2])
        field, key = re.findall(r'\["([^"]+)"\]', argv[-1])
        doc = json.loads(path.read_text()[4:])
        doc.setdefault(field, {})[key] = json.loads(input)
        path.write_text("ENC:" + json.dumps(doc))
        return ok()


def sops_world(fake):
    w = World()
    w.run = fake
    return w


def sops_doc(path):
    return json.loads(path.read_text()[4:])


def test_sops_new_file(tmp_path):
    f = tmp_path / "app-keys.sops.yaml"
    fake = FakeSops()
    w = sops_world(fake)
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f),
                   "--secret-name", "garm-app-keys", "--namespace", "garm"]) == 0
    assert sops_doc(f) == {"apiVersion": "v1", "kind": "Secret",
                           "metadata": {"name": "garm-app-keys", "namespace": "garm"},
                           "type": "Opaque", "stringData": {"acme-release.pem": PEM}}
    assert json.loads(w.out.getvalue()) == {
        "app_id": 42, "slug": "acme-release", "client_id": "Iv23abc", "installation_id": 777,
        "html_url": "https://github.com/apps/acme-release", "sops_file": str(f),
        "sops_key": "acme-release.pem"}
    assert all(cwd == str(tmp_path) for _, _, cwd in fake.calls)
    assert [p.name for p in tmp_path.iterdir()] == ["app-keys.sops.yaml"]
    w.no_pem_leak()


def test_sops_adds_to_existing_file_with_custom_key(tmp_path):
    f = tmp_path / "k.sops.yaml"
    f.write_text("ENC:" + json.dumps({"stringData": {"other.pem": "x"}}))
    w = sops_world(FakeSops())
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f),
                   "--sops-key", "garm.pem"]) == 0
    assert sops_doc(f) == {"stringData": {"other.pem": "x", "garm.pem": PEM}}
    assert [p.name for p in tmp_path.iterdir()] == ["k.sops.yaml"]


def test_sops_data_file_gets_base64(tmp_path):
    import base64
    f = tmp_path / "k.sops.yaml"
    f.write_text("ENC:" + json.dumps({"data": {}}))
    w = sops_world(FakeSops())
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f)]) == 0
    assert base64.b64decode(sops_doc(f)["data"]["acme-release.pem"]).decode() == PEM


def test_sops_duplicate_refused_without_replace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "k.sops.yaml"
    before = "ENC:" + json.dumps({"stringData": {"acme-release.pem": "old"}})
    f.write_text(before)
    w = sops_world(FakeSops())
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f)]) == 1
    assert "--replace" in w.err.getvalue() and f.read_text() == before
    assert (tmp_path / "acme-release.private-key.pem").read_text() == PEM
    w = sops_world(FakeSops())
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f),
                   "--replace"]) == 0
    assert sops_doc(f)["stringData"]["acme-release.pem"] == PEM


def test_sops_verification_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "k.sops.yaml"
    f.write_text("ENC:" + json.dumps({"stringData": {}}))
    w = sops_world(FakeSops(break_readback=True))
    assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f),
                   "--sops-key", "late.pem"]) == 1
    assert "doesn't read back" in w.err.getvalue()
    w.no_pem_leak()


def test_sops_pem_never_in_argv(tmp_path):
    new, old = tmp_path / "new.sops.yaml", tmp_path / "old.sops.yaml"
    old.write_text("ENC:" + json.dumps({"stringData": {}}))
    fake = FakeSops()
    for f in (new, old):
        sops_world(fake).main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", str(f),
                               "--secret-name", "s", "--namespace", "ns"])
    assert len(fake.calls) >= 5
    assert all("MIIfake" not in " ".join(argv) for argv, _, _ in fake.calls)


def test_sops_relative_path_with_a_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "old.sops.yaml").write_text("ENC:" + json.dumps({"stringData": {}}))
    for name in ("sub/new.sops.yaml", "sub/old.sops.yaml"):
        w = sops_world(FakeSops())
        assert w.main(["--org", "acme", "--name", "n", "--to", "sops", "--sops-file", name,
                       "--secret-name", "s", "--namespace", "ns"]) == 0, w.err.getvalue()
        assert sops_doc(tmp_path / name)["stringData"]["acme-release.pem"] == PEM
    assert sorted(p.name for p in (tmp_path / "sub").iterdir()) == ["new.sops.yaml", "old.sops.yaml"]


@pytest.mark.parametrize("args,msg", [
    (["--to", "sops"], "--sops-file"),
    (["--to", "sops", "--sops-file", "/nonexistent/x.sops.yaml"], "--secret-name"),
])
def test_sops_usage_errors(args, msg, capsys):
    with pytest.raises(SystemExit):
        World().main(["--org", "acme", "--name", "n", *args])
    assert msg in capsys.readouterr().err
