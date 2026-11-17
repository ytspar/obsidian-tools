"""Check the notes on disk, one file at a time.

The sync writes notes; this module reads them back and complains when a file is
not what it pretends to be -- a missing article body, a placeholder left behind,
frontmatter that does not parse, a status that contradicts the flags, or a note
that has been mangled in transit (iCloud has been known to hand back half a
file).  It is deliberately stdlib-only and defensive: every read error becomes a
finding rather than a crash.
"""

from __future__ import annotations

import html
import json
import os
import re

from . import html2md, notes

PLACEHOLDER = notes.PENDING_NOTICE
UNAVAILABLE = notes.UNAVAILABLE_NOTICE
ARTICLE_HEADING = "## Article"
NOTES_MARKER = "<!-- instapaper-sync:user-notes -->"

REQUIRED_KEYS = ("instapaper_id", "title", "url", "saved", "status", "archived")
STATUSES = ("to-read", "reading", "read", "archived")

_YAML_STRING = re.compile(r'^(\s*)([A-Za-z_][\w-]*):\s*(.*)$')
_STRIP_BLOCKS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_CODE_CHUNKS = re.compile(r"\n\s*\n")
_TAGS = re.compile(r"(?s)<[^>]+>")

# A page that stops in the middle of a subscription nudge was cut short, even if
# Instapaper did not set its paywall flag.
SHORT_ENDINGS = (
    "subscribe to continue",
    "subscribers to continue",
    "continue reading with",
    "to continue reading",
    "this article is for subscribers",
    "for subscribers only",
    "sign in to continue",
    "log in to continue",
    "already a subscriber",
    "you have reached your",
    "you've reached your",
    "start your free trial",
    "read the full article",
)


def parse_frontmatter(text):
    """Return (mapping, ok). Handles the flat subset of YAML the sync emits."""
    if not text.startswith("---\n"):
        return {}, False
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, False
    block = text[4:end]
    data = {}
    for line in block.split("\n"):
        if not line.strip() or line.lstrip().startswith("- "):
            continue  # list items are inspected by callers via raw_frontmatter
        match = _YAML_STRING.match(line)
        if not match:
            continue
        key, raw = match.group(2), match.group(3).strip()
        data[key] = _scalar(raw)
    return data, True


def _scalar(raw):
    if raw in ("", "null", "~"):
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def frontmatter_tags(text):
    """Pull the block-form ``tags:`` list out of the raw frontmatter."""
    lines = text.split("\n")
    tags = []
    inside = False
    for line in lines:
        if line.startswith("tags:"):
            inside = True
            continue
        if inside:
            stripped = line.strip()
            if stripped.startswith("- "):
                value = stripped[2:].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                tags.append(value)
            elif stripped:
                break
    return tags


def page_words(source_html):
    """Word count of the text inside a parsed page's HTML."""
    if not source_html:
        return 0
    text = _TAGS.sub(" ", _STRIP_BLOCKS.sub(" ", source_html))
    text = html.unescape(text)
    # Instapaper sometimes flattens an inline consent script into body text; the
    # renderer drops it, so it must not count as article words here either.
    kept = [chunk for chunk in _CODE_CHUNKS.split(text) if not html2md.looks_like_code(chunk)]
    return notes.word_count(" ".join(kept))


def completeness(body, payload):
    """Is the stored article as complete as the page Instapaper parsed?

    The page comes from the parse cache, so this compares our rendering with the
    text it was made from -- the only reference that does not depend on the live
    site.
    """
    findings = []
    content = (payload or {}).get("content") or {}
    source_html = content.get("body") or ""
    stored = notes.word_count(body)

    if content.get("paywalled") and stored:
        findings.append(
            ("warn", "Instapaper could only read part of this article (paywall)")
        )

    words_in_page = page_words(source_html)
    # An embed-only page is an iframe and a caption: Instapaper's count comes from
    # metadata, not from text we could have rendered.
    embed_only = "<iframe" in source_html.lower() and stored < 60
    if words_in_page >= 400 and not embed_only and stored < words_in_page * 0.5:
        findings.append(
            (
                "error",
                f"text may be incomplete: {stored} words stored, "
                f"{words_in_page} in the parsed page",
            )
        )

    flattened = " ".join(body.split())[-220:].lower()
    for phrase in SHORT_ENDINGS:
        if phrase in flattened:
            findings.append(
                ("warn", f"article stops in a subscription prompt ({phrase!r})")
            )
            break
    return findings


def article_section(text):
    """Return the text between ``## Article`` and the notes marker, or None."""
    start = text.find(ARTICLE_HEADING)
    if start < 0:
        return None
    start += len(ARTICLE_HEADING)
    end = text.find(NOTES_MARKER, start)
    if end < 0:
        end = len(text)
    return text[start:end]


def inspect(path, text, raw_ok=True, payload=None):
    """Return a list of (severity, message) findings for one note."""
    problems = []
    name = os.path.basename(path)

    if not raw_ok:
        return [("error", f"{name}: could not be read from disk")]

    data, ok = parse_frontmatter(text)
    if not ok:
        problems.append(("error", f"{name}: no parsable frontmatter block"))
        return problems

    for key in REQUIRED_KEYS:
        if key not in data or data[key] in (None, ""):
            problems.append(("error", f"{name}: frontmatter is missing {key!r}"))

    status = data.get("status")
    if status is not None and status not in STATUSES:
        problems.append(("error", f"{name}: unknown status {status!r}"))

    archived = data.get("archived")
    if isinstance(archived, bool) and status is not None:
        expected = "archived" if archived else None
        if expected and status != expected:
            problems.append(
                ("error", f"{name}: archived is true but status is {status!r}")
            )
        if not archived and status == "archived":
            problems.append(
                ("error", f"{name}: status is 'archived' but archived is false")
            )

    if NOTES_MARKER not in text:
        problems.append(("error", f"{name}: lost the user-notes marker"))

    placeholder = PLACEHOLDER in text or UNAVAILABLE in text
    words = data.get("words")
    if placeholder:
        if words:
            problems.append(
                ("error", f"{name}: placeholder body but frontmatter claims {words} words")
            )
        elif UNAVAILABLE in text:
            problems.append(("info", f"{name}: article text unavailable from Instapaper"))
        else:
            problems.append(("info", f"{name}: article text not downloaded yet"))

    section = article_section(text)
    if section is None:
        if not placeholder:
            problems.append(("error", f"{name}: has no '## Article' section"))
        return problems

    body = section.strip()
    if placeholder:
        pass
    elif not body:
        problems.append(("error", f"{name}: '## Article' section is empty"))
    elif isinstance(words, int) and words >= 60:
        # the rendered markdown should be in the same ballpark as the API's count
        rendered = notes.word_count(body)
        if rendered < words * 0.35:
            problems.append(
                ("error", f"{name}: body has {rendered} words, frontmatter says {words}")
            )

    if payload is not None and body and not placeholder:
        for severity, message in completeness(body, payload):
            problems.append((severity, f"{name}: {message}"))

    tags = frontmatter_tags(text)
    if "instapaper" not in tags:
        problems.append(("error", f"{name}: missing the 'instapaper' tag"))

    if data.get("words") is None and not placeholder:
        problems.append(("warn", f"{name}: body present but no word count recorded"))

    return problems


def load_payload(cache_dir, identifier):
    """The cached parse for a bookmark, or None when it is not on disk."""
    if not cache_dir or identifier in (None, ""):
        return None
    path = os.path.join(os.path.expanduser(cache_dir), "parse", f"{identifier}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def scan(
    vault_path,
    notes_dir="Instapaper",
    limit=None,
    index_note="Instapaper Index",
    cache_dir=None,
):
    """Walk the notes folder and inspect every markdown file."""
    root = os.path.join(vault_path, notes_dir)
    findings = []
    files = []
    skip = {f"{index_note}.md", "index.md"} if index_note else set()
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.endswith(".md") and name not in skip:
                files.append(os.path.join(base, name))
    for path in files[:limit] if limit else files:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            findings.append(("error", f"{os.path.basename(path)}: could not be read"))
            continue
        identifier = parse_frontmatter(text)[0].get("instapaper_id")
        findings.extend(
            inspect(path, text, payload=load_payload(cache_dir, identifier))
        )
    return files, findings


def summarise(files, findings):
    counts = {"error": 0, "warn": 0, "info": 0}
    for severity, _message in findings:
        counts[severity] = counts.get(severity, 0) + 1
    with_body = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        section = article_section(text)
        if section and PLACEHOLDER not in section and UNAVAILABLE not in section and section.strip():
            with_body += 1
    lines = [
        f"files          {len(files)}",
        f"with article   {with_body}",
        f"without text   {len(files) - with_body}",
        f"errors         {counts['error']}",
        f"warnings       {counts['warn']}",
    ]
    return "\n".join(lines), with_body


def report(findings, verbose=False, sample=10):
    """Group findings into readable output."""
    order = {"error": 0, "warn": 1, "info": 2}
    grouped = {}
    for severity, message in findings:
        detail = message.split(": ", 1)[1] if ": " in message else message
        kind = _kind(detail)
        grouped.setdefault((severity, kind), []).append(message)
    lines = []
    for (severity, kind), messages in sorted(
        grouped.items(), key=lambda item: (order[item[0][0]], -len(item[1]))
    ):
        lines.append(f"{severity.upper():5} {kind} ({len(messages)})")
        if severity != "info" or verbose:
            shown = messages if verbose else messages[:sample]
            for message in shown:
                lines.append(f"        {message}")
            if not verbose and len(messages) > len(shown):
                lines.append(f"        ... and {len(messages) - len(shown)} more")
    return "\n".join(lines)


def _kind(detail):
    known = [
        "article text not downloaded yet",
        "could not be read",
        "could not be read from disk",
        "no parsable frontmatter block",
        "has no '## Article' section",
        "'## Article' section is empty",
        "lost the user-notes marker",
        "missing the 'instapaper' tag",
        "body present but no word count recorded",
    ]
    for candidate in known:
        if detail == candidate:
            return candidate
    if detail.startswith("frontmatter is missing"):
        return "frontmatter is missing a key"
    if detail.startswith("unknown status"):
        return "unknown status"
    if detail.startswith("archived is true but status"):
        return "status contradicts archived flag"
    if detail.startswith("status is 'archive' but"):
        return "status contradicts archived flag"
    if detail.startswith("placeholder body but frontmatter claims"):
        return "placeholder body with a word count"
    if " words, frontmatter says " in detail:
        return "body shorter than the recorded word count"
    return detail
