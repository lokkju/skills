from session_decisions.subject import Draft, format_subject, parse_draft, parse_label, problems


def test_plain_task_is_not_a_draft():
    assert parse_draft("Run the migration") is None


def test_decide_draft_parsed():
    assert parse_draft("DECIDE: retire X? (recommend yes) [#812]") == Draft(
        "DECIDE", "retire X? (recommend yes) [#812]"
    )


def test_lowercase_keyword_normalized():
    assert parse_draft("action: enable job tokens [no link]").kind == "ACTION"


def test_existing_label_is_dropped_for_renumbering():
    assert parse_draft("D7 DECIDE: x (recommend y) [no link]") == Draft("DECIDE", "x (recommend y) [no link]")


def test_decided_word_is_not_a_keyword():
    assert parse_draft("DECIDED: we use uv") is None


def test_valid_decide_has_no_problems():
    assert problems(Draft("DECIDE", "retire X? (recommend yes) [#812]")) == []


def test_decide_without_recommendation():
    assert any("recommend" in p for p in problems(Draft("DECIDE", "retire X? [#812]")))


def test_missing_link_token():
    assert any("link" in p for p in problems(Draft("ACTION", "enable job tokens")))


def test_brackets_inside_question_do_not_count_as_link():
    assert any("link" in p for p in problems(Draft("DECIDE", "[A] or [B]? (recommend A)")))


def test_action_needs_no_recommendation():
    assert problems(Draft("ACTION", "enable job tokens [no link]")) == []


def test_empty_body():
    assert problems(Draft("DECIDE", "")) == ["write the question or action after the colon"]


def test_format_and_parse_round_trip():
    subject = format_subject("D", 3, Draft("DECIDE", "x (recommend y) [no link]"))
    assert subject == "D3 DECIDE: x (recommend y) [no link]"
    label = parse_label(subject)
    assert (label.letter, label.number, label.kind, label.body, label.text) == (
        "D", 3, "DECIDE", "x (recommend y) [no link]", "D3",
    )


def test_parse_label_rejects_unlabelled():
    assert parse_label("DECIDE: x (recommend y) [no link]") is None
