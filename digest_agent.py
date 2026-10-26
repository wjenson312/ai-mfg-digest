#!/usr/bin/env python3
"""
AI -> Manufacturing Weekly Digest
=================================

Scouts the past week of AI research and releases, then writes a plain-English
report on what it means for manufacturing.

HOW THIS IS BUILT (and why it's cheap)
--------------------------------------
This is a *workflow*, not an autonomous agent. Three stages:

  Stage 1  COLLECT  - plain Python hits the APIs directly.      Cost: $0.00
  Stage 2  SCREEN   - Claude Haiku 4.5 tags/scores candidates.   Cost: ~$0.03
  Stage 3  WRITE    - Claude Opus 5.5 writes the report.         Cost: ~$0.25

The expensive mistake in a project like this is letting a frontier model do the
fetching and filtering -- paying $20/million output tokens to read RSS feeds.
Deterministic code does the fetching for free. A cheap model does the bulk
reading. The best model only touches the ~20 items that survived, where judgment
and writing quality actually show up. That is the whole cost strategy.

Typical run: ~$0.30. Budget ceiling is enforced in code (default $2.00).

SETUP
-----
    pip install anthropic requests pydantic
    export ANTHROPIC_API_KEY="sk-ant-..."

RUN
---
    python digest_agent.py                 # full run, writes digest_YYYY-MM-DD.md
    python digest_agent.py --dry-run       # collect + print sources only, costs $0
    python digest_agent.py --days 14       # widen the window
    python digest_agent.py --budget 1.00   # tighter spend cap
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal, Optional

import anthropic
import requests
from pydantic import BaseModel, Field

import mailer

# ----------------------------------------------------------------------------
# MODELS AND PRICING
# ----------------------------------------------------------------------------
# Two models on purpose. The screener reads a lot and decides little, so it runs
# on the cheapest capable model. The writer reads little and decides everything,
# so it runs on the best one. Prices are USD per million tokens.

WRITER_MODEL = "claude-opus-5-5"
SCREEN_MODEL = "claude-haiku-4-5"

# Both are overridable from .env (WRITER_MODEL / SCREEN_MODEL) so you can trade
# cost for quality without editing code. Applied in main() once .env is loaded.

PRICES = {
    "claude-opus-5-5":  {"in": 4.00, "out": 20.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    # If you ever want a middle option, Sonnet 5.5 is $2/$10:
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00},
}

USER_AGENT = "ai-mfg-digest/1.0 (weekly research scout; student project)"

# The six manufacturing levers. Every kept item gets exactly one.
LEVERS = [
    "Quality & inspection",
    "Maintenance",
    "Scheduling & logistics",
    "Process control",
    "Design & simulation",
    "Robotics",
]

# arXiv keyword searches. THIS IS THE HIGH-VALUE PART OF COLLECTION.
# Browsing cs.RO or cs.LG by date returns mostly self-driving cars and LLM
# benchmarks. These targeted phrase searches are what actually surface factory
# work. Add your own terms here -- it is the cheapest way to improve the digest.
ARXIV_TERMS = [
    "digital twin", "predictive maintenance", "anomaly detection",
    "defect detection", "process control", "production scheduling",
    "bin picking", "robotic assembly", "additive manufacturing",
    "surrogate model", "quality inspection", "job shop", "manufacturing",
    "tool wear", "weld", "injection molding", "CNC machining",
]

# cs.LG is enormous, so it only counts if the text mentions something physical.
CS_LG_GATE = [
    "scheduling", "anomaly detection", "digital twin", "predictive maintenance",
    "process control", "reinforcement learning control", "defect detection",
    "optimization", "simulation", "manufacturing", "industrial",
]

# Lab blogs. Verified working as of 2026-10-05 -- see SOURCE NOTES at the bottom
# of this file for the two feeds that are dead and what replaced them.
RSS_FEEDS = {
    "Google DeepMind":        "https://deepmind.google/blog/rss.xml",
    "OpenAI":                 "https://openai.com/news/rss.xml",
    "NVIDIA Developer":       "https://developer.nvidia.com/blog/feed",
    "NVIDIA Blog":            "https://blogs.nvidia.com/feed/",
    "Meta Engineering":       "https://engineering.fb.com/feed/",
    "Meta Newsroom":          "https://about.fb.com/news/feed/",
}

# Anthropic publishes no RSS feed at any path, and its news page carries no feed
# autodiscovery link. The sitemap is the working substitute: it gives dated URLs,
# which is enough to spot what's new.
ANTHROPIC_SITEMAP = "https://www.anthropic.com/sitemap.xml"
ANTHROPIC_PREFIXES = ("/news/", "/research/", "/engineering/")


# ----------------------------------------------------------------------------
# DATA SHAPES
# ----------------------------------------------------------------------------

@dataclass
class Item:
    """One candidate for the digest, from any source."""
    source: str                     # "arXiv", "HF Daily Papers", "OpenAI", ...
    kind: str                       # "paper" | "release"
    title: str
    url: str
    date: str                       # YYYY-MM-DD
    summary: str = ""               # abstract or feed description
    votes: int = 0                  # HF upvotes, where available
    # Filled in by Stage 2:
    lever: Optional[str] = None
    score: Optional[int] = None
    why: str = ""

    def dedupe_key(self) -> str:
        """Papers show up in several sources. Match on a normalized title."""
        return re.sub(r"[^a-z0-9]+", "", self.title.lower())[:70]


class Verdict(BaseModel):
    """Stage 2's judgment on one item."""
    index: int = Field(description="The item's index number, copied exactly.")
    keep: bool = Field(description="False if there is no realistic manufacturing use.")
    lever: str = Field(description=f"One of: {', '.join(LEVERS)}. Empty string if keep is false.")
    score: int = Field(ge=0, le=5, description="0=no use, 5=a plant could act on this now.")
    why: str = Field(description="One short sentence: the concrete factory use, or why it was dropped.")


class Verdicts(BaseModel):
    verdicts: list[Verdict]


# ----------------------------------------------------------------------------
# SPEND TRACKING
# ----------------------------------------------------------------------------

class Spend:
    """Adds up real token usage so the run reports what it actually cost."""

    def __init__(self, budget: float) -> None:
        self.budget = budget
        self.rows: list[tuple[str, str, int, int, float]] = []

    def add(self, label: str, model: str, usage: Any) -> float:
        p = PRICES.get(model, {"in": 0.0, "out": 0.0})
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        # Cached reads are billed at a fraction, but we don't use caching by
        # default (see --cache), so treat everything as full-price input.
        tin += getattr(usage, "cache_read_input_tokens", 0) or 0
        tin += getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (tin / 1e6) * p["in"] + (tout / 1e6) * p["out"]
        self.rows.append((label, model, tin, tout, cost))
        return cost

    @property
    def total(self) -> float:
        return sum(r[4] for r in self.rows)

    def check(self, about_to_spend: float = 0.0) -> None:
        if self.total + about_to_spend > self.budget:
            raise RuntimeError(
                f"Budget stop: spent ${self.total:.3f}, next step could exceed "
                f"${self.budget:.2f}. Raise it with --budget if that's fine."
            )

    def report(self) -> str:
        lines = ["", "Spend breakdown", "-" * 64,
                 f"{'step':<22}{'model':<20}{'in':>9}{'out':>8}{'$':>9}"]
        for label, model, tin, tout, cost in self.rows:
            lines.append(f"{label:<22}{model:<20}{tin:>9,}{tout:>8,}{cost:>9.4f}")
        lines += ["-" * 64, f"{'TOTAL':<51}${self.total:>8.4f}"]
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# STAGE 1 - COLLECT.  Costs nothing. No model involved.
# ----------------------------------------------------------------------------

def http_get(url: str, *, timeout: int = 40, params: dict | None = None,
             attempts: int = 2) -> Optional[requests.Response]:
    """GET with a real user agent. Returns None instead of raising, because one
    dead feed should never kill the run. Retries once -- arXiv in particular
    times out intermittently, and a dropped query loses a whole search term."""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=timeout, params=params,
                             headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                return r
            # A 4xx won't change on retry; only retry 5xx and rate limits.
            if r.status_code < 500 and r.status_code != 429:
                print(f"    ! {r.status_code} {url}", file=sys.stderr)
                return None
            print(f"    ! {r.status_code} {url} (attempt {attempt})", file=sys.stderr)
        except requests.RequestException as e:
            print(f"    ! {type(e).__name__} {url} (attempt {attempt})", file=sys.stderr)
        if attempt < attempts:
            time.sleep(4)
    return None


def collect_hf_papers(since: dt.date, today: dt.date) -> list[Item]:
    """Hugging Face Daily Papers, one call per day. Some days return empty."""
    out: list[Item] = []
    day = since
    while day <= today:
        r = http_get("https://huggingface.co/api/daily_papers",
                     params={"date": day.isoformat()}, timeout=25)
        day += dt.timedelta(days=1)
        if r is None:
            continue
        try:
            payload = r.json()
        except ValueError:
            continue
        if not isinstance(payload, list):
            continue
        for entry in payload:
            p = entry.get("paper") or {}
            pid = p.get("id")
            if not pid:
                continue
            out.append(Item(
                source="HF Daily Papers", kind="paper",
                title=" ".join((p.get("title") or "").split()),
                url=f"https://arxiv.org/abs/{pid}",
                date=(p.get("publishedAt") or "")[:10] or day.isoformat(),
                summary=" ".join((p.get("summary") or "").split())[:1600],
                votes=int(p.get("upvotes") or 0),
            ))
    out.sort(key=lambda i: -i.votes)
    return out[:30]          # keep the 30 most-upvoted, per the brief


ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _arxiv_query(query: str, max_results: int) -> list[ET.Element]:
    # NOTE: arXiv needs HTTPS and rejects some default clients. `requests`
    # bundles its own CA certificates, which is why this works where Python's
    # stdlib urllib fails with a certificate error on macOS.
    r = http_get("https://export.arxiv.org/api/query", params={
        "search_query": query, "start": 0, "max_results": max_results,
        "sortBy": "submittedDate", "sortOrder": "descending",
    })
    if r is None:
        return []
    try:
        return ET.fromstring(r.content).findall("a:entry", ATOM)
    except ET.ParseError:
        return []


def _entry_to_item(e: ET.Element) -> Optional[Item]:
    try:
        published = (e.find("a:published", ATOM).text or "")[:10]
        title = " ".join((e.find("a:title", ATOM).text or "").split())
        abstract = " ".join((e.find("a:summary", ATOM).text or "").split())
        url = (e.find("a:id", ATOM).text or "").strip()
    except AttributeError:
        return None
    return Item(source="arXiv", kind="paper", title=title, url=url,
                date=published, summary=abstract[:1600])


def collect_arxiv(since: dt.date, cap: int = 160) -> list[Item]:
    """Two passes: broad category browse, then the targeted phrase searches."""
    found: dict[str, Item] = {}
    iso = since.isoformat()

    # Pass A - categories. cs.LG gets gated on physical-process keywords.
    for cat, gate in (("cs.RO", False), ("eess.SY", False), ("cs.LG", True)):
        print(f"    arXiv category {cat}")
        for e in _arxiv_query(f"cat:{cat}", 120 if gate else 60):
            item = _entry_to_item(e)
            if item is None or item.date < iso:
                continue
            if gate:
                blob = (item.title + " " + item.summary).lower()
                if not any(k in blob for k in CS_LG_GATE):
                    continue
            found.setdefault(item.url, item)
        time.sleep(3)        # arXiv asks for ~3s between calls. Be polite.

    # Pass B - phrase searches. This is where the factory papers come from.
    for term in ARXIV_TERMS:
        print(f"    arXiv search \"{term}\"")
        for e in _arxiv_query(f'all:"{term}"', 40):
            item = _entry_to_item(e)
            if item is None or item.date < iso:
                continue
            found.setdefault(item.url, item)
        time.sleep(3)

    items = sorted(found.values(), key=lambda i: i.date, reverse=True)
    return items[:cap]


def collect_rss(since: dt.date) -> list[Item]:
    """Lab blogs. Handles both RSS <item> and Atom <entry> feeds."""
    out: list[Item] = []
    iso = since.isoformat()
    for name, url in RSS_FEEDS.items():
        print(f"    feed {name}")
        r = http_get(url, timeout=30)
        if r is None:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            print(f"    ! malformed XML from {name}", file=sys.stderr)
            continue

        for node in root.iter("item"):                       # RSS 2.0
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            raw_date = (node.findtext("pubDate") or "").strip()
            when = _parse_rfc822(raw_date)
            if not title or when is None or when.isoformat() < iso:
                continue
            desc = re.sub(r"<[^>]+>", " ", node.findtext("description") or "")
            out.append(Item(source=name, kind="release", title=title, url=link,
                            date=when.isoformat(),
                            summary=" ".join(desc.split())[:700]))

        for node in root.findall("a:entry", ATOM):           # Atom
            title = " ".join((node.findtext("{http://www.w3.org/2005/Atom}title") or "").split())
            when = (node.findtext("{http://www.w3.org/2005/Atom}updated") or "")[:10]
            link_el = node.find("a:link", ATOM)
            link = link_el.get("href") if link_el is not None else ""
            if not title or not when or when < iso:
                continue
            desc = node.findtext("{http://www.w3.org/2005/Atom}summary") or ""
            out.append(Item(source=name, kind="release", title=title,
                            url=link or "", date=when,
                            summary=" ".join(re.sub(r"<[^>]+>", " ", desc).split())[:700]))
    return out


def _parse_rfc822(value: str) -> Optional[dt.date]:
    from email.utils import parsedate_to_datetime
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def collect_anthropic(since: dt.date) -> list[Item]:
    """No RSS feed exists, so read the sitemap and filter by lastmod date.
    Gives URLs and dates only -- titles come from the slug."""
    print("    Anthropic sitemap")
    r = http_get(ANTHROPIC_SITEMAP, timeout=25)
    if r is None:
        return []
    out: list[Item] = []
    iso = since.isoformat()
    pattern = re.compile(
        r"<loc>(https://www\.anthropic\.com/[^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?")
    for url, lastmod in pattern.findall(r.text):
        if not lastmod or lastmod[:10] < iso:
            continue
        if not any(p in url for p in ANTHROPIC_PREFIXES):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        out.append(Item(
            source="Anthropic", kind="release",
            title=slug.replace("-", " ").capitalize(),
            url=url, date=lastmod[:10],
            summary="(Title inferred from URL slug; Anthropic publishes no RSS feed.)",
        ))
    return out


def collect_trending() -> list[Item]:
    """Hugging Face trending models. Usually general-purpose, but occasionally a
    vision model lands that matters for inspection work."""
    print("    HF trending models")
    r = http_get("https://huggingface.co/api/models", timeout=30, params={
        "sort": "trendingScore", "direction": -1, "limit": 10})
    if r is None:
        return []
    try:
        models = r.json()
    except ValueError:
        return []
    out = []
    for m in models:
        mid = m.get("id", "")
        out.append(Item(
            source="HF Trending", kind="release", title=mid,
            url=f"https://huggingface.co/{mid}",
            date=(m.get("createdAt") or "")[:10],
            summary=f"task={m.get('pipeline_tag')} downloads={m.get('downloads')} likes={m.get('likes')}",
        ))
    return out


def collect_all(days: int) -> list[Item]:
    today = dt.date.today()
    since = today - dt.timedelta(days=days)
    print(f"[1/3] Collecting {since} .. {today}  (free, no model calls)")

    items: list[Item] = []
    items += collect_hf_papers(since, today)
    items += collect_arxiv(since)
    items += collect_rss(since)
    items += collect_anthropic(since)
    items += collect_trending()

    # Dedupe on normalized title -- the same paper arrives from HF and arXiv.
    unique: dict[str, Item] = {}
    for it in items:
        key = it.dedupe_key()
        if not key:
            continue
        if key in unique:
            # Keep whichever copy has the better abstract and the HF vote count.
            kept = unique[key]
            if len(it.summary) > len(kept.summary):
                it.votes = max(it.votes, kept.votes)
                unique[key] = it
            else:
                kept.votes = max(kept.votes, it.votes)
        else:
            unique[key] = it

    result = list(unique.values())
    papers = sum(1 for i in result if i.kind == "paper")
    print(f"      {len(result)} unique items ({papers} papers, {len(result)-papers} releases)")
    return result


# ----------------------------------------------------------------------------
# STAGE 2 - SCREEN.  Cheap model, bulk reading.
# ----------------------------------------------------------------------------

SCREEN_SYSTEM = f"""You screen AI research for a manufacturing engineering audience.

For each numbered item, decide whether a real factory could plausibly use it.

Assign exactly one lever:
{chr(10).join('- ' + l for l in LEVERS)}

Score 0-5 on real-world manufacturing potential:
  5 = a plant could pilot this now, on existing equipment
  4 = clear factory use, needs integration work
  3 = relevant but early, or demonstrated only in simulation
  2 = adjacent; the manufacturing use is a stretch
  1 = barely related
  0 = no realistic manufacturing use at all

Set keep=false for score 0-1. Be strict. Most AI papers are about chatbots,
agents, video generation or self-driving cars and should be dropped. A paper
is NOT manufacturing-relevant just because it mentions "optimization",
"industrial" or "efficiency" in passing.

Favor work on physical processes and real equipment: machining, welding,
additive manufacturing, injection molding, assembly, inspection, plant
scheduling, machine health. Judge only from the title and abstract given.
Return one verdict per item, with the index copied exactly."""


def screen(client: anthropic.Anthropic, items: list[Item], spend: Spend,
           chunk: int = 35) -> list[Item]:
    print(f"[2/3] Screening {len(items)} items on {SCREEN_MODEL}")
    kept: list[Item] = []

    for start in range(0, len(items), chunk):
        batch = items[start:start + chunk]
        spend.check(0.05)

        listing = "\n\n".join(
            f"[{start + n}] ({it.source}, {it.date}) {it.title}\n{it.summary[:900]}"
            for n, it in enumerate(batch)
        )
        prompt = (f"Screen these {len(batch)} items.\n\n{listing}")

        try:
            # Structured outputs guarantee valid JSON back, so there is no
            # regex-scraping of model prose here.
            resp = client.messages.parse(
                model=SCREEN_MODEL,
                max_tokens=8000,
                system=SCREEN_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_format=Verdicts,
            )
            # NOTE: no output_config.effort here -- Haiku 4.5 rejects it.
            verdicts = resp.parsed_output.verdicts
            spend.add(f"screen {start//chunk + 1}", SCREEN_MODEL, resp.usage)
        except anthropic.APIStatusError as e:
            # If structured output isn't available, fall back to asking for raw
            # JSON. Costs the same; just needs parsing.
            print(f"      structured output failed ({e.status_code}); retrying plain", file=sys.stderr)
            resp = client.messages.create(
                model=SCREEN_MODEL, max_tokens=8000,
                system=SCREEN_SYSTEM + '\n\nReply with JSON only: {"verdicts":[{"index":0,'
                '"keep":true,"lever":"...","score":4,"why":"..."}]}',
                messages=[{"role": "user", "content": prompt}],
            )
            spend.add(f"screen {start//chunk + 1}", SCREEN_MODEL, resp.usage)
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            blob = re.search(r"\{.*\}", text, re.S)
            verdicts = [Verdict(**v) for v in json.loads(blob.group(0))["verdicts"]] if blob else []

        by_index = {v.index: v for v in verdicts}
        for n, it in enumerate(batch):
            v = by_index.get(start + n)
            if v is None or not v.keep:
                continue
            it.lever = v.lever if v.lever in LEVERS else None
            it.score = v.score
            it.why = v.why
            if it.lever and it.score and it.score >= 2:
                kept.append(it)

        print(f"      batch {start//chunk + 1}: kept {len(kept)} so far  (${spend.total:.3f})")

    kept.sort(key=lambda i: (-(i.score or 0), -i.votes))
    return kept


def pick_top(kept: list[Item], want: int = 8, per_lever: int = 2) -> list[Item]:
    """Highest-scoring items, but no lever more than twice, so the digest stays
    varied. Returns fewer than `want` on a slow week rather than padding."""
    chosen: list[Item] = []
    used: dict[str, int] = {}
    for it in kept:
        if len(chosen) >= want:
            break
        if used.get(it.lever or "", 0) >= per_lever:
            continue
        chosen.append(it)
        used[it.lever or ""] = used.get(it.lever or "", 0) + 1
    return chosen


# ----------------------------------------------------------------------------
# STAGE 3 - WRITE.  Best model, small input, where quality shows up.
# ----------------------------------------------------------------------------

WRITER_SYSTEM = """You write a weekly email newsletter about AI and
manufacturing. It goes to a general mailing list: engineers, students, plant
managers, a few people who are simply curious. Assume an interested adult who
is NOT an AI researcher and may not work in a factory.

VOICE
Write like a smart friend catching someone up over coffee. Short sentences.
Plain words. No hype -- never "revolutionary", "game-changing", "unlock",
"paradigm shift". No throat-clearing: get to the point in the first line.

Explain every piece of jargon in a few words, on both sides of the fence:
  - AI terms: "a world model -- a network that learns how a system reacts to
    your actions, so you can test moves before making them"
  - Shop-floor terms: "the melt pool -- the small puddle of molten metal the
    laser leaves behind"
If you would have to stop and look a word up, define it.

This is an EMAIL. People skim it on a phone. So:
  - Keep paragraphs to 2-4 sentences. No walls of text.
  - Lead every section with the point, not the background.
  - Bold the few phrases that carry the meaning. Don't bold whole sentences.

Short paragraphs, but MANY of them. Writing for skimmability means tight
sentences and clear signposts -- it does NOT mean covering less. Never drop a
real finding to make the email shorter. A reader who skims should get the
headlines; a reader who reads should get everything that happened.

HARD RULES
1. Never invent a result, number, benchmark or company name that is not in the
   source text you were given. No estimated percentages, no made-up cycle
   times or dollar figures. Without a number, describe the effect in words.
2. Link only the URLs provided. Never construct or guess a link.
3. Never pad. A short honest issue beats a padded one. If only five items
   deserve a slot, write five and say it was a quiet week.
4. Say so plainly when something does not matter. "Nothing here for a factory"
   is useful information, not a failure.

OUTPUT FORMAT -- follow this exactly.

First line, nothing above it:
SUBJECT: [a specific, curiosity-driven subject line under 60 characters. Name
the single most interesting thing in the issue. Not "Weekly AI Digest" -- that
tells the reader nothing. Good: "A model that watches the weld pool"]

Then a blank line, then the body in Markdown:

# [A short headline for the issue, 3-7 words]

*[One italic line: the date range, and how many items you looked at.]*

**The short version**

- [4 bullets. One sentence each. The whole week for someone who reads nothing
  else. Each bullet must contain an actual fact, not a tease.]

## What happened in AI this week

The notable releases and announcements, in plain language. COVER EVERYTHING
NOTABLE -- aim for 5-7 short paragraphs, not 3. Bold the lab or product name at
the start of each. What shipped, who shipped it, what it actually is. Include
the link inline, as a Markdown link: [Read more](url).

Include the smaller stuff too, grouped if need be: notable open-source model
releases, developer tooling, hardware. A reader should finish this section
knowing everything that happened, not just the three biggest headlines.

No manufacturing angle yet -- that is the next section.

## Why a factory should care

Take the items above that have a real connection and name the concrete
consequence on a shop floor. AIM FOR 4-6 paragraphs, each led by a bolded
phrase naming the shift.

KEEP EACH ONE TO 2-3 SENTENCES -- the same tight register as the section above.
State the consequence and stop. Don't walk through the reasoning that got you
there, don't add a second example, don't restate the finding in other words.
Many short paragraphs, never a few long ones.

Do not collapse two distinct consequences into one paragraph to save space --
if cheaper models and local hardware both matter, they are two paragraphs,
because they change different decisions.

Then one final short paragraph: the announcements with no manufacturing read,
so the reader knows they were considered and dismissed rather than missed.

## Digging deeper

The research items. For each, use exactly this shape:

### [N]. [A plain-English title. NOT the paper's title.]

*[Lever given to you] · [the source title], [link]*

**What's new.** 2-3 sentences. What did they build, and why is it better than
what came before? Explain it so someone outside AI follows.

**On the floor.** 2-3 sentences. ONE concrete scene in a real plant. Name the
machine or the process. Say what gets better -- less scrap, less downtime,
shorter cycle time, better material yield, fewer programming hours.

**The catch.** One sentence. The single biggest thing stopping a plant from
using this today. Be blunt. Readers trust this line most, so never soften it.

Keep each numbered item under 130 words.

## One thing to watch

Two or three sentences on the single item most worth following, and why.

Return only the SUBJECT line and the Markdown body. No preamble, no notes
about your process, no sign-off -- the mailer adds the footer."""


def write_report(client: anthropic.Anthropic, picks: list[Item],
                 releases: list[Item], spend: Spend, use_cache: bool) -> str:
    print(f"[3/3] Writing report on {WRITER_MODEL}")
    spend.check(0.60)

    papers_block = "\n\n".join(
        f"ITEM {n}\n"
        f"Assigned lever: {it.lever}\n"
        f"Paper title: {it.title}\n"
        f"URL: {it.url}\n"
        f"Date: {it.date}\n"
        f"Screener note: {it.why}\n"
        f"Abstract: {it.summary}"
        for n, it in enumerate(picks, 1)
    )

    releases_block = "\n\n".join(
        f"{it.source} ({it.date}): {it.title}\n{it.url}\n{it.summary}"
        for it in releases
    ) or "(No lab releases found in the window.)"

    user_msg = (
        f"Week ending {dt.date.today().isoformat()}.\n\n"
        f"=== RELEASES AND ANNOUNCEMENTS (for Parts 1 and 2) ===\n\n{releases_block}\n\n"
        f"=== RESEARCH ITEMS (for Part 3), {len(picks)} of them, pre-screened "
        f"and lever-tagged ===\n\n{papers_block}\n\n"
        "Write the three-part digest. Use only the facts above."
    )

    # Caching is OFF by default on purpose: this script runs once a week, and a
    # cache entry expires long before the next run, so a cache write would add
    # ~25% to input cost for a hit that never comes. --cache is for the day you
    # re-run it five times while tuning the prompt.
    system: Any = (
        [{"type": "text", "text": WRITER_SYSTEM, "cache_control": {"type": "ephemeral"}}]
        if use_cache else WRITER_SYSTEM
    )

    kwargs: dict[str, Any] = dict(
        model=WRITER_MODEL,
        max_tokens=32000,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        # Opus 5.5 defaults to medium effort; this is a writing-quality task, so
        # pay for high. Thinking is always on for this model -- do not pass a
        # `thinking` parameter, it returns a 400.
        output_config={"effort": "high"},
    )

    text, usage, stop_reason = _stream_with_fallback(client, kwargs)
    spend.add("write report", WRITER_MODEL, usage)

    if stop_reason == "refusal":
        raise RuntimeError(
            "The model declined this request (stop_reason=refusal). That is "
            "unexpected for a manufacturing digest -- inspect the collected "
            "items for something odd."
        )
    return text


def _supports_server_fallbacks(client: anthropic.Anthropic) -> bool:
    """Server-side fallbacks reroute a request if a safety classifier declines
    it, instead of failing. The parameter only exists in newer SDK builds, so
    check rather than guess -- on anthropic 0.96.0 it is absent."""
    import inspect
    try:
        return "fallbacks" in inspect.signature(client.beta.messages.stream).parameters
    except (TypeError, ValueError):
        return False


def _stream_with_fallback(client: anthropic.Anthropic, kwargs: dict[str, Any]):
    """Stream the report. Streaming matters here: a long report with a high
    max_tokens would otherwise risk the HTTP request timing out.
    """
    if _supports_server_fallbacks(client):
        try:
            with client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **kwargs,
            ) as stream:
                for chunk in stream.text_stream:
                    print(chunk, end="", flush=True)
                msg = stream.get_final_message()
            print()
            return _text_of(msg), msg.usage, msg.stop_reason
        except (anthropic.BadRequestError, anthropic.NotFoundError, TypeError) as e:
            # Beta not enabled on this account -- not fatal, just unroute.
            print(f"\n      (server-side fallbacks rejected: {type(e).__name__}; "
                  f"using the standard endpoint)", file=sys.stderr)

    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
        msg = stream.get_final_message()
    print()
    return _text_of(msg), msg.usage, msg.stop_reason


def _text_of(msg: Any) -> str:
    # Thinking blocks come back alongside text blocks -- filter by .type.
    return "\n".join(b.text for b in msg.content if b.type == "text").strip()


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="AI -> Manufacturing weekly digest")
    ap.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    ap.add_argument("--items", type=int, default=8, help="items in Part 3 (default 8)")
    # default None so we can tell "user passed --budget" from "user didn't",
    # and let the CLI flag win over DIGEST_BUDGET_USD in .env.
    ap.add_argument("--budget", type=float, default=None,
                    help="hard USD cap (default 2.00, or DIGEST_BUDGET_USD in .env)")
    ap.add_argument("--dry-run", action="store_true", help="collect only; no model calls, $0")
    ap.add_argument("--cache", action="store_true", help="cache the system prompt (only pays off on same-hour re-runs)")
    ap.add_argument("--out", default=None, help="output path (default digest_YYYY-MM-DD.md)")
    # --- email ---
    ap.add_argument("--send", action="store_true",
                    help="actually email the digest to the list (default: preview only)")
    ap.add_argument("--test-email", metavar="ADDR", default=None,
                    help="send only to this one address, ignoring the list")
    ap.add_argument("--check-smtp", action="store_true",
                    help="log in to the mail server and exit; emails nobody")
    ap.add_argument("--email-file", metavar="PATH", default=None,
                    help="email an existing .md file instead of generating a new one")
    ap.add_argument("--env", default=".env", help="path to the .env file (default .env)")
    args = ap.parse_args()

    mailer.load_env(args.env)

    # .env may override the models and the budget. Warn rather than fail on an
    # unknown model id -- the price table just can't cost it.
    global WRITER_MODEL, SCREEN_MODEL
    WRITER_MODEL = mailer.env("WRITER_MODEL") or WRITER_MODEL
    SCREEN_MODEL = mailer.env("SCREEN_MODEL") or SCREEN_MODEL
    for label, model in (("WRITER_MODEL", WRITER_MODEL), ("SCREEN_MODEL", SCREEN_MODEL)):
        if model not in PRICES:
            print(f"      note: {label}={model} is not in the price table, so its "
                  f"spend will show as $0.00", file=sys.stderr)
    if args.budget is None:                 # not given on the CLI
        try:
            args.budget = float(mailer.env("DIGEST_BUDGET_USD") or 2.00)
        except ValueError:
            args.budget = 2.00

    # --check-smtp: prove the credentials work before trusting a 10am cron job.
    if args.check_smtp:
        cfg = mailer.MailConfig.from_env()
        issues = cfg.problems()
        # A connection test doesn't need recipients.
        issues = [i for i in issues if "recipient" not in i]
        if issues:
            print("Fix these in .env first:", file=sys.stderr)
            for i in issues:
                print(f"  - {i}", file=sys.stderr)
            return 1
        return 0 if mailer.check_connection(cfg) else 1

    # --email-file: re-send (or preview) a digest already on disk. Useful when
    # the 10am send failed but the report itself is fine.
    if args.email_file:
        body = open(args.email_file, encoding="utf-8").read()
        return _deliver(body, args, generated=False)

    items = collect_all(args.days)
    if not items:
        print("Nothing collected. Every source failed -- check your network.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run: collected sources, no model calls, $0.00 spent.\n")
        for it in sorted(items, key=lambda i: (i.kind, i.date), reverse=True):
            print(f"  [{it.kind:7}] {it.date}  {it.source:18} {it.title[:88]}")
        print(f"\n{len(items)} items. Drop --dry-run to screen and write.")
        return 0

    # Credentials: a bare Anthropic() also picks up an `ant auth login` profile,
    # so an unset ANTHROPIC_API_KEY does not necessarily mean no credentials.
    client = anthropic.Anthropic()
    spend = Spend(args.budget)

    try:
        kept = screen(client, items, spend)
        if not kept:
            print("Nothing survived screening. Slow week, or the filters are too tight.")
            return 0

        picks = pick_top(kept, want=args.items)
        releases = [i for i in items if i.kind == "release"][:22]

        print(f"\n      Selected {len(picks)} of {len(kept)} kept:")
        for n, it in enumerate(picks, 1):
            print(f"        {n}. [{it.lever}] (score {it.score}) {it.title[:66]}")
        print()

        report = write_report(client, picks, releases, spend, args.cache)

    except RuntimeError as e:                       # our own budget / refusal stop
        print(f"\nStopped: {e}", file=sys.stderr)
        print(spend.report(), file=sys.stderr)
        return 1
    # Most specific first. Lumping these together loses the retryable/fatal split.
    except anthropic.NotFoundError as e:
        print(f"\nModel or endpoint not found: {e}", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("\nAuth failed. Set ANTHROPIC_API_KEY, or run `ant auth login`.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("\nRate limited after the SDK's own retries. Wait and re-run.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"\nAPI error {e.status_code}: {e.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError as e:
        print(f"\nCould not reach the API: {e}", file=sys.stderr)
        return 1

    path = args.out or f"digest_{dt.date.today().isoformat()}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(report.strip() + "\n")

    print(f"\nWrote {path}  ({len(report.split())} words)")
    print(spend.report())

    rc = _deliver(report, args, generated=True)
    return rc


def _deliver(body: str, args: argparse.Namespace, generated: bool) -> int:
    """Preview or send the digest. Sending requires --send, every time."""
    cfg = mailer.MailConfig.from_env()

    fallback_subject = f"AI x Manufacturing - week of {dt.date.today().isoformat()}"
    subject, email_body = mailer.split_subject(body, fallback_subject)

    recipients = [args.test_email] if args.test_email else None

    issues = cfg.problems()
    if args.test_email:
        issues = [i for i in issues if "recipient" not in i]
    if issues:
        if args.send:
            print("\nCannot send -- fix these in .env:", file=sys.stderr)
            for i in issues:
                print(f"  - {i}", file=sys.stderr)
            print("\nThe digest file was still written, so nothing is lost.",
                  file=sys.stderr)
            return 1
        # Not sending anyway: just note it and move on.
        print(f"\n(Email not configured yet: {issues[0]}. "
              f"Fill in .env, then use --check-smtp.)")
        return 0

    delivered = mailer.send_digest(cfg, subject, email_body,
                                   recipients=recipients, dry_run=not args.send)

    # A send that reached nobody used to exit 0, which made a dead SMTP app
    # password look exactly like a good week. Say so in the exit code, so the
    # workflow can raise the alarm instead of recording a marker.
    if args.send and not delivered:
        print("\nSend reached 0 recipients -- treating this as a failure.",
              file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------------------
# SOURCE NOTES  (verified 2026-10-05 -- recheck if a source goes quiet)
# ----------------------------------------------------------------------------
# Anthropic : has NO RSS feed. /rss.xml, /news/rss.xml, /feed.xml, /index.xml,
#             /atom.xml and ten other paths all 404, and the news page carries
#             no feed autodiscovery link. The sitemap is the working substitute.
# Meta AI   : ai.meta.com/blog/rss/ is dead (404, serves malformed HTML).
#             Replaced by engineering.fb.com/feed/ (technical, low volume) and
#             about.fb.com/news/feed/ (announcements). research.facebook.com/feed/
#             also works but is very sparse.
# OpenAI    : the RSS feed works, but article pages return 403 to scripts. The
#             feed descriptions are detailed enough -- don't try to fetch pages.
# arXiv     : needs HTTPS. Python's stdlib urllib fails certificate verification
#             on macOS; `requests` works because it ships its own CA bundle.
#             Browsing by category returns mostly irrelevant work -- the
#             ARXIV_TERMS phrase searches are what find the factory papers.
# GitHub    : trending has no API and needs HTML scraping that breaks often.
#             Deliberately left out; it has never produced a digest item.

if __name__ == "__main__":
    sys.exit(main())
