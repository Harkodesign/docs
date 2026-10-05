# ALPHAONE — your hourly AI briefing agent

ALPHAONE is an autonomous agent that wakes up every hour, finds what's new in AI, reads the
actual articles and research papers, and emails you a newsletter at
**harkomal.design@gmail.com**.

Every issue has:

- **The hour in one breath**: one or two sentences.
- **60-second brief**: one bullet per development.
- **Top stories (detailed overview)**: what happened, why it matters, the details (numbers,
  pricing, availability, context), what to watch, and links to the sources.
- **Research radar**: papers explained in plain English: the problem, the big idea (with an
  analogy), key results, why it matters, and caveats.
- **Also on the radar**: smaller items, one line each.
- **Jargon buster**: every technical term in the issue, explained.

## How it works

Each hourly run goes through four steps:

1. **Sweep.** It polls about 30 free sources in parallel: official blogs (OpenAI, Google
   DeepMind, Google Research, Microsoft, NVIDIA, AWS, Apple, Hugging Face), the tech press
   (TechCrunch, The Verge, VentureBeat, MIT Technology Review, Ars Technica, Wired), Google
   News searches for companies without feeds (Anthropic, Meta, xAI, Mistral, DeepSeek, Qwen
   and others), arXiv, and Hugging Face Daily Papers. It drops anything it has already
   covered.
2. **Research.** Claude triages the new items, runs live web searches for anything the
   feeds missed, then opens and reads the primary sources: full announcements and the
   papers themselves. Out of that it writes a fact-checked research dossier.
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

### 4. Turn it on

Scheduled workflows only run from the repository's default branch, so merge this branch
into `main`. Then open the **Actions** tab, choose **ALPHAONE hourly AI briefing**, and
click **Run workflow** to send the first issue right away. After that it runs every hour,
at 7 minutes past.

Tip: for the very first try, tick **Dry run**. It does everything except send the email,
and you can download the issue from the run's artifacts and check the cost in the run
summary.

## Cost

You pay for the Claude API usage. ALPHAONE runs 24 times a day, and each run does deep
research (up to 12 web searches and 14 full-page reads by default) on Claude Opus 5.5, the
most capable default model. That adds up. **Expect a few dollars per run, which can mean
tens of dollars per day.** The real number depends on how much news there is, so:

- Every run prints its token usage and approximate cost in the run summary on the Actions
  page. Check it after the first few runs.
- Set a monthly spend limit in the Claude Console so there are no surprises.
- To spend less, use any of the settings below: a cheaper model, fewer searches and page
  reads, or a less frequent schedule (change the `cron` line in the workflow, e.g.
  `7 */3 * * *` for every 3 hours).

## Settings (optional)

Set these under **Settings → Secrets and variables → Actions → Variables**. Leave any of
them unset to keep the default.

| Variable | Default | What it does |
| --- | --- | --- |
| `ALPHAONE_TO` | harkomal.design@gmail.com | Where the newsletter goes |
| `ALPHAONE_TIMEZONE` | `UTC` | Time zone for the timestamp, e.g. `Asia/Kolkata`, `America/New_York` |
| `ALPHAONE_MODEL` | `claude-opus-5-5` | Claude model; `claude-sonnet-5-5` costs about half as much |
| `ALPHAONE_EFFORT` | `high` | Research depth: `low`, `medium`, `high`, `xhigh`, `max` |
| `ALPHAONE_MAX_SEARCHES` | `12` | Web searches per run |
| `ALPHAONE_MAX_FETCHES` | `14` | Full articles and papers read per run |
| `ALPHAONE_SKIP_QUIET` | `false` | `true` skips the email when nothing significant happened |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `465` | Use another mail provider (port 587 uses STARTTLS) |

To change which feeds it watches, edit the lists at the top of `sources.py`.

## Good to know

- **Memory** lives in the GitHub Actions cache and carries over from run to run. If it's ever
  cleared, the next issue simply covers the past 24 hours again.
- **If a run fails**, GitHub emails you, and the run log on the Actions page shows why. A
  broken feed never stops a run; it's just listed in the run summary.
- **Timing**: GitHub's scheduler can start runs a few minutes late when it's busy.
- **Public repositories**: GitHub pauses scheduled workflows after 60 days with no activity
  in the repository. It emails you first, and you can re-enable it from the Actions tab.
- ALPHAONE reads primary sources and is told never to invent facts, but it's still an AI.
  Double-check anything you plan to act on.

## Running it locally

```bash
cd alphaone
pip install -r requirements.txt
python main.py --preview     # render a sample issue to out/preview.html, no API calls
export ANTHROPIC_API_KEY=...
python main.py --dry-run     # real research, writes out/issue.html, sends nothing
pip install pytest && python -m pytest   # offline tests
```
