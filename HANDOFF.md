# Handoff Notes

What was learned building this, and why things are the way they are. The
[README](README.md) covers *how to use it*; this covers *why it's built this
way* and what will break if you change it.

---

## Part 1 — Email formatting

### The format, and what each piece is for

```
SUBJECT: A model that watches the weld pool     ← line 1, stripped by the mailer
# Watching the Weld Pool                        ← headline, 3-7 words
*Week of Sep 28 – Oct 5. 273 items reviewed, 8 kept.*
**The short version**        ← 4 bullets, each a real fact, not a tease
## What happened in AI this week   ← 5-7 paragraphs, plain language
## Why a factory should care       ← 4-6 connections + what to ignore
## Digging deeper                  ← the research items
## One thing to watch              ← the standout
```

The subject line is written fresh each week by the model and must name the most
interesting *specific* thing. "Weekly AI Digest #14" tells a reader nothing and
trains them not to open it. `EMAIL_SUBJECT_PREFIX` supplies the `[AI x Mfg]` tag
separately.

Each research item is always the same three beats:

- **What's new.** — what they built, why it beats what came before
- **On the floor.** — one concrete scene in a real plant, and what improves
- **The catch.** — the single biggest blocker, blunt

**The catch is the most valuable line in the email.** It's what makes the rest
credible. Never soften it, never drop it.

### Lesson: "skimmable" does not mean "less"

The first newsletter draft lost ~760 words against the original report. The
prompt said *keep paragraphs short, people skim on a phone* — so the model cut
whole findings. It did exactly what was asked.

The fix was to separate the two ideas explicitly in `WRITER_SYSTEM`:

> Short paragraphs, but MANY of them. Writing for skimmability means tight
> sentences and clear signposts — it does NOT mean covering less. Never drop a
> real finding to make the email shorter.

Plus hard counts: 5-7 paragraphs in Part 1, 4-6 in Part 2. **If you ever loosen
the brevity instructions, re-add explicit section counts or coverage silently
shrinks again.** Word count is the canary: a healthy issue is ~2,000-2,400 words.

### Lesson: tight sentences ARE wanted — just not fewer of them

Part 2 was later tightened to 2-3 sentences per paragraph to match Part 1's
register. Same number of connections, ~35% less text. The instruction that
worked: *state the consequence and stop — don't walk through the reasoning that
got you there, don't add a second example, don't restate it in other words.*

### Lesson: bold-on-bold is unreadable

Originally the item title and the three labels were all bold at the same size,
so each item read as four identical bold runs with nothing to anchor the eye.

Fixed with hierarchy: titles went to 17.5px/darker, labels became 11px uppercase
letter-spaced in the rust accent color. If you rename a label, update
`ITEM_LABELS` in `mailer.py` **and** `WRITER_SYSTEM` in `digest_agent.py` — they
are two separate lists and the styling silently stops applying if they drift.

### Email client rules that cost real debugging

| Rule | Why |
|---|---|
| **Inline styles, never CSS classes, for anything load-bearing** | Some clients strip `<style>` blocks. A label that loses its styling falls straight back to looking like the heading — the exact bug being fixed. |
| **Linkify bare URLs yourself** | The `markdown` package only links explicit `[text](url)`. A plain `https://...` renders as dead text. `_linkify()` wraps the rest, skipping anything already inside a tag. |
| **Send multipart: text/plain + text/html** | Plain-text fallback is also what spam filters read. |
| **Recipients go in Bcc** | To:/Cc: leaks every subscriber's address to every other subscriber. The single most common small-newsletter privacy failure. |
| **Set `List-Unsubscribe`** | Gives the native Unsubscribe button in Gmail and Apple Mail, and signals legitimacy. CAN-SPAM requires a working opt-out anyway. |

The unsubscribe wording adapts to the value: an address produces *"Want off the
list? Just reply and say so"* (right for a hand-managed list); a URL produces a
link. Don't print a bare address and expect people to copy it.

### Voice

Audience is a general mailing list, not researchers. **Define jargon on both
sides** — "a world model, a network that learns how a system reacts to your
actions" *and* "the melt pool, the small puddle of molten metal the laser leaves
behind." Assume an interested adult who neither builds AI nor works in a plant.

Banned: *revolutionary, game-changing, unlock, paradigm shift.* Never invent a
number that isn't in the source. Saying "nothing here for a factory" is useful
information — the Part 2 section explicitly ends with what to ignore.

---

## Part 2 — GitHub setup

### Sending email: what actually works in 2026

**Microsoft is dead for SMTP.** Basic authentication was permanently retired,
reaching 100% rejection on 30 April 2026. App passwords *are* basic auth, so
they're rejected too. A correct Outlook address with a correct app password
still fails `535 5.7.3 Authentication unsuccessful`. There is no password that
works — Microsoft now requires OAuth2, which this project does not implement.

**Gmail app passwords still work.** Requires 2-Step Verification on, then
generate a 16-character app password. Google *displays* it as four groups of
four, and people copy the spaces — a 19-character value that fails auth with an
error that explains nothing. `_normalize_password()` strips spaces when the
value matches Google's exact 4×4 display pattern, and leaves anything else alone.

**Past ~100 subscribers, move to Resend or SES.** Gmail caps around 500/day and
flags personal accounts for bulk sending. It's a four-line `.env` change.

### macOS: zero trusted certificates

python.org builds of Python on macOS don't read the system keychain. Unless
you've run `Install Certificates.command`, `ssl.create_default_context()` returns
a context with **zero** CAs and every TLS connection dies with
`CERTIFICATE_VERIFY_FAILED` — which looks like a server or password problem and
isn't. `_ssl_context()` prefers certifi's bundle. Same root cause made stdlib
`urllib` fail against arXiv, which is why collection uses `requests`.

This does not affect GitHub Actions, where the runner's trust store is fine.

### Pushing: the `workflow` scope

`gh auth login` does **not** grant permission to create `.github/workflows/`
files. The push is rejected with *"refusing to allow an OAuth App to create or
update workflow ... without `workflow` scope."* Fix:

```bash
gh auth refresh -h github.com -s workflow
```

Interactive — answer the prompt, copy the one-time code, authorize in browser.

### Cron is UTC and ignores daylight saving

A single cron line would silently become 9am every November. The workflow fires
**twice** — 15:00 UTC (10am CDT) and 16:00 UTC (10am CST) — and the first step
checks real local time in `America/Chicago`, exiting unless it's 10am. Exactly
one proceeds each week; the other exits in ~3 seconds.

To change the time: edit `TARGET_TZ` / `TARGET_HOUR` **and** both cron lines.

### Secrets

Four, all under Settings → Secrets and variables → Actions:

`ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_RECIPIENTS`

**The recipient list is a secret, not config** — those are other people's
addresses. Everything non-sensitive (host, port, sender name, footer) lives in
the workflow file where it's readable.

No `.env` is needed on Actions. `load_env()` uses `os.environ.setdefault`, so
real environment variables always win and a missing `.env` is a no-op. This was
verified locally with a scrubbed environment before the first push.

### The `.env.example` leak

A real API key and Gmail app password ended up in `.env.example` — the one env
file that is *meant* to be committed. **`.gitignore` cannot protect you there.**
Caught by a pre-commit scan before the first commit, so nothing was published.

`.githooks/pre-commit` now blocks any commit containing an API-key-shaped string,
or a tracked `.env`-style file with filled-in values. Active via
`git config core.hooksPath .githooks` — **a fresh clone must run that command
again**, hooks don't travel with the repository.

### Testing order that works

`workflow_dispatch` exposes three modes. Use them in order:

| Mode | Proves | Cost | Time |
|---|---|---|---|
| `dry-run` | Runner can reach arXiv, HF, RSS feeds | $0 | ~90s |
| `preview` | Screening + writing work; shows the email | ~$0.39 | ~6 min |
| `send` | Full path including SMTP | ~$0.39 | ~6 min |

**`gh workflow run` returns immediately.** A clean terminal means *queued*, not
*succeeded* — this caused a false "it didn't send" alarm. To wait:

```bash
gh run watch $(gh run list --workflow="Weekly Digest" --limit 1 \
  --json databaseId --jq '.[0].databaseId') --exit-status
```

### Things that will bite later

- **60 days of repo inactivity disables scheduled workflows.** A warning email
  arrives first, easy to miss when the thing has run itself perfectly for two
  months. Any push or manual run resets it.
- **Scheduled runs are not punctual.** 5-20 minutes late is normal; under load a
  run can be dropped. Fine for a newsletter.
- **Never run both schedulers.** If the local launchd job was ever installed,
  remove it: `launchctl bootout gui/$(id -u)/com.aimfgdigest.weekly`. Otherwise
  subscribers get two copies.
- **Rotating the Gmail password** means updating the GitHub secret too — the
  local `.env` and the secret are independent copies.
- `digest_*.md` is gitignored, so each run uploads the issue as a 90-day
  artifact. That's the archive.

---

## Part 3 — Cost, and the one structural decision

Measured on a real run: **$0.39** — Haiku $0.11, Opus $0.28. About $20/year.

The architecture is a **workflow, not an agent**, and that is the whole cost
story. Deterministic Python does all collection ($0). Haiku screens ~270 items,
where there is a lot of text and little judgment. Opus only ever sees the ~20
survivors, where writing quality is the entire deliverable.

The expensive mistake would be letting a frontier model do the fetching and
filtering — paying $20/MTok to read RSS feeds. **If you optimize cost later,
move the writer down to Sonnet 5.5 (halves it). Never move the screener up:
it's the stage with the most tokens and the least judgment.**

Prompt caching is deliberately **off**. At one run per week the cache expires
long before the next run, so a cache write would add ~25% to input cost for a hit
that never comes. `--cache` exists only for same-hour re-runs while tuning.

### The highest-value knob

`ARXIV_TERMS` in `digest_agent.py`. Browsing arXiv by category returns mostly
self-driving cars and chatbot benchmarks; the keyword phrase searches are what
surface factory work — they found every item in the first two issues. Adding
`"sheet metal forming"`, `"grinding"`, `"metrology"` costs nothing and improves
the digest more than any prompt change.
