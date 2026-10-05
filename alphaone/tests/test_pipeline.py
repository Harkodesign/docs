import json
import re
import smtplib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import main
import mailer
import research
from config import Settings
from newsletter import IssueMeta, Newsletter, render_html, render_text
from sources import Item
from state import State

HERE = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
CREDS = dict(smtp_username="bot@example.com", smtp_password="x", recipient="me@example.com")
REAL_VERIFY = mailer.verify  # the wired fixture patches it out


def sample_issue() -> Newsletter:
    return Newsletter.model_validate(json.loads((HERE / "sample_issue.json").read_text(encoding="utf-8")))


META = IssueMeta(issue_number=3, sent_at="Mon 5 Oct 2026, 14:00 UTC", sources_scanned=28, candidates=40, searches=6, pages_read=9)


# ---------------------------------------------------------------- state


def test_state_round_trip_and_pruning(tmp_path):
    path = tmp_path / "state.json"
    state = State.load(path)
    assert state.issue_number == 0
    state.seen["old"] = (NOW - timedelta(days=30)).isoformat()
    state.record_run(NOW, ["a", "b"], [{"headline": "H", "urls": []}], sent=True)
    state.save(path)

    loaded = State.load(path)
    assert set(loaded.seen) == {"a", "b"}
    assert loaded.recent_headlines() == ["H (sent 2026-10-05 14:00 UTC)"]
    assert loaded.issue_number == 1

    loaded.record_run(NOW + timedelta(hours=80), [], [], sent=False)
    assert loaded.recent_headlines() == []
    assert loaded.issue_number == 1


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert State.load(path).seen == {}


def test_unsent_issue_is_not_remembered_as_a_briefing():
    state = State()
    state.record_run(NOW, ["a"], [{"headline": "H", "urls": []}], sent=False)
    assert "a" in state.seen
    assert state.recent_headlines() == [] and state.last_run is None and state.issue_number == 0


# ---------------------------------------------------------------- rendering


def test_render_html_has_every_section_and_escapes():
    issue = sample_issue()
    issue.stories[0].headline = "<script>alert(1)</script>"
    issue.stories[0].sources[0].url = "javascript:alert(1)"
    html = render_html(issue, META)
    for heading in ["60-second brief", "Top stories", "Papers, explained simply", "Also on the radar", "Jargon buster"]:
        assert heading in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert 'href="https://example.org/nova-2-review"' in html
    assert 'href="https://example.com/news/gpu"' in html  # radar items link to their source
    assert "Issue #3" in html


def test_render_text_mentions_stories_and_papers():
    text = render_text(sample_issue(), META)
    assert "TOP STORIES" in text and "PAPERS, EXPLAINED SIMPLY" in text
    assert "https://example.com/blog/nova-2" in text
    assert "https://example.com/news/gpu" in text


@pytest.mark.parametrize("url", ["", "arxiv.org/abs/2610.00001", "javascript:alert(1)"])
def test_radar_item_without_a_usable_link_has_no_source_label(url):
    issue = sample_issue()
    issue.radar[0].url = url
    html, text = render_html(issue, META), render_text(issue, META)
    assert "AI workloads. Source" not in html and "AI workloads.</li>" in html
    assert "• A fictional chipmaker announced a data-center GPU shipping next spring, promising 2x speed on AI workloads.\n" in text


def test_markdown_is_stripped_from_prose_but_not_urls():
    raw = json.loads((HERE / "sample_issue.json").read_text(encoding="utf-8"))
    raw["stories"][0]["headline"] = "## **Nova-2** ships with `vLLM` support"
    raw["stories"][0]["details"][0] = "See [the docs](https://example.com/docs) for Q* and A*"
    raw["stories"][0]["sources"][0]["url"] = "https://docs.python.org/3/reference/datamodel.html#object.__init__"
    story = Newsletter.model_validate(raw).stories[0]
    assert story.headline == "Nova-2 ships with vLLM support"
    assert story.details[0] == "See the docs for Q* and A*"
    assert story.sources[0].url.endswith("#object.__init__")


def test_largest_issue_stays_under_gmail_clipping():
    raw = json.loads((HERE / "sample_issue.json").read_text(encoding="utf-8"))
    sentence = "A sentence of about a hundred and ten characters, with numbers like 42% and names like O'Brien — typical."
    story, paper = raw["stories"][0], raw["papers"][0]
    for key in ("one_liner", "what_happened", "why_it_matters", "what_to_watch"):
        story[key] = sentence
    story["details"] = [sentence] * 8
    story["sources"] = [{"title": "Publisher: " + sentence[:60], "url": "https://example.com/" + "a" * 60}] * 4
    for key in ("one_liner", "the_problem", "the_big_idea", "why_it_matters", "caveats"):
        paper[key] = sentence
    paper["key_results"] = [sentence] * 5
    raw.update(
        brief=[sentence] * 7,
        stories=[story] * 6,
        papers=[paper] * 4,
        radar=[{"text": sentence, "url": "https://example.com/" + "b" * 60}] * 10,
        glossary=[{"term": "Term", "meaning": sentence}] * 12,
    )
    assert len(render_html(Newsletter.model_validate(raw), META).encode("utf-8")) < 0.9 * main.GMAIL_CLIP_BYTES


# ---------------------------------------------------------------- Claude passes


def _usage(**kw):
    base = dict(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=3),
        iterations=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _message(stop_reason, text=None, content=None, model="claude-opus-5-5"):
    if content is None:
        content = [_text(text)] if text else []
    return SimpleNamespace(
        stop_reason=stop_reason, content=content, usage=_usage(), stop_details=None, model=model, container=None
    )


def _issue_message(issue=None):
    return _message("end_turn", (issue or sample_issue()).model_dump_json())


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if isinstance(self.message, Exception):
            raise self.message
        return self.message


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the list grows later
        return FakeStream(self.responses.pop(0))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(research.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mailer.time, "sleep", lambda seconds: None)


def test_research_resumes_after_pause_turn():
    paused = _message("pause_turn", "part one")
    paused.container = SimpleNamespace(id="container_abc")
    client = FakeClient([paused, _message("end_turn", "part two")])
    usage = research.RunUsage()
    dossier = research.run_research(client, Settings(), "prompt", usage)

    assert dossier == "part one\n\npart two"
    assert usage.requests == 2 and usage.searches == 4 and usage.fetches == 6
    first, second = client.calls
    assert first["fallbacks"] == "default" and first["betas"] == [research.FALLBACK_BETA]
    assert first["max_tokens"] == research.RESEARCH_MAX_TOKENS
    assert [t["name"] for t in first["tools"]] == ["web_search", "web_fetch"]
    assert len(first["messages"]) == 1 and "Budget for this run: 12 web searches" in first["messages"][0]["content"]
    assert first["container"] is anthropic.omit
    assert [m["role"] for m in second["messages"]] == ["user", "assistant"]
    assert second["messages"][1]["content"][0].text == "part one"
    assert second["container"] == "container_abc"
    assert second["tools"] == first["tools"]


def test_research_refusal_raises_refusal_error():
    refused = _message("refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    with pytest.raises(research.RefusalError, match="declined"):
        research.run_research(FakeClient([refused]), Settings(), "prompt", research.RunUsage())


def test_refusal_when_the_fallback_was_too_busy_is_retried_next_hour():
    refused = _message("refusal")
    refused.stop_details = SimpleNamespace(category="cyber", recommended_model="claude-opus-4-8")
    with pytest.raises(research.ResearchError) as caught:
        research.run_research(FakeClient([refused]), Settings(), "prompt", research.RunUsage())
    assert not isinstance(caught.value, research.RefusalError)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_research_cut_short_raises(stop_reason):
    with pytest.raises(research.ResearchError, match=stop_reason):
        research.run_research(FakeClient([_message(stop_reason, "half a dossier")]), Settings(), "prompt", research.RunUsage())


def _paused(searches, fetches, content=None):
    message = _message("pause_turn", content=content or [])
    message.usage = _usage(server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=fetches))
    return message


def _pending_call():
    return [SimpleNamespace(type="server_tool_use", id="srvtoolu_1")]


def test_research_still_going_after_the_request_cap_raises():
    # No tool use at all, so only the request cap can trigger the wrap-up.
    client = FakeClient([_paused(0, 0) for _ in range(research.MAX_REQUESTS)])
    with pytest.raises(research.ResearchError, match="still going"):
        research.run_research(client, Settings(), "prompt", research.RunUsage())
    assert len(client.calls) == research.MAX_REQUESTS
    last = client.calls[-1]["messages"]
    assert last[-1] == {"role": "user", "content": research.WRAP_UP}  # the last request asked for the dossier
    assert sum(m.get("content") == research.WRAP_UP for m in last) == 1


def test_spent_search_budget_still_allows_page_reads():
    client = FakeClient([_paused(12, 9, _pending_call()), _message("end_turn", "dossier")])
    assert research.run_research(client, Settings(), "prompt", research.RunUsage()) == "dossier"
    first, second = client.calls
    assert second["tools"] == first["tools"]  # the budget is enforced by notes, never by changing tools
    assert [b.type for b in second["messages"][1]["content"]] == ["server_tool_use"]  # the pending call is resumed
    note = second["messages"][-1]
    assert note["role"] == "system"
    assert "don't search again" in note["content"] and "up to 5 pages" in note["content"]


def test_spent_fetch_budget_still_allows_searches():
    client = FakeClient([_paused(1, 14, _pending_call()), _message("end_turn", "dossier")])
    research.run_research(client, Settings(), "prompt", research.RunUsage())
    note = client.calls[1]["messages"][-1]
    assert "don't fetch any more pages" in note["content"] and "up to 11 web searches" in note["content"]


def test_both_budgets_spent_asks_for_the_dossier_once():
    client = FakeClient([_paused(12, 3), _paused(0, 11), _paused(0, 0), _message("end_turn", "dossier")])
    research.run_research(client, Settings(), "prompt", research.RunUsage())
    notes = [m["content"] for m in client.calls[-1]["messages"] if m["role"] in ("user", "system")][1:]
    assert notes[0].startswith("This run's web search budget is used up") and notes[1:] == [research.WRAP_UP]


def test_under_budget_pause_resumes_without_a_note():
    client = FakeClient([_message("pause_turn"), _message("end_turn", "dossier")])
    research.run_research(client, Settings(), "prompt", research.RunUsage())
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_note_after_a_finished_tool_call_is_a_user_message():
    content = [
        SimpleNamespace(type="server_tool_use", id="srvtoolu_1"),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="srvtoolu_1"),
    ]
    assert research._wrap_up_message(content, "x")["role"] == "user"


def test_note_role_follows_the_turn_actually_sent_after_a_fallback():
    # The declined attempt's unanswered call is dropped from the echo, so nothing is pending.
    content = [
        SimpleNamespace(type="server_tool_use", id="s1"),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="server_tool_use", id="s2"),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="s2"),
    ]
    client = FakeClient([_paused(12, 14, content), _message("end_turn", "dossier")])
    research.run_research(client, Settings(), "prompt", research.RunUsage())
    assert client.calls[1]["messages"][-1] == {"role": "user", "content": research.WRAP_UP}


def test_refused_note_resumes_without_it():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    refused = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
    client = FakeClient([_paused(12, 14, _pending_call()), refused, _message("end_turn", "dossier")])
    usage = research.RunUsage()
    assert research.run_research(client, Settings(), "prompt", usage) == "dossier"
    assert client.calls[1]["messages"][-1]["role"] == "system"
    assert client.calls[2]["messages"][-1]["role"] == "assistant"
    assert any("budget note" in note for note in usage.notes)


def test_other_bad_requests_are_not_swallowed():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    refused = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=request), body=None)
    with pytest.raises(anthropic.BadRequestError):
        research.run_research(FakeClient([_message("pause_turn"), refused]), Settings(), "prompt", research.RunUsage())


def _mid_stream_error(error_type):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": error_type, "message": "boom"}}
    return anthropic.APIStatusError(str(body), response=httpx2.Response(200, request=request), body=body)


@pytest.mark.parametrize(
    "error",
    [
        _mid_stream_error("overloaded_error"),
        _mid_stream_error("api_error"),
        httpx2.RemoteProtocolError("peer closed connection"),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
        _message(None, "a stream that just stopped"),
        AssertionError(),  # the SDK's check when a stream ends before the message starts
    ],
)
def test_failures_after_the_stream_opened_are_retried_with_the_same_request(error):
    client = FakeClient([_message("pause_turn", "one"), error, _message("end_turn", "two")])
    usage = research.RunUsage()
    assert research.run_research(client, Settings(), "prompt", usage) == "one\n\ntwo"
    assert len(client.calls) == 3 and client.calls[1]["messages"] == client.calls[2]["messages"]
    assert usage.requests == 2 and usage.notes


def test_bad_requests_are_not_retried():
    client = FakeClient([_mid_stream_error("invalid_request_error")])
    with pytest.raises(anthropic.APIStatusError):
        research.run_research(client, Settings(), "prompt", research.RunUsage())
    assert len(client.calls) == 1


def test_retries_give_up_after_the_last_delay():
    client = FakeClient([_mid_stream_error("overloaded_error")] * (len(research.RETRY_DELAYS) + 1))
    with pytest.raises(anthropic.APIStatusError):
        research.run_research(client, Settings(), "prompt", research.RunUsage())
    assert len(client.calls) == len(research.RETRY_DELAYS) + 1


def test_dossier_keeps_cited_sentences_whole():
    content = [
        _text("1. OpenAI: "),
        _text("GPT-9 launched"),
        _text(" today."),
        SimpleNamespace(type="server_tool_use", id="s1"),
        _text("Next."),
    ]
    client = FakeClient([_message("end_turn", content=content)])
    assert research.run_research(client, Settings(), "prompt", research.RunUsage()) == "1. OpenAI: GPT-9 launched today.\n\nNext."


def test_continuation_after_a_mid_stream_fallback_drops_the_declined_attempts_internals():
    content = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="server_tool_use", id="s1"),
        _text("partial"),
        SimpleNamespace(type="fallback"),
        SimpleNamespace(type="thinking", thinking=""),
        _text("more"),
    ]
    assert [b.type for b in research._echo(content)] == ["text", "fallback", "thinking", "text"]


def test_echo_without_a_fallback_keeps_the_whole_turn():
    content = [
        SimpleNamespace(type="thinking", thinking="t"),
        SimpleNamespace(type="server_tool_use", id="s1"),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="s1"),
        _text("x"),
        SimpleNamespace(type="server_tool_use", id="s2"),
    ]
    assert research._echo(content) == content


def test_echo_keeps_an_answered_server_tool_call_before_the_fallback():
    content = [
        SimpleNamespace(type="server_tool_use", id="s1"),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="s1"),
        _text("partial"),
        SimpleNamespace(type="fallback"),
        _text("more"),
    ]
    assert [b.type for b in research._echo(content)] == [
        "server_tool_use", "web_search_tool_result", "text", "fallback", "text"
    ]


def test_editor_returns_the_newsletter():
    client = FakeClient([_issue_message()])
    result = research.run_editor(client, Settings(), "dossier", NOW, research.RunUsage(), reader_time="Mon 19:30 IST")
    assert result == sample_issue()
    call = client.calls[0]
    assert "output_format" not in call
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Mon 19:30 IST" in call["messages"][0]["content"]


def test_editor_cut_off_raises_a_clear_error():
    with pytest.raises(research.ResearchError, match="max_tokens"):
        research.run_editor(FakeClient([_message("max_tokens", '{"subject_line": "cut')]), Settings(), "d", NOW, research.RunUsage())


def test_editor_refusal_mid_stream_is_a_refusal_not_a_parse_error():
    refused = _message("refusal", '{"subject_line": "cut')
    with pytest.raises(research.RefusalError):
        research.run_editor(FakeClient([refused]), Settings(), "d", NOW, research.RunUsage())


@pytest.mark.parametrize("restart", [True, False])
def test_editor_parses_the_answer_after_a_mid_stream_fallback(restart):
    full = sample_issue().model_dump_json()
    after = _text(full) if restart else _text(full[120:])  # the fallback restarted or continued the partial
    content = [_text(full[:120]), SimpleNamespace(type="fallback"), after]
    client = FakeClient([_message("end_turn", content=content, model="claude-opus-4-8")])
    assert research.run_editor(client, Settings(), "d", NOW, research.RunUsage()) == sample_issue()


def test_editor_invalid_json_raises():
    with pytest.raises(research.ResearchError, match="no valid newsletter"):
        research.run_editor(FakeClient([_message("end_turn", "not json")]), Settings(), "d", NOW, research.RunUsage())


def test_refused_editor_request_is_still_counted():
    usage = research.RunUsage()
    with pytest.raises(research.RefusalError):
        research.run_editor(FakeClient([_message("refusal")]), Settings(), "d", NOW, usage)
    assert usage.requests == 1 and usage.input_tokens == 1000


def test_research_prompt_lists_items_and_recent_headlines():
    item = Item("news", "TechCrunch AI", "Big launch", "https://example.com/x", NOW, summary="snippet")
    paper = Item("paper", "arXiv", "Squeeze", "https://arxiv.org/abs/2610.01234v2", NOW)
    prompt = research.build_research_prompt(NOW, None, [item], [paper], ["Old story (sent 2026-10-05 13:07 UTC)"])
    assert "[1] Big launch" in prompt
    assert "- Old story (sent 2026-10-05 13:07 UTC)" in prompt
    assert "cover the past 24 hours" in prompt
    # web fetch can only open URLs that are in the conversation, so the full text is listed
    assert "full text: https://arxiv.org/html/2610.01234" in prompt and prompt.count("full text:") == 1


# ---------------------------------------------------------------- usage and cost


def test_usage_counts_a_declined_attempt_and_prices_each_model():
    iterations = [
        SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=50_000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1_000_000, output_tokens=100_000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0),
    ]
    usage = research.RunUsage()
    usage.add(_usage(iterations=iterations), "claude-opus-4-8")
    assert usage.input_tokens == 2_000_000 and usage.output_tokens == 150_000
    assert set(usage.by_model) == {"claude-opus-5-5", "claude-opus-4-8"}
    assert main.estimate_cost(usage) == pytest.approx(4 + 1.0 + 5 + 2.5 + 0.02)
    assert any("claude-opus-4-8" in note for note in usage.notes)


def test_usage_without_iterations_uses_the_message_model():
    usage = research.RunUsage()
    usage.add(_usage(), "claude-opus-5-5")
    assert usage.by_model == {"claude-opus-5-5": [1000, 200, 0, 0]} and usage.notes == []


def test_unknown_model_gives_no_cost():
    usage = research.RunUsage()
    usage.add(_usage(), "claude-haiku-4-5")
    assert main.estimate_cost(usage) is None
    assert "claude-haiku-4-5" not in main.PRICES  # it can't run the web tools ALPHAONE uses
    assert {"claude-opus-5", "claude-opus-4-8"} <= set(main.PRICES)


# ---------------------------------------------------------------- email


def test_build_message_is_multipart():
    message = mailer.build_message(Settings(**CREDS, sender=""), "Subject", "plain", "<p>html</p>")
    assert message["To"] == "me@example.com"
    assert message["From"] == "ALPHAONE <bot@example.com>"
    assert message["Date"] and message["Message-ID"].endswith("@example.com>")
    assert [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "text/html"]


def test_from_override_accepts_a_display_name():
    message = mailer.build_message(Settings(**CREDS, sender="News Desk <news@example.org>"), "S", "p", "<p>h</p>")
    assert message["From"] == "ALPHAONE <news@example.org>"
    assert message["Message-ID"].endswith("@example.org>")


def test_sender_must_be_an_address():
    settings = Settings(smtp_username="apikey", smtp_password="x", sender="")  # e.g. SendGrid
    with pytest.raises(mailer.MailConfigError, match="ALPHAONE_FROM"):
        mailer.verify(settings)
    with pytest.raises(mailer.MailConfigError, match="ALPHAONE_FROM"):
        mailer.build_message(settings, "S", "p", "h")


def test_send_requires_credentials():
    with pytest.raises(mailer.MailConfigError, match="SMTP_USERNAME"):
        mailer.send(Settings(smtp_username="", smtp_password=""), "s", "t", "h")
    with pytest.raises(mailer.MailConfigError, match="SMTP_USERNAME"):
        mailer.verify(Settings(smtp_username="", smtp_password=""))


class FakeSMTP:
    """Each connection takes one outcome: None = works, an exception = login (for verify) or
    send_message raises it, ("quit", exc) = works, then QUIT raises it, ("mail", code) = MAIL FROM
    gets that reply code."""

    outcomes: list = []
    attempted: list = []  # Message-ID of every send_message call, failed ones included
    delivered: list = []
    verified: int = 0

    def __init__(self, *args, **kwargs):
        self.outcome = FakeSMTP.outcomes.pop(0)

    def login(self, user, password):
        pass

    def mail(self, sender):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if isinstance(self.outcome, tuple) and self.outcome[0] == "mail":
            return self.outcome[1], b"reply"
        return 250, b"ok"

    def rset(self):
        FakeSMTP.verified += 1

    def send_message(self, message):
        FakeSMTP.attempted.append(message["Message-ID"])
        if isinstance(self.outcome, Exception):
            raise self.outcome
        FakeSMTP.delivered.append(message["Message-ID"])

    def quit(self):
        if isinstance(self.outcome, tuple) and self.outcome[0] == "quit":
            raise self.outcome[1]

    def close(self):
        pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.attempted, FakeSMTP.delivered, FakeSMTP.verified = [], [], 0
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_send_retries_transient_failures_with_one_message_id(fake_smtp):
    fake_smtp.outcomes = [smtplib.SMTPServerDisconnected("gone"), TimeoutError(), smtplib.SMTPDataError(451, b"later"), None]
    mailer.send(Settings(**CREDS), "s", "t", "h")
    assert len(fake_smtp.delivered) == 1 and fake_smtp.outcomes == []
    assert len(fake_smtp.attempted) == 4 and len(set(fake_smtp.attempted)) == 1


def test_verify_retries_a_transient_failure(fake_smtp):
    fake_smtp.outcomes = [smtplib.SMTPServerDisconnected("gone"), None]
    mailer.verify(Settings(**CREDS))
    assert fake_smtp.verified == 1 and fake_smtp.outcomes == []


def test_verify_fails_when_the_server_refuses_the_sender(fake_smtp):
    fake_smtp.outcomes = [("mail", 553), None]
    with pytest.raises(smtplib.SMTPSenderRefused):
        mailer.verify(Settings(**CREDS))
    assert fake_smtp.outcomes == [None]


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPAuthenticationError(535, b"bad password"),
        smtplib.SMTPDataError(552, b"too big"),
        smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no such user")}),
    ],
)
def test_send_does_not_retry_permanent_failures(fake_smtp, error):
    fake_smtp.outcomes = [error, None]
    with pytest.raises(type(error)):
        mailer.send(Settings(**CREDS), "s", "t", "h")
    assert fake_smtp.outcomes == [None]


def test_quit_failing_after_delivery_is_not_a_failed_send(fake_smtp):
    fake_smtp.outcomes = [("quit", smtplib.SMTPResponseException(451, b"bye")), None]
    mailer.send(Settings(**CREDS), "s", "t", "h")
    assert len(fake_smtp.delivered) == 1 and fake_smtp.outcomes == [None]


# ---------------------------------------------------------------- end to end


@pytest.fixture
def wired(monkeypatch, tmp_path):
    items = [Item("news", "TechCrunch AI", "Big launch", "https://example.com/x", datetime.now(timezone.utc))]
    monkeypatch.setattr(main.sources, "fetch_all", lambda: (items, {"TechCrunch AI": "ok (1)", "Wired AI": "failed: x"}))
    client = FakeClient([_message("end_turn", "dossier text"), _issue_message()])
    monkeypatch.setattr(main.anthropic, "Anthropic", lambda **kw: client)
    monkeypatch.setattr(main.mailer, "verify", lambda settings: None)
    sent = []
    monkeypatch.setattr(main.mailer, "send", lambda settings, subject, text, html: sent.append(subject))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    settings = Settings(state_path=tmp_path / "state.json", out_dir=tmp_path / "out")
    return settings, sent


def _use_client(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(main.anthropic, "Anthropic", lambda **kw: client)
    return client


def test_full_run_sends_and_remembers(wired):
    settings, sent = wired
    assert main.run(settings, dry_run=False) == 0
    assert sent and sent[0].startswith("ALPHAONE #1 · SAMPLE")
    state = State.load(settings.state_path)
    assert "https://example.com/x" in state.seen
    assert "https://example.com/blog/nova-2" in state.seen  # a sent story's own link
    assert "https://example.com/news/gpu" in state.seen  # a sent radar item's link
    assert state.issue_number == 1
    assert (settings.out_dir / "issue.html").exists()
    assert "Approximate cost this run" in Path(main.os.environ["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")


def test_dry_run_sends_nothing_and_keeps_memory(wired):
    settings, sent = wired
    assert main.run(settings, dry_run=True) == 0
    assert sent == []
    assert not settings.state_path.exists()
    assert (settings.out_dir / "dossier.md").read_text(encoding="utf-8") == "dossier text"


def test_smtp_is_checked_before_any_paid_work(wired, monkeypatch):
    settings, _ = wired
    client = _use_client(monkeypatch, [])
    monkeypatch.setattr(main.mailer, "verify", REAL_VERIFY)
    with pytest.raises(mailer.MailConfigError):
        main.run(replace(settings, smtp_username="", smtp_password=""), dry_run=False)
    assert client.calls == []


def test_dry_run_needs_no_smtp(wired, monkeypatch):
    settings, _ = wired
    monkeypatch.setattr(main.mailer, "verify", REAL_VERIFY)
    assert main.run(replace(settings, smtp_username="", smtp_password=""), dry_run=True) == 0


def test_refusal_skips_the_items_without_sending(wired, monkeypatch):
    settings, sent = wired
    _use_client(monkeypatch, [_message("refusal")])
    assert main.run(settings, dry_run=False) == 0
    state = State.load(settings.state_path)
    assert sent == [] and "https://example.com/x" in state.seen
    assert state.last_run is None and state.issue_number == 0
    summary = Path(main.os.environ["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "won't be offered again" in summary and "1 requests" in summary  # the refused request is counted


def test_dry_run_refusal_keeps_memory_untouched(wired, monkeypatch):
    settings, sent = wired
    _use_client(monkeypatch, [_message("refusal")])
    assert main.run(settings, dry_run=True) == 0
    assert sent == [] and not settings.state_path.exists()
    summary = Path(main.os.environ["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "won't be offered again" not in summary and "memory unchanged" in summary


def test_editor_failure_keeps_the_dossier(wired, monkeypatch):
    settings, sent = wired
    _use_client(monkeypatch, [_message("end_turn", "dossier text"), _message("end_turn", "not json")])
    with pytest.raises(research.ResearchError):
        main.run(settings, dry_run=False)
    assert sent == [] and not settings.state_path.exists()
    assert (settings.out_dir / "dossier.md").read_text(encoding="utf-8") == "dossier text"


def test_sent_paper_link_is_remembered_by_its_arxiv_key(wired, monkeypatch):
    settings, sent = wired
    issue = sample_issue()
    issue.papers[0].url = "https://arxiv.org/abs/2610.01234v2"
    _use_client(monkeypatch, [_message("end_turn", "dossier text"), _issue_message(issue)])
    main.run(settings, dry_run=False)
    assert sent and "arxiv:2610.01234" in State.load(settings.state_path).seen


def test_failed_run_saves_nothing_but_still_reports_its_usage(wired, monkeypatch):
    settings, sent = wired
    _use_client(monkeypatch, [_message("max_tokens", "half a dossier")])
    with pytest.raises(research.ResearchError):
        main.run(settings, dry_run=False)
    assert sent == [] and not settings.state_path.exists()
    summary = Path(main.os.environ["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8")
    assert "Run failed: ResearchError" in summary and "1 requests" in summary


def test_skipped_quiet_hour_is_not_remembered_as_sent(wired, monkeypatch):
    settings, sent = wired
    issue = sample_issue()
    issue.quiet_hour = True
    _use_client(monkeypatch, [_message("end_turn", "dossier text"), _issue_message(issue)])
    main.run(replace(settings, skip_quiet_hours=True), dry_run=False)
    state = State.load(settings.state_path)
    assert sent == [] and "https://example.com/x" in state.seen
    assert state.recent_headlines() == [] and state.last_run is None and state.issue_number == 0
    # Links that were never emailed aren't marked seen.
    assert "https://example.com/blog/nova-2" not in state.seen and "https://example.com/news/gpu" not in state.seen


def test_long_gap_since_the_last_briefing_covers_the_lookback_window(wired, monkeypatch):
    settings, _ = wired
    old = datetime.now(timezone.utc) - timedelta(days=5)
    State(covered=[{"at": old.isoformat(), "headline": "Five-day-old story", "urls": []}], last_run=old.isoformat()).save(
        settings.state_path
    )
    client = _use_client(monkeypatch, [_message("end_turn", "dossier text"), _issue_message()])
    main.run(settings, dry_run=True)
    prompt = client.calls[0]["messages"][0]["content"]
    assert "cover the past 24 hours" in prompt and "Five-day-old story" not in prompt


def test_subject_newlines_are_flattened(wired, monkeypatch):
    settings, sent = wired
    issue = sample_issue()
    issue.subject_line = "Line one\r\nBcc: someone@example.com"
    _use_client(monkeypatch, [_message("end_turn", "dossier text"), _issue_message(issue)])
    main.run(settings, dry_run=False)
    assert "\n" not in sent[0] and "\r" not in sent[0]


def test_outputs_are_utf8(tmp_path):
    main.write_outputs(tmp_path, {"x.md": "DeepSeek 深度 → ≈ \U0001F680 — ·"})
    assert (tmp_path / "x.md").read_bytes().decode("utf-8").startswith("DeepSeek 深度")


def test_format_time_falls_back_on_bad_timezone():
    assert main.format_time(NOW, "Not/AZone") == "Mon 5 Oct 2026, 14:00 UTC"
    assert main.format_time(NOW, "Asia/Kolkata") == "Mon 5 Oct 2026, 19:30 IST"


# ---------------------------------------------------------------- repository


@pytest.mark.repo
def test_workflow_passes_every_setting_through():
    workflow = (HERE.parents[1] / ".github" / "workflows" / "alphaone.yml").read_text(encoding="utf-8")
    config = (HERE.parent / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'_env(?:_int|_bool)?\("([A-Z_]+)"', config)) - {"ALPHAONE_STATE", "ALPHAONE_OUT"}
    assert names and all(f"{name}: ${{{{ " in workflow for name in names)


@pytest.mark.repo
def test_readme_is_safe_for_mdx():
    # The repo is a Mintlify site, which reads Markdown as MDX: raw angle brackets or braces break it.
    text = (HERE.parent / "README.md").read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    assert not re.search(r"[<>{}]", text)
