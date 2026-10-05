# ALPHAONE — your hourly AI briefing agent

ALPHAONE is an autonomous agent that wakes up every hour, finds what's new in AI, reads the
actual articles and research papers, and emails you a newsletter at
**harkomal.design@gmail.com**.

Every issue has:

- **The hour in one breath**: one or two sentences.
- **60-second brief**: one bullet per development.
- **Top stories (detailed overview)**: what happened, why it matters, the details (numbers,
  pricing, availability, context), what to watch, and links to the sources.
- **Papers, explained simply**: the problem, the big idea (with an analogy), key results,
  why it matters, and caveats.
- **Also on the radar**: smaller items, one line each, with a link.
- **Jargon buster**: every technical term in the issue, explained.

## How it works

Each hourly run goes through four steps:

1. **Sweep.** It polls 32 free sources in parallel: 12 official feeds (OpenAI, Anthropic,
   Google DeepMind, Google AI, Google Research, Microsoft, Microsoft Research, NVIDIA, AWS,
   Apple, Mistral, Hugging Face), 6 tech-press feeds (TechCrunch, The Verge, MIT Technology
   Review, Ars Technica, Wired, Simon Willison), 12 Google News searches (one per big
   company, including Meta, xAI and the Chinese labs that have no feed of their own, plus
   one for AI policy), arXiv, and Hugging Face Daily Papers. It skips anything it has
   already looked at, news more than a day old and papers more than four days old.
2. **Research.** Claude triages the new items, runs live web searches for anything the
   feeds missed, then opens and reads the primary sources: full announcements and the
   papers themselves. Out of that it writes a research dossier based on those sources.
3. **Edit.** A second Claude pass turns the dossier into the newsletter, written to be easy
   to digest.
4. **Send and remember.** It emails the issue and records what it covered, so the next hour
   only brings what's new.

It runs on GitHub Actions (`.github/workflows/alphaone.yml`), so it keeps going without
your computer being on.

## Setup (about 10 minutes)

### 1. Get a Claude API key

Create an API key in the Claude Console at [platform.claude.com](https://platform.claude.com).
While you're there, **set a monthly spend limit** (see the cost section below).

### 2. Create a Gmail App Password for sending

ALPHAONE sends through Gmail's mail server. You can send from any Gmail account, including
harkomal.design@gmail.com itself.

1. Turn on 2-Step Verification for the sending Google account.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
   create an app password named "ALPHAONE", and copy the 16-character password.

### 3. Add the secrets to GitHub

In this repository: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `SMTP_USERNAME` | The Gmail address that sends the email |
| `SMTP_PASSWORD` | The 16-character app password from step 2 |

Optional: on the **Variables** tab next to Secrets, add `ALPHAONE_TIMEZONE` (for example
`Asia/Kolkata`) so each issue shows your local time.

### 4. Turn it on

Scheduled workflows only run from the repository's default branch, so merge pull request
#1 into `main`. Then open the **Actions** tab, choose **ALPHAONE hourly AI briefing**, and
click **Run workflow** to send the first issue right away. After that it runs every hour,
at 7 minutes past.

Tip: for the very first try, tick **Dry run**. It does everything except send the email,
and you can download the issue from the run's artifacts and check the cost in the run
summary.

## Cost

You pay for the Claude API usage, and ALPHAONE is not cheap to run. Every run does deep
research on Claude Opus 5.5: it reads the new headlines and papers, runs about 12 web
searches, reads about 14 full pages, and then writes the issue. (Claude is given that budget
and reminded of it during long research turns, but each step has its own limit, so a long
run can go somewhat over.) **Expect roughly $1 to $5 per run**, depending on how much news
there is. At 24 runs a day that is roughly $25 to
$120 a day, or about $700 to $3,500 a month. A quiet hour costs nearly as much as a busy
one, because the research happens before ALPHAONE knows the hour was quiet.

- Do a dry run first and read the approximate cost in the run summary. Multiply it by 720
  for a rough monthly figure.
- Set a monthly spend limit in the Claude Console so there are no surprises.
- To spend less: set `ALPHAONE_MODEL` to `claude-sonnet-5-5` (about half the cost), lower
  `ALPHAONE_MAX_SEARCHES`, `ALPHAONE_MAX_FETCHES` or `ALPHAONE_FETCH_MAX_TOKENS`, or run
  less often by changing the `cron` line in the workflow, e.g. `7 */3 * * *` for every 3
  hours. `ALPHAONE_SKIP_QUIET` only skips the email, not the research, so it doesn't save
  money.

## Settings (optional)

Set these under **Settings → Secrets and variables → Actions → Variables**. Leave any of
them unset to keep the default.

| Variable | Default | What it does |
| --- | --- | --- |
| `ALPHAONE_TO` | harkomal.design@gmail.com | Where the newsletter goes |
| `ALPHAONE_FROM` | the SMTP username | Sender email address, needed for mail providers whose SMTP username isn't one (Gmail only allows a verified "Send mail as" address) |
| `ALPHAONE_TIMEZONE` | `UTC` | Time zone for the timestamp, e.g. `Asia/Kolkata`, `America/New_York` |
| `ALPHAONE_MODEL` | `claude-opus-5-5` | Claude model; `claude-sonnet-5-5` costs about half as much. It must support effort and the 2026-02-09 web search and fetch tools, so Haiku 4.5 won't work |
| `ALPHAONE_EFFORT` | `high` | Research depth: `low`, `medium`, `high`, `xhigh`, `max` |
| `ALPHAONE_EDITOR_EFFORT` | `medium` | How hard the editor pass thinks |
| `ALPHAONE_MAX_SEARCHES` | `12` | Web search budget per run (a soft limit, see Cost) |
| `ALPHAONE_MAX_FETCHES` | `14` | Budget of full articles and papers read per run (a soft limit, see Cost) |
| `ALPHAONE_FETCH_MAX_TOKENS` | `25000` | The most of any one page Claude reads; lower it to spend less |
| `ALPHAONE_LOOKBACK_HOURS` | `24` | Feed news older than this is ignored |
| `ALPHAONE_PAPER_LOOKBACK_HOURS` | `96` | Papers older than this are ignored (arXiv's weekend batch is two days old when it appears) |
| `ALPHAONE_MAX_NEWS` / `ALPHAONE_MAX_PAPERS` | `80` / `40` | The most news items and papers handed to Claude each run |
| `ALPHAONE_SKIP_QUIET` | `false` | `true` skips the email when nothing significant happened |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | Use another mail provider (port 587 uses STARTTLS) |

`ALPHAONE_STATE` and `ALPHAONE_OUT` move the memory file and the output folder for local
runs. Leave them unset on GitHub, where the workflow expects the defaults.

To change which feeds it watches, edit the lists at the top of `sources.py`. The Anthropic
feed is a community mirror of anthropic.com/news, because Anthropic doesn't publish one.

## Good to know

- **Memory** lives in the GitHub Actions cache and carries over from run to run. If it's
  ever lost (GitHub deletes caches that haven't been used for 7 days, so this happens if the
  workflow is paused for a week), ALPHAONE starts fresh: the next issue covers the past 24
  hours, may repeat a few stories you've already had, and the issue numbers start again at
  #1. If GitHub's cache service has a hiccup and can't hand back recent memory, that hour is
  skipped (the run fails) rather than starting over.
- **Before any paid work**, each run runs the offline tests, checks that the secrets are
  set and logs in to the mail server. A broken setup fails right away and costs nothing.
- **Manual runs on any branch other than** `main` are always dry runs, so they never send
  email or change the memory the hourly runs use.
- **If a run fails**, GitHub emails you, and the run log on the Actions page shows why,
  along with what the run cost. A broken feed never stops a run; it's just listed in the
  run summary. Brief hiccups at Claude or the mail server are retried automatically.
- **If Claude declines to cover an hour** (its safety checks can occasionally misfire on
  news about security or biology), that hour is skipped with a warning on the run page, and
  the next hour moves on to new items.
- **Timing**: GitHub's scheduler can start runs a few minutes late when it's busy.
- **Public repositories**: GitHub pauses scheduled workflows after 60 days without a
  commit. After every successful scheduled run, ALPHAONE marks its own workflow as active
  again, which resets that clock. If runs keep failing for 60 days in a row, GitHub pauses
  it and emails you; turn it back on from the Actions tab.
- **This repository is public**, so anyone signed in to GitHub can read the run logs and
  download the saved issues from dry runs and failed runs. They hold the newsletter and the
  address it goes to, nothing secret: your API key and Gmail password stay in GitHub
  Secrets and are hidden from logs.
- ALPHAONE reads primary sources and is told never to invent facts, but it's still an AI.
  Double-check anything you plan to act on.

## Running it locally

You need Python 3.10 or newer.

macOS or Linux:

```bash
cd alphaone
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
python main.py --preview     # sample issue in out/preview.html, no API calls
python -m pytest -q          # offline tests
export ANTHROPIC_API_KEY=your-key
python main.py --dry-run     # real research (this costs money), writes out/issue.html, sends nothing
```

Windows (PowerShell):

```powershell
cd alphaone
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python main.py --preview
python -m pytest -q
$env:ANTHROPIC_API_KEY = "your-key"
python main.py --dry-run
```
