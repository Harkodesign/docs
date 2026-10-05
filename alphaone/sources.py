"""Where ALPHAONE looks for news and research.

The feeds here are free and fast to poll, so every run starts by sweeping
them. Claude then goes further with live web search and by reading the full
articles and papers (see research.py). Add or remove feeds freely - a feed
that fails to load is logged and skipped, it never breaks a run.
"""

from __future__ import annotations

import gzip
import html
import json
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser

log = logging.getLogger("alphaone.sources")

USER_AGENT = "ALPHAONE-news-agent/1.0 (+https://github.com/Harkodesign/docs)"
TIMEOUT_SECONDS = 20

# Official blogs of the big AI companies and labs.
COMPANY_FEEDS: dict[str, str] = {
    "OpenAI": "https://openai.com/news/rss.xml",
    # Anthropic publishes no feed; this community mirror of anthropic.com/news updates hourly.
    "Anthropic (unofficial feed)": "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Google AI (The Keyword)": "https://blog.google/innovation-and-ai/technology/ai/rss/",
    "Google Research": "https://research.google/blog/rss/",
    "Microsoft News": "https://news.microsoft.com/source/feed/",
    "Microsoft Research": "https://www.microsoft.com/en-us/research/feed/",
    "NVIDIA Blog": "https://blogs.nvidia.com/feed/",
    "AWS Machine Learning": "https://aws.amazon.com/blogs/machine-learning/feed/",
    "Apple Machine Learning": "https://machinelearning.apple.com/rss.xml",
    "Mistral AI": "https://mistral.ai/rss.xml",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
}

# Tech press that covers AI closely.
PRESS_FEEDS: dict[str, str] = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "MIT Technology Review AI": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica AI": "https://arstechnica.com/ai/feed/",
    "Wired AI": "https://www.wired.com/feed/tag/ai/latest/rss",
    "Simon Willison": "https://simonwillison.net/atom/everything/",
}

# Google News searches catch companies that don't publish an RSS feed
# (Meta, xAI, the Chinese labs, ...) and coverage from any outlet.
# In Google News, OR binds tighter than the implicit AND between words.
NEWS_SEARCHES: dict[str, str] = {
    "OpenAI": '"OpenAI" OR ChatGPT',
    "Anthropic": '"Anthropic" OR "Claude AI"',
    "Google": '"Google DeepMind" OR "Google Gemini"',
    "Meta": '"Meta AI" OR "Meta Superintelligence" OR Llama Meta',
    "Microsoft": '"Microsoft" AI Copilot OR "Microsoft AI"',
    "NVIDIA": "NVIDIA AI chips OR GPU OR model",
    "Apple": '"Apple Intelligence" OR "Apple" AI model',
    "Amazon": '"Amazon" AI OR "AWS" Bedrock OR Nova model',
    "xAI": '"xAI" OR Grok',
    "Mistral": '"Mistral AI"',
    "Chinese labs": 'DeepSeek OR Qwen OR "Moonshot AI" OR Zhipu OR MiniMax',
    "AI policy": "AI regulation OR \"AI Act\" OR \"AI safety\" law",
}

# News ranking: official posts first, then the press, then Google News.
COMPANY_TIER, PRESS_TIER = 2, 1

ARXIV_QUERY = "cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:cs.CV"
ARXIV_URL = (
    "https://export.arxiv.org/api/query?"
    + urlencode(
        {
            "search_query": ARXIV_QUERY,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": "60",
        }
    )
)
HF_DAILY_PAPERS_URL = "https://huggingface.co/api/daily_papers"

_TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|cmpid|guccounter)$", re.I)
_ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


@dataclass
class Item:
    kind: str  # "news" or "paper"
    source: str
    title: str
    url: str
    published: datetime | None
    summary: str = ""
    extra: str = ""
    score: int = 0  # ranking weight: Hugging Face upvotes for papers, source tier for news

    @property
    def key(self) -> str:
        return item_key(self.url, self.title)

    def to_prompt_line(self, index: int) -> str:
        when = self.published.strftime("%Y-%m-%d %H:%M UTC") if self.published else "time unknown"
        parts = [f"[{index}] {self.title}", f"    source: {self.source} | {when}"]
        if self.extra:
            parts[-1] += f" | {self.extra}"
        parts.append(f"    url: {self.url}")
        match = _ARXIV_ID.search(self.url) if self.kind == "paper" else None
        if match:  # web fetch only opens URLs already in the conversation, so list the full text too
            parts.append(f"    full text: https://arxiv.org/html/{match.group(1)}")
        if self.summary:
            parts.append(f"    snippet: {self.summary}")
        return "\n".join(parts)


def item_key(url: str, title: str = "") -> str:
    """A stable identity for de-duplication across runs."""
    match = _ARXIV_ID.search(url or "")
    if match:
        return f"arxiv:{match.group(1)}"
    if url:
        return normalize_url(url)
    return "title:" + _SPACES.sub(" ", title.lower()).strip()


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:  # e.g. a malformed [IPv6] host; select_new runs outside the per-feed guard
        return url.strip()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    fragment = parts.fragment if parts.fragment.startswith(("/", "!")) else ""  # keep #/ and #! routes
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, fragment))


def clean_text(raw: str, limit: int = 320) -> str:
    text = _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", raw or ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "tm_year"):  # time.struct_time from feedparser (always UTC)
        return datetime(*value[:6], tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        return _parse_time(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return _parse_time(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = response.read()
    # Some CDNs (deepmind.google) send gzip even when it wasn't asked for, and urllib doesn't unzip.
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


# ---------------------------------------------------------------- parsers


def _parse_xml_feed(data: bytes):
    feed = feedparser.parse(data)
    if not feed.get("version") and not feed.entries:  # a bot wall or error page, not an empty feed
        raise ValueError(f"not an RSS/Atom feed ({len(data)} bytes)")
    return feed


def parse_feed(data: bytes, source: str, kind: str = "news", score: int = 0) -> list[Item]:
    feed = _parse_xml_feed(data)
    items = []
    for entry in feed.entries:
        title = clean_text(entry.get("title", ""), limit=240)
        url = entry.get("link", "")
        if not title or not url:
            continue
        published = _parse_time(entry.get("published_parsed") or entry.get("updated_parsed"))
        summary = clean_text(entry.get("summary", ""))
        if summary.startswith(title.rsplit(" - ", 1)[0]) and len(summary) < len(title) + 40:
            summary = ""  # the snippet only repeats the title (all Google News items do this)
        publisher = (entry.get("source") or {}).get("title", "")
        label = f"{source} via {publisher}" if publisher and publisher not in source else source
        items.append(
            Item(kind=kind, source=label, title=title, url=url, published=published, summary=summary, score=score)
        )
    return items


def parse_arxiv(data: bytes) -> list[Item]:
    feed = _parse_xml_feed(data)
    items = []
    for entry in feed.entries:
        # The Atom id is http://arxiv.org/abs/ID; use https, as the HF items and arXiv's own links do.
        url = (entry.get("id") or entry.get("link", "")).replace("http://", "https://", 1)
        authors = ", ".join(a.get("name", "") for a in entry.get("authors", [])[:4])
        if len(entry.get("authors", [])) > 4:
            authors += " et al."
        items.append(
            Item(
                kind="paper",
                source="arXiv",
                title=clean_text(entry.get("title", ""), limit=240),
                url=url,
                published=_parse_time(entry.get("published_parsed")),
                summary=clean_text(entry.get("summary", ""), limit=400),
                extra=authors,
            )
        )
    return [i for i in items if i.title and i.url]


def parse_hf_daily_papers(data: bytes) -> list[Item]:
    items = []
    for entry in json.loads(data):
        paper = entry.get("paper") or {}
        if not isinstance(paper, dict):
            continue
        paper_id = paper.get("id") or ""
        title = clean_text(paper.get("title") or entry.get("title") or "", limit=240)
        if not paper_id or not title:
            continue
        upvotes = paper.get("upvotes")
        upvotes = upvotes if isinstance(upvotes, int) else 0
        authors = [a.get("name", "") for a in (paper.get("authors") or [])[:4] if isinstance(a, dict)]
        extra = f"{upvotes} upvotes on Hugging Face"
        if authors:
            extra += " | " + ", ".join(authors) + (" et al." if len(paper.get("authors") or []) > 4 else "")
        items.append(
            Item(
                kind="paper",
                source="Hugging Face Daily Papers",
                title=title,
                url=f"https://arxiv.org/abs/{paper_id}",
                # When it was featured; publishedAt is the arXiv date, often days older.
                published=_parse_time(paper.get("submittedOnDailyAt") or entry.get("publishedAt")),
                summary=clean_text(paper.get("summary", ""), limit=400),
                extra=extra,
                score=upvotes,
            )
        )
    return items


# ---------------------------------------------------------------- gathering


def google_news_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:1d')}&hl=en-US&gl=US&ceid=US:en"


def _jobs() -> list[tuple[str, str, Callable[[bytes], list[Item]]]]:
    jobs: list[tuple[str, str, Callable[[bytes], list[Item]]]] = []
    for tier, feeds in ((COMPANY_TIER, COMPANY_FEEDS), (PRESS_TIER, PRESS_FEEDS)):
        for name, url in feeds.items():
            jobs.append((name, url, lambda data, n=name, t=tier: parse_feed(data, n, score=t)))
    for name, query in NEWS_SEARCHES.items():
        label = f"Google News: {name}"
        jobs.append((label, google_news_url(query), lambda data, n=label: parse_feed(data, n)))
    jobs.append(("arXiv", ARXIV_URL, parse_arxiv))
    jobs.append(("Hugging Face Daily Papers", HF_DAILY_PAPERS_URL, parse_hf_daily_papers))
    return jobs


def fetch_all(fetch: Callable[[str], bytes] = _http_get) -> tuple[list[Item], dict[str, str]]:
    """Poll every source in parallel. Returns (items, per-source status)."""
    jobs = _jobs()
    status: dict[str, str] = {}

    def run(job):
        name, url, parser = job
        try:
            items = parser(fetch(url))
            status[name] = f"ok ({len(items)})"
            return items
        except Exception as exc:  # one broken feed must never stop the run
            status[name] = f"failed: {type(exc).__name__}: {exc}"[:200]
            log.warning("source %s failed: %s", name, exc)
            return []

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(run, jobs))
    return [item for batch in results for item in batch], status


def select_new(
    items: Iterable[Item],
    seen: set[str],
    now: datetime,
    lookback_hours: int,
    max_news: int,
    max_papers: int,
    paper_lookback_hours: int = 96,
) -> tuple[list[Item], list[Item]]:
    """Drop duplicates, already-seen and stale items; rank and cap the rest.

    Papers get a longer window: arXiv dates are submission times, and the batch
    announced on Sunday night is already two days old when it appears.
    """
    cutoff = {
        "news": now - timedelta(hours=lookback_hours),
        "paper": now - timedelta(hours=paper_lookback_hours),
    }
    unique: dict[str, Item] = {}
    for item in items:
        if item.key in seen:
            continue
        when = item.published
        if when and item.kind == "news" and when.time() == datetime.min.time():
            # Date-only feeds (the Anthropic mirror) stamp every post 00:00 UTC: count the whole
            # listed day, or a post from late that day expires before it reaches the feed.
            when += timedelta(days=1)
        if when and when < cutoff[item.kind]:
            continue
        current = unique.get(item.key)
        # Keep the richer copy when the same link shows up in several feeds.
        if current is None or (item.score, len(item.summary)) > (current.score, len(current.summary)):
            unique[item.key] = item

    # Highest score first (official posts for news, upvotes for papers), then newest.
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def ranked(kind: str) -> list[Item]:
        return sorted(
            (i for i in unique.values() if i.kind == kind),
            key=lambda i: (i.score, i.published or oldest),
            reverse=True,
        )

    return ranked("news")[:max_news], ranked("paper")[:max_papers]
