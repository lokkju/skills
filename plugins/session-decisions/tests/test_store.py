import json
import threading

from session_decisions import paths, store
from session_decisions.subject import Draft


def write_task(directory, task_id, subject, status="pending", description=""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{task_id}.json").write_text(
        json.dumps({"id": str(task_id), "subject": subject, "description": description,
                    "status": status, "blocks": [], "blockedBy": []})
    )


def test_read_task_items_filters_and_tolerates_junk(tmp_path):
    tasks = tmp_path / "tasks"
    write_task(tasks, 1, "D1 DECIDE: x (recommend y) [no link]")
    write_task(tasks, 2, "Run the migration")
    (tasks / "3.json").write_text("{not json")
    (tasks / "4.json").write_text("[]")
    assert [i.label for i in store.read_task_items(tasks)] == ["D1"]


def test_task_ruling_read_from_description(tmp_path):
    tasks = tmp_path / "tasks"
    write_task(tasks, 1, "D1 DECIDE: x (recommend y) [no link]", status="completed",
               description="context\nRuling (2026-10-01): yes")
    (item,) = store.read_task_items(tasks)
    assert item.ruling == "Ruling (2026-10-01): yes" and not item.is_open


def test_missing_tasks_dir_is_empty(tmp_path):
    assert store.read_task_items(tmp_path / "nope") == []


def test_assign_label_counts_per_kind(tmp_path):
    state_file = store.SessionState(tmp_path, "s1")
    with state_file.locked() as state:
        assert store.assign_label(state, "DECIDE", []) == "D1"
        assert store.assign_label(state, "ACTION", []) == "A1"
        assert store.assign_label(state, "DECIDE", []) == "D2"
    assert state_file.load()["counters"] == {"D": 2, "A": 1}


def test_assign_label_skips_past_existing_items(tmp_path):
    tasks = tmp_path / "tasks"
    write_task(tasks, 9, "D7 DECIDE: x (recommend y) [no link]")
    state_file = store.SessionState(tmp_path / "data", "s1")
    with state_file.locked() as state:
        assert store.assign_label(state, "DECIDE", store.all_items(tasks, state)) == "D8"


def test_counter_never_reuses_after_task_deleted(tmp_path):
    state_file = store.SessionState(tmp_path, "s1")
    with state_file.locked() as state:
        store.assign_label(state, "DECIDE", [])
    with state_file.locked() as state:
        assert store.assign_label(state, "DECIDE", []) == "D2"


def test_corrupt_state_file_starts_fresh(tmp_path):
    state_file = store.SessionState(tmp_path, "s1")
    state_file.path.parent.mkdir(parents=True)
    state_file.path.write_text("garbage")
    assert state_file.load()["counters"] == {"D": 0, "A": 0}


def test_concurrent_assignments_are_unique(tmp_path):
    state_file = store.SessionState(tmp_path, "s1")
    labels, guard = [], threading.Lock()

    def work():
        with state_file.locked() as state:
            label = store.assign_label(state, "DECIDE", [])
        with guard:
            labels.append(label)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(labels, key=lambda label: int(label[1:])) == [f"D{n}" for n in range(1, 9)]


def test_ledger_add_and_close(tmp_path):
    state_file = store.SessionState(tmp_path, "s1")
    with state_file.locked() as state:
        item = store.ledger_add(state, Draft("DECIDE", "x? (recommend y) [no link]"), "context", [])
    assert item.label == "D1" and item.is_open and item.source == "ledger"
    with state_file.locked() as state:
        assert store.ledger_close(state, "D1", "yes, go") is True
        assert store.ledger_close(state, "D9", "no") is False
    (closed,) = store.ledger_items(state_file.load())
    assert closed.status == "completed"
    assert closed.ruling == f"Ruling ({store.today()}): yes, go"


def test_all_items_merges_sources_in_creation_order(tmp_path):
    tasks = tmp_path / "tasks"
    state_file = store.SessionState(tmp_path / "data", "s1")
    with state_file.locked() as state:
        label = store.assign_label(state, "DECIDE", [])
        state["created"][label] = "2026-10-01T01:00:00Z"
        store.ledger_add(state, Draft("ACTION", "enable it [no link]"), "", [])
        state["created"]["A1"] = "2026-10-01T02:00:00Z"
        state["ledger"][0]["created"] = "2026-10-01T02:00:00Z"
    write_task(tasks, 1, f"{label} DECIDE: x (recommend y) [no link]")
    items = store.all_items(tasks, state_file.load())
    assert [(i.label, i.source) for i in items] == [("D1", "tasks"), ("A1", "ledger")]


def test_session_id_order_and_safety():
    assert paths.session_id({"CLAUDE_CODE_SESSION_ID": "b"}, "a") == "a"
    assert paths.session_id({"CLAUDE_CODE_SESSION_ID": "b"}, "") == "b"
    assert paths.session_id({"CLAUDE_SESSION_ID": "c", "CLAUDE_CODE_SESSION_ID": "b"}) == "c"
    assert paths.session_id({}, "../etc") is None
    assert paths.session_id({}) is None


def test_task_list_id_overrides_session_dir(env_for):
    env = env_for(CLAUDE_CODE_TASK_LIST_ID="shared")
    assert paths.tasks_dir(env, "s1").name == "shared"
    assert paths.tasks_dir(env_for(), "s1").name == "s1"


def test_data_dir_resolution(env_for, tmp_path):
    env = env_for()
    assert paths.data_dir(env, "/x") == paths.Path("/x")
    assert paths.data_dir(env) == tmp_path / "data"
    no_plugin_data = {k: v for k, v in env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert paths.data_dir(no_plugin_data) == tmp_path / "state" / "session-decisions"
    paths.write_pointer(env, tmp_path / "data")
    assert paths.data_dir(no_plugin_data) == tmp_path / "data"
