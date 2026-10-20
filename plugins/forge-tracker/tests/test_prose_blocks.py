"""Blocking written only as prose: reading it from an issue body, and /groom proposing the
native link it should have been. Every forge here is fake; nothing can reach a real one."""

import json

import pytest
from forge_tracker import refs
from test_groom import CONFIG, Forge, Repo, issue, run_cli


@pytest.fixture
def env(tmp_path, repo_dir):
    return {
        "repo": repo_dir(CONFIG),
        "environ": {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")},
    }


# --- reading prose ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, found",
    [
        ("Blocked by: #17", [("blocked_by", None, 17)]),
        (
            "**Blocked by:** #17, #18 and acme/other#4",
            [
                ("blocked_by", None, 17),
                ("blocked_by", None, 18),
                ("blocked_by", "acme/other", 4),
            ],
        ),
        ("This is blocked by #17.", [("blocked_by", None, 17)]),
        ("blocked on #21 and #10", [("blocked_by", None, 21), ("blocked_by", None, 10)]),
        ("Depends on #5 (needs x) #6", [("blocked_by", None, 5)]),
        ("- Blocks #18", [("blocks", None, 18)]),
        ("- [ ] Blocking: #8", [("blocks", None, 8)]),
        ("Blocks: acme/other#4", [("blocks", "acme/other", 4)]),
        # Not claims about this issue.
        ("This fixes code blocks #3", []),
        ("It's no longer blocked by #17", []),
        ("isn't blocked by #17", []),
        ("> Blocked by #9", []),
        ("```\nBlocked by #9\n```", []),
        ("~~~md\nBlocked by #9\n~~~", []),
        ("see `Blocked by #9` in the template", []),
        ("~~Blocked by #7~~", []),
        ("<!-- Blocked by #3 -->", []),
        ("Blocked by #0", []),
        ("Blocked by the release", []),
    ],
)
def test_prose_blocks(body, found):
    assert [hit[:3] for hit in refs.prose_blocks(body)] == found


def test_prose_blocks_keeps_the_line_and_lists_a_reference_once():
    hits = refs.prose_blocks("Blocked by: #17\nAlso blocked by #17")
    assert hits == [("blocked_by", None, 17, "Blocked by: #17")]


# --- /groom: blocking written only as prose ------------------------------------------------------


def prose_repo():
    repo = Repo()
    bodies = {
        70: "Blocked by: #71",  # an open blocker, no native link: link-add
        72: "Waiting.\n\n**Blocked by:** #19",  # closed blocker: listed, not proposed
        74: "- Blocks #70",  # #70 waits on #74: link-add
        75: "Depends on #60",  # a pull request: unclear
        76: "```\nBlocked by #71\n```\n> Blocked by #71\nsee `Blocked by #71`",  # nothing
        77: "Blocked by acme/other#4",  # another project's blocker: link-add with blocker_project
        78: "Blocks acme/other#5",  # the blocked side is elsewhere: unclear
        79: "No longer blocked by #71. Blocked by #79.",  # negated, and a self-reference
    }
    for number, body in bodies.items():
        repo.issues.append(issue(number, f"issue {number}", body=body))
    repo.issues.append(issue(71, "the blocker"))
    repo.issues[[i["number"] for i in repo.issues].index(26)]["body"] = "Blocked by #19"
    repo.recent = repo.issues + [
        issue(19, "an old blocker", state="closed", closed_at="2026-10-09T00:00:00Z")
    ]
    return repo


def prose_forge(repo=None):
    world = (repo or prose_repo()).world()
    world.route(
        "/repos/acme/other/issues/4", {"id": 7004, "number": 4, "state": "open", "closed_at": None}
    )
    world.route("/repos/acme/widgets/issues/70/dependencies/blocked_by", [])
    world.route("/repos/acme/widgets/issues/77/dependencies/blocked_by", [])
    f = Forge(world)
    f.changes.add(60)
    return f


def test_groom_proposes_native_links_for_prose_blocking(env):
    code, text = run_cli(env, "groom", "--json", http=prose_forge())
    assert code == 0, text
    report = json.loads(text)
    prose = {p["id"]: p for p in report["proposals"] if p["check"] == "prose_blocks"}
    assert set(prose) == {
        "link-add #70 blocked by #71",
        "link-add #70 blocked by #74",
        "link-add #77 blocked by acme/other#4",
    }
    assert prose["link-add #70 blocked by #71"]["op"] == "link-add"
    assert prose["link-add #70 blocked by #71"]["payload"] == {
        "issue": 70,
        "blocker": 71,
        "repo": "acme/widgets",
    }
    assert prose["link-add #70 blocked by #71"]["evidence"] == (
        "#70's body says 'Blocked by: #71'; no native link, so /next doesn't see it"
    )
    assert prose["link-add #77 blocked by acme/other#4"]["payload"]["blocker_project"] == (
        "acme/other"
    )
    listed = [i["evidence"] for i in report["listed"] if i["check"] == "prose_blocks"]
    assert listed == [
        (
            "#72's body says '**Blocked by:** #19', but #19 closed 2026-10-09 and nothing is "
            "linked; the prose is stale"
        )
    ]
    unclear = [u["evidence"] for u in report["unclear"] if u["check"] == "prose_blocks"]
    assert unclear == [
        "#75's body says 'Depends on #60': #60 is a pull request; a blocking link joins two issues",
        "#78's body says 'Blocks acme/other#5': acme/other#5 is in another project; link it there",
    ]
    # #26 says it in prose too, but its native link exists: closed_blockers handles it alone.
    assert not any(p["payload"].get("issue") == 26 for p in prose.values())


def test_groom_with_prose_still_reads_only(env):
    f = prose_forge()
    run_cli(env, "groom", http=f)
    assert f.writes == []
    for method, url, _headers, _body in f.world.calls:
        assert method == "GET" or url == "https://api.github.com/graphql"


def test_a_blocker_that_cant_be_read_is_unclear_and_failed(env):
    repo = prose_repo()
    repo.issues.append(issue(80, "issue 80", body="Blocked by acme/gone#9"))
    f = prose_forge(repo)
    f.world.route("/repos/acme/gone/issues/9", {"message": "Not Found"}, status=404)
    code, text = run_cli(env, "groom", http=f)
    assert code == 1
    assert "#80's body says 'Blocked by acme/gone#9': acme/gone#9 FAILED (HTTP 404" in text
    assert "blocking written only as prose: FAILED (acme/gone#9: HTTP 404" in text
    assert "link-add #70 blocked by #71" in text  # the rest of the check still reports


def test_groom_apply_writes_an_approved_link(env):
    f = prose_forge()
    code, text = run_cli(
        env,
        "groom",
        "apply",
        http=f,
        stdin=json.dumps({"approved": ["link-add #77 blocked by acme/other#4"]}),
    )
    assert code == 0, text
    assert text == "link-add #77 blocked by acme/other#4: #77: now blocked by acme/other#4\n"
    assert f.writes == [
        ("POST", "/repos/acme/widgets/issues/77/dependencies/blocked_by", {"issue_id": 7004})
    ]


def test_a_native_link_makes_the_proposal_go_away(env):
    repo = prose_repo()
    world_links = repo.world().graphql["BlockingLinks"]

    def links(v):
        data = world_links(v)
        data["repository"]["issues"]["nodes"].append(
            {
                "number": 70,
                "blockedBy": {
                    "nodes": [
                        {
                            "number": 71,
                            "state": "OPEN",
                            "closedAt": None,
                            "repository": {"nameWithOwner": "acme/widgets"},
                        }
                    ]
                },
            }
        )
        return data

    f = prose_forge(repo)
    f.world.op("BlockingLinks", links)
    _, text = run_cli(env, "groom", http=f)
    assert "link-add #70 blocked by #71" not in text
    assert "link-add #70 blocked by #74" in text
