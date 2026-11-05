import pytest
from forge_tracker import refs

# lokkju/skills#65's title, which /catchup filed under #300 (#96).
PR_65 = "fix: point the hand-off nudge at deferred TaskCreate (haiku is Haiku 5.5 since 2.1.300)"


@pytest.mark.parametrize(
    "branch, numbers",
    [
        # Clearly an issue number.
        ("21-github-adapter", {21}),
        ("fix/21", {21}),
        ("fix/12-x", {12}),
        ("sd-26", {26}),
        ("fix-42", {42}),
        ("loki/issue-58-in-flight", {58}),
        ("gh-45-branch-parsing", {45}),
        ("lokkju/revert-107", {107}),
        # Versions, run ids and dates from lokkju/infra's merged branches (#45).
        ("garm-runner-2.338.0-38790455020", set()),
        ("garm-runner-2.337.0-37440948656", set()),
        ("cilium-1.20.2", set()),
        ("cilium-1.19.8", set()),
        ("chore/mcp-searxng-2.6.0", set()),
        ("lokkju/k3s-1.36-followups", set()),
        ("lokkju/k3s-1.34.12", set()),
        ("lokkju/cloudflare-6.22", set()),
        ("kubectl-1.35", set()),
        ("cert-manager-v1.21.2", set()),
        ("agent-runner-1.18.35-5e5bdad-38507457856", set()),
        ("agent-dispatcher-e97e9db-38816641062", set()),
        ("lokkju/plans-2026-10-08", set()),
        ("lokkju/flux-phase7-cleanup", set()),
        ("d90-storage-vlan-prep", set()),
        # A dashed version: lokkju/skills#65's branch, which /catchup filed under #300 (#96).
        ("fix/task-tools-2-1-300", set()),
        ("chore/claude-code-2-1-286", set()),
        ("k3s-1-36", set()),
        # Other shapes that name no issue.
        ("v1", set()),
        ("release/v2", set()),
        ("yeet/release-2026-10-14", set()),
        ("release/2026.10.15.1", set()),
        ("feat/forge-tracker-next", set()),
        ("flux-phase-2-docs", set()),
        ("fix-0", set()),
        ("fix-007", set()),
        ("dependabot/github_actions/actions/checkout-4", set()),
        ("renovate/node-18", set()),
    ],
)
def test_branch_numbers(branch, numbers):
    assert refs.branch_numbers(branch) == numbers


@pytest.mark.parametrize(
    "title, numbers",
    [
        ("fix: x (#12)", {12}),
        ("fix: see acme/other#9 and #5", {5}),
        ("fix: lokkju/skills#9 and #5", {9, 5}),
        ("fix: LOKKJU/Skills#9", {9}),
        ("fix: GH-21 and gh-22", {21, 22}),
        ('Revert "feat: x (#40)" (#41)', {40, 41}),
        ("fix: #0 isn't an issue", set()),
        ("fix: #007 isn't one either", set()),
        ("fix: #12345678901 is a run id", set()),
        # Versions, dates and other bare numbers name no issue (#96).
        (PR_65, set()),
        ("bump yeet to v0.17.1", set()),
        ("Claude Code 2.1.286", set()),
        ("chore: release 2026.10.15.1", set()),
        ("chore: plans for 2026-10-20", set()),
        ("fix: retry 3 times, not 300", set()),
        ("fix: #1.2 is a version", set()),
        ("feat: GH-1.2 is a version", set()),
        ("chore: x/GH-12 is a path", set()),
        ("fix: see lokkju/skills.wiki#9", set()),
    ],
)
def test_title_numbers(title, numbers):
    assert refs.title_numbers(title, "lokkju/skills") == numbers


def test_title_numbers_without_a_project():
    # Without the project, no `owner/repo#N` can be said to be this one's.
    assert refs.title_numbers("fix: lokkju/skills#9 and #5") == {5}


# --- paths and symbols an issue body names (/groom's possibly-done check) -----------------------


def test_body_names_reads_paths_and_symbols_and_skips_code_blocks_quotes_and_urls():
    body = (
        "The parser in `src/parse.py` drops rows; see plugins/forge-tracker/commands/groom.md.\n"
        "`check_merged` and `adapter.closing_refs()` and `ClosingRef` need it; `labels` doesn't.\n"
        "Not a file: e.g. github.com, nor https://example.org/a/b.py, nor `plugins/forge/`.\n"
        "> quoted `old_symbol` in quoted/path.py\n"
        "```\n"
        "fenced/path.py `fenced_symbol`\n"
        "```\n"
        "~~struck/path.py~~ and `config.toml` is generic but still named.\n"
    )
    paths, symbols = refs.body_names(body)
    assert paths == ["src/parse.py", "plugins/forge-tracker/commands/groom.md", "config.toml"]
    assert symbols == ["check_merged", "closing_refs", "ClosingRef"]


@pytest.mark.parametrize(
    "named, changed, matches",
    [
        ("src/parse.py", "src/parse.py", True),
        ("forge_tracker/groom.py", "plugins/x/scripts/forge_tracker/groom.py", True),
        ("groom.py", "plugins/x/forge_tracker/groom.py", True),
        ("groom.py", "plugins/x/forge_tracker/mygroom.py", False),
        ("README.md", "plugins/x/README.md", False),  # generic alone says nothing
        ("README.md", "README.md", True),
        ("x/README.md", "plugins/x/README.md", True),
    ],
)
def test_path_matches(named, changed, matches):
    assert refs.path_matches(named, changed) is matches


def test_patch_names_reads_changed_lines_and_hunk_headers_not_context():
    patch = (
        "@@ -10,6 +10,7 @@ def check_merged(f, reads):\n"
        "     unchanged_symbol()\n"
        "-    old_call()\n"
        "+    new_call(closing_refs)\n"
    )
    assert refs.patch_names(patch, "check_merged")  # the hunk is inside it
    assert refs.patch_names(patch, "closing_refs")
    assert refs.patch_names(patch, "old_call")
    assert not refs.patch_names(patch, "unchanged_symbol")
    assert not refs.patch_names(patch, "closing")  # whole words only
