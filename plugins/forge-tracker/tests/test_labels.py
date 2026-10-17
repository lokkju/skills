import io
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from conftest import FakeRun, World, response
from forge_tracker import cli, config, labelsetup
from forge_tracker.detect import Remote
from forge_tracker.model import Label

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
REMOTE = Remote("github.com", "acme/widgets")


def labels(*names):
    return [Label(n) for n in names]


def roles(proposal):
    return {role: entry["label"] for role, entry in proposal["roles"].items()}


# --- matching ---------------------------------------------------------------------------------


def test_common_names_map_to_roles():
    p = labelsetup.suggest(
        labels("bug", "needs-human", "decision", "decided", "blocked", "agent-ready", "P1", "P0"),
        config.Config(),
    )
    assert roles(p) == {
        "needs_human": "needs-human",
        "decision": "decision",
        "decision_made": "decided",
        "blocked": "blocked",
        "agent": "agent-ready",
    }
    assert p["tiers"]["labels"] == ["P0", "P1"]
    assert p["missing"] == []


def test_variants_and_separators_are_normalized():
    p = labelsetup.suggest(
        labels(
            "Needs Human",
            "question",
            "status: blocked",
            "AI-ready",
            "priority:high",
            "priority:low",
            "priority:critical",
        ),
        config.Config(),
    )
    assert roles(p)["needs_human"] == "Needs Human"
    assert roles(p)["decision"] == "question"
    assert roles(p)["blocked"] == "status: blocked"
    assert roles(p)["agent"] == "AI-ready"
    assert p["tiers"]["labels"] == ["priority:critical", "priority:high", "priority:low"]


def test_preference_order_beats_label_order():
    p = labelsetup.suggest(labels("question", "needs-decision", "human"), config.Config())
    assert roles(p)["decision"] == "needs-decision"
    assert roles(p)["needs_human"] == "human"


def test_decision_made_isnt_taken_for_decision():
    p = labelsetup.suggest(labels("decision-made"), config.Config())
    assert roles(p)["decision"] is None
    assert roles(p)["decision_made"] == "decision-made"


def test_the_biggest_tier_family_wins():
    p = labelsetup.suggest(
        labels("P1", "priority: 0", "priority: 1", "priority: 2"), config.Config()
    )
    assert p["tiers"]["labels"] == ["priority: 0", "priority: 1", "priority: 2"]


def test_nothing_matched_proposes_none_and_names_to_create():
    p = labelsetup.suggest(labels("bug", "enhancement", "documentation"), config.Config())
    assert set(roles(p).values()) == {None}
    assert p["tiers"]["labels"] == []
    assert [m["name"] for m in p["missing"]] == [
        "needs-human",
        "decision",
        "decided",
        "blocked",
        "agent-ready",
    ]


def test_existing_config_wins_and_tilde_stays():
    cfg = config.from_mapping(
        {"labels": {"needs_human": "triage", "agent": None}, "tiers": {"labels": ["hi", "lo"]}}
    )
    p = labelsetup.suggest(labels("needs-human", "agent", "blocked", "P0"), cfg)
    assert roles(p)["needs_human"] == "triage"
    assert roles(p)["agent"] is None and p["roles"]["agent"]["source"] == "config"
    assert roles(p)["blocked"] == "blocked"
    assert p["tiers"]["labels"] == ["hi", "lo"]
    # ~ means no such label yet, so it's still one to create; a mapped role never is.
    assert [m["key"] for m in p["missing"]] == ["decision", "decision_made", "agent"]


# --- the proposed file --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["needs-human", "Needs Human", "a#b", "it's", "~", "null", "12", "x, y", " pad", "-x"]
)
def test_label_names_round_trip_through_the_parser(name):
    text = f"---\nlabels:\n  blocked: {labelsetup.yaml_str(name)}\n---\n"
    assert config.from_mapping(config.parse_frontmatter(text)).label("blocked") == name


def test_proposed_file_loads_and_silences_the_setup_notes(repo_dir):
    data = labelsetup.report(
        labels("needs-human", "question", "P2", "P1", "it's, odd"), config.Config(), REMOTE, NOW
    )
    content = data["content"]
    assert content.startswith("---\nlabels:\n  needs_human: needs-human  # matched")
    assert "  decision_made: ~  # no such label here; ~ records that\n" in content
    assert "tiers:\n  labels: [P1, P2]  # matched an existing label\n" in content
    assert "ruling_pattern: ~" in content
    assert "on 2026-10-16" in content
    cfg = config.load(repo_dir(content))
    assert cfg.label("needs_human") == "needs-human" and cfg.label("decision") == "question"
    assert cfg.tier_labels == ("P1", "P2")
    keys = ("labels.needs_human", "labels.decision", "labels.blocked", "ruling_pattern")
    # Nothing is unset; the roles at ~ are offered for creation until setup is declined.
    assert cfg.setup_note(keys).startswith(
        "setup: labels.decision_made, labels.blocked and labels.agent set to ~ in "
    )
    assert replace(cfg, setup_declined="2026-10-16").setup_note(keys) is None


def test_existing_file_gets_only_the_missing_keys():
    cfg = config.Config(found=True, labels={"needs_human": "nh"}, tier_labels=("P0",))
    data = labelsetup.report(labels("decision", "nh", "P0"), cfg, REMOTE, NOW)
    content = data["content"]
    assert not content.startswith("---")
    assert "needs_human" not in content and "tiers" not in content
    assert "  decision: decision  # matched an existing label" in content
    text = labelsetup.render(data)
    assert ".claude/forge-tracker.local.md exists. Add these keys" in text


def test_create_commands_name_the_repo():
    data = labelsetup.report(labels("bug"), config.Config(), REMOTE, NOW)
    commands = [m["command"] for m in data["proposal"]["missing"]]
    assert commands[0] == (
        'gh label create needs-human -R acme/widgets --color d93f0b --description "Needs a '
        'person to act; agents leave it alone"'
    )
    assert "  forge-tracker labels create needs-human decision decided blocked agent-ready" in (
        labelsetup.render(data)
    )
    enterprise = labelsetup.report(labels(), config.Config(), Remote("ghe.example", "a/b"), NOW)
    assert "-R ghe.example/a/b " in enterprise["proposal"]["missing"][0]["command"]


# --- the CLI ------------------------------------------------------------------------------------


def label_world(names, pages=1):
    w = World()
    payload = [{"name": n, "description": "", "color": "ededed"} for n in names]
    w.route("/repos/acme/widgets/labels", lambda url: response(payload))
    return w


def run(argv, repo, http=None, environ=None, fake=None):
    out = io.StringIO()
    code = cli.main(
        ["--repo", repo, *argv],
        http=http,
        run=fake or FakeRun(),
        now=NOW,
        environ=environ or {},
        out=out,
    )
    return code, out.getvalue()


def test_suggest_reads_labels_with_get_and_writes_nothing(tmp_path):
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    w = label_world(["needs-human", "decision", "bug", "P0", "P1"])
    environ = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    code, text = run(["labels", "suggest"], repo, http=w, environ=environ)
    assert code == 0
    assert {c[0] for c in w.calls} == {"GET"}
    assert text.startswith("acme/widgets on github.com: 5 labels\n\nProposed ")
    assert "  needs_human: needs-human  # matched an existing label" in text
    assert "gh label create blocked -R acme/widgets" in text
    assert os.listdir(repo) == []
    assert not (tmp_path / "cache").exists()


def test_suggest_json(tmp_path):
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    code, text = run(["labels", "suggest", "--json"], repo, http=label_world(["blocked"]))
    assert code == 0
    data = json.loads(text)
    assert data["labels_read"] == ["blocked"]
    assert data["proposal"]["roles"]["blocked"] == {"label": "blocked", "source": "matched"}
    assert data["config_exists"] is False
    assert data["content"].startswith("---\n")


def test_suggest_fails_plainly_when_the_read_fails(tmp_path):
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    w = World().route("/repos/acme/widgets/labels", {"message": "nope"}, status=500)
    code, text = run(["labels", "suggest"], repo, http=w)
    assert code == 1
    assert text.startswith("labels: FAILED (")


def test_decline_writes_one_private_cache_file_and_reads_no_forge(tmp_path):
    repo = str(tmp_path / "repo")
    os.mkdir(repo)
    environ = {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    fake = FakeRun(token="")
    del fake.answers[("gh", "auth", "token")]  # any gh call would fail the test
    code, _text = run(["labels", "decline"], repo, environ=environ, fake=fake)
    assert code == 0
    path = tmp_path / "cache" / "forge-tracker" / "setup" / "github.com_acme__widgets.json"
    assert json.loads(path.read_text())["declined"] == "2026-10-16"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.listdir(repo) == []
    cfg = labelsetup.with_decline(config.Config(), REMOTE, environ)
    assert cfg.setup_declined == "2026-10-16"
    assert "Setup was declined on 2026-10-16" in cfg.setup_note(("labels.needs_human",))


# --- labels create (#76) ------------------------------------------------------------------------

SETUP = """---
labels:
  needs_human: ~
  decision: question
  decision_made: ~
  blocked: ~
  agent: ~
---
"""


def wrapped(text):
    return text.replace("---\n", "---\nwrite_wrapper: ./w\n", 1)


class LabelForge:
    """The label list for reads, and a recorder for writes, which never reach the World."""

    def __init__(self, names):
        self.world = label_world(names)
        self.writes = []

    @property
    def calls(self):
        return self.world.calls

    def __call__(self, method, url, headers, body=None):
        if method in ("POST", "PATCH", "DELETE"):
            path = url.split("api.github.com", 1)[1]
            self.writes.append((method, path, json.loads(body) if body else None))
            return response({})
        return self.world(method, url, headers, body)


@pytest.fixture
def setup_env(tmp_path, repo_dir):
    return {
        "repo": repo_dir(SETUP),
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")},
        "log": tmp_path / "cache" / "forge-tracker" / "writes" / "github.com_acme__widgets.jsonl",
    }


def create(env, *names, forge=None, fake=None, json_out=False):
    forge = forge or LabelForge(["bug", "question"])
    argv = ["labels", "create", *names] + (["--json"] if json_out else [])
    code, text = run(argv, env["repo"], http=forge, environ=env["environ"], fake=fake)
    return code, text, forge


def write_op(env, payload, forge, fake=None):
    out = io.StringIO()
    code = cli.main(
        ["--repo", env["repo"], "write", "label-create"],
        http=forge,
        run=fake or FakeRun(),
        now=NOW,
        environ=env["environ"],
        out=out,
        stdin=io.StringIO(json.dumps(payload)),
    )
    return code, out.getvalue()


def test_create_makes_each_approved_label_and_prints_the_config_lines(setup_env):
    code, text, forge = create(setup_env, "needs-human", "agent=ready-for-agent", "blocked")
    assert code == 0, text
    assert forge.writes == [
        (
            "POST",
            "/repos/acme/widgets/labels",
            {
                "name": "needs-human",
                "color": "d93f0b",
                "description": "Needs a person to act; agents leave it alone",
            },
        ),
        (
            "POST",
            "/repos/acme/widgets/labels",
            {
                "name": "ready-for-agent",
                "color": "1d76db",
                "description": "Ready for an agent to pick up",
            },
        ),
        (
            "POST",
            "/repos/acme/widgets/labels",
            {
                "name": "blocked",
                "color": "b60205",
                "description": "Can't start until something else lands",
            },
        ),
    ]
    assert text == (
        "needs-human: created label needs-human (#d93f0b)\n"
        "ready-for-agent: created label ready-for-agent (#1d76db)\n"
        "blocked: created label blocked (#b60205)\n"
        "Put these in .claude/forge-tracker.local.md in place of their ~:\n"
        "  labels.needs_human: needs-human\n"
        "  labels.agent: ready-for-agent\n"
        "  labels.blocked: blocked\n"
    )
    entries = [json.loads(line) for line in setup_env["log"].read_text().splitlines()]
    assert [(e["op"], e["status"], e["via"]) for e in entries] == [
        ("label-create", "written", "forge")
    ] * 3
    assert stat.S_IMODE(setup_env["log"].stat().st_mode) == 0o600


def test_create_json_reports_what_was_created(setup_env):
    code, text, _ = create(setup_env, "decided", json_out=True)
    assert code == 0
    data = json.loads(text)
    assert data["config"] == {"decision_made": "decided"}
    assert data["results"][0]["status"] == "written"
    assert data["results"][0]["color"] == "0e8a16"


def test_with_a_wrapper_create_runs_it_and_sends_nothing(setup_env, repo_dir):
    env = dict(setup_env, repo=repo_dir(wrapped(SETUP)))
    fake = FakeRun()
    fake.answer(["./w"], 0, "made by policy\n", "")
    code, text, forge = create(env, "needs-human", fake=fake)
    assert code == 0, text
    assert forge.writes == []
    assert text.startswith("needs-human: write-wrapper did it: made by policy\n")
    assert "  labels.needs_human: needs-human\n" in text
    calls = [(argv, kw) for argv, kw in zip(fake.calls, fake.kwargs) if argv[0] == "./w"]
    assert [argv for argv, _ in calls] == [["./w", "label-create"]]
    assert json.loads(calls[0][1]["input"]) == {
        "name": "needs-human",
        "color": "d93f0b",
        "description": "Needs a person to act; agents leave it alone",
        "repo": "acme/widgets",
    }


def test_a_wrapper_refusal_creates_nothing_and_sets_no_config(setup_env, repo_dir):
    env = dict(setup_env, repo=repo_dir(wrapped(SETUP)))
    fake = FakeRun()
    fake.answer(["./w"], 1, "", "labels are managed in terraform")
    code, text, forge = create(env, "blocked", fake=fake)
    assert code == 1
    assert text == "blocked: REFUSED (write-wrapper refused: labels are managed in terraform)\n"
    assert forge.writes == []


def test_nothing_is_created_without_the_approved_names(setup_env):
    """Setup reads; only `labels create` with names given writes, and nothing else does."""
    forge = LabelForge(["bug", "question"])
    bare = os.path.join(os.path.dirname(setup_env["repo"]), "bare")
    os.mkdir(bare)
    code, text = run(["labels", "suggest"], bare, http=forge, environ=setup_env["environ"])
    assert code == 0
    assert "  forge-tracker labels create needs-human decided blocked agent-ready\n" in text
    code, _, _ = create(setup_env, forge=forge)
    assert code == 2
    assert forge.writes == []
    assert {c[0] for c in forge.calls} == {"GET"}
    assert not setup_env["log"].exists()


@pytest.mark.parametrize(
    "names, message",
    [
        (["Needs-Human"], "the repo already has 'needs-human'"),
        (["decision"], "labels.decision is already 'question'"),
        (["blocked", "blocked=other"], "labels.blocked is named twice"),
        (["needs-human", "agent=NEEDS-HUMAN"], "'NEEDS-HUMAN' is named twice"),
        (["triage"], "'triage': not a default name"),
        (["owner=x"], "'owner=x': not a default name"),
        (["agent="], "'agent=': the name is empty"),
    ],
)
def test_create_refuses_names_that_dont_fit_and_writes_nothing(setup_env, capsys, names, message):
    forge = LabelForge(["bug", "question", "needs-human"])
    code, _, forge = create(setup_env, *names, forge=forge)
    assert code == 2
    assert message in capsys.readouterr().err
    assert forge.writes == []


def test_the_write_op_refuses_an_existing_name_without_regard_to_case(setup_env, repo_dir):
    for config_text in (SETUP, wrapped(SETUP)):
        env = dict(setup_env, repo=repo_dir(config_text))
        forge = LabelForge(["Bug"])
        # FakeRun has no answer for ./w, so running the wrapper would fail the test.
        code, text = write_op(env, {"name": "bug", "color": "ededed"}, forge)
        assert code == 2
        assert "a label named 'Bug' already exists (names compare without case)" in text
        assert forge.writes == []


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"name": "x", "color": "red"}, "color must be six hex digits"),
        ({"name": " x", "color": "ededed"}, "no outer spaces"),
        ({"name": "x" * 51, "color": "ededed"}, "at most 50 characters"),
        ({"name": "x", "color": "ededed", "description": "d" * 101}, "at most 100"),
        ({"color": "ededed"}, "name must be a non-empty string"),
    ],
)
def test_the_write_op_validates_before_reading_or_sending(setup_env, payload, message):
    forge = LabelForge([])
    code, text = write_op(setup_env, payload, forge)
    assert code == 2
    assert message in text
    assert forge.writes == []
    assert not any("/labels" in c[1] for c in forge.calls)


SKILLS = ["bug", "documentation", "enhancement", "question", "no-release"]

SUGGEST_ON_TILDES = """\
acme/widgets on github.com: 5 labels

.claude/forge-tracker.local.md already sets every key this proposes; nothing to add.

Not on the forge (labels.needs_human, labels.decision_made, labels.blocked, labels.agent at ~). With the user's approval, create them with
  forge-tracker labels create needs-human decided blocked agent-ready
(through the write-wrapper when one is set), or the user can run:
  gh label create needs-human -R acme/widgets --color d93f0b --description "Needs a person to act; agents leave it alone"
  gh label create decided -R acme/widgets --color 0e8a16 --description "The decision is recorded in a comment"
  gh label create blocked -R acme/widgets --color b60205 --description "Can't start until something else lands"
  gh label create agent-ready -R acme/widgets --color 1d76db --description "Ready for an agent to pick up"
and then put each name in place of its ~.
"""


def test_an_existing_config_with_tilde_roles_is_still_offered_creation(setup_env, repo_dir):
    """A config written from an earlier suggest, with the unmatched roles at ~ (#76)."""
    setup_env = dict(
        setup_env, repo=repo_dir(SETUP.replace("---\n", "---\nruling_pattern: ~\n", 1))
    )
    forge = LabelForge(SKILLS)
    code, text = run(
        ["labels", "suggest"], setup_env["repo"], http=forge, environ=setup_env["environ"]
    )
    assert code == 0
    assert text == SUGGEST_ON_TILDES
    code, text, forge = create(
        setup_env, "needs-human", "decided", "blocked", "agent-ready", forge=forge
    )
    assert code == 0, text
    assert [w[2]["name"] for w in forge.writes] == [
        "needs-human",
        "decided",
        "blocked",
        "agent-ready",
    ]
    assert text.endswith(
        "Put these in .claude/forge-tracker.local.md in place of their ~:\n"
        "  labels.needs_human: needs-human\n"
        "  labels.decision_made: decided\n"
        "  labels.blocked: blocked\n"
        "  labels.agent: agent-ready\n"
    )


def test_tilde_roles_get_a_setup_line_until_declined(setup_env):
    cfg = config.load(setup_env["repo"])
    note = cfg.setup_note(("labels.needs_human", "labels.decision"))
    assert note == (
        "setup: labels.needs_human, labels.decision_made, labels.blocked and labels.agent set to "
        "~ in .claude/forge-tracker.local.md, so no label marks those roles yet. `forge-tracker "
        "labels suggest` lists the labels it can create for them"
    )
    assert replace(cfg, setup_declined="2026-10-16").setup_note(("labels.needs_human",)) is None


def test_record_is_refused_for_labels_create(setup_env):
    forge = LabelForge([])
    code, _ = run(
        ["--record", str(setup_env["log"]) + ".rec", "labels", "create", "blocked"],
        setup_env["repo"],
        http=forge,
        environ=setup_env["environ"],
    )
    assert code == 2
    assert forge.writes == []
