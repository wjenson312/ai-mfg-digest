#!/usr/bin/env python3
"""
Email delivery for the weekly digest.

Reads SMTP settings and the recipient list from .env, converts the Markdown
digest to a readable HTML email, and sends it.

Two things worth knowing about how this sends:

1. RECIPIENTS GO IN BCC. Everyone on the list gets the same message, but nobody
   sees anybody else's address. Putting a mailing list in To: or Cc: leaks every
   subscriber's email to every other subscriber, which is the single most common
   way a small newsletter creates a privacy problem.

2. NOTHING SENDS UNLESS YOU PASS --send. The default is a preview. This is
   deliberate: an email is not undoable, so sending is opt-in every single time.
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------------
# .env LOADING
# ----------------------------------------------------------------------------

def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Minimal .env reader -- no dependency needed.

    Real environment variables always win, so you can override any single
    setting for one run without editing the file:
        EMAIL_RECIPIENTS="just.me@example.com" python digest_agent.py --send
    """
    found: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            found[key] = value
            os.environ.setdefault(key, value)   # setdefault = real env wins
    return found


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

@dataclass
class MailConfig:
    host: str
    port: int
    user: str
    password: str
    security: str            # "starttls" | "ssl" | "none"
    from_addr: str
    from_name: str
    reply_to: str
    subject_prefix: str
    recipients: list[str]
    batch_size: int
    batch_pause: int
    footer: str
    unsubscribe: str

    @classmethod
    def from_env(cls) -> "MailConfig":
        recipients = _read_recipients()
        return cls(
            host=env("SMTP_HOST"),
            port=int(env("SMTP_PORT", "587") or 587),
            user=env("SMTP_USER"),
            password=_normalize_password(env("SMTP_PASSWORD")),
            security=env("SMTP_SECURITY", "starttls").lower(),
            from_addr=env("EMAIL_FROM") or env("SMTP_USER"),
            from_name=env("EMAIL_FROM_NAME", "AI x Manufacturing Digest"),
            reply_to=env("EMAIL_REPLY_TO"),
            subject_prefix=env("EMAIL_SUBJECT_PREFIX"),
            recipients=recipients,
            batch_size=int(env("EMAIL_BATCH_SIZE", "40") or 40),
            batch_pause=int(env("EMAIL_BATCH_PAUSE_SECONDS", "5") or 5),
            footer=env("EMAIL_FOOTER"),
            unsubscribe=env("EMAIL_UNSUBSCRIBE"),
        )

    def problems(self) -> list[str]:
        """Everything wrong with the config, all at once -- so you fix it in one
        pass instead of discovering one missing field per run."""
        issues = []
        if not self.host:
            issues.append("SMTP_HOST is empty")
        if not self.user:
            issues.append("SMTP_USER is empty")
        if not self.password:
            issues.append("SMTP_PASSWORD is empty")
        if not self.from_addr:
            issues.append("EMAIL_FROM is empty (and SMTP_USER was not set either)")
        if not self.recipients:
            issues.append("no recipients (set EMAIL_RECIPIENTS or EMAIL_RECIPIENTS_FILE)")
        if self.security not in ("starttls", "ssl", "none"):
            issues.append(f"SMTP_SECURITY must be starttls, ssl, or none (got '{self.security}')")
        bad = [r for r in self.recipients if not _looks_like_email(r)]
        if bad:
            issues.append(f"these don't look like addresses: {', '.join(bad[:5])}")
        return issues


def _normalize_password(value: str) -> str:
    """Strip the display spaces out of a Google app password.

    Google shows app passwords as four groups of four ("abcd efgh ijkl mnop"),
    and people copy them exactly as shown. The real secret is the 16 characters
    without spaces, so a pasted password fails authentication for a reason the
    server error never explains.

    Only the 4x4 display pattern is touched -- any other password is returned
    untouched, in case the spaces are genuinely part of it.
    """
    if re.fullmatch(r"[A-Za-z0-9]{4}(?: [A-Za-z0-9]{4}){3}", value):
        return value.replace(" ", "")
    return value


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _read_recipients() -> list[str]:
    """Addresses from EMAIL_RECIPIENTS (comma-separated) and/or a file.
    A file is easier to maintain once the list grows past a handful."""
    out: list[str] = []
    inline = env("EMAIL_RECIPIENTS")
    if inline:
        out += [a.strip() for a in re.split(r"[,;]", inline) if a.strip()]

    path = env("EMAIL_RECIPIENTS_FILE")
    if path and Path(path).exists():
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                out.append(line.split(",")[0].strip())

    seen, unique = set(), []
    for addr in out:
        low = addr.lower()
        if low not in seen:
            seen.add(low)
            unique.append(addr)
    return unique


# ----------------------------------------------------------------------------
# MARKDOWN -> HTML
# ----------------------------------------------------------------------------

EMAIL_CSS = """
  body { margin:0; padding:0; background:#f4f4f2; }
  .wrap { max-width:640px; margin:0 auto; padding:28px 20px 40px;
          font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
          font-size:16px; line-height:1.6; color:#1a1a1a; background:#ffffff; }
  h1 { font-size:24px; line-height:1.3; margin:0 0 4px; }
  h2 { font-size:19px; margin:34px 0 10px; padding-top:18px;
       border-top:1px solid #e4e4e0; }
  /* Item titles sit above the labels in the hierarchy, so they are larger and
     darker. Without this they render at body size and the bold labels below
     compete with them. */
  h3 { font-size:17.5px; margin:26px 0 2px; color:#111; letter-spacing:-0.01em; }
  p  { margin:0 0 14px; }
  ul { margin:0 0 16px; padding-left:22px; }
  li { margin:0 0 7px; }
  a  { color:#9b4a1b; }
  hr { border:0; border-top:1px solid #e4e4e0; margin:26px 0; }
  code { background:#f1f1ee; padding:1px 5px; border-radius:3px; font-size:14px; }
  blockquote { margin:0 0 16px; padding:12px 16px; background:#faf9f6;
               border-left:3px solid #d8d4c8; }
  .meta { color:#6a6a6a; font-size:14px; margin:0 0 22px; }
  .foot { margin-top:34px; padding-top:16px; border-top:1px solid #e4e4e0;
          color:#777; font-size:13px; }
  .foot a { color:#777; }
"""


def markdown_to_html(md: str) -> str:
    """Render the digest body. Uses the `markdown` package when installed and
    falls back to a small built-in converter so a missing dependency downgrades
    the formatting instead of breaking the send."""
    try:
        import markdown as md_lib
        html = md_lib.markdown(md, extensions=["extra", "sane_lists", "nl2br"])
        return _style_item_labels(_linkify(html))
    except ImportError:
        # _basic_markdown linkifies as it goes, so don't run _linkify over it
        # again -- that would nest one <a> inside another.
        return _style_item_labels(_basic_markdown(md))


# The three labels inside each research item. They are styled as small caps
# rather than plain bold so they don't compete with the item's own title, which
# is also bold. If you rename them in WRITER_SYSTEM, rename them here too.
ITEM_LABELS = ("What's new", "On the floor", "The catch")

# Inline styles, not a CSS class: some mail clients (notably parts of Gmail)
# strip <style> blocks, and a label that loses its styling would fall back to
# looking exactly like the heading again -- the thing we're fixing.
_LABEL_STYLE = (
    "display:inline-block;font-size:11px;font-weight:700;"
    "letter-spacing:0.09em;text-transform:uppercase;color:#9b4a1b;"
    "margin-right:7px;"
)


def _style_item_labels(html: str) -> str:
    """Turn `<strong>What's new.</strong>` at the start of a paragraph into a
    small-caps label.

    Without this, the label and the item title are both bold text at the same
    size, so an item reads as four identical-looking bold runs and the eye has
    nothing to anchor on.
    """
    for label in ITEM_LABELS:
        escaped = re.escape(label)
        # Match the label as the first thing in a paragraph, with the trailing
        # period either inside or outside the bold, and a curly or straight
        # apostrophe (Markdown renderers may smarten quotes).
        pattern = re.compile(
            r"(<p[^>]*>)\s*<strong>\s*"
            + escaped.replace(r"\'", "['’]")
            + r"\.?\s*</strong>\.?\s*",
            re.IGNORECASE,
        )
        # No trailing space: the span's own margin-right supplies the gap, and
        # adding a literal space on top of it reads as a double gap.
        html = pattern.sub(
            lambda m: f'{m.group(1)}<span style="{_LABEL_STYLE}">{label}</span>',
            html,
        )
    return html


def _linkify(html: str) -> str:
    """Make bare URLs clickable.

    The markdown package only linkifies explicit [text](url) syntax, so a plain
    `https://...` in the source renders as dead text. In an email that is the
    difference between a reader following a source and not, so every bare URL
    outside an existing tag or attribute gets wrapped here.
    """
    parts = re.split(r"(<[^>]+>)", html)      # keep tags intact, touch text only
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[i] = re.sub(r"(?<![\w@/])(https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?])",
                          r'<a href="\1">\1</a>', part)
    return "".join(parts)


def _basic_markdown(md: str) -> str:
    """Enough Markdown for this digest: headings, bold, italic, links, lists,
    rules, paragraphs. Not a general-purpose converter."""
    def inline(text: str) -> str:
        text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        # Bare URLs -> links, so nothing is left unclickable in an email.
        text = re.sub(r'(?<!["\'>=])(https?://[^\s<)]+)', r'<a href="\1">\1</a>', text)
        return text

    html: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                html.append("</ul>")
                in_list = False
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line.strip()):
            if in_list:
                html.append("</ul>"); in_list = False
            html.append("<hr>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            if in_list:
                html.append("</ul>"); in_list = False
            level = len(heading.group(1))
            html.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            if not in_list:
                html.append("<ul>"); in_list = True
            html.append(f"<li>{inline(bullet.group(1))}</li>")
            continue
        numbered = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if numbered:
            if not in_list:
                html.append("<ul>"); in_list = True
            html.append(f"<li>{inline(numbered.group(1))}</li>")
            continue
        if in_list:
            html.append("</ul>"); in_list = False
        html.append(f"<p>{inline(line)}</p>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def unsubscribe_line(value: str) -> str:
    """Phrase the opt-out for how it actually works.

    With a hosted page, point at the page. With a plain address -- the normal
    case for a hand-managed list -- telling someone to reply is both friendlier
    and more accurate than printing an address they'd have to copy.
    """
    if not value:
        return ""
    if value.startswith("http"):
        return f"To stop receiving this: {value}"
    return "Want off the list? Just reply to this email and say so."


def build_html(body_md: str, cfg: MailConfig) -> str:
    footer_bits = []
    if cfg.footer:
        footer_bits.append(cfg.footer)
    if cfg.unsubscribe:
        footer_bits.append(unsubscribe_line(cfg.unsubscribe))
    footer = ""
    if footer_bits:
        footer = '<div class="foot">' + "<br>".join(
            re.sub(r'(https?://[^\s<]+)', r'<a href="\1">\1</a>', b) for b in footer_bits
        ) + "</div>"

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{EMAIL_CSS}</style></head><body><div class='wrap'>"
        f"{markdown_to_html(body_md)}{footer}"
        "</div></body></html>"
    )


# ----------------------------------------------------------------------------
# SUBJECT LINE
# ----------------------------------------------------------------------------

def split_subject(md: str, fallback: str) -> tuple[str, str]:
    """The writer puts `SUBJECT: ...` on the first line. Pull it off and return
    (subject, body). If it's missing, fall back to the first heading."""
    lines = md.lstrip().splitlines()
    if lines and lines[0].upper().startswith("SUBJECT:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).lstrip()
        return subject or fallback, body
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip() or fallback, md
    return fallback, md


# ----------------------------------------------------------------------------
# SENDING
# ----------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """Build a TLS context with a trust store that actually has certificates in it.

    On macOS, python.org builds of Python do NOT read the system keychain. Unless
    you've run "Install Certificates.command", ssl.create_default_context() comes
    back with zero trusted CAs and every TLS connection fails with
    CERTIFICATE_VERIFY_FAILED -- which looks like a server or password problem but
    isn't. certifi ships the same CA bundle browsers use, so prefer it.

    This never disables verification. If neither store has certificates, the
    connection is supposed to fail.
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        if ctx.get_ca_certs():
            return ctx
    except (ImportError, OSError):
        pass

    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        print("  warning: no trusted CA certificates found. Install certifi with\n"
              "    python3 -m pip install certifi\n"
              "  or run Install Certificates.command in your Python folder.",
              file=sys.stderr)
    return ctx


def _connect(cfg: MailConfig) -> smtplib.SMTP:
    context = _ssl_context()
    if cfg.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=45)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=45)
        server.ehlo()
        if cfg.security == "starttls":
            server.starttls(context=context)
            server.ehlo()
    if cfg.user and cfg.password:
        server.login(cfg.user, cfg.password)
    return server


def _compose(cfg: MailConfig, subject: str, body_md: str, bcc: list[str]) -> EmailMessage:
    msg = EmailMessage()
    full_subject = f"{cfg.subject_prefix} {subject}".strip() if cfg.subject_prefix else subject
    msg["Subject"] = full_subject
    msg["From"] = formataddr((cfg.from_name, cfg.from_addr))
    # To: is the sender. Real recipients are in Bcc so the list stays private.
    msg["To"] = formataddr((cfg.from_name, cfg.from_addr))
    msg["Bcc"] = ", ".join(bcc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    if cfg.unsubscribe:
        # Shows the native "Unsubscribe" button in Gmail and Apple Mail.
        if cfg.unsubscribe.startswith("http"):
            msg["List-Unsubscribe"] = f"<{cfg.unsubscribe}>"
        elif _looks_like_email(cfg.unsubscribe):
            msg["List-Unsubscribe"] = f"<mailto:{cfg.unsubscribe}>"

    plain = body_md
    if cfg.footer:
        plain += f"\n\n---\n{cfg.footer}"
    if cfg.unsubscribe:
        plain += f"\n{unsubscribe_line(cfg.unsubscribe)}"

    msg.set_content(plain)                                   # text/plain part
    msg.add_alternative(build_html(body_md, cfg), subtype="html")  # text/html part
    return msg


def send_digest(cfg: MailConfig, subject: str, body_md: str,
                recipients: Optional[list[str]] = None,
                dry_run: bool = True) -> int:
    """Send to everyone, in batches. Returns the number of addresses delivered to.

    dry_run=True prints what would happen and sends nothing. That is the default
    everywhere in this project.
    """
    targets = recipients if recipients is not None else cfg.recipients
    if not targets:
        print("No recipients -- nothing to send.", file=sys.stderr)
        return 0

    prefix = f"{cfg.subject_prefix} " if cfg.subject_prefix else ""
    if dry_run:
        print("\n--- EMAIL PREVIEW (nothing sent) ---")
        print(f"  From:    {cfg.from_name} <{cfg.from_addr}>")
        print(f"  Subject: {prefix}{subject}")
        print(f"  Bcc:     {len(targets)} recipient(s)")
        for addr in targets[:10]:
            print(f"             {addr}")
        if len(targets) > 10:
            print(f"             ... and {len(targets) - 10} more")
        print(f"  Server:  {cfg.host}:{cfg.port} ({cfg.security})")
        print(f"  Body:    {len(body_md.split())} words")
        print("--- pass --send to actually deliver this ---\n")
        return 0

    sent = 0
    batches = [targets[i:i + cfg.batch_size]
               for i in range(0, len(targets), cfg.batch_size)]
    print(f"Sending to {len(targets)} recipient(s) in {len(batches)} batch(es)...")

    try:
        server = _connect(cfg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        print(f"Could not connect to {cfg.host}:{cfg.port} -- {type(e).__name__}: {e}",
              file=sys.stderr)
        return 0

    try:
        for n, batch in enumerate(batches, 1):
            msg = _compose(cfg, subject, body_md, batch)
            try:
                server.send_message(msg)
                sent += len(batch)
                print(f"  batch {n}/{len(batches)}: {len(batch)} sent")
            except smtplib.SMTPRecipientsRefused as e:
                print(f"  batch {n}: all recipients refused -- {e.recipients}", file=sys.stderr)
            except smtplib.SMTPException as e:
                print(f"  batch {n} failed: {type(e).__name__}: {e}", file=sys.stderr)
            if n < len(batches) and cfg.batch_pause:
                time.sleep(cfg.batch_pause)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass

    print(f"Delivered to {sent}/{len(targets)} recipient(s).")
    return sent


def check_connection(cfg: MailConfig) -> bool:
    """Log in and hang up. Proves the credentials work without emailing anyone."""
    print(f"Testing {cfg.host}:{cfg.port} ({cfg.security}) as {cfg.user} ...")
    try:
        server = _connect(cfg)
        server.quit()
        print("SMTP login OK.")
        return True
    except smtplib.SMTPAuthenticationError as e:
        print(f"Login rejected: {e}\n"
              "  Gmail/Workspace: you need an App Password, not your normal "
              "password (myaccount.google.com -> Security -> App passwords).",
              file=sys.stderr)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        print(f"Connection failed: {type(e).__name__}: {e}", file=sys.stderr)
    return False
