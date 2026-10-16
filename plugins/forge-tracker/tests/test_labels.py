import io
import json
import os
import stat
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
    assert "agent" not in [m["key"] for m in p["missing"]]


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
    assert cfg.setup_note(keys) is None


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
        'gh label create needs-human -R acme/widgets --description "Needs a person to act; '
        'agents leave it alone"'
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
