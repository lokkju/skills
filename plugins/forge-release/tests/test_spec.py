"""github-app-create --spec: the JSON infra's `mise run github-app-manifest <name>` prints."""

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from forge_release import cli
from test_cli import (
    CLIENT_SECRET,
    CONVERSION,
    NON_EXPIRING,
    PEM,
    FakeSops,
    World,
    manifest_of,
    no_secret_leak,
    sops_doc,
    token_world,
)

WEBHOOK_SECRET = "whSECRETwh"
AGENT_MANIFEST = {
    "name": "acme-agent",
    "url": "https://github.com/acme/infra",
    "public": True,
    "default_permissions": {"contents": "write", "issues": "write", "metadata": "read"},
    "default_events": ["issues", "issue_comment", "pull_request"],
    "description": "Dispatches agents",
    "callback_urls": ["https://example.com/oauth"],
    "request_oauth_on_install": True,
    "hook_attributes": {"url": "https://hooks.example.com/github/webhook", "active": True},
}
STEPS = [
    "Turn on Device Flow: https://github.com/organizations/acme/settings/apps/acme-agent, ...",
    "Install it on acme with all repositories: https://github.com/apps/acme-agent/installations/new",
]


def spec_file(tmp_path, outputs, manifest=AGENT_MANIFEST, owner="acme", owner_type="org"):
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "owner": owner,
                "owner_type": owner_type,
                "manifest": manifest,
                "outputs": outputs,
                "manual_steps": STEPS,
            }
        )
    )
    return str(path)


class FakeRun:
    """pulumi and git are recorded (git answers `toplevel`); sops goes to FakeSops."""

    def __init__(self, toplevel=None):
        self.sops = FakeSops()
        self.calls = []
        self.toplevel = toplevel

    def __call__(self, argv, input=None, capture_output=None, text=None, cwd=None):
        if argv[0] == "sops":
            return self.sops(argv, input=input, cwd=cwd)
        self.calls.append((argv, input, cwd))
        if argv[0] == "git":
            if self.toplevel is None:
                return subprocess.CompletedProcess(argv, 128, "", "not a git repository")
            return subprocess.CompletedProcess(argv, 0, self.toplevel + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def spec_world(fake=None):
    w = World()
    w.http_responses[0] = (
        201,
        dict(CONVERSION, client_secret=CLIENT_SECRET, webhook_secret=WEBHOOK_SECRET),
    )
    w.run = fake or FakeRun()
    return w


def no_spec_secret_leak(w):
    w.no_pem_leak()
    out = w.out.getvalue() + w.err.getvalue()
    assert "SECRET" not in out
    assert all("SECRET" not in " ".join(argv) for argv, _, _ in w.run.calls + w.run.sops.calls)


def test_spec_keeps_webhook_and_events_and_stores_exact_pulumi_names(tmp_path):
    outputs = {
        "pulumi": {
            "client_id": "fluxKeysAppId",
            "private_key": "fluxKeysAppPrivateKey",
            "webhook_secret": "fluxKeysWebhookSecret",
            "installation_id": "fluxKeysAppInstallationId",
        }
    }
    w = spec_world()
    args = ["--spec", spec_file(tmp_path, outputs), "--stack", "prod", "--cwd", "/srv/infra"]
    assert w.main(args) == 0, w.err.getvalue()
    assert manifest_of(w.server.page) == {**AGENT_MANIFEST, "redirect_url": w.server.redirect_url}
    assert 'action="https://github.com/organizations/acme/settings/apps/new?state=st"' in (
        w.server.page
    )
    base = ["pulumi", "config", "set", "--secret", "--stack", "prod", "--cwd", "/srv/infra"]
    assert [(argv, stdin) for argv, stdin, _ in w.run.calls] == [
        (base + ["fluxKeysAppId"], "Iv23abc"),
        (base + ["fluxKeysAppPrivateKey"], PEM),
        (base + ["fluxKeysWebhookSecret"], WEBHOOK_SECRET),
        (base + ["fluxKeysAppInstallationId"], "777"),
    ]
    err = w.err.getvalue()
    # The remaining clicks come after the App exists and before the install wait.
    assert err.index("Created ") < err.index(STEPS[0]) < err.index("pick the repositories")
    assert "1. Turn on Device Flow" in err and "2. Install it on acme" in err
    assert "any account can install it" in err
    summary = json.loads(w.out.getvalue())
    assert summary["installation_id"] == 777 and "private_key_file" not in summary
    assert summary["stored"] == [
        "pulumi:fluxKeysAppId",
        "pulumi:fluxKeysAppPrivateKey",
        "pulumi:fluxKeysWebhookSecret",
        "pulumi:fluxKeysAppInstallationId",
    ]
    assert list(tmp_path.iterdir()) == [tmp_path / "spec.json"]  # no key file
    no_spec_secret_leak(w)


def test_spec_stores_into_pulumi_and_sops_under_the_repo_root(tmp_path):
    root = tmp_path / "repo"
    secret = root / "homelab/agent/github-app.sops.yaml"
    secret.parent.mkdir(parents=True)
    secret.write_text("ENC:" + json.dumps({"stringData": {"other": "x"}}))
    outputs = {
        "pulumi": {"client_id": "agentClientId", "client_secret": "agentClientSecret"},
        "sops": {
            "file": "homelab/agent/github-app.sops.yaml",
            "keys": {
                "app_id": "app-id",
                "private_key": "private-key.pem",
                "webhook_secret": "webhook-secret",
                "installation_id": "installation-id",
            },
        },
    }
    w = spec_world(FakeRun(toplevel="/elsewhere"))
    args = ["--spec", spec_file(tmp_path, outputs), "--stack", "s", "--repo-root", str(root)]
    assert w.main(args) == 0, w.err.getvalue()
    assert [(argv[-1], stdin) for argv, stdin, _ in w.run.calls] == [
        ("agentClientId", "Iv23abc"),
        ("agentClientSecret", CLIENT_SECRET),
    ]
    assert sops_doc(secret)["stringData"] == {
        "other": "x",
        "app-id": "42",
        "private-key.pem": PEM,
        "webhook-secret": WEBHOOK_SECRET,
        "installation-id": "777",
    }
    assert json.loads(w.out.getvalue())["stored"] == [
        "pulumi:agentClientId",
        "pulumi:agentClientSecret",
        "sops:app-id",
        "sops:private-key.pem",
        "sops:webhook-secret",
        "sops:installation-id",
    ]
    assert sorted(p.name for p in secret.parent.iterdir()) == ["github-app.sops.yaml"]
    no_spec_secret_leak(w)


def test_spec_sops_file_defaults_to_the_git_toplevel_and_can_be_created(tmp_path):
    outputs = {"sops": {"file": "k/app.sops.yaml", "keys": {"private_key": "acme.pem"}}}
    (tmp_path / "k").mkdir()
    w = spec_world(FakeRun(toplevel=str(tmp_path)))
    args = ["--spec", spec_file(tmp_path, outputs), "--secret-name", "s", "--namespace", "ns"]
    assert w.main(args) == 0, w.err.getvalue()
    assert sops_doc(tmp_path / "k/app.sops.yaml")["stringData"] == {"acme.pem": PEM}
    assert w.run.calls[0][0] == ["git", "rev-parse", "--show-toplevel"]


def test_spec_drops_unnamed_secrets_and_keeps_an_unstored_key_in_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = spec_world()
    args = ["--spec", spec_file(tmp_path, {"pulumi": {"app_id": "appId"}}), "--stack", "s"]
    assert w.main(args) == 0, w.err.getvalue()
    assert [(argv[-1], stdin) for argv, stdin, _ in w.run.calls] == [("appId", "42")]
    saved = tmp_path / "acme-release.private-key.pem"
    assert saved.read_text() == PEM and stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    assert json.loads(w.out.getvalue())["private_key_file"] == saved.name
    no_spec_secret_leak(w)


def test_spec_output_github_doesnt_return_is_reported_not_stored(tmp_path):
    w = spec_world()
    w.http_responses[0] = (201, CONVERSION)  # no webhook, so no webhook_secret
    outputs = {"pulumi": {"private_key": "key", "webhook_secret": "hookSecret"}}
    assert w.main(["--spec", spec_file(tmp_path, outputs), "--stack", "s"]) == 0
    assert [argv[-1] for argv, _, _ in w.run.calls] == ["key"]
    assert "GitHub returned no webhook_secret" in w.err.getvalue()


def test_spec_from_stdin(tmp_path, monkeypatch):
    path = spec_file(tmp_path, {"pulumi": {"private_key": "key"}}, owner="me", owner_type="user")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(Path(path).read_text()))
    w = spec_world()
    assert w.main(["--spec", "-", "--stack", "s"]) == 0, w.err.getvalue()
    assert 'action="https://github.com/settings/apps/new?state=st"' in w.server.page


def test_spec_with_user_token_appends_the_listener_to_callback_urls(tmp_path):
    f = tmp_path / "token.sops.yaml"
    f.write_text("ENC:" + json.dumps({"stringData": {}}))
    w = token_world(reply=NON_EXPIRING)
    path = spec_file(tmp_path, {}, owner="me", owner_type="user")
    assert w.main(["--spec", path, "--user-token", "--sops-file", str(f)]) == 0, w.err.getvalue()
    m = manifest_of(w.server.page)
    assert m["callback_urls"] == ["https://example.com/oauth", w.server.redirect_url]
    assert sops_doc(f)["stringData"] == {"token": "ghu_SECRETtoken"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spec.json", "token.sops.yaml"]
    no_secret_leak(w)


@pytest.mark.parametrize(
    "extra,msg",
    [
        (["--name", "n"], "--spec replaces --name"),
        (["--org", "acme"], "--spec replaces --org"),
        (["--permission", "contents=read", "--public"], "--permission, --public"),
        (["--homepage", "https://e.x"], "--homepage"),
        (["--to", "sops"], "--to"),
        (["--client-id-var", "X"], "--client-id-var"),
        (["--sops-file", "x.sops.yaml"], "only for the --user-token token"),
        (["--user-token"], "needs --sops-file"),
    ],
)
def test_spec_usage_errors(tmp_path, extra, msg, capsys):
    w = spec_world()
    path = spec_file(tmp_path, {"pulumi": {"private_key": "key"}})
    with pytest.raises(SystemExit) as e:
        w.main(["--spec", path, "--stack", "s", *extra])
    assert e.value.code == 2 and msg in capsys.readouterr().err
    assert w.opened == [] and w.http_calls == []


@pytest.mark.parametrize(
    "outputs,args,msg",
    [
        ({"pulumi": {"private_key": "key"}}, [], "needs --stack"),
        ({"pulumi": {"pem": "key"}}, ["--stack", "s"], "'pem' isn't one of"),
        ({"vault": {"private_key": "k"}}, [], "unknown stores vault"),
        ({"sops": {"file": "x.sops.yaml"}}, [], "{file, keys}"),
        (
            {"sops": {"file": "missing.sops.yaml", "keys": {"private_key": "k"}}},
            ["--repo-root", "/nonexistent"],
            "--secret-name and --namespace",
        ),
        ({"sops": {"file": "x.sops.yaml", "keys": {"private_key": "k"}}}, [], "--repo-root"),
    ],
)
def test_spec_content_errors(tmp_path, outputs, args, msg, capsys):
    w = spec_world()
    with pytest.raises(SystemExit) as e:
        w.main(["--spec", spec_file(tmp_path, outputs), *args])
    assert e.value.code == 2 and msg in capsys.readouterr().err
    assert w.opened == [] and w.http_calls == []


def test_spec_that_isnt_json_is_a_usage_error(tmp_path, capsys):
    path = tmp_path / "spec.json"
    path.write_text("apps: []")
    with pytest.raises(SystemExit):
        spec_world().main(["--spec", str(path)])
    assert "not JSON" in capsys.readouterr().err


def test_repo_root_without_spec_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        World().main(["--org", "acme", "--name", "n", "--repo", "a/b", "--repo-root", "."])
    assert "--repo-root only applies with --spec" in capsys.readouterr().err


def test_missing_name_or_owner_without_spec(capsys):
    with pytest.raises(SystemExit):
        World().main(["--org", "acme", "--repo", "a/b"])
    assert "--name is required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        World().main(["--name", "n", "--repo", "a/b"])
    assert "--org, --user or --spec" in capsys.readouterr().err
