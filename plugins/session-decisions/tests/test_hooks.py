import json
import os

from session_decisions import hooks


def payload(subject, sid="s1", tool="TaskCreate"):
    return {"session_id": sid, "tool_name": tool, "tool_input": {"subject": subject, "description": "ctx"}}


def test_other_tools_and_plain_tasks_pass_through(env_for):
    env = env_for()
    assert hooks.pre_tool_use(payload("x", tool="Bash"), env) is None
    assert hooks.pre_tool_use(payload("Run the migration"), env) is None


def test_decide_gets_numbered(env_for):
    out = hooks.pre_tool_use(payload("DECIDE: retire X? (recommend yes) [#812]"), env_for())
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "allow"
    assert spec["updatedInput"] == {"subject": "D1 DECIDE: retire X? (recommend yes) [#812]", "description": "ctx"}


def test_action_gets_its_own_counter(env_for):
    env = env_for()
    hooks.pre_tool_use(payload("DECIDE: a (recommend b) [no link]"), env)
    out = hooks.pre_tool_use(payload("ACTION: enable job tokens [no link]"), env)
    assert out["hookSpecificOutput"]["updatedInput"]["subject"] == "A1 ACTION: enable job tokens [no link]"


def test_self_numbered_subject_is_renumbered(env_for):
    env = env_for()
    hooks.pre_tool_use(payload("DECIDE: a (recommend b) [no link]"), env)
    out = hooks.pre_tool_use(payload("D1 DECIDE: c (recommend d) [no link]"), env)
    assert out["hookSpecificOutput"]["updatedInput"]["subject"] == "D2 DECIDE: c (recommend d) [no link]"


def test_malformed_item_is_denied_with_reason(env_for):
    out = hooks.pre_tool_use(payload("DECIDE: retire X?"), env_for())
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "recommend" in spec["permissionDecisionReason"]
    assert "link" in spec["permissionDecisionReason"]


def test_unsafe_session_id_passes_through(env_for):
    assert hooks.pre_tool_use(payload("DECIDE: a (recommend b) [no link]", sid="../x"), env_for()) is None


def test_session_start_injects_open_items_after_compaction(env_for, tmp_path):
    env = env_for()
    tasks = tmp_path / "cfg" / "tasks" / "s1"
    tasks.mkdir(parents=True)
    (tasks / "1.json").write_text(json.dumps({"id": "1", "subject": "D1 DECIDE: a (recommend b) [no link]", "status": "pending"}))
    (tasks / "2.json").write_text(json.dumps({"id": "2", "subject": "D2 DECIDE: done (recommend x) [no link]", "status": "completed"}))
    out = hooks.session_start({"session_id": "s1", "source": "compact"}, env)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "D1 DECIDE: a (recommend b) [no link]" in context
    assert "D2" not in context


def test_session_start_with_nothing_open_injects_nothing(env_for):
    assert hooks.session_start({"session_id": "s1", "source": "resume"}, env_for()) is None


def test_session_start_on_fresh_start_injects_nothing(env_for, tmp_path):
    tasks = tmp_path / "cfg" / "tasks" / "s1"
    tasks.mkdir(parents=True)
    (tasks / "1.json").write_text(json.dumps({"id": "1", "subject": "D1 DECIDE: a (recommend b) [no link]", "status": "pending"}))
    assert hooks.session_start({"session_id": "s1", "source": "startup"}, env_for()) is None


def test_session_start_installs_statusline_shim(env_for, tmp_path):
    hooks.session_start({"session_id": "s1", "source": "startup"}, env_for(CLAUDE_PLUGIN_ROOT="/opt/sd"))
    shim = tmp_path / "data" / "bin" / "decisions-status"
    assert shim.exists() and os.access(shim, os.X_OK)
    assert "/opt/sd/scripts/decisions.py" in shim.read_text()


def test_session_start_records_data_dir_pointer(env_for, tmp_path):
    hooks.session_start({"session_id": "s1", "source": "startup"}, env_for())
    assert (tmp_path / "state" / "session-decisions" / "data-dir").read_text() == str(tmp_path / "data")
