"""The MCP server over stdio: the server runs as a child process and these tests speak MCP to it,
over the recorded fixtures, with no network."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES
from forge_tracker import server

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "skills/forge-tracker/scripts/forge-tracker-mcp"

REPLAY_CONFIG = """---
labels:
  needs_human: needs-human
  decision: decision
tiers:
  labels: [P0, P1, P2, P3]
ruling_pattern: '^Ruling'
changelog: plugins/session-decisions/CHANGELOG.md
report_tags: ["session-decisions--v*", "forge-release--v*", "registry:app"]
---
"""


class Client:
    """A line-delimited JSON-RPC client over a child's stdin and stdout."""

    def __init__(self, argv, cwd=None):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        self.next_id = 0

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self):
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

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def initialize(self, version="2025-11-25"):
        response = self.request(
            "initialize",
            {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return response["result"]

    def call(self, name, arguments):
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        return response["result"]

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        stderr = self.proc.stderr.read()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code, stderr


@pytest.fixture
def harness(tmp_path):
    clients = []

    def start(fixture="-"):
        path = str(FIXTURES / "github" / fixture) if fixture != "-" else "-"
        client = Client([sys.executable, str(HERE / "mcp_harness.py"), path, str(tmp_path)])
        clients.append(client)
        return client

    yield start
    for client in clients:
        if client.proc.poll() is None:
            client.close()


def repo(tmp_path, config_text):
    directory = tmp_path / "repo"
    (directory / ".claude").mkdir(parents=True, exist_ok=True)
    (directory / ".claude" / "forge-tracker.local.md").write_text(config_text)
    return str(directory)


# --- the protocol ---------------------------------------------------------------------------------


def test_the_entry_script_initializes_lists_tools_and_exits_on_eof(tmp_path):
    client = Client([sys.executable, str(SCRIPT)], cwd=tmp_path)
    result = client.initialize()
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "forge-tracker"
    assert client.request("ping")["result"] == {}
    tools = client.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "catchup",
        "next_report",
        "next_facts",
        "save_slate",
        "groom",
        "labels_suggest",
        "smoke",
        "labels_decline",
        "groom_apply",
        "link",
        "write",
        "labels_create",
    ]
    code, _ = client.close()
    assert code == 0


def test_tool_definitions_are_well_formed():
    for tool in server.TOOLS:
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert "repo" in schema["properties"], tool["name"]
        annotations = tool["annotations"]
        if tool["name"] in server.WRITE_TOOLS:
            assert annotations["readOnlyHint"] is False and annotations["openWorldHint"]
        assert len(f"mcp__plugin_forge-tracker_forge-tracker__{tool['name']}") <= 64


@pytest.mark.parametrize(
    "asked, answered",
    [("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"), ("1999-01-01", "2025-11-25")],
)
def test_version_negotiation(harness, asked, answered):
    client = harness()
    assert client.initialize(asked)["protocolVersion"] == answered


def test_protocol_errors(harness):
    client = harness()
    client.initialize()
    assert client.request("resources/list")["error"]["code"] == -32601
    unknown = client.request("tools/call", {"name": "nope", "arguments": {}})
    assert unknown["error"]["code"] == -32602
    client.send({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": []})
    assert client.receive()["error"]["code"] == -32602
    client.proc.stdin.write("{not json\n")
    client.proc.stdin.flush()
    assert client.receive()["error"]["code"] == -32700
    # A cancellation for a finished request is a notification: no answer, and the server goes on.
    client.notify("notifications/cancelled", {"requestId": 1})
    assert client.request("ping")["result"] == {}


def test_a_bad_argument_is_a_tool_error_the_model_can_read(harness, tmp_path):
    client = harness()
    client.initialize()
    result = client.call("catchup", {"bogus": 1})
    assert result["isError"] is True
    assert "unknown argument bogus" in result["content"][0]["text"]
    result = client.call("catchup", {"repo": str(tmp_path), "since": "soon", "fetch": False})
    assert result["isError"] is True and "forge-tracker:" in result["content"][0]["text"]
    result = client.call("save_slate", {})
    assert result["isError"] is True and "missing argument slate" in result["content"][0]["text"]


def test_the_stateless_revision(harness):
    """2026-07-28: no handshake; the version rides in each request's _meta."""
    client = harness()
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    discovered = client.request("server/discover", {"_meta": meta})["result"]
    assert discovered["resultType"] == "complete"
    assert "2026-07-28" in discovered["supportedVersions"]
    assert discovered["capabilities"] == {"tools": {}}
    listed = client.request("tools/list", {"_meta": meta})["result"]
    assert listed["resultType"] == "complete" and listed["cacheScope"] == "public"
    assert isinstance(listed["ttlMs"], int) and len(listed["tools"]) == len(server.TOOLS)
    assert listed["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "forge-tracker"
    wrong = dict(meta, **{"io.modelcontextprotocol/protocolVersion": "2030-01-01"})
    error = client.request("tools/list", {"_meta": wrong})["error"]
    assert error["code"] == -32022 and error["data"]["supported"] == ["2026-07-28"]
    lacking = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    assert client.request("tools/list", {"_meta": lacking})["error"]["code"] == -32602


# --- the read tools over recorded fixtures --------------------------------------------------------


def test_catchup_over_the_recorded_fixture_matches_the_cli_golden(harness, tmp_path):
    client = harness("catchup-lokkju-skills.json")
    client.initialize()
    result = client.call(
        "catchup", {"repo": repo(tmp_path, REPLAY_CONFIG), "since": "1d", "fetch": False}
    )
    golden = (FIXTURES / "github" / "catchup-lokkju-skills.txt").read_text()
    assert result["content"] == [{"type": "text", "text": golden}]
    assert "isError" not in result
    # Claude Code shows structuredContent in place of the text, so the rendered report has none.
    assert "structuredContent" not in result
    as_json = client.call(
        "catchup",
        {"repo": repo(tmp_path, REPLAY_CONFIG), "since": "1d", "fetch": False, "json": True},
    )
    assert json.loads(as_json["content"][0]["text"]) == as_json["structuredContent"]
    assert as_json["structuredContent"]["ok"] is True
    assert as_json["structuredContent"]["marker"] == "keep"


def test_catchup_fetches_first_unless_told_not_to(harness, tmp_path):
    client = harness("catchup-lokkju-skills.json")
    client.initialize()
    path = repo(tmp_path, REPLAY_CONFIG)
    client.call("catchup", {"repo": path, "since": "1d"})
    _, stderr = client.close()
    assert f"run: git -C {path} fetch --quiet origin" in stderr


def test_next_report_and_facts_over_the_recorded_fixture(harness, tmp_path):
    client = harness("next-lokkju-skills.json")
    client.initialize()
    path = repo(tmp_path, "---\n---\n")
    report = client.call("next_report", {"repo": path, "fetch": False})
    golden = (FIXTURES / "github" / "next-lokkju-skills.txt").read_text()
    assert report["content"][0]["text"] == golden
    # The facts are cached under the fingerprint, so the facts call reads nothing new.
    facts = client.call("next_facts", {"repo": path})
    data = json.loads(facts["content"][0]["text"])
    assert data == facts["structuredContent"] and data["ok"] is True
    assert data["slate"]["status"] == "skipped" and '"body":' in facts["content"][0]["text"]


def test_save_slate_refuses_a_bad_slate_and_names_the_problems(harness, tmp_path):
    client = harness("next-lokkju-skills.json")
    client.initialize()
    path = repo(tmp_path, "---\n---\n")
    client.call("next_report", {"repo": path, "fetch": False})
    result = client.call("save_slate", {"repo": path, "slate": {"queue": ["#999999"]}})
    assert result["isError"] is True
    assert "slate refused" in result["content"][0]["text"]


def test_groom_over_the_recorded_fixture_matches_the_cli_golden(harness, tmp_path):
    client = harness("groom-lokkju-skills.json")
    client.initialize()
    result = client.call("groom", {"repo": repo(tmp_path, "---\n---\n"), "fetch": False})
    golden = (FIXTURES / "github" / "groom-lokkju-skills.txt").read_text()
    assert result["content"][0]["text"] == golden
    as_json = client.call(
        "groom", {"repo": repo(tmp_path, "---\n---\n"), "fetch": False, "json": True}
    )
    assert as_json["structuredContent"]["ok"] is True


def test_a_workspace_folder_is_named_for_tools_that_read_one_repo(harness, tmp_path):
    client = harness()
    client.initialize()
    result = client.call("groom", {"repo": str(tmp_path / "nowhere"), "fetch": False})
    assert result["isError"] is True


# --- the write tools ------------------------------------------------------------------------------


def test_groom_apply_with_an_id_that_isnt_a_proposal_writes_nothing(harness, tmp_path):
    client = harness("groom-lokkju-skills.json")
    client.initialize()
    result = client.call(
        "groom_apply", {"repo": repo(tmp_path, "---\n---\n"), "approved": ["close #999999"]}
    )
    assert result["isError"] is True
    assert "nothing written" in result["content"][0]["text"]


def test_link_with_an_unparseable_issue_is_refused_before_any_read(harness, tmp_path):
    client = harness()
    client.initialize()
    result = client.call(
        "link",
        {"repo": repo(tmp_path, "---\n---\n"), "action": "add", "issue": "x", "blocked_by": 1},
    )
    assert result["isError"] is True
    assert "link takes an issue number" in result["content"][0]["text"]


def test_the_write_tools_are_the_only_ones_not_read_only():
    read_only = {t["name"] for t in server.TOOLS if t["annotations"].get("readOnlyHint")}
    assert read_only.isdisjoint(server.WRITE_TOOLS)
    assert read_only == {
        "next_report",
        "next_facts",
        "save_slate",
        "groom",
        "labels_suggest",
        "smoke",
    }


def test_nothing_but_protocol_reaches_stdout(harness, tmp_path):
    """A stray print inside a tool would corrupt the stream; main() sends prints to stderr."""
    env = dict(os.environ, PYTHONPATH=str(SCRIPT.parent))
    code = (
        "import sys, forge_tracker.server as s\n"
        "s.HANDLERS['smoke'] = lambda env, args: (print('stray'), s.Outcome('ok'))[1]\n"
        "sys.exit(s.main())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "smoke", "arguments": {}},
            }
        )
        + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    lines = proc.stdout.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["result"]["content"][0]["text"] == "ok"
    assert "stray" in proc.stderr
