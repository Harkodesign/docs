import json
from datetime import datetime, timedelta, timezone

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
