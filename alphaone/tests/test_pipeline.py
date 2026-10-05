import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


def sample_issue() -> Newsletter:
    return Newsletter.model_validate(json.loads((HERE / "sample_issue.json").read_text()))


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
    assert loaded.recent_headlines() == ["H"]
    assert loaded.issue_number == 1

    loaded.record_run(NOW + timedelta(hours=80), [], [], sent=False)
    assert loaded.recent_headlines() == []
    assert loaded.issue_number == 1


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert State.load(path).seen == {}


# ---------------------------------------------------------------- rendering


def test_render_html_has_every_section_and_escapes():
    issue = sample_issue()
    issue.stories[0].headline = "<script>alert(1)</script>"
    issue.stories[0].sources[0].url = "javascript:alert(1)"
    html = render_html(issue, META)
    for heading in ["60-second brief", "Top stories", "Research radar", "Also on the radar", "Jargon buster"]:
        assert heading in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert 'href="https://example.org/nova-2-review"' in html
    assert "Issue #3" in html


def test_render_text_mentions_stories_and_papers():
    text = render_text(sample_issue(), META)
    assert "TOP STORIES" in text and "RESEARCH RADAR" in text
    assert "https://example.com/blog/nova-2" in text


# ---------------------------------------------------------------- Claude passes


def _usage(**kw):
    base = dict(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=3),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _message(stop_reason, text=None, parsed=None):
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=_usage(), stop_details=None, parsed_output=parsed)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.responses.pop(0))


def test_research_resumes_after_pause_turn():
    client = FakeClient([_message("pause_turn", "part one"), _message("end_turn", "part two")])
    usage = research.RunUsage()
    dossier = research.run_research(client, Settings(), "prompt", usage)

    assert dossier == "part one\npart two"
    assert usage.requests == 2 and usage.searches == 4 and usage.fetches == 6
    first, second = client.calls
    assert first["fallbacks"] == "default" and first["betas"] == [research.FALLBACK_BETA]
    assert [t["name"] for t in first["tools"]] == ["web_search", "web_fetch"]
    assert second["messages"][-1]["role"] == "assistant"


def test_research_refusal_raises():
    refused = _message("refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    with pytest.raises(research.ResearchError, match="declined"):
        research.run_research(FakeClient([refused]), Settings(), "prompt", research.RunUsage())


def test_editor_returns_parsed_newsletter():
    issue = sample_issue()
    client = FakeClient([_message("end_turn", "{}", parsed=issue)])
    result = research.run_editor(client, Settings(), "dossier", NOW, research.RunUsage())
    assert result is issue
    assert client.calls[0]["output_format"] is Newsletter


def test_research_prompt_lists_items_and_recent_headlines():
    item = Item("news", "TechCrunch AI", "Big launch", "https://example.com/x", NOW, summary="snippet")
    prompt = research.build_research_prompt(NOW, None, [item], [], ["Old story"])
    assert "[1] Big launch" in prompt
    assert "- Old story" in prompt
    assert "first issue" in prompt


# ---------------------------------------------------------------- email


def test_build_message_is_multipart():
    settings = Settings(smtp_username="bot@example.com", smtp_password="x", recipient="me@example.com")
    message = mailer.build_message(settings, "Subject", "plain", "<p>html</p>")
    assert message["To"] == "me@example.com"
    assert message["From"] == "ALPHAONE <bot@example.com>"
    assert [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "text/html"]


def test_send_requires_credentials():
    with pytest.raises(mailer.MailConfigError):
        mailer.send(Settings(smtp_username="", smtp_password=""), "s", "t", "h")


# ---------------------------------------------------------------- end to end


@pytest.fixture
def wired(monkeypatch, tmp_path):
    items = [Item("news", "TechCrunch AI", "Big launch", "https://example.com/x", datetime.now(timezone.utc))]
    monkeypatch.setattr(main.sources, "fetch_all", lambda: (items, {"TechCrunch AI": "ok (1)", "Wired AI": "failed: x"}))
    client = FakeClient([_message("end_turn", "dossier text"), _message("end_turn", "{}", parsed=sample_issue())])
    monkeypatch.setattr(main.anthropic, "Anthropic", lambda **kw: client)
    sent = []
    monkeypatch.setattr(main.mailer, "send", lambda settings, subject, text, html: sent.append(subject))
    settings = Settings(state_path=tmp_path / "state.json", out_dir=tmp_path / "out")
    return settings, sent


def test_full_run_sends_and_remembers(wired):
    settings, sent = wired
    assert main.run(settings, dry_run=False) == 0
    assert sent and sent[0].startswith("ALPHAONE #1 · SAMPLE")
    state = State.load(settings.state_path)
    assert "https://example.com/x" in state.seen
    assert state.issue_number == 1
    assert (settings.out_dir / "issue.html").exists()


def test_dry_run_sends_nothing_and_keeps_memory(wired):
    settings, sent = wired
    assert main.run(settings, dry_run=True) == 0
    assert sent == []
    assert not settings.state_path.exists()
    assert (settings.out_dir / "dossier.md").read_text() == "dossier text"


def test_subject_newlines_are_flattened(wired, monkeypatch):
    settings, sent = wired
    issue = sample_issue()
    issue.subject_line = "Line one\r\nBcc: someone@example.com"
    client = FakeClient([_message("end_turn", "dossier text"), _message("end_turn", "{}", parsed=issue)])
    monkeypatch.setattr(main.anthropic, "Anthropic", lambda **kw: client)
    main.run(settings, dry_run=False)
    assert "\n" not in sent[0] and "\r" not in sent[0]


def test_format_time_falls_back_on_bad_timezone():
    assert main.format_time(NOW, "Not/AZone") == "Mon 5 Oct 2026, 14:00 UTC"
    assert main.format_time(NOW, "Asia/Kolkata") == "Mon 5 Oct 2026, 19:30 IST"
