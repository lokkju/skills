import os
import stat
import subprocess

import pytest

from forge_release import store
from forge_release.manifest import FlowError

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----\n"


def test_github_commands_per_repo_secret_on_stdin():
    cmds = store.github_commands("Iv23abc", PEM, ["acme/site", "acme/api"],
                                 "YEET_APP_ID", "YEET_APP_PRIVATE_KEY")
    assert cmds == [
        (["gh", "variable", "set", "YEET_APP_ID", "-R", "acme/site", "--body", "Iv23abc"], None),
        (["gh", "secret", "set", "YEET_APP_PRIVATE_KEY", "-R", "acme/site"], PEM),
        (["gh", "variable", "set", "YEET_APP_ID", "-R", "acme/api", "--body", "Iv23abc"], None),
        (["gh", "secret", "set", "YEET_APP_PRIVATE_KEY", "-R", "acme/api"], PEM),
    ]
    assert all(PEM not in " ".join(argv) for argv, _ in cmds)


@pytest.mark.parametrize("name,key", [("YEET_APP_ID", "yeetAppId"),
                                      ("YEET_APP_PRIVATE_KEY", "yeetAppPrivateKey"),
                                      ("yeet-app-installation-id", "yeetAppInstallationId"),
                                      ("already:namespaced", "already:namespaced")])
def test_pulumi_key(name, key):
    assert store.pulumi_key(name) == key


def test_pulumi_commands_values_on_stdin():
    cmds = store.pulumi_commands([("yeetAppId", "Iv23abc"), ("yeetAppPrivateKey", PEM)],
                                 stack="prod", cwd="/srv/infra")
    assert cmds == [
        (["pulumi", "config", "set", "--secret", "--stack", "prod", "--cwd", "/srv/infra", "yeetAppId"],
         "Iv23abc"),
        (["pulumi", "config", "set", "--secret", "--stack", "prod", "--cwd", "/srv/infra",
          "yeetAppPrivateKey"], PEM),
    ]


def test_run_commands_passes_stdin_and_stops_on_failure():
    seen = []

    def run(argv, input=None, capture_output=None, text=None):
        seen.append((argv, input))
        return subprocess.CompletedProcess(argv, 1 if argv[0] == "bad" else 0, "", "boom")

    with pytest.raises(FlowError) as e:
        store.run_commands([(["ok"], "s"), (["bad", "x"], PEM), (["never"], None)], run)
    assert seen == [(["ok"], "s"), (["bad", "x"], PEM)]
    assert "bad x" in str(e.value) and "boom" in str(e.value) and "MIIfake" not in str(e.value)


def test_run_commands_missing_tool():
    def run(argv, **k):
        raise FileNotFoundError(argv[0])

    with pytest.raises(FlowError, match="gh isn't on PATH"):
        store.run_commands([(["gh", "x"], None)], run)


def test_write_key_file_is_0600_and_never_overwrites(tmp_path):
    path = tmp_path / "app.pem"
    store.write_key_file(str(path), PEM)
    assert path.read_text() == PEM
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(FlowError, match="exists"):
        store.write_key_file(str(path), "other")
    assert path.read_text() == PEM


def test_run_commands_redacts_stdin_echoed_in_stderr():
    def run(argv, input=None, **k):
        return subprocess.CompletedProcess(argv, 1, "", f"bad value: {input}")

    with pytest.raises(FlowError) as e:
        store.run_commands([(["gh", "secret", "set", "K"], PEM)], run)
    assert "MIIfake" not in str(e.value) and "[redacted]" in str(e.value)
