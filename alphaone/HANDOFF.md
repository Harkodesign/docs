# ALPHAONE — handoff to continue locally

In your local clone of `Harkodesign/docs`, run `git fetch origin && git checkout claude/loving-clarke-fbnt2f`,
start `claude`, and say: "Read alphaone/HANDOFF.md and continue." Delete this file before merging PR #1.

## What ALPHAONE is

An agent that runs hourly on GitHub Actions. It sweeps ~30 AI news and research sources,
has Claude (claude-opus-5-5) search the web and read the full articles and papers, writes a
newsletter, and emails it to harkomal.design@gmail.com. The newsletter has: a one-breath
summary, a 60-second brief, detailed top stories, papers explained simply, "also on the
radar", and a jargon buster.

## Where things are

- Repo: `Harkodesign/docs` (a Mintlify docs site; ALPHAONE was added alongside it)
- Branch: `claude/loving-clarke-fbnt2f`, PR: https://github.com/Harkodesign/docs/pull/1 (open, mergeable)
- Pushed commit: `20f9122 Add ALPHAONE, an hourly AI news briefing agent`
- Files:
  - `.github/workflows/alphaone.yml`: cron `7 * * * *`, Actions-cache memory, artifact upload, `workflow_dispatch` with a dry_run input
  - `alphaone/main.py`: orchestration (sweep → research → edit → render → send → remember); `--dry-run`, `--preview`
  - `alphaone/sources.py`: feeds, Google News RSS searches, arXiv API, HF daily papers; dedupe and ranking
  - `alphaone/research.py`: Claude research pass (web_search_20260209 + web_fetch_20260209, streaming, pause_turn continuation, `fallbacks="default"` with beta `server-side-fallback-2026-07-01`) and editor pass (structured output into the `Newsletter` pydantic model)
  - `alphaone/newsletter.py`: schema, plus inline-CSS HTML email and plain-text rendering
  - `alphaone/mailer.py`: SMTP (Gmail SSL 465 by default, STARTTLS otherwise)
  - `alphaone/state.py`, `alphaone/config.py`, `alphaone/README.md` (setup guide), `alphaone/tests/` (20 offline tests, all passing)

Run locally:
```bash
cd alphaone
pip install -r requirements.txt pytest
python -m pytest -q          # offline tests
python main.py --preview     # sample issue -> out/preview.html, no API calls
```

## Status

- Code complete and pushed; 20/20 tests pass. The SDK request shapes were checked against a local fake server.
- **Never run live yet**: there was no API key in the cloud session, and its network blocked the news sites.
- A 6-dimension review was stopped partway (to save credits). Four reviewers finished, with 30 findings.
  These findings were **not yet double-checked**, so verify each one against the code before fixing.
  The two that didn't run: news-source URL validity, and fit to the request / README accuracy.

## Review findings to verify and fix (most important first)

High:
1. **SMTP credentials are checked only after the paid Claude passes** (`main.py` run()). Fail fast before any API spend when not a dry run: check that SMTP_USERNAME and SMTP_PASSWORD are set, and ideally add `mailer.verify()` that connects and logs in, called before `sources.fetch_all()`. Optionally add a preflight secrets check in the workflow.
2. **The prompt tells Claude to fetch `arxiv.org/html/ID`, but web_fetch only fetches URLs already in the conversation** (`research.py` ~L45). Add a `full text: https://arxiv.org/html/ID` line for each paper in `Item.to_prompt_line` / `build_research_prompt`, or tell Claude to fetch only the URLs listed.
3. **Scheduled workflows are auto-disabled after 60 days without repo activity** (public repo). Add a keepalive step: give the job `permissions: actions: write` and run `gh api -X PUT repos/OWNER/REPO/actions/workflows/alphaone.yml/enable` with `GH_TOKEN: github.token` on success. Then update the README bullet.

Medium:
4. **The editor pass uses `output_format=`**: the SDK may raise ValidationError on partial text, so the refusal and max_tokens checks never run. Pass the schema via `output_config={"effort": ..., "format": {"type": "json_schema", "schema": transform_schema(Newsletter)}}`, check `stop_reason` first, then join the text blocks and `Newsletter.model_validate_json`, wrapping errors in ResearchError. (Verify `transform_schema` is exported from `anthropic` in 1.11.)
5. **A research pass cut off by max_tokens or the continuation cap is treated as success**, so the truncated dossier is emailed and every item is marked seen. Raise ResearchError instead (or raise max_tokens to 128000).
6. **pause_turn continuation doesn't pass the container ID**: pass `container=message.container.id` on continuations when it's set.
7. **max_uses resets on each continuation**, so the 12/14 search and fetch budget isn't enforced across the run. Stop continuing once `usage.searches` / `usage.fetches` reach the caps, or lower MAX_CONTINUATIONS to 2–3. Don't change the `tools` array mid-turn.
8. **No retry on mid-stream API errors**: add a 3-attempt backoff loop for APIStatusError with status 500 or above, overloaded errors, and APIConnectionError.
9. **A refusal fails the whole run, and the same items keep failing for up to 24h**: on refusal, mark the considered items seen, or retry once without the papers.
10. **No SMTP retry**: retry 3 times (10s, 30s, 90s) on disconnects, timeouts and 4xx codes, but not on authentication or 5xx errors.
11. **From is always SMTP_USERNAME**: add an `ALPHAONE_FROM` setting (defaulting to the username), pass it through the workflow env, and document it.
12. **The arXiv sweep only sees the latest 60 submissions, and the 24h cutoff drops weekend batches**: give papers a longer lookback (72–96h), raise max_results, or use the rss.arxiv.org category feeds.
13. **The candidate cap ranks news purely by recency**, so official company posts can be crowded out by Google News items. Rank by (source priority, published), or reserve a quota per source group.

Low (fix if cheap): quiet-hour skipped issues still record their stories as "sent"; one bad date or link in a feed can crash `select_new`/`parse_feed`; URL normalisation can merge distinct links (fragment routes); write output files with `encoding="utf-8"`; usage/cost undercounts when a fallback model runs; `PRICES` lists Haiku 4.5, which doesn't support these web tool versions; add a Date header and use a real Message-ID domain; markdown asterisks can show up in the email; the heavy inline styles could hit Gmail's 102KB clipping; update action majors (checkout/setup-python/cache/upload-artifact) if Node 20 versions are deprecated; in a public repo, run logs and artifacts (issue, dossier, recipient address) are world-readable, so consider dropping the artifact upload or making the repo private.

## Constraints to keep in mind

- `alphaone/README.md` sits in a Mintlify repo and may be parsed as MDX: **no raw `<`, `>`, `{`, `}` outside code blocks**, or the docs build can break.
- Claude API facts (newer than older training data): `claude-opus-5-5` is $4/$20 per MTok, cache reads $0.20; thinking is always on (effort controls depth, default medium); forced `tool_choice` returns a 400; the web tool versions `_20260209` are right for this model.
- Keep the commit messages' existing style. Push to `claude/loving-clarke-fbnt2f` so PR #1 updates.

## What the user still has to do

1. Create a Claude API key, and set a monthly spend limit in the Claude Console.
2. Turn on 2-Step Verification for the sending Gmail account and create an App Password.
3. In GitHub, under Settings → Secrets and variables → Actions, add `ANTHROPIC_API_KEY`, `SMTP_USERNAME` and `SMTP_PASSWORD`. Optionally set the variable `ALPHAONE_TIMEZONE`, e.g. `Asia/Kolkata`.
4. Merge PR #1 into `main` (schedules only run from the default branch). Then go to Actions → "ALPHAONE hourly AI briefing" → Run workflow, ticking **Dry run** first to check the output and the cost in the run summary.

Cost warning: on Opus 5.5 with deep research every hour, expect a few dollars per run, which could mean tens of dollars a day. Check the run summary after the first runs. To cut cost: `ALPHAONE_MODEL=claude-sonnet-5-5`, lower `ALPHAONE_MAX_SEARCHES` / `ALPHAONE_MAX_FETCHES`, or a less frequent cron.
