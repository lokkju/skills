import pytest
from forge_tracker import refs


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
        ("fix: #0 isn't an issue", set()),
        ("chore: release 2026.10.15.1", set()),
    ],
)
def test_title_numbers(title, numbers):
    assert refs.title_numbers(title) == numbers
