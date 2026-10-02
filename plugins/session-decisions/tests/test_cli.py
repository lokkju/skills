import io
import json
import subprocess
import sys
from pathlib import Path

from session_decisions import cli, hooks

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "decisions.py"


def run(args, env, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(args, env, io.StringIO(stdin), out, err)
    return code, out.getvalue(), err.getvalue()


def test_add_list_close_round_trip(env_for):
    env = env_for(CLAUDE_CODE_SESSION_ID="s1")
    code, out, _ = run(["add", "--kind", "decide", "--text", "retire X? (recommend yes) [no link]",
                        "--description", "why"], env)
    assert code == 0 and out.strip() == "D1 DECIDE: retire X? (recommend yes) [no link]"
    code, out, _ = run(["list"], env)
    assert "D1 DECIDE: retire X?" in out and "ledger" in out and "waiting" in out
    assert run(["close", "d1", "--ruling", "yes"], env)[0] == 0
    assert run(["list"], env)[1].strip() == "No open decisions or actions."
    assert "Ruling (" in run(["list", "--all"], env)[1]


def test_add_rejects_malformed(env_for):
    code, _, err = run(["add", "--kind", "DECIDE", "--text", "retire X?"], env_for(CLAUDE_CODE_SESSION_ID="s1"))
    assert code == 2 and "recommend" in err


def test_close_task_list_item_points_at_taskupdate(env_for):
    code, _, err = run(["close", "D4", "--ruling", "x"], env_for(CLAUDE_CODE_SESSION_ID="s1"))
    assert code == 1 and "TaskUpdate" in err


def test_no_session_is_a_clear_error(env_for):
    code, _, err = run(["list"], env_for())
    assert code == 2 and "--session" in err


def test_empty_session_flag_falls_back_to_env(env_for):
    assert run(["list", "--session", ""], env_for(CLAUDE_CODE_SESSION_ID="s1"))[0] == 0


def test_status_counts_open_items_for_the_statusline_session(env_for):
    run(["add", "--kind", "ACTION", "--text", "enable it [no link]"], env_for(CLAUDE_CODE_SESSION_ID="s1"))
    assert run(["status"], env_for(), stdin=json.dumps({"session_id": "s1"})) == (0, "1 open", "")


def test_status_is_silent_on_bad_input(env_for):
    assert run(["status"], env_for(), stdin="not json") == (0, "", "")


def test_hook_commands_never_fail(env_for):
    assert run(["hook-pretooluse"], env_for(), stdin="garbage")[0] == 0
    assert run(["hook-sessionstart"], env_for(), stdin="")[0] == 0


def test_hook_pretooluse_prints_json(env_for):
    stdin = json.dumps({"session_id": "s1", "tool_name": "TaskCreate",
                        "tool_input": {"subject": "DECIDE: a (recommend b) [no link]"}})
    code, out, _ = run(["hook-pretooluse"], env_for(), stdin=stdin)
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["updatedInput"]["subject"].startswith("D1 ")


def test_data_dir_pointer_used_when_plugin_data_unset(env_for):
    env = env_for(CLAUDE_CODE_SESSION_ID="s1")
    hooks.session_start({"session_id": "s1", "source": "startup"}, env)
    run(["add", "--kind", "ACTION", "--text", "enable it [no link]"], env)
    bash_env = {k: v for k, v in env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert "A1 ACTION: enable it [no link]" in run(["list"], bash_env)[1]


def test_entry_script_runs_standalone(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "status"], input="{}",
                            capture_output=True, text=True, env={"XDG_STATE_HOME": str(tmp_path)})
    assert result.returncode == 0 and result.stdout == ""
