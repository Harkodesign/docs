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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic

import mailer
import sources
from config import HERE, Settings
from newsletter import IssueMeta, Newsletter, render_html, render_text
from research import RefusalError, RunUsage, build_research_prompt, run_editor, run_research
from state import State

log = logging.getLogger("alphaone")

# Approximate list prices (USD per million tokens: input, output, cache read) for the
# cost line in the run summary. Cache writes are billed at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    # Server-side fallback targets: a declined request is re-run on these at their own rates.
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
WEB_SEARCH_PRICE = 10.00 / 1000
GMAIL_CLIP_BYTES = 102_000  # Gmail hides the rest of a longer message behind "View entire message"


def format_time(now: datetime, tz_name: str) -> str:
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        local = now
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M} {local.tzname()}"


def estimate_cost(usage: RunUsage) -> float | None:
    if any(model not in PRICES for model in usage.by_model):
        return None
    tokens = 0.0
    for model, (inp, out, cache_read, cache_write) in usage.by_model.items():
        price_in, price_out, price_cache_read = PRICES[model]
        tokens += inp * price_in + cache_write * price_in * 1.25 + cache_read * price_cache_read + out * price_out
    return tokens / 1_000_000 + usage.searches * WEB_SEARCH_PRICE


def recent_last_run(last_run: str | None, now: datetime, lookback_hours: int) -> str | None:
    """The last briefing's time, or None when it is older than the window the feeds cover."""
    try:
        when = datetime.fromisoformat(last_run or "")
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return last_run if now - when <= timedelta(hours=lookback_hours) else None


def write_outputs(out_dir: Path, files: dict[str, str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8")


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def warn_in_actions(text: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{text}", flush=True)  # shows on the run's page


def preview(settings: Settings) -> int:
    issue = Newsletter.model_validate(json.loads((HERE / "tests" / "sample_issue.json").read_text(encoding="utf-8")))
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
    if not dry_run:
        mailer.verify(settings)  # bad SMTP settings fail here, before any paid Claude work
    state = State.load(settings.state_path)
    state.prune(now)  # forget old headlines before they reach the prompt

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
        settings.paper_lookback_hours,
    )
    considered = [i.key for i in news + papers]
    log.info("sources: %d/%d healthy, %d items, %d new news, %d new papers", healthy, len(status), len(items), len(news), len(papers))

    client = anthropic.Anthropic(max_retries=4)
    usage = RunUsage()
    summary = [f"## ALPHAONE run - {format_time(now, settings.timezone)}"]
    try:
        # 2. Research desk: search the web and read full articles and papers.
        # After a long gap the feeds only reach back lookback_hours, so cover that window.
        last_run = recent_last_run(state.last_run, now, settings.lookback_hours)
        prompt = build_research_prompt(now, last_run, news, papers, state.recent_headlines(), settings.lookback_hours)
        try:
            dossier = run_research(client, settings, prompt, usage)
            write_outputs(settings.out_dir, {"dossier.md": dossier})  # kept even if a later step fails
            # 3. Editor: turn the dossier into the newsletter.
            issue = run_editor(client, settings, dossier, now, usage, reader_time=format_time(now, settings.timezone))
        except RefusalError as exc:
            # Claude and its fallback model declined. Skip these items so the next hour moves on
            # instead of paying to fail on them again, and exit cleanly so the memory is saved.
            if dry_run:
                fate = "would be skipped on a real run (dry run: memory unchanged)"
            else:
                fate = "won't be offered again"
                state.record_run(now, considered, [], sent=False)
                state.save(settings.state_path)
            log.warning("%s - no email this hour; its %d feed items %s", exc, len(considered), fate)
            summary.append(f"- No email this hour: {exc}. Its {len(considered)} feed items {fate}.")
            warn_in_actions(f"ALPHAONE skipped an hour: {exc}")
            return 0

        meta = IssueMeta(
            issue_number=state.issue_number + 1,
            sent_at=format_time(now, settings.timezone),
            sources_scanned=healthy,
            candidates=len(considered),
            searches=usage.searches,
            pages_read=usage.fetches,
        )
        subject = " ".join(f"ALPHAONE #{meta.issue_number} · {issue.subject_line}".split())
        html_body = render_html(issue, meta)
        text_body = render_text(issue, meta)
        write_outputs(settings.out_dir, {"issue.html": html_body, "issue.txt": text_body})
        size = len(html_body.encode("utf-8"))
        if size > GMAIL_CLIP_BYTES * 0.9:
            usage.note(f"the HTML email is {size // 1000} KB; Gmail clips messages over about 102 KB")

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
            # The links that were sent count as seen too, so an item found by web search doesn't
            # come back in a feed next hour. (Radar items only get this, to keep the prompt short.)
            sent_urls = [url for entry in covered for url in entry["urls"]] + [r.url for r in issue.radar]
            covered_keys = [sources.item_key(url) for url in sent_urls if url] if sent else []
            state.record_run(now, considered + covered_keys, covered, sent=sent)
            state.save(settings.state_path)

        summary += [
            f"- Subject: {subject}",
            f"- Sent: {'yes' if sent else 'no'} (quiet hour: {issue.quiet_hour})",
            f"- Stories: {len(issue.stories)}, papers: {len(issue.papers)}, radar: {len(issue.radar)}",
        ]
        return 0
    except Exception as exc:
        summary.append(f"- Run failed: {type(exc).__name__}: {exc}")
        raise
    finally:
        # Written even when the run fails, so a billed run always shows what it cost.
        cost = estimate_cost(usage)
        summary += [
            f"- Sources healthy: {healthy}/{len(status)}; new items considered: {len(considered)}",
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
