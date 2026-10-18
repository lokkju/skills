import dataclasses
import os
import stat

import pytest
from conftest import FakeRun
from forge_tracker import config, detect, state
from forge_tracker.model import ConfigError

# --- detection ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, host, project",
    [
        ("https://github.com/acme/widgets.git", "github.com", "acme/widgets"),
        ("https://github.com/acme/widgets", "github.com", "acme/widgets"),
        ("https://user@GitHub.com/acme/widgets/", "github.com", "acme/widgets"),
        ("git@github.com:acme/widgets.git", "github.com", "acme/widgets"),
        ("ssh://git@github.com:22/acme/widgets.git", "github.com", "acme/widgets"),
        ("git@gitlab.com:group/sub/project.git", "gitlab.com", "group/sub/project"),
        (
            "https://code.example.org/gitlab/group/project",
            "code.example.org",
            "gitlab/group/project",
        ),
    ],
)
def test_parse_remote(url, host, project):
    remote = detect.parse_remote(url)
    assert (remote.host, remote.project) == (host, project)


@pytest.mark.parametrize("url", ["", "widgets", "https://github.com/acme", "file:///tmp/x"])
def test_parse_remote_rejects(url):
    with pytest.raises(ConfigError):
        detect.parse_remote(url)


def test_remote_key_escapes_slashes():
    assert detect.Remote("gitlab.com", "group/sub/p").key == "gitlab.com_group__sub__p"


def test_detect_forge_order():
    gh = detect.Remote("github.com", "a/b")
    assert detect.detect_forge(gh, None) == "github"
    ghe = detect.Remote("git.corp.example", "a/b")
    assert detect.detect_forge(ghe, "github") == "github"
    with pytest.raises(ConfigError, match="set forge:"):
        detect.detect_forge(ghe, None)
    with pytest.raises(ConfigError, match="only GitHub so far"):
        detect.detect_forge(detect.Remote("gitlab.com", "a/b"), None)
    with pytest.raises(ConfigError, match="only GitHub so far"):
        detect.detect_forge(ghe, "forgejo")


# --- per-clone git config keys (tracker.forge, tracker.web) -------------------------------------


def _locate(configured=None, origin="git@github.com:acme/widgets.git", **keys):
    run = FakeRun(origin=origin, git_config=keys)
    return detect.locate("/repo", configured, run)


def test_no_keys_reads_the_origin_remote():
    remote, named, _ = _locate()
    assert (remote.host, remote.project, named) == ("github.com", "acme/widgets", None)


def test_tracker_web_replaces_the_remotes_host_and_path():
    # An SSH alias the built-in rules can't read, and a mirror's path.
    remote, named, _ = _locate(
        origin="gh-work:mirrors/widgets.git", **{"tracker.web": "https://github.com/acme/widgets"}
    )
    assert (remote.host, remote.project, named) == ("github.com", "acme/widgets", None)


def test_tracker_web_needs_no_origin_remote():
    remote, _, _ = _locate(origin=None, **{"tracker.web": "https://github.com/acme/widgets.git/"})
    assert remote.project == "acme/widgets"


def test_tracker_forge_wins_over_the_config_and_the_host():
    remote, named, source = _locate(
        "gitlab", origin="https://git.corp.example/a/b.git", **{"tracker.forge": "GitHub"}
    )
    assert named == "github" and source == "git config tracker.forge"
    assert detect.detect_forge(remote, named, source) == "github"
    # Without the git key, the config file's forge: is the default.
    _, named, source = _locate("github", origin="https://git.corp.example/a/b.git")
    assert named == "github" and source.startswith("forge: in ")


def test_tracker_forge_names_its_source_when_unsupported():
    remote, named, source = _locate(**{"tracker.forge": "gitea"})
    assert named == "forgejo"
    with pytest.raises(ConfigError, match=r"is forgejo \(by git config tracker.forge\)"):
        detect.detect_forge(remote, named, source)


def test_tracker_web_path_is_split_by_kind():
    web = "https://code.example.org/forge/group/sub/project"
    # GitLab: the whole path; GitHub and Forgejo: the last two segments, the rest a subpath.
    assert detect.web_remote(web, "gitlab").project == "forge/group/sub/project"
    assert detect.web_remote(web, "forgejo").project == "sub/project"
    remote, _, _ = _locate(**{"tracker.web": web, "tracker.forge": "gitlab"})
    assert remote.project == "forge/group/sub/project"


def test_tracker_web_on_an_unknown_host_needs_a_kind():
    with pytest.raises(ConfigError, match="git config tracker.forge <kind>"):
        _locate(**{"tracker.web": "https://git.corp.example/a/b"})
    remote, named, _ = _locate("github", **{"tracker.web": "https://git.corp.example/a/b"})
    assert (remote.host, named) == ("git.corp.example", "github")


@pytest.mark.parametrize(
    "keys, message",
    [
        ({"tracker.forge": "bitbucket"}, "tracker.forge is 'bitbucket'"),
        ({"tracker.web": "http://github.com/a/b"}, "isn't the https URL"),
        ({"tracker.web": "https://github.com/a"}, "isn't the https URL"),
        ({"tracker.web": "https://user@github.com/a/b"}, "isn't the https URL"),
        ({"tracker.web": "https://github.com/a/b?tab=issues"}, "isn't the https URL"),
        ({"tracker.web": "https://github.com/a/../b"}, "isn't the https URL"),
        ({"tracker.web": "git@github.com:a/b.git"}, "isn't the https URL"),
    ],
)
def test_unusable_keys_fail_naming_the_key(keys, message):
    with pytest.raises(ConfigError, match=message):
        _locate(**keys)


# --- config ------------------------------------------------------------------------------------

SPEC_EXAMPLE = """---
forge: github                       # only when detection can't tell
api_base: https://code.example.org/api/v3
ca_file: /etc/ssl/example-root.pem  # Forgejo; GitLab uses glab's ca_cert
labels:
  needs_human: needs-human
  decision: decision
  decision_made: decided
  blocked: blocked
  agent: agent-ready
tiers:                              # first match wins; milestone prefixes are the fallback
  labels: [P0, P1, P2, P3]
  milestone_prefixes: ["Tier 0", "Tier 1", "Tier 2"]
person_only_markers: [hardware, legal, purchase]
blocking: relates_to+blocked        # GitLab CE convention; native where the forge has it
ruling_pattern: '^Ruling \\((\\d{4}-\\d{2}-\\d{2})\\):'
changelog: CHANGELOG.md
report_tags: ["v*", "registry:app"]
stale_days: 14
write_wrapper: ./scripts/tracker-write
---
Releases are cut by yeet; don't file issues for release PRs.
"""


def test_spec_example_loads(repo_dir):
    cfg = config.load(repo_dir(SPEC_EXAMPLE))
    assert cfg.forge == "github"
    assert cfg.api_base == "https://code.example.org/api/v3"
    assert cfg.label("needs_human") == "needs-human" and cfg.label("decision_made") == "decided"
    assert cfg.tier_labels == ("P0", "P1", "P2", "P3")
    assert cfg.milestone_prefixes == ("Tier 0", "Tier 1", "Tier 2")
    assert cfg.person_only_markers == ("hardware", "legal", "purchase")
    assert cfg.blocking == "relates_to+blocked"
    assert cfg.ruling_pattern.search("Ruling (2026-10-15): yes").group(1) == "2026-10-15"
    assert cfg.report_tags == ("v*", "registry:app")
    assert cfg.stale_days == 14
    assert cfg.write_wrapper == "./scripts/tracker-write"


def test_missing_file_and_no_frontmatter_are_defaults(repo_dir):
    assert config.load(repo_dir()) == config.Config()
    assert config.load(repo_dir("just notes\n")) == config.Config(found=True)


def test_tilde_and_null_record_none(repo_dir):
    cfg = config.load(
        repo_dir(
            "---\nlabels:\n  needs_human: ~\n  decision: null\n  blocked: 'null'\n"
            "ruling_pattern: ~\n---\n"
        )
    )
    assert cfg.found
    assert cfg.label("needs_human") is None and cfg.label("decision") is None
    assert cfg.label("blocked") == "null"  # quoted, so a name
    assert cfg.set_to_none == {"labels.needs_human", "labels.decision", "ruling_pattern"}
    keys = ("labels.needs_human", "labels.decision", "ruling_pattern")
    # Labels at ~ don't exist yet, so the line offers to create them until setup is declined.
    note = cfg.setup_note(keys)
    assert note.startswith("setup: labels.needs_human and labels.decision set to ~ in ")
    assert "ruling_pattern" not in note
    assert dataclasses.replace(cfg, setup_declined="2026-10-16").setup_note(keys) is None
    assert config.from_mapping({"ruling_pattern": None}).setup_note(("ruling_pattern",)) is None
    assert config.Config(found=True).setup_note(("labels.blocked",)) is not None


def test_block_lists_and_quotes(repo_dir):
    cfg = config.load(
        repo_dir("---\nreport_tags:\n  - 'v*'\n  - \"rel-#1\"\nchangelog: 'it''s.md'\n---\n")
    )
    assert cfg.report_tags == ("v*", "rel-#1")
    assert cfg.changelog == "it's.md"


def test_tier_lookup():
    cfg = config.Config(tier_labels=("P0", "P1"), milestone_prefixes=("Tier 1",))
    assert cfg.tier(("bug", "P1"), "Tier 1: later") == "P1"
    assert cfg.tier(("bug",), "Tier 1: later") == "Tier 1"
    assert cfg.tier(("bug",), None) is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("---\nlabel: x\n---\n", "unknown key label"),
        ("---\nlabels:\n  needs: x\n---\n", "unknown key labels.needs"),
        ("---\ntiers:\n  label: [P0]\n---\n", "unknown key tiers.label"),
        ("---\nforge: bitbucket\n---\n", "forge must be one of"),
        ("---\nchangelog: 'open\n---\n", "unterminated"),
        ("---\nruling_pattern: '(('\n---\n", "valid regex"),
        ("---\nstale_days: soon\n---\n", "positive integer"),
        ("---\nreport_tags: v*\n---\n", "list of strings"),
        ("---\n  forge: github\n---\n", "unexpected indentation"),
        ("---\nforge github\n---\n", "expected 'key: value'"),
        ("---\nforge: github\nforge: gitlab\n---\n", "set twice"),
        ("---\nforge: github\n", "no closing ---"),
        ("---\nlabels: {a: b}\n---\n", "isn't supported"),
        ("---\ntiers:\n  labels: [[P0]]\n---\n", "nested"),
    ],
)
def test_bad_config_fails_naming_the_problem(repo_dir, text, message):
    with pytest.raises(ConfigError, match=message):
        config.load(repo_dir(text))


# --- state -------------------------------------------------------------------------------------


def test_cache_root_ignores_relative_xdg(tmp_path):
    home = str(tmp_path)
    assert state.cache_root({"HOME": home}) == os.path.join(home, ".cache", "forge-tracker")
    assert state.cache_root({"HOME": home, "XDG_CACHE_HOME": "rel/cache"}) == os.path.join(
        home, ".cache", "forge-tracker"
    )
    assert state.cache_root({"HOME": home, "XDG_CACHE_HOME": "/x/c"}) == "/x/c/forge-tracker"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_pending_write_is_0600_in_0700_dirs_and_atomic(tmp_path):
    old = os.umask(0o022)
    try:
        target = tmp_path / "cache" / "forge-tracker" / "catchup" / "m.json"
        pending = state.PendingWrite(str(target), {"end": "new"})
        assert not target.exists()
        assert mode(tmp_path / "cache") == 0o700
        assert mode(target.parent) == 0o700
        assert mode(pending.temp) == 0o600
        pending.commit()
        assert state.read_json(str(target)) == {"end": "new"}
        assert mode(target) == 0o600
        assert os.listdir(target.parent) == ["m.json"]
    finally:
        os.umask(old)


def test_pending_write_discard_keeps_the_old_file(tmp_path):
    target = tmp_path / "m.json"
    state.write_json(str(target), {"end": "old"})
    pending = state.PendingWrite(str(target), {"end": "new"})
    pending.discard()
    assert state.read_json(str(target)) == {"end": "old"}
    assert os.listdir(tmp_path) == ["m.json"]


def test_read_json_tolerates_missing_and_corrupt(tmp_path):
    assert state.read_json(str(tmp_path / "nope.json")) is None
    (tmp_path / "bad.json").write_text("{")
    assert state.read_json(str(tmp_path / "bad.json")) is None
