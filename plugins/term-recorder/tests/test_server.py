"""The MCP server over stdio: it runs as a child process and these tests speak MCP to it, driving
short recordings of small shell targets on real tmux."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "skills/term-recorder/scripts"
SERVER = SCRIPTS / "term-recorder-mcp"
REPO = HERE.parent.parent.parent

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux isn't installed")

# Prints a prompt, echoes one line back, then waits so the pane stays up.
ECHO = ["sh", "-c", 'printf "ready> "; read x; echo "got [$x]"; sleep 30']


class Client:
    """A line-delimited JSON-RPC client over a child's stdin and stdout."""

    def __init__(self, cwd: Path):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd))
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
        )
        self.next_id = 0

    def send(self, message):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self):
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, f"the server closed stdout; stderr:\n{self.proc.stderr.read()}"
        assert line.endswith("\n") and "\n" not in line[:-1]
        return json.loads(line)

    def request(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self.receive()
        assert response["id"] == self.next_id
        return response

    def initialize(self):
        response = self.request(
            "initialize",
            {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t"}},
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response["result"]

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self):
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def client(tmp_path):
    c = Client(tmp_path)
    c.initialize()
    yield c
    c.close()


def text(result) -> str:
    return result["content"][0]["text"]


def test_the_protocol_module_is_forge_trackers_copy():
    """Each plugin installs on its own, so each carries mcp_stdio.py; they must not drift."""
    ours = SCRIPTS / "term_recorder/mcp_stdio.py"
    source = REPO / "plugins/forge-tracker/skills/forge-tracker/scripts/forge_tracker/mcp_stdio.py"
    if not source.is_file():
        pytest.skip("not in the lokkju/skills checkout")
    assert ours.read_bytes() == source.read_bytes(), "run scripts/sync-mcp-stdio.py"


def test_handshake_and_tools(tmp_path):
    c = Client(tmp_path)
    try:
        init = c.initialize()
        assert init["protocolVersion"] == "2025-11-25"
        assert init["serverInfo"]["name"] == "term-recorder"
        assert "record" in init["instructions"]
        assert c.request("ping")["result"] == {}
        tools = {t["name"]: t for t in c.request("tools/list")["result"]["tools"]}
        assert set(tools) == {"record", "check_steps", "snaps"}
        assert tools["record"]["inputSchema"]["required"] == ["command"]
        assert tools["snaps"]["annotations"]["readOnlyHint"] is True
        assert tools["record"]["annotations"]["readOnlyHint"] is False
    finally:
        c.close()


def test_stateless_request(tmp_path):
    c = Client(tmp_path)
    try:
        discover = c.request("server/discover")["result"]
        assert "2026-07-28" in discover["supportedVersions"]
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        result = c.request(
            "tools/call",
            {"name": "check_steps", "arguments": {"steps": "snap one"}, "_meta": meta},
        )["result"]
        assert result["resultType"] == "complete"
        assert text(result).startswith("1 step(s) OK")
    finally:
        c.close()


def test_check_steps_expands_examples_and_reports_errors(client):
    ok = client.call("check_steps", {"steps": "include ${EXAMPLES}/claude-start.steps\nsnap s"})
    assert "isError" not in ok
    assert "claude-start.steps:" in text(ok) and "<steps>:2: snap s" in text(ok)
    bad = client.call("check_steps", {"steps": "snap a\nfrobnicate"})
    assert bad["isError"] is True
    assert "<steps>:2: unknown step 'frobnicate'" in text(bad)
    unknown = client.call("check_steps", {"steps": "snap a", "nope": 1})
    assert unknown["isError"] is True and "unknown argument nope" in text(unknown)


@needs_tmux
def test_record_returns_snaps_as_text(client, tmp_path):
    out = tmp_path / "out"
    result = client.call(
        "record",
        {
            "command": ECHO,
            "steps": "wait 'ready>' 20\ntype hello 0\nkeys Enter\nwait 'got \\[hello\\]' 20\n"
            "snap echoed",
            "size": "40x6",
            "out": "out",
            "cast": "run.cast",
        },
    )
    shown = text(result)
    assert "isError" not in result, shown
    assert f"--- echoed ({out / 'echoed.txt'}) ---" in shown
    assert "got [hello]" in shown
    assert "--- final (" in shown
    assert f"snaps in {out}: echoed, final" in shown
    assert f"cast: {tmp_path / 'run.cast'}" in shown
    assert "got [hello]" in (out / "echoed.txt").read_text(encoding="utf-8")
    header = json.loads((tmp_path / "run.cast").read_text(encoding="utf-8").splitlines()[0])
    assert (header["width"], header["height"]) == (40, 6)

    listed = client.call("snaps", {"out": str(out)})
    assert text(listed) == f"snaps in {out}: echoed, final\n"
    read = client.call("snaps", {"out": str(out), "name": "echoed"})
    assert "got [hello]" in text(read)
    missing = client.call("snaps", {"out": str(out), "name": "nope"})
    assert missing["isError"] is True


@needs_tmux
def test_a_failed_step_is_an_error_with_the_screen(client):
    result = client.call(
        "record",
        {"command": ECHO, "steps": "wait 'never there' 1", "size": "40x6", "show": "final"},
    )
    assert result["isError"] is True
    shown = text(result)
    assert "timed out after 1s waiting for /never there/" in shown
    assert "ready>" in shown and "--- final (" in shown


def test_record_refuses_bad_arguments_before_running(client):
    no_command = client.call("record", {"command": [], "steps": "snap a"})
    assert no_command["isError"] is True and "command" in text(no_command)
    bad_size = client.call("record", {"command": ["true"], "size": "9x2"})
    assert bad_size["isError"] is True and "size wants COLSxROWS" in text(bad_size)
    bad_step = client.call("record", {"command": ["true"], "steps": "wait"})
    assert bad_step["isError"] is True and "wait takes 1 to 2 argument(s)" in text(bad_step)


@needs_tmux
def test_nothing_but_protocol_reaches_stdout(client):
    """A recording runs tmux and a child program; none of their output may reach stdout."""
    client.call("record", {"command": ["sh", "-c", "echo noisy; sleep 5"], "steps": "wait noisy"})
    # The next exchange still parses, so the stream is intact.
    assert client.request("ping")["result"] == {}
