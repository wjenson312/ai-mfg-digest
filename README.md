# AI → Manufacturing Weekly Digest

Scouts the past week of AI research and releases, writes a plain-English email
newsletter about what it means for manufacturing, and sends it to a list every
Tuesday at 10am.

---

## Quick start

```bash
pip install anthropic requests pydantic markdown
cp .env.example .env          # then fill it in
cp recipients.example.txt recipients.txt
```

Work through it in this order. Each step is safe and costs nothing until the
last one.

```bash
python digest_agent.py --dry-run                    # 1. sources work?  ($0)
python digest_agent.py --check-smtp                 # 2. mail login works?  (sends nothing)
python digest_agent.py                              # 3. write a digest + preview the email (~$0.30)
python digest_agent.py --test-email you@you.com --send   # 4. send one copy to yourself
./install_schedule.sh                               # 5. schedule Tuesdays 10am
```

**Nothing is ever emailed unless you pass `--send`.** The default is a preview
that prints the subject, the recipient count, and the word count. An email can't
be unsent, so sending is opt-in every single time.

---

## What you need to fill in

Everything lives in `.env` — copy `.env.example` and work down it. The four
sections:

| Section | What it's for |
|---|---|
| **1. Anthropic API** | `ANTHROPIC_API_KEY` from console.anthropic.com. Optional model and budget overrides. |
| **2. Mail server** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`. Settings for Gmail, Outlook, iCloud, Fastmail, SendGrid, Mailgun and SES are listed inline. |
| **3. How it looks** | Sender name, reply-to, subject prefix, footer, unsubscribe link. |
| **4. The list** | `EMAIL_RECIPIENTS` inline, and/or `EMAIL_RECIPIENTS_FILE` pointing at `recipients.txt`. |

**Two things that trip everyone up:**

*Your normal password will not work.* Gmail, Outlook and iCloud all reject
account passwords over SMTP. You need an app-specific password — the exact menu
path for each provider is in `.env.example`.

*Set `EMAIL_UNSUBSCRIBE`.* It adds a footer link and turns on the native
Unsubscribe button in Gmail and Apple Mail. Sending bulk email without a working
opt-out is bad practice, and in the US CAN-SPAM requires one.

---

## The newsletter format

Written for a general audience — engineers, students, plant managers, the merely
curious — not for AI researchers. Jargon gets defined on both sides: AI terms
*and* shop-floor terms.

```
SUBJECT: A model that watches the weld pool     ← the mailer strips this line

# Watching the Weld Pool
*Week of Sep 28 – Oct 5. 273 items reviewed, 8 worth your time.*

**The short version**          ← 4 bullets, the whole week
## What happened in AI this week    ← plain language, no factory angle yet
## Why a factory should care        ← the connections, plus what to ignore
## Digging deeper                   ← 8 research items
     What's new. / On the floor. / The catch.
## One thing to watch
```

The model writes a real subject line each week rather than "Weekly Digest #14".
`EMAIL_SUBJECT_PREFIX` is prepended, so inboxes show `[AI x Mfg] A model that
watches the weld pool`.

`digest_2026-10-05.md` is a complete example. Open `email_preview.html` in a
browser to see how it renders.

---

## Scheduling

```bash
./install_schedule.sh
```

Installs a macOS launchd job for **Tuesdays at 10:00**. It refuses to install if
`.env` is missing an API key or SMTP host, so you can't schedule a job that
can't work.

**launchd rather than cron**, for one reason: if your Mac is asleep at 10am on
Tuesday, cron silently skips the week. launchd runs the job when the machine
wakes. For a weekly newsletter that difference matters.

| Command | Does |
|---|---|
| `launchctl print gui/$(id -u)/com.aimfgdigest.weekly` | Is it loaded? |
| `launchctl kickstart -k gui/$(id -u)/com.aimfgdigest.weekly` | Run now — **this sends email** |
| `launchctl bootout gui/$(id -u)/com.aimfgdigest.weekly` | Cancel the schedule |

Output goes to `run.log`. If a run fails you also get a macOS notification, so a
silent Tuesday doesn't go unnoticed.

**If you prefer cron**, the equivalent line is:

```
0 10 * * 2 /Users/williamjenson/Desktop/Projects/ai-mfg-digest/run_weekly.sh
```

`run_weekly.sh` pins the right Python interpreter. This matters more than it
sounds: a scheduled job doesn't inherit your shell's PATH, and the system
`/usr/bin/python3` does **not** have these packages installed. `install_schedule.sh`
records the correct interpreter in `.python_path`, and the runner checks the
imports before doing anything.

---

## Running it on GitHub Actions

Runs in the cloud, so your Mac doesn't need to be awake on Tuesday morning.

### 1. Create the repository

**Make it private.** The code is harmless, but the digests and your config
history don't need to be public, and private repos get 2,000 free Actions
minutes a month — this job uses about 5.

```bash
gh repo create ai-mfg-digest --private --source=. --remote=origin --push
```

Or create it on github.com and then:

```bash
git remote add origin https://github.com/YOUR-USERNAME/ai-mfg-digest.git
git push -u origin main
```

### 2. Add the four secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your `sk-ant-...` key |
| `SMTP_USER` | `williamjenson121@gmail.com` |
| `SMTP_PASSWORD` | The 16-character Gmail app password |
| `EMAIL_RECIPIENTS` | Comma-separated list of subscribers |

The recipient list is a secret, not a config value — those are other people's
addresses and they shouldn't sit in a repo. The workflow reuses `SMTP_USER` for
reply-to and unsubscribe, so there's nothing else to set.

Everything non-secret (host, port, sender name, subject prefix, footer) is in
the workflow file itself, where you can read and edit it.

### 3. Test before trusting it

**Actions → Weekly Digest → Run workflow.** Pick a mode:

| Mode | Does | Costs |
|---|---|---|
| `dry-run` | Hits every source, lists what it found. No model calls, no email. | $0 |
| `preview` | Writes the digest, shows the email, sends nothing. | ~$0.30 |
| `send` | Writes it and emails the list. | ~$0.30 |

Run `dry-run` first. If it finds a few hundred items, your network path works.
Then `preview`. Then `send` once you're happy.

Every run uploads the digest as a downloadable artifact, kept 90 days — so you
have an archive even though `digest_*.md` is gitignored.

### Why there are two cron schedules

GitHub Actions cron is **always UTC and ignores daylight saving**, so a single
schedule drifts by an hour twice a year. The workflow fires at both 15:00 UTC
(10am CDT) and 16:00 UTC (10am CST), and the first step checks the real local
time in `America/Chicago` and stops the run unless it's 10am. Exactly one of the
two proceeds each week; the other exits in about three seconds.

To change the time or timezone, edit `TARGET_TZ` and `TARGET_HOUR` at the top of
the workflow, and move both cron lines to match.

### Things that will bite you eventually

- **GitHub disables scheduled workflows after 60 days of no repo activity.** You
  get an email warning first. Any push or a manual run resets the clock — easy to
  miss if the thing runs itself perfectly for two months.
- **Scheduled runs are not punctual.** GitHub queues them during busy periods;
  5–20 minutes late is normal, and under heavy load a run can be dropped. Fine
  for a newsletter, not for anything time-critical.
- **A failed run emails you** by default (GitHub's own notification), so a silent
  Tuesday still reaches you.
- **Rotating the Gmail app password** means updating the `SMTP_PASSWORD` secret
  too. The local `.env` and the GitHub secret are separate copies.

### Local scheduling vs. Actions

You now have both. Run one, not both, or subscribers get two copies.

If you installed the launchd job earlier, remove it before enabling Actions:

```bash
launchctl bootout gui/$(id -u)/com.aimfgdigest.weekly
```

---

## Keeping secrets out of the repository

A `pre-commit` hook in `.githooks/` blocks any commit containing something
shaped like an API key, or a tracked `.env`-style file with filled-in values.
It's already active (`git config core.hooksPath .githooks`).

This exists because a real API key and app password did end up in
`.env.example` during setup. `.gitignore` does not protect you there — that file
is *meant* to be committed, just with empty values. The hook catches what
.gitignore structurally cannot.

Override in an emergency with `git commit --no-verify`.

---

## How it works, and why it's cheap

| Stage | Does what | Runs on | Cost |
|---|---|---|---|
| 1. Collect | arXiv, Hugging Face, 6 RSS feeds, Anthropic sitemap | Plain Python | **$0.00** |
| 2. Screen | Tags ~270 items with a lever and a 0–5 score | Claude Haiku 4.5 | ~$0.03 |
| 3. Write | Writes the newsletter | Claude Opus 5.5 | ~$0.25 |
| 4. Send | Renders HTML, sends via SMTP in Bcc batches | Plain Python | $0.00 |

**~$0.30 per run**, about $15/year. The script prints a real token-and-dollar
breakdown from the API's own usage numbers.

The expensive mistake in a project like this is letting a frontier model do the
fetching and filtering — paying $20 per million output tokens to read RSS feeds.
So code fetches for free, a cheap model does the bulk reading, and the best model
only touches the ~20 items that survived, where judgment and writing quality show.

Swapping the writer to Sonnet 5.5 roughly halves the cost. Set `WRITER_MODEL=claude-sonnet-5-5`
in `.env`. Don't move the *screener* up — it's the stage with the most tokens and
the least judgment, exactly the wrong place to spend.

---

## All the flags

| Flag | Default | Does |
|---|---|---|
| `--dry-run` | off | Collect and list only. No model calls, $0. |
| `--send` | off | Actually deliver the email. |
| `--test-email ADDR` | — | Send to one address only, ignoring the list. |
| `--check-smtp` | — | Log in to the mail server and exit. Emails nobody. |
| `--email-file PATH` | — | Send an existing `.md` instead of generating one. |
| `--days N` | 7 | Lookback window. |
| `--items N` | 8 | How many research items. |
| `--budget N` | 2.00 | Hard USD cap. Aborts before exceeding it. |
| `--cache` | off | Cache the system prompt. Only pays off on same-hour re-runs. |
| `--out PATH` | `digest_<date>.md` | Where to write. |
| `--env PATH` | `.env` | Config file location. |

`--email-file` is the recovery path: if a Tuesday send fails but the digest
itself is fine, re-send it without paying to regenerate.

---

## Tuning it

**Edit `ARXIV_TERMS` in `digest_agent.py`.** This is the highest-value knob by
far. Browsing arXiv by category returns mostly self-driving cars and chatbot
benchmarks; the keyword searches are what surface factory work. Add the processes
you care about — `"sheet metal forming"`, `"composite layup"`, `"grinding"`,
`"metrology"` — and the next run picks them up.

`WRITER_SYSTEM` controls the voice and structure. `SCREEN_SYSTEM` and `LEVERS`
control what gets kept and how it's tagged.

---

## Mailing-list hygiene

- **Everyone goes in Bcc.** Nobody sees anyone else's address. Putting a list in
  To: or Cc: is the most common way a small newsletter creates a privacy problem.
- **Sent in batches** (`EMAIL_BATCH_SIZE`, default 40) with a pause between, to
  stay under provider limits. Gmail free is ~500/day, Workspace ~2,000/day.
- **Only email people who asked.** Beyond being the law in most places, an
  unsolicited list gets your sending domain blocked fast.
- Past ~100 recipients, move to a real sending service (SendGrid, Mailgun, SES).
  Settings are already in `.env.example` — it's a four-line change, and your
  deliverability will be much better than a personal Gmail account's.

---

## Known source problems

Verified 2026-10-05. The script handles all of these.

| Source | Status |
|---|---|
| **Anthropic** | **No RSS feed exists at any path.** 15 conventional URLs all 404, and the news page has no feed autodiscovery link. The script reads `sitemap.xml` and filters by date instead. |
| **Meta AI** | `ai.meta.com/blog/rss/` is dead. Replaced with `engineering.fb.com/feed/` (technical, often empty for a given week) and `about.fb.com/news/feed/`. |
| **OpenAI** | Feed works; article pages return 403 to scripts. Feed descriptions are detailed enough — don't fetch the pages. |
| **arXiv** | Needs HTTPS and rate-limits if pushed. The script sleeps 3s between calls and retries once. Python's stdlib `urllib` fails certificate verification on macOS, which is why this uses `requests`. |
| **GitHub Trending** | No API; scraping breaks often. Left out — it has never produced a digest item. |

---

## Files

| File | What |
|---|---|
| `HANDOFF.md` | Why things are built this way, and what breaks if you change them. |
| `digest_agent.py` | Main script: collect, screen, write. |
| `mailer.py` | Email rendering and SMTP delivery. |
| `.env.example` | **Copy to `.env` and fill in.** |
| `recipients.example.txt` | Copy to `recipients.txt`. |
| `run_weekly.sh` | What the scheduler calls. |
| `install_schedule.sh` | Installs the Tuesday 10am job. |
| `com.aimfgdigest.weekly.plist` | The launchd job definition. |
| `.github/workflows/weekly-digest.yml` | The GitHub Actions schedule. |
| `requirements.txt` | Pinned dependencies, used by Actions. |
| `.githooks/pre-commit` | Blocks commits containing secrets. |
| `email_preview.html` | Last rendered email, openable in a browser. |
| `digest_2026-10-05.md` | Example issue in the newsletter format. |
| `run.log` | Scheduled run output. |

`.env` and `recipients.txt` are in `.gitignore` — they hold your key, your mail
password, and other people's addresses. Don't commit them.
