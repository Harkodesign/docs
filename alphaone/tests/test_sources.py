import gzip
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

import sources
from sources import Item, item_key, select_new

NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
<item><title>Lab ships &lt;b&gt;new&lt;/b&gt; model</title>
<link>https://example.com/post?utm_source=rss&amp;id=7</link>
<pubDate>Mon, 05 Oct 2026 13:00:00 GMT</pubDate>
<description>&lt;p&gt;A short &lt;em&gt;summary&lt;/em&gt;.&lt;/p&gt;</description>
<source url="https://publisher.example">Publisher Daily</source></item>
<item><title></title><link>https://example.com/empty</link></item>
</channel></rss>"""

ARXIV = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2610.01234v2</id>
<published>2026-10-05T10:00:00Z</published>
<title>Squeeze:
  Compressing Caches</title>
<summary>We compress things.</summary>
<author><name>A. Author</name></author><author><name>B. Author</name></author>
</entry></feed>"""

HF = json.dumps(
    [
        {
            "publishedAt": "2026-10-05T08:00:00.000Z",
            "paper": {
                "id": "2610.04321",
                "title": "Popular Paper",
                "summary": "Big results.",
                "upvotes": 120,
                "authors": [{"name": "C. Author"}],
            },
        },
        {"paper": {"id": "", "title": "No id"}},
    ]
).encode()


def test_parse_feed_cleans_html_and_keeps_publisher():
    items = sources.parse_feed(RSS, "Google News: Example")
    assert len(items) == 1
    item = items[0]
    assert item.title == "Lab ships new model"
    assert item.summary == "A short summary ."
    assert item.source == "Google News: Example via Publisher Daily"
    assert item.published == datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)


def test_parse_arxiv():
    [paper] = sources.parse_arxiv(ARXIV)
    assert paper.kind == "paper"
    assert paper.title == "Squeeze: Compressing Caches"
    assert paper.extra == "A. Author, B. Author"
    assert paper.key == "arxiv:2610.01234"
    assert paper.url == "https://arxiv.org/abs/2610.01234v2"  # https, like the HF items and arXiv's links
    assert "full text: https://arxiv.org/html/2610.01234" in paper.to_prompt_line(1)


def test_parse_hf_daily_papers():
    [paper] = sources.parse_hf_daily_papers(HF)
    assert paper.url == "https://arxiv.org/abs/2610.04321"
    assert paper.score == 120
    assert "120 upvotes" in paper.extra


def test_item_key_normalises_urls_and_arxiv_versions():
    assert item_key("https://Example.com/a/?utm_source=x&id=1#top") == "https://example.com/a?id=1"
    assert item_key("https://arxiv.org/pdf/2610.01234v3") == item_key("https://arxiv.org/abs/2610.01234")
    assert item_key("", "Some  Title") == "title:some title"


def _item(url, hours_ago=1, kind="news", score=0, summary=""):
    return Item(kind, "src", url, url, NOW - timedelta(hours=hours_ago), summary=summary, score=score)


def test_select_new_filters_seen_stale_and_duplicates():
    items = [
        _item("https://a.example/1", hours_ago=1),
        _item("https://a.example/1?utm_medium=feed", hours_ago=1, summary="richer copy"),
        _item("https://a.example/old", hours_ago=30),
        _item("https://a.example/seen", hours_ago=1),
        _item("https://a.example/newest", hours_ago=0),
        _item("https://arxiv.org/abs/2610.00001", kind="paper", score=5),
        _item("https://arxiv.org/abs/2610.00002", kind="paper", score=50),
    ]
    news, papers = select_new(items, {"https://a.example/seen"}, NOW, 24, max_news=10, max_papers=1)
    assert [i.url for i in news] == ["https://a.example/newest", "https://a.example/1?utm_medium=feed"]
    assert news[1].summary == "richer copy"
    assert [p.url for p in papers] == ["https://arxiv.org/abs/2610.00002"]


def test_fetch_all_survives_broken_sources():
    def fake_fetch(url):
        if "arxiv" in url:
            return ARXIV
        if "huggingface.co/api" in url:
            return HF
        if "openai.com" in url:
            raise TimeoutError("slow feed")
        return RSS

    items, status = sources.fetch_all(fetch=fake_fetch)
    assert status["OpenAI"].startswith("failed: TimeoutError")
    assert status["arXiv"] == "ok (1)"
    assert any(i.kind == "paper" for i in items)
    assert sum(1 for s in status.values() if s.startswith("ok")) == len(status) - 1


def test_hf_daily_papers_are_dated_by_the_day_they_were_featured():
    data = json.dumps(
        [
            {
                "publishedAt": "2026-10-01T20:00:00.000Z",
                "paper": {
                    "id": "2610.04321",
                    "title": "P",
                    "upvotes": 3,
                    "publishedAt": "2026-10-02T00:00:00.000Z",
                    "submittedOnDailyAt": "2026-10-05T00:00:00.000Z",
                },
            },
            {"paper": "not a dict"},
            {"paper": {"id": "2610.0001", "title": "Odd", "upvotes": "1.2k", "authors": None}},
        ]
    ).encode()
    paper, odd = sources.parse_hf_daily_papers(data)
    assert paper.published == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert select_new([paper], set(), NOW, 24, 10, 10)[1] == [paper]
    assert odd.score == 0


def test_select_new_gives_papers_a_longer_lookback():
    sunday_batch = _item("https://arxiv.org/abs/2610.00003", hours_ago=54, kind="paper")
    old_news = _item("https://a.example/old", hours_ago=54)
    news, papers = select_new([sunday_batch, old_news], set(), NOW, 24, 10, 10, paper_lookback_hours=96)
    assert news == [] and papers == [sunday_batch]


def test_select_new_ranks_official_posts_above_google_news():
    official = _item("https://openai.com/index/post", hours_ago=7, score=sources.COMPANY_TIER)
    press = _item("https://techcrunch.com/post", hours_ago=5, score=sources.PRESS_TIER)
    searches = [_item(f"https://news.google.com/rss/articles/{n}", hours_ago=0) for n in range(5)]
    news, _ = select_new([*searches, press, official], set(), NOW, 24, max_news=3, max_papers=0)
    assert news[:2] == [official, press] and len(news) == 3


def test_fetch_all_tags_news_by_source_tier():
    items, _ = sources.fetch_all(fetch=lambda url: HF if "huggingface.co/api" in url else ARXIV if "arxiv" in url else RSS)
    scores = {i.source.split(" via ")[0]: i.score for i in items if i.kind == "news"}
    assert scores["OpenAI"] == sources.COMPANY_TIER
    assert scores["TechCrunch AI"] == sources.PRESS_TIER
    assert scores["Google News: OpenAI"] == 0


def test_select_new_survives_malformed_links():
    bad, good = _item("https://[not-a-host]/post"), _item("https://a.example/ok")
    news, _ = select_new([bad, good], set(), NOW, 24, max_news=10, max_papers=10)
    assert {i.url for i in news} == {bad.url, good.url}


def test_item_key_keeps_fragment_routes_only():
    assert item_key("https://site.example/#/post/1") != item_key("https://site.example/#/post/2")
    assert item_key("https://site.example/#!/post/3") == "https://site.example/#!/post/3"
    assert item_key("https://example.com/a#comments") == "https://example.com/a"


def test_http_get_unzips_gzip_sent_unasked(monkeypatch):
    for body in (gzip.compress(RSS), RSS):
        monkeypatch.setattr(sources.urllib.request, "urlopen", lambda request, timeout, b=body: io.BytesIO(b))
        assert len(sources.parse_feed(sources._http_get("https://example.com/feed"), "x")) == 1


def test_parse_feed_rejects_pages_that_are_not_feeds():
    with pytest.raises(ValueError):
        sources.parse_feed(b"<html><head><title>Just a moment...</title></head><body></body></html>", "x")
    with pytest.raises(ValueError):
        sources.parse_feed(gzip.compress(RSS), "x")
    assert sources.parse_feed(b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>', "x") == []


def test_google_news_snippets_that_repeat_the_title_are_dropped():
    rss = RSS.replace(b"&lt;p&gt;A short &lt;em&gt;summary&lt;/em&gt;.&lt;/p&gt;", b"Lab ships new model&amp;nbsp;&amp;nbsp;Publisher Daily")
    rss = rss.replace(b"Lab ships &lt;b&gt;new&lt;/b&gt; model</title>", b"Lab ships new model - Publisher Daily</title>")
    [item] = sources.parse_feed(rss, "Google News: Example")
    assert item.summary == ""


def test_date_only_news_counts_its_whole_listed_day():
    # The Anthropic mirror stamps every post 00:00 UTC of its listed day.
    listed_yesterday = Item("news", "src", "t", "https://a.example/late-post", datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert select_new([listed_yesterday], set(), NOW, 24, 10, 10)[0] == [listed_yesterday]
    two_days_ago = Item("news", "src", "t", "https://a.example/older", datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert select_new([two_days_ago], set(), NOW, 24, 10, 10)[0] == []
    timed = Item("news", "src", "t", "https://a.example/timed", datetime(2026, 10, 4, 0, 0, 1, tzinfo=timezone.utc))
    assert select_new([timed], set(), NOW, 24, 10, 10)[0] == []
