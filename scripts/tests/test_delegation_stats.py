"""Tests for scripts/delegation-stats.py over small synthetic transcripts."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "delegation-stats.py"
spec = importlib.util.spec_from_file_location("delegation_stats", SCRIPT)
ds = importlib.util.module_from_spec(spec)
sys.modules["delegation_stats"] = ds
spec.loader.exec_module(ds)

SESSION = "11111111-2222-3333-4444-555555555555"
SECRET = "SECRET-PROMPT-TEXT"
USAGE = {
    "input_tokens": 1,
    "cache_read_input_tokens": 1,
    "cache_creation_input_tokens": 1,
    "output_tokens": 1,
}


class Transcript:
    """Builds a session's records in order, with a clock that ticks one minute per record."""

    def __init__(self, day="2026-10-05", sidechain=False):
        self.day = day
        self.tag = f"{day}-{sidechain}"  # keeps ids unique when records are merged
        self.sidechain = sidechain
        self.records = []
        self.minute = 0
        self.responses = 0
        self.calls = 0

    def _stamp(self):
        self.minute += 1
        return f"{self.day}T{10 + self.minute // 60:02d}:{self.minute % 60:02d}:00.000Z"

    def _base(self, kind, **extra):
        return {
            "type": kind,
            "isSidechain": self.sidechain,
            "timestamp": self._stamp(),
            "sessionId": SESSION,
            **extra,
        }

    def prompt(self, text=SECRET, **flags):
        self.records.append(self._base("user", message={"role": "user", "content": text}, **flags))
        return self

    def response(self, tools=(), context=100, output=10, split=False, model="claude-test"):
        """One API response. With split, each block is its own record sharing message.id, the
        way Claude Code writes them."""
        self.responses += 1
        message_id = f"msg_{self.tag}_{self.responses}"
        blocks = [{"type": "thinking", "thinking": SECRET}]
        for name in tools:
            self.calls += 1
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"tu_{self.tag}_{self.calls}",
                    "name": name,
                    "input": SECRET,
                }
            )
        usage = {
            "input_tokens": context // 2,
            "cache_read_input_tokens": context // 4,
            "cache_creation_input_tokens": context - context // 2 - context // 4,
            "output_tokens": output,
        }
        groups = [[b] for b in blocks] if split else [blocks]
        for group in groups:
            message = {
                "id": message_id,
                "model": model,
                "role": "assistant",
                "content": group,
                "usage": usage,
            }
            self.records.append(self._base("assistant", message=message))
        for name in tools:
            result = [{"type": "tool_result", "tool_use_id": "x", "content": SECRET}]
            self.records.append(self._base("user", message={"role": "user", "content": result}))
        return self

    def reads(self, count, **kwargs):
        for _ in range(count):
            self.response(["Read"], **kwargs)
        return self

    def raw(self, record):
        self.records.append(record)
        return self

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "ai-title", "aiTitle": SECRET, "sessionId": SESSION})]
        lines += [json.dumps(r) for r in self.records]
        path.write_text("\n".join(lines) + "\n")
        return path


@pytest.fixture
def root(tmp_path):
    return tmp_path / "projects"


@pytest.fixture
def project(root):
    return root / "-home-me-work"


def run(project, capsys, *extra):
    code = ds.main([str(project), "--json", "--projects-root", str(project.parent), *extra])
    captured = capsys.readouterr()
    return code, captured


def stats(project, capsys, *extra):
    code, captured = run(project, capsys, *extra)
    assert code == 0, captured.err
    assert SECRET not in captured.out
    return json.loads(captured.out)


def only_session(data):
    assert len(data["sessions"]) == 1
    return data["sessions"][0]


def test_dedupes_responses_on_message_id(project, capsys):
    t = Transcript().prompt()
    t.response(["Bash", "Read"], context=1000, split=True)
    t.response(["Edit"], context=3000, split=True)
    t.write(project / f"{SESSION}.jsonl")
    session = only_session(stats(project, capsys))
    assert session["main_api_calls"] == 2
    assert session["main_context_median"] == 2000
    assert session["main_context_peak"] == 3000
    assert session["tool_calls"] == {"Bash": 1, "Read": 1, "Edit": 1}
    assert session["prompts"][0]["peak_context"] == 3000


def test_read_runs_end_at_prompts_and_agent_calls(project, capsys):
    t = Transcript().prompt()
    t.reads(3).response(["Edit"]).reads(2)  # Edit neither counts nor ends the run: 5
    t.response(["Agent"]).reads(11)  # Agent ends it; then a run of 11
    t.prompt().reads(4).response(["Task"]).reads(1)  # Task ends a run like Agent does
    t.prompt().reads(10)
    t.write(project / f"{SESSION}.jsonl")
    session = only_session(stats(project, capsys))
    assert [p["longest_read_run"] for p in session["prompts"]] == [11, 4, 10]
    assert [p["agent_call"] for p in session["prompts"]] == [True, True, False]
    assert session["longest_read_run"] == 11
    assert session["runs_of_10_plus_reads"] == 2
    assert session["prompts_with_agent_call"] == 2
    assert session["agent_share"] == round(2 / 3, 4)


def test_injected_strings_are_not_prompts(project, capsys):
    t = Transcript().prompt().reads(6)
    t.prompt("<task-notification>done</task-notification>").reads(6)
    t.prompt("<system-reminder>note</system-reminder>").reads(6)
    t.prompt("<local-command-stdout>out</local-command-stdout>").reads(6)
    t.prompt("peer message", isMeta=True).reads(6)
    t.prompt("summary", isCompactSummary=True, isVisibleInTranscriptOnly=True).reads(6)
    t.write(project / f"{SESSION}.jsonl")
    session = only_session(stats(project, capsys))
    assert session["prompts_with_tool_calls"] == 1
    assert session["longest_read_run"] == 36


def test_prompts_without_tool_calls_are_skipped(project, capsys):
    t = Transcript().prompt().response().prompt().reads(2).prompt()
    t.write(project / f"{SESSION}.jsonl")
    session = only_session(stats(project, capsys))
    assert session["prompts_with_tool_calls"] == 1
    assert len(session["prompts"]) == 1


def test_sidechain_records_in_the_main_transcript_are_skipped(project, capsys):
    t = Transcript().prompt().reads(2)
    side = Transcript(sidechain=True).prompt().reads(20, context=999_999)
    t.records += side.records
    t.reads(1)
    t.write(project / f"{SESSION}.jsonl")
    session = only_session(stats(project, capsys))
    assert session["longest_read_run"] == 3
    assert session["main_context_peak"] == 100
    assert session["tool_calls"] == {"Read": 3}


def test_subagent_tokens_are_summed_after_dedupe(project, capsys):
    Transcript().prompt().response(["Agent"]).write(project / f"{SESSION}.jsonl")
    agents = project / SESSION / "subagents"
    sub = Transcript(sidechain=True).prompt()
    sub.response(["Read"], context=1000, output=50, split=True)  # two records, one response
    sub.response([], context=2000, output=70)
    sub.write(agents / "agent-a1.jsonl")
    (agents / "agent-a1.meta.json").write_text(
        json.dumps({"agentType": "Explore", "model": "haiku", "description": SECRET})
    )
    Transcript(sidechain=True).prompt().response(context=400, output=5).write(
        agents / "agent-a2.jsonl"
    )
    data = stats(project, capsys)
    session = only_session(data)
    assert session["subagent_count"] == 2
    assert session["subagent_tokens"]["total"] == 1000 + 50 + 2000 + 70 + 400 + 5
    assert session["subagent_tokens"]["output_tokens"] == 125
    first = data["subagents"][SESSION][0]
    assert first == {
        "agent_id": "a1",
        "agent_type": "Explore",
        "model": "haiku",
        "responses": 2,
        "tokens": {
            "input_tokens": 1500,
            "cache_read_input_tokens": 750,
            "cache_creation_input_tokens": 750,
            "output_tokens": 120,
            "total": 3120,
        },
    }
    # The main thread's own context ignores subagent responses.
    assert session["main_context_peak"] == 100


def test_date_range_filters_prompts_and_sessions(project, capsys):
    t = Transcript(day="2026-10-01").prompt().reads(12)
    later = Transcript(day="2026-10-03").prompt().reads(3).response(["Agent"])
    t.records += later.records
    t.write(project / f"{SESSION}.jsonl")
    Transcript(day="2026-09-20").prompt().reads(1).write(project / "old-session.jsonl")
    data = stats(project, capsys, "--since", "2026-10-02", "--until", "2026-10-31")
    session = only_session(data)
    assert session["session"] == SESSION
    assert session["prompts_with_tool_calls"] == 1
    assert session["runs_of_10_plus_reads"] == 0
    assert session["agent_share"] == 1.0
    everything = stats(project, capsys)
    assert len(everything["sessions"]) == 2
    assert everything["total"]["prompts_with_tool_calls"] == 3
    assert everything["total"]["runs_of_10_plus_reads"] == 1


def test_session_filter_and_cwd_mapping(root, capsys, tmp_path):
    cwd = tmp_path / "my.repo"
    cwd.mkdir()
    project = root / ds.re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))
    Transcript().prompt().reads(1).write(project / f"{SESSION}.jsonl")
    Transcript().prompt().reads(2).write(project / "99999999-other.jsonl")
    code = ds.main([str(cwd), "--json", "--projects-root", str(root), "--session", "1111"])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [s["session"] for s in data["sessions"]] == [SESSION]


def test_table_output_has_numbers_only(project, capsys):
    Transcript().prompt().reads(2).response(["Agent"]).write(project / f"{SESSION}.jsonl")
    Transcript().prompt().reads(1).write(project / "22222222-x.jsonl")
    assert ds.main([str(project), "--projects-root", str(project.parent)]) == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    lines = out.splitlines()
    assert lines[0].startswith("session")
    assert lines[1].startswith("11111111")
    assert lines[-1].startswith("all")


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ({"type": "brand-new-type", "sessionId": SESSION}, "unrecognized record type"),
        ({"no": "type"}, "string `type`"),
        (
            {
                "type": "user",
                "isSidechain": False,
                "timestamp": "2026-10-05T12:00:00Z",
                "message": {"content": {"text": SECRET}},
            },
            "neither a string nor a list",
        ),
        (
            {
                "type": "assistant",
                "isSidechain": False,
                "timestamp": "2026-10-05T12:00:00Z",
                "message": {"id": "m", "content": [{"type": "new_block"}], "usage": USAGE},
            },
            "unrecognized type 'new_block'",
        ),
        (
            {
                "type": "assistant",
                "isSidechain": False,
                "timestamp": "2026-10-05T12:00:00Z",
                "message": {"content": [], "usage": USAGE},
            },
            "message.id",
        ),
        (
            {
                "type": "assistant",
                "isSidechain": False,
                "timestamp": "2026-10-05T12:00:00Z",
                "message": {"id": "m", "content": [], "usage": {"input_tokens": 1}},
            },
            "`message.usage.cache_read_input_tokens` missing",
        ),
    ],
)
def test_unrecognized_shapes_fail_loudly(project, capsys, record, message):
    t = Transcript().prompt().reads(1).raw(record)
    path = t.write(project / f"{SESSION}.jsonl")
    code, captured = run(project, capsys)
    assert code == 2
    assert captured.out == ""
    # The ai-title line, the prompt, one response and its tool result come first.
    assert f"{path}:5: " in captured.err
    assert message in captured.err
    assert SECRET not in captured.err


def test_invalid_json_names_the_line(project, capsys):
    path = Transcript().prompt().write(project / f"{SESSION}.jsonl")
    with path.open("a") as handle:
        handle.write("{not json\n")
    code, captured = run(project, capsys)
    assert code == 2
    assert f"{path}:3: not valid JSON" in captured.err


def test_missing_project_exits_with_a_message(root):
    root.mkdir()
    with pytest.raises(SystemExit, match="no project directory"):
        ds.main(["/nowhere/at/all", "--projects-root", str(root)])
