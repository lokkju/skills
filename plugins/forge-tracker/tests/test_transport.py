import json
import subprocess

import pytest
from conftest import TOKEN, World, response
from forge_tracker import transport
from forge_tracker.model import ForgeError
from forge_tracker.transport import (
    GraphQLRefused,
    RecordingHttp,
    check_read,
    graphql_operations,
    redact,
)

GQL = "https://api.github.com/graphql"


def body(query):
    return json.dumps({"query": query, "variables": {}}).encode()


class NeverCalled:
    def __call__(self, *args, **kwargs):
        raise AssertionError("the request was sent")


# --- the read guard ----------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "post", "HEAD", "OPTIONS"])
def test_write_methods_are_refused_before_sending(transport_for, method):
    t = transport_for(NeverCalled())
    with pytest.raises(ForgeError, match="refused"):
        t.send(method, "https://api.github.com/repos/a/b/issues/1", b"{}")


def test_get_is_allowed(transport_for):
    world = World().route("/user", {"login": "me"})
    assert transport_for(world).get("/user") == {"login": "me"}


def test_get_with_a_body_is_refused():
    with pytest.raises(ForgeError, match="no body"):
        check_read("GET", "https://api.github.com/user", b"x", GQL)


def test_post_outside_graphql_is_refused(transport_for):
    t = transport_for(NeverCalled())
    with pytest.raises(ForgeError, match="only to the GraphQL endpoint"):
        t.send("POST", "https://api.github.com/repos/a/b/issues", body("{ viewer { login } }"))


def test_post_to_graphql_lookalike_path_is_refused():
    with pytest.raises(ForgeError, match="only to the GraphQL endpoint"):
        check_read("POST", GQL + "?x=1", body("{ viewer { login } }"), GQL)


def test_post_without_a_graphql_endpoint_is_refused():
    with pytest.raises(ForgeError, match="refused"):
        check_read("POST", GQL, body("{ viewer { login } }"), None)


@pytest.mark.parametrize(
    "document",
    [
        "mutation { addComment(input: {}) { clientMutationId } }",
        "mutation M($x: ID!) { closeIssue(input: {issueId: $x}) { clientMutationId } }",
        "query Q { viewer { login } }\nmutation M { deleteIssue(input: {}) { x } }",
        "{ viewer { login } } mutation { x }",
        "subscription S { issueUpdated { id } }",
        "# a comment\nquery Q { a }   \n\n  mutation{x}",
    ],
)
def test_graphql_mutations_anywhere_are_refused(document):
    with pytest.raises(ForgeError, match="contains a (mutation|subscription)"):
        check_read("POST", GQL, body(document), GQL)


@pytest.mark.parametrize(
    "document",
    [
        "{ viewer { login } }",
        "query { viewer { login } }",
        'query Q($q: String = "mutation { x }") { search(query: $q) { issueCount } }',
        "# mutation { evil }\nquery Q { viewer { login } }",
        'query Q { repository(owner: "a", name: "b") { issue(number: 1) { title } } }',
        'query Q { a(s: """block\n mutation { x } """) { b } }',
        "query Q { ...F } fragment F on Query { viewer { login } }",
        "query Q @include(if: true) { a }",
    ],
)
def test_graphql_queries_are_allowed(document):
    check_read("POST", GQL, body(document), GQL)


@pytest.mark.parametrize(
    "document, reason",
    [
        ("", "empty"),
        ("   # only a comment", "empty"),
        ("query Q { a { b }", "unbalanced braces"),
        ("query Q ( { a }", "unexpected .}."),
        ('query Q { a(s: "open) { b } }', "unterminated string"),
        ("mutatio { a }", "where a definition should start"),
        ("fragment F on Query { a }", "no query"),
        ("query Q", "no selection set"),
        ("query Q { a } ; mutation { b }", "unexpected character"),
    ],
)
def test_graphql_documents_that_dont_parse_are_refused(document, reason):
    with pytest.raises(ForgeError, match=reason):
        check_read("POST", GQL, body(document), GQL)


def test_graphql_body_must_be_json_with_a_query():
    for raw in (
        b"not json",
        json.dumps({"q": "{ a }"}).encode(),
        json.dumps({"query": 1}).encode(),
    ):
        with pytest.raises(ForgeError, match="JSON body with a query"):
            check_read("POST", GQL, raw, GQL)


def test_operations_lists_each_definition():
    doc = "query A { a } { b } fragment F on X { c } mutation M { d }"
    assert graphql_operations(doc) == ["query", "query", "fragment", "mutation"]


def test_graphql_refused_is_a_forge_error():
    assert issubclass(GraphQLRefused, ForgeError)


def test_transport_graphql_sends_query_and_returns_data(transport_for):
    world = World().op("Viewer", {"viewer": {"login": "me"}})
    data = transport_for(world).graphql("query Viewer { viewer { login } }")
    assert data == {"viewer": {"login": "me"}}
    method, url, headers, _ = world.calls[0]
    assert (method, url) == ("POST", GQL)
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_transport_graphql_mutation_never_sent(transport_for):
    t = transport_for(NeverCalled())
    with pytest.raises(ForgeError, match="mutation"):
        t.graphql("query A { a } mutation B { b }")


def test_graphql_errors_are_failures(transport_for):
    world = World().op("Q", {"errors": [{"message": "Field 'x' doesn't exist"}]})
    with pytest.raises(ForgeError, match="GraphQL error: Field 'x'"):
        transport_for(world).graphql("query Q { x }")


# --- pagination --------------------------------------------------------------------------------


def paged(pages, link_style=True):
    """A handler serving `pages` (a list of lists) by ?page=N, with Link headers."""

    def handler(url):
        import urllib.parse

        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        n = int(query.get("page", "1"))
        headers = {}
        if n < len(pages):
            nxt = f"https://api.github.com/items?page={n + 1}"
            if link_style:
                headers["Link"] = (
                    f'<{nxt}>; rel="next", <https://api.github.com/items?page={len(pages)}>; '
                    'rel="last"'
                )
            else:
                headers["X-Next-Page"] = str(n + 1)
        return response(pages[n - 1], headers=headers)

    return handler


def test_get_all_follows_link_to_the_last_page(transport_for):
    world = World().route("/items", paged([[1, 2], [3], [4, 5]]))
    assert transport_for(world).get_all("/items") == [1, 2, 3, 4, 5]
    assert len(world.calls) == 3
    assert "per_page=100" in world.calls[0][1]


def test_get_all_follows_x_next_page(transport_for):
    world = World().route("/items", paged([[1], [2], [3]], link_style=False))
    assert transport_for(world).get_all("/items") == [1, 2, 3]


def test_page_cap_hit_is_a_failure_not_a_short_list(transport_for):
    world = World().route("/items", paged([[1], [2], [3], [4]]))
    with pytest.raises(ForgeError, match="page cap of 3 pages hit"):
        transport_for(world, page_cap=3).get_all("/items")


def test_page_cap_exactly_reached_is_fine(transport_for):
    world = World().route("/items", paged([[1], [2], [3]]))
    assert transport_for(world, page_cap=3).get_all("/items") == [1, 2, 3]


def test_get_all_with_key_and_incomplete_results(transport_for):
    world = World().route("/search/issues", {"items": [1], "incomplete_results": True})
    with pytest.raises(ForgeError, match="incomplete"):
        transport_for(world).get_all("/search/issues", key="items")
    world = World().route("/search/issues", {"total_count": 0})
    with pytest.raises(ForgeError, match="no 'items' list"):
        transport_for(world).get_all("/search/issues", key="items")


def test_get_all_rejects_a_non_list(transport_for):
    world = World().route("/items", {"message": "odd"})
    with pytest.raises(ForgeError, match="other than a list"):
        transport_for(world).get_all("/items")


def test_graphql_all_pages_and_caps(transport_for):
    def pages(variables):
        cursor = variables.get("cursor")
        n = 0 if cursor is None else int(cursor)
        return {
            "repository": {
                "things": {
                    "pageInfo": {"hasNextPage": n < 2, "endCursor": str(n + 1)},
                    "nodes": [{"n": n}],
                }
            }
        }

    world = World().op("Things", pages)
    query = "query Things($cursor: String) { repository { things { nodes { n } } } }"
    t = transport_for(world)
    assert t.graphql_all(query, {}, ("repository", "things")) == [{"n": 0}, {"n": 1}, {"n": 2}]
    with pytest.raises(ForgeError, match="page cap of 2 pages hit"):
        transport_for(world, page_cap=2).graphql_all(query, {}, ("repository", "things"))
    stopped = t.graphql_all(query, {}, ("repository", "things"), stop=lambda node: node["n"] == 1)
    assert stopped == [{"n": 0}]


# --- errors and secrets ------------------------------------------------------------------------


def test_http_error_names_status_and_redacts_the_token(transport_for):
    world = World().route("/user", {"message": f"Bad credentials for {TOKEN}"}, status=401)
    with pytest.raises(ForgeError) as error:
        transport_for(world).get("/user")
    assert "HTTP 401 from /user: Bad credentials" in str(error.value)
    assert TOKEN not in str(error.value) and "***" in str(error.value)


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "gho_" + "B" * 30,
        "ghs_" + "c1" * 15,
        "github_pat_" + "x" * 40,
        "glpat-" + "y" * 20,
    ],
)
def test_redact_token_patterns(secret):
    assert secret not in redact(f"failed: {secret} rejected")


def test_redact_authorization_header_and_known_secrets():
    text = redact("Authorization: Bearer abc.def and custom-token-value", ["custom-token-value"])
    assert "abc.def" not in text and "custom-token-value" not in text


def test_redact_cuts_to_300_characters_after_replacing():
    text = redact("x" * 290 + " " + TOKEN + " " + "y" * 100)
    assert len(text) <= 300 and TOKEN[:8] not in text


def test_gh_token_reads_gh_and_redacts_failures():
    def run_ok(argv, **kwargs):
        assert argv == ["gh", "auth", "token", "--hostname", "github.com"]
        return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")

    assert transport.gh_token("github.com", run_ok) == TOKEN

    def run_fail(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", f"no oauth token ({TOKEN})")

    with pytest.raises(ForgeError) as error:
        transport.gh_token("github.com", run_fail)
    assert "gh auth login" in str(error.value) and TOKEN not in str(error.value)

    def run_missing(argv, **kwargs):
        raise FileNotFoundError

    with pytest.raises(ForgeError, match="isn't on PATH"):
        transport.gh_token("github.com", run_missing)


# --- recording ---------------------------------------------------------------------------------


def test_recording_scrubs_tokens_headers_and_bulk():
    def inner(method, url, headers, body=None):
        return response(
            [
                {
                    "id": 1,
                    "note": f"leaked {TOKEN}",
                    "repository": {"big": True},
                    "events_url": "https://x",
                    "html_url": "https://h",
                }
            ],
            headers={"Link": "<n>; rel=next", "Set-Cookie": "s=1", "X-RateLimit-Remaining": "1"},
        )

    recorder = RecordingHttp(inner, [TOKEN])
    recorder("GET", "https://api.github.com/x", {"Authorization": f"Bearer {TOKEN}"})
    dumped = recorder.dump(recorded_at="2026-10-15T00:00:00Z")
    assert TOKEN not in dumped and "Bearer" not in dumped
    data = json.loads(dumped)
    exchange = data["exchanges"][0]
    assert exchange["headers"] == {"link": "<n>; rel=next"}
    assert exchange["response"] == [{"id": 1, "note": "leaked ***", "html_url": "https://h"}]
    assert data["recorded_at"] == "2026-10-15T00:00:00Z"
