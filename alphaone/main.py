"""ALPHAONE - an autonomous agent that emails you an hourly AI briefing.

Usage:
    python main.py              # full run: sweep, research, write, email
    python main.py --dry-run    # everything except sending email / updating memory
    python main.py --preview    # render the sample issue to out/preview.html (no API calls)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic

import mailer
import sources
from config import HERE, Settings
from newsletter import IssueMeta, Newsletter, render_html, render_text
from research import RunUsage, build_research_prompt, run_editor, run_research
from state import State

log = logging.getLogger("alphaone")

# Approximate list prices (USD per million tokens: input, output, cache read) for the
# cost line in the run summary. Cache writes are billed at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
WEB_SEARCH_PRICE = 10.00 / 1000


def format_time(now: datetime, tz_name: str) -> str:
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        local = now
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M} {local.tzname()}"


def estimate_cost(model: str, usage: RunUsage) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_out, price_cache_read = PRICES[model]
    return (
        usage.input_tokens * price_in
        + usage.cache_write_tokens * price_in * 1.25
        + usage.cache_read_tokens * price_cache_read
        + usage.output_tokens * price_out
    ) / 1_000_000 + usage.searches * WEB_SEARCH_PRICE


def write_outputs(out_dir: Path, files: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content)


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


def preview(settings: Settings) -> int:
    issue = Newsletter.model_validate(json.loads((HERE / "tests" / "sample_issue.json").read_text()))
    meta = IssueMeta(
        issue_number=1,
        sent_at=format_time(datetime.now(timezone.utc), settings.timezone),
        sources_scanned=30,
        candidates=57,
        searches=9,
        pages_read=11,
    )
    write_outputs(settings.out_dir, {"preview.html": render_html(issue, meta), "preview.txt": render_text(issue, meta)})
    log.info("wrote %s", settings.out_dir / "preview.html")
    return 0


def run(settings: Settings, dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    state = State.load(settings.state_path)

    # 1. Sweep the feeds (free, fast) and keep only what's new.
    items, status = sources.fetch_all()
    healthy = sum(1 for s in status.values() if s.startswith("ok"))
    news, papers = sources.select_new(
        items,
        state.seen_keys(),
        now,
        settings.lookback_hours,
        settings.max_news_candidates,
        settings.max_paper_candidates,
    )
    log.info("sources: %d/%d healthy, %d items, %d new news, %d new papers", healthy, len(status), len(items), len(news), len(papers))

    # 2. Research desk: search the web and read full articles and papers.
    client = anthropic.Anthropic(max_retries=4)
    usage = RunUsage()
    prompt = build_research_prompt(now, state.last_run, news, papers, state.recent_headlines())
    dossier = run_research(client, settings, prompt, usage)

    # 3. Editor: turn the dossier into the newsletter.
    issue = run_editor(client, settings, dossier, now, usage)

    meta = IssueMeta(
        issue_number=state.issue_number + 1,
        sent_at=format_time(now, settings.timezone),
        sources_scanned=healthy,
        candidates=len(news) + len(papers),
        searches=usage.searches,
        pages_read=usage.fetches,
    )
    subject = " ".join(f"ALPHAONE #{meta.issue_number} · {issue.subject_line}".split())
    html_body = render_html(issue, meta)
    text_body = render_text(issue, meta)
    write_outputs(settings.out_dir, {"issue.html": html_body, "issue.txt": text_body, "dossier.md": dossier})

    # 4. Deliver.
    sent = False
    if dry_run:
        log.info("dry run - not sending. Newsletter written to %s", settings.out_dir)
    elif issue.quiet_hour and settings.skip_quiet_hours:
        log.info("quiet hour and ALPHAONE_SKIP_QUIET is on - no email this time")
    else:
        mailer.send(settings, subject, text_body, html_body)
        sent = True
        log.info("sent '%s' to %s", subject, settings.recipient)

    # 5. Remember what was covered so the next hour only brings new things.
    if not dry_run:
        covered = [{"headline": s.headline, "urls": [src.url for src in s.sources][:3]} for s in issue.stories]
        covered += [{"headline": f"Paper: {p.title}", "urls": [p.url]} for p in issue.papers]
        state.record_run(now, [i.key for i in news + papers], covered, sent=sent)
        state.save(settings.state_path)

    cost = estimate_cost(settings.model, usage)
    summary = [
        f"## ALPHAONE run - {meta.sent_at}",
        f"- Subject: {subject}",
        f"- Sent: {'yes' if sent else 'no'} (quiet hour: {issue.quiet_hour})",
        f"- Stories: {len(issue.stories)}, papers: {len(issue.papers)}",
        f"- Sources healthy: {healthy}/{len(status)}; new items considered: {meta.candidates}",
        f"- Claude: {usage.requests} requests, {usage.input_tokens:,} input / {usage.output_tokens:,} output tokens, "
        f"{usage.cache_read_tokens:,} cache-read, {usage.searches} searches, {usage.fetches} pages read",
    ]
    if cost is not None:
        summary.append(f"- Approximate cost this run: ${cost:.2f}")
    summary += [f"- Note: {n}" for n in usage.notes]
    failed = {name: s for name, s in status.items() if not s.startswith("ok")}
    if failed:
        summary.append("- Sources that failed this run: " + "; ".join(f"{k} ({v})" for k, v in failed.items()))
    for line in summary:
        log.info(line.lstrip("#- "))
    write_step_summary(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ALPHAONE - hourly AI news agent")
    parser.add_argument("--dry-run", action="store_true", help="don't send email or update memory")
    parser.add_argument("--preview", action="store_true", help="render the sample issue without calling any API")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if args.preview:
        return preview(settings)
    return run(settings, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
