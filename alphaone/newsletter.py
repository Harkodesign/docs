"""The newsletter's shape and how it's rendered into an email.

Claude fills in the `Newsletter` model (structured output), and this module
turns it into a mobile-friendly HTML email plus a plain-text fallback.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- schema


class Source(BaseModel):
    title: str = Field(description="Publisher and title, e.g. 'OpenAI blog: Introducing ...'")
    url: str = Field(description="Full https URL of the source")


class Story(BaseModel):
    headline: str = Field(description="Clear, specific headline. No clickbait.")
    company: str = Field(description="Main company or lab involved, e.g. 'OpenAI', 'Google DeepMind'")
    category: str = Field(
        description="One of: Model launch, Product, Research, Funding & deals, Policy & legal, "
        "Hardware & compute, Partnership, People, Safety"
    )
    impact: Literal["high", "medium", "low"] = Field(description="How much this changes the AI landscape")
    one_liner: str = Field(description="The whole story in one plain-English sentence")
    what_happened: str = Field(description="2-4 short sentences: the facts, with concrete numbers and dates")
    why_it_matters: str = Field(description="2-3 sentences on the significance for the industry and for users")
    details: list[str] = Field(
        description="4-8 bullets for the detailed overview: specs, benchmarks, pricing, availability, "
        "quotes, context, competitive picture. Each bullet one idea, plain language."
    )
    what_to_watch: str = Field(description="One sentence: what to look out for next")
    sources: list[Source] = Field(description="Primary source first, then the best coverage")


class Paper(BaseModel):
    title: str
    authors_or_lab: str = Field(description="Lab or company if known, else lead authors")
    url: str = Field(description="Link to the paper (arXiv abs page preferred)")
    one_liner: str = Field(description="What this paper does, in one sentence a non-expert understands")
    the_problem: str = Field(description="1-2 sentences: what problem it tackles and why it is hard")
    the_big_idea: str = Field(
        description="2-4 sentences explaining the core idea simply, ideally with an everyday analogy"
    )
    key_results: list[str] = Field(description="2-5 bullets with the headline results, numbers included")
    why_it_matters: str = Field(description="1-2 sentences on the practical significance")
    caveats: str = Field(description="1-2 sentences: limitations, open questions, or reasons for caution")


class GlossaryTerm(BaseModel):
    term: str
    meaning: str = Field(description="One-sentence plain-English definition")


class Newsletter(BaseModel):
    subject_line: str = Field(description="Email subject: the 1-2 biggest items, under 90 characters")
    headline_summary: str = Field(description="The hour in one breath: 1-2 sentences")
    brief: list[str] = Field(
        description="The 60-second brief: 3-7 bullets, one per development, each a single sentence"
    )
    stories: list[Story] = Field(description="Top stories, most important first")
    papers: list[Paper] = Field(description="Research papers worth knowing about, most important first")
    radar: list[str] = Field(description="Smaller items worth a glance, one sentence each")
    glossary: list[GlossaryTerm] = Field(description="Jargon used in this issue, explained")
    quiet_hour: bool = Field(description="True if nothing significant and new happened this hour")


# ---------------------------------------------------------------- rendering


@dataclass
class IssueMeta:
    issue_number: int
    sent_at: str  # already formatted for display, e.g. "Mon 5 Oct 2026, 14:07 UTC"
    sources_scanned: int
    candidates: int
    searches: int
    pages_read: int


ACCENT = "#FF4C00"
INK = "#1d1b19"
MUTED = "#6b645c"
PAPER_BG = "#f5efe5"
CARD_BG = "#ffffff"
RULE = "#e7ded1"
IMPACT_COLORS = {"high": "#c0392b", "medium": "#d97706", "low": "#6b645c"}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _safe_url(url: str) -> str | None:
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else None


def _link(text: str, url: str) -> str:
    safe = _safe_url(url)
    if not safe:
        return _e(text)
    return f'<a href="{_e(safe)}" style="color:{ACCENT};text-decoration:underline;">{_e(text)}</a>'


def _section_title(text: str) -> str:
    return (
        f'<tr><td style="padding:28px 28px 8px 28px;">'
        f'<div style="font:700 12px/1.4 {FONT};letter-spacing:2px;text-transform:uppercase;color:{ACCENT};">'
        f"{_e(text)}</div></td></tr>"
    )


def _label(text: str) -> str:
    return (
        f'<div style="font:700 11px/1.4 {FONT};letter-spacing:1px;text-transform:uppercase;'
        f'color:{MUTED};margin:14px 0 4px 0;">{_e(text)}</div>'
    )


def _para(text: str, size: int = 15) -> str:
    return f'<p style="margin:0;font:400 {size}px/1.6 {FONT};color:{INK};">{_e(text)}</p>'


def _bullets(items: list[str]) -> str:
    lis = "".join(
        f'<li style="margin:0 0 6px 0;font:400 15px/1.55 {FONT};color:{INK};">{_e(i)}</li>' for i in items
    )
    return f'<ul style="margin:0;padding:0 0 0 20px;">{lis}</ul>'


def _pill(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:2px 8px;margin:0 6px 6px 0;border-radius:10px;'
        f'border:1px solid {color};color:{color};font:600 11px/1.6 {FONT};">{_e(text)}</span>'
    )


def _card(inner: str) -> str:
    return (
        f'<tr><td style="padding:8px 20px;">'
        f'<div style="background:{CARD_BG};border:1px solid {RULE};border-radius:12px;padding:20px;">'
        f"{inner}</div></td></tr>"
    )


def _story_html(index: int, story: Story) -> str:
    pills = _pill(story.company, ACCENT) + _pill(story.category, MUTED)
    pills += _pill(f"{story.impact} impact", IMPACT_COLORS.get(story.impact, MUTED))
    sources = " · ".join(_link(s.title, s.url) for s in story.sources)
    inner = (
        f"<div>{pills}</div>"
        f'<div style="font:700 19px/1.35 {FONT};color:{INK};margin:4px 0 8px 0;">'
        f"{index}. {_e(story.headline)}</div>"
        f'<p style="margin:0;font:600 15px/1.55 {FONT};color:{INK};">{_e(story.one_liner)}</p>'
        + _label("What happened")
        + _para(story.what_happened)
        + _label("Why it matters")
        + _para(story.why_it_matters)
        + _label("The details")
        + _bullets(story.details)
        + _label("What to watch")
        + _para(story.what_to_watch)
        + (_label("Sources") + f'<p style="margin:0;font:400 13px/1.6 {FONT};">{sources}</p>' if sources else "")
    )
    return _card(inner)


def _paper_html(paper: Paper) -> str:
    inner = (
        f'<div style="font:700 17px/1.35 {FONT};color:{INK};margin:0 0 4px 0;">{_link(paper.title, paper.url)}</div>'
        f'<div style="font:400 13px/1.5 {FONT};color:{MUTED};margin:0 0 10px 0;">{_e(paper.authors_or_lab)}</div>'
        f'<p style="margin:0;font:600 15px/1.55 {FONT};color:{INK};">{_e(paper.one_liner)}</p>'
        + _label("The problem")
        + _para(paper.the_problem)
        + _label("The big idea, simply")
        + _para(paper.the_big_idea)
        + _label("Key results")
        + _bullets(paper.key_results)
        + _label("Why it matters")
        + _para(paper.why_it_matters)
        + _label("Caveats")
        + _para(paper.caveats)
    )
    return _card(inner)


def render_html(issue: Newsletter, meta: IssueMeta) -> str:
    rows: list[str] = []

    rows.append(
        f'<tr><td style="background:{INK};padding:26px 28px;border-radius:14px 14px 0 0;">'
        f'<div style="font:800 26px/1.1 {FONT};letter-spacing:6px;color:#ffffff;">ALPHA<span style="color:{ACCENT};">ONE</span></div>'
        f'<div style="font:400 13px/1.5 {FONT};color:#cfc6ba;margin-top:6px;">'
        f"Hourly AI briefing · Issue #{meta.issue_number} · {_e(meta.sent_at)}</div></td></tr>"
    )
    rows.append(
        f'<tr><td style="padding:24px 28px 4px 28px;">'
        f'<div style="font:700 11px/1.4 {FONT};letter-spacing:2px;text-transform:uppercase;color:{MUTED};">The hour in one breath</div>'
        f'<div style="font:600 20px/1.45 {FONT};color:{INK};margin-top:8px;">{_e(issue.headline_summary)}</div>'
        f"</td></tr>"
    )

    if issue.brief:
        rows.append(_section_title("60-second brief"))
        rows.append(_card(_bullets(issue.brief)))

    if issue.stories:
        rows.append(_section_title("Top stories · detailed overview"))
        rows.extend(_story_html(i, s) for i, s in enumerate(issue.stories, start=1))

    if issue.papers:
        rows.append(_section_title("Research radar · papers explained"))
        rows.extend(_paper_html(p) for p in issue.papers)

    if issue.radar:
        rows.append(_section_title("Also on the radar"))
        rows.append(_card(_bullets(issue.radar)))

    if issue.glossary:
        rows.append(_section_title("Jargon buster"))
        terms = "".join(
            f'<p style="margin:0 0 8px 0;font:400 14px/1.55 {FONT};color:{INK};">'
            f"<strong>{_e(t.term)}</strong> — {_e(t.meaning)}</p>"
            for t in issue.glossary
        )
        rows.append(_card(terms))

    rows.append(
        f'<tr><td style="padding:24px 28px 32px 28px;font:400 12px/1.6 {FONT};color:{MUTED};">'
        f"This hour ALPHAONE swept {meta.sources_scanned} sources, weighed {meta.candidates} new items, "
        f"ran {meta.searches} web searches and read {meta.pages_read} full articles and papers. "
        f"Next briefing in about an hour.<br>ALPHAONE is an AI agent: it reads primary sources, "
        f"but double-check anything you plan to act on.</td></tr>"
    )

    body = "".join(rows)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(issue.subject_line)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAPER_BG};">'
        f'<div style="display:none;max-height:0;overflow:hidden;">{_e(issue.headline_summary)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER_BG};">'
        f'<tr><td align="center" style="padding:20px 10px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:680px;background:{PAPER_BG};">{body}</table>'
        f"</td></tr></table></body></html>"
    )


def render_text(issue: Newsletter, meta: IssueMeta) -> str:
    out: list[str] = [
        f"ALPHAONE · Hourly AI briefing · Issue #{meta.issue_number} · {meta.sent_at}",
        "",
        issue.headline_summary,
        "",
    ]
    if issue.brief:
        out += ["60-SECOND BRIEF", *[f"• {b}" for b in issue.brief], ""]
    if issue.stories:
        out.append("TOP STORIES")
        for i, s in enumerate(issue.stories, start=1):
            out += [
                "",
                f"{i}. {s.headline}  [{s.company} · {s.category} · {s.impact} impact]",
                s.one_liner,
                f"What happened: {s.what_happened}",
                f"Why it matters: {s.why_it_matters}",
                "The details:",
                *[f"  - {d}" for d in s.details],
                f"What to watch: {s.what_to_watch}",
                "Sources:",
                *[f"  {src.title}: {src.url}" for src in s.sources],
            ]
        out.append("")
    if issue.papers:
        out.append("RESEARCH RADAR")
        for p in issue.papers:
            out += [
                "",
                f"{p.title} ({p.authors_or_lab})",
                p.url,
                p.one_liner,
                f"The problem: {p.the_problem}",
                f"The big idea: {p.the_big_idea}",
                "Key results:",
                *[f"  - {r}" for r in p.key_results],
                f"Why it matters: {p.why_it_matters}",
                f"Caveats: {p.caveats}",
            ]
        out.append("")
    if issue.radar:
        out += ["ALSO ON THE RADAR", *[f"• {r}" for r in issue.radar], ""]
    if issue.glossary:
        out += ["JARGON BUSTER", *[f"{t.term}: {t.meaning}" for t in issue.glossary], ""]
    out.append(
        f"Swept {meta.sources_scanned} sources, weighed {meta.candidates} new items, "
        f"ran {meta.searches} searches, read {meta.pages_read} pages."
    )
    return "\n".join(out)
