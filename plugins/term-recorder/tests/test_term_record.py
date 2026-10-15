"""term-record against real tmux sessions running small shell and Python targets."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from term_recorder import steps as steplib

TERM_RECORD = SCRIPTS / "term-record"

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux isn't installed")


def record(tmp_path: Path, steps: str, *command: str, extra: tuple[str, ...] = ()):
    step_file = tmp_path / "run.steps"
    step_file.write_text(steps, encoding="utf-8")
    out = tmp_path / "out"
    return subprocess.run(
        [
            sys.executable,
            str(TERM_RECORD),
            "-s",
            str(step_file),
            "-o",
            str(out),
            *extra,
            "--",
            *command,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    ), out


# --- step files ----------------------------------------------------------------------------------


def test_parse_quoting_comments_and_vars(tmp_path: Path):
    lines = [
        "# a comment",
        "",
        "wait 'trust this folder|❯' 30   # trailing comment",
        'type "hello ${WHO}" 0',
        "keys Down Enter",
        "when 'trust' keys Enter",
    ]
    parsed = steplib.parse(lines, "t", tmp_path, {"WHO": "world"})
    assert [(s.action, s.args) for s in parsed] == [
        ("wait", ["trust this folder|❯", "30"]),
        ("type", ["hello world", "0"]),
        ("keys", ["Down", "Enter"]),
        ("when", ["trust"]),
    ]
    assert parsed[3].then is not None and parsed[3].then.args == ["Enter"]


def test_include_resolves_relative_to_its_file(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.steps").write_text("sleep 0\n", encoding="utf-8")
    (tmp_path / "outer.steps").write_text("include sub/inner.steps\nsnap x\n", encoding="utf-8")
    parsed = steplib.load(tmp_path / "outer.steps", {})
    assert [s.action for s in parsed] == ["sleep", "snap"]


@pytest.mark.parametrize(
    "line, message",
    [
        ("frobnicate", "unknown step"),
        ("wait", "takes 1 to 2 argument(s), got 0"),
        ("keys", "takes 1 or more argument(s)"),
        ("sleep soon", "isn't a number"),
        ("wait '(' 3", "bad regex"),
        ("record maybe", "record takes on or off"),
        ("resize wide", "resize wants COLSxROWS"),
        ("resize 5x2", "at least 10x3"),
        ("when 'x' include y", "when can't guard include"),
        ("type ${NOPE}", "no --var NOPE"),
        ("type 'unclosed", "No closing quotation"),
    ],
)
def test_bad_steps_name_the_line(tmp_path: Path, line: str, message: str):
    with pytest.raises(steplib.StepError) as err:
        steplib.parse(["sleep 0", line], "file.steps", tmp_path, {})
    assert "file.steps:2" in str(err.value)
    assert message in str(err.value)


def test_include_loop_is_refused(tmp_path: Path):
    (tmp_path / "a.steps").write_text("include b.steps\n", encoding="utf-8")
    (tmp_path / "b.steps").write_text("include a.steps\n", encoding="utf-8")
    with pytest.raises(steplib.StepError, match="loops back"):
        steplib.load(tmp_path / "a.steps", {})


# --- sessions ------------------------------------------------------------------------------------


@needs_tmux
def test_drives_a_prompt_and_snaps_the_screen(tmp_path: Path):
    done, out = record(
        tmp_path,
        "wait 'name\\?' 10\ntype 'world wide' 0.01\nkeys Enter\nwait 'hello world wide' 10\n"
        "snap greet\n",
        "sh",
        "-c",
        'stty size; printf "name? "; read n; echo "hello $n"; sleep 30',
        extra=("--size", "60x8", "--cast", str(tmp_path / "c.cast")),
    )
    assert done.returncode == 0, done.stderr
    greet = (out / "greet.txt").read_text(encoding="utf-8")
    assert greet.splitlines()[0] == "8 60"
    assert "hello world wide" in greet
    assert "--- greet" in done.stdout
    assert (out / "final.txt").exists()
    cast = (tmp_path / "c.cast").read_text(encoding="utf-8").splitlines()
    assert json.loads(cast[0]) == {"version": 2, "width": 60, "height": 8}
    times = [json.loads(line)[0] for line in cast[1:]]
    assert times and times == sorted(times)


@needs_tmux
def test_the_command_gets_a_minimal_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TR_SECRET", "leaked")
    monkeypatch.setenv("TR_PASSED", "passed")
    done, out = record(
        tmp_path,
        "wait 'env:' 10\nsnap env\n",
        "sh",
        "-c",
        'echo "env:[$TR_SECRET][$TR_PASSED][$TR_SET][$TERM]"; sleep 30',
        extra=("--pass", "TR_PASSED", "--env", "TR_SET=set"),
    )
    assert done.returncode == 0, done.stderr
    assert "env:[][passed][set][screen-256color]" in (out / "env.txt").read_text()


@needs_tmux
def test_click_sends_an_sgr_mouse_press(tmp_path: Path):
    # Reads the press in raw mode after turning on SGR mouse reporting, then prints it.
    target = (
        "import sys, tty\n"
        "sys.stdout.write('\\x1b[?1000h\\x1b[?1006h  [Accept] [Close]\\n'); sys.stdout.flush()\n"
        "tty.setraw(0)\n"
        "seq = b''\n"
        "while not seq.endswith(b'M'):\n"
        "    seq += sys.stdin.buffer.read(1)\n"
        "sys.stdout.write('\\r\\ngot ' + repr(seq) + '\\r\\n'); sys.stdout.flush()\n"
        "import time; time.sleep(30)\n"
    )
    done, out = record(
        tmp_path,
        "wait 'Accept' 10\nclick Close\nwait 'got' 10\nsnap clicked\n",
        sys.executable,
        "-c",
        target,
    )
    assert done.returncode == 0, done.stderr
    # "Close" starts at column 13 (1-based); the click lands one column in, on row 1.
    assert "\\x1b[<0;14;1M" in (out / "clicked.txt").read_text()


@needs_tmux
def test_resize_changes_the_terminal_and_lands_in_the_cast(tmp_path: Path):
    # Prints the size at start and on every SIGWINCH.
    done, out = record(
        tmp_path,
        "wait 'size 30 100' 10\nresize 60x12\nwait 'size 12 60' 10\nsnap narrow\n",
        "bash",
        "-c",
        "trap 'echo size $(stty size)' WINCH; echo size $(stty size); while :; do sleep 0.1; done",
        extra=("--cast", str(tmp_path / "c.cast")),
    )
    assert done.returncode == 0, done.stderr
    assert "size 12 60" in (out / "narrow.txt").read_text()
    events = [json.loads(line) for line in (tmp_path / "c.cast").read_text().splitlines()[1:]]
    assert [e[2] for e in events if e[1] == "r"] == ["60x12"]


@needs_tmux
def test_a_wait_that_times_out_fails_with_the_screen(tmp_path: Path):
    done, _ = record(tmp_path, "wait 'never shown' 1\n", "sh", "-c", "echo hi; sleep 30")
    assert done.returncode == 1
    assert "timed out after 1s waiting for /never shown/" in done.stderr
    assert "hi" in done.stderr


@needs_tmux
def test_a_command_that_exits_early_says_so(tmp_path: Path):
    done, _ = record(tmp_path, "wait 'never shown' 20\n", "sh", "-c", "echo bye; exit 3")
    assert done.returncode == 1
    assert "the command exited (status 3)" in done.stderr


@needs_tmux
def test_when_skips_a_step_whose_screen_does_not_match(tmp_path: Path):
    done, out = record(
        tmp_path,
        "wait 'ready' 10\nwhen 'trust' type nope 0\nwhen 'ready' type yes 0\nkeys Enter\n"
        "wait 'got yes' 10\n",
        "sh",
        "-c",
        'echo ready; read x; echo "got $x"; sleep 30',
    )
    assert done.returncode == 0, done.stderr
    assert "nope" not in (out / "final.txt").read_text()


@needs_tmux
def test_idle_waits_for_a_busy_marker_to_clear(tmp_path: Path):
    done, out = record(
        tmp_path,
        "wait 'working' 10\nidle 'working' 0.5 10\nsnap after\n",
        "sh",
        "-c",
        "printf 'working\\n'; sleep 1; printf '\\033[2J\\033[Hdone\\n'; sleep 30",
    )
    assert done.returncode == 0, done.stderr
    after = (out / "after.txt").read_text()
    assert "done" in after and "working" not in after


def test_usage_errors_exit_2():
    done = subprocess.run(
        [sys.executable, str(TERM_RECORD), "--size", "huge", "--", "true"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2
    assert "--size wants COLSxROWS" in done.stderr
    done = subprocess.run(
        [sys.executable, str(TERM_RECORD)], check=False, capture_output=True, text=True
    )
    assert done.returncode == 2
    assert "no command" in done.stderr
