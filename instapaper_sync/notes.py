"""Rendering of Instapaper bookmarks as Obsidian notes."""

from __future__ import annotations

import datetime
import os
import re

from .categorize import domain_of, slugify

USER_NOTES_MARKER = "<!-- instapaper-sync:user-notes -->"
# Two distinct empty states: text we have not downloaded yet, and text
# Instapaper cannot produce at all (dead link, paywall, blocked site).
PENDING_NOTICE = "Article text not downloaded"
UNAVAILABLE_NOTICE = "Article text unavailable"
ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


# --------------------------------------------------------------------- helpers


def iso_date(timestamp):
    if not timestamp:
        return None
    return datetime.datetime.utcfromtimestamp(int(timestamp)).strftime("%Y-%m-%d")


def iso_datetime(timestamp):
    if not timestamp:
        return None
    return datetime.datetime.utcfromtimestamp(int(timestamp)).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def status_of(bookmark):
    progress = bookmark.get("progress") or {}
    percentage = float(progress.get("percentage") or 0)
    if bookmark.get("archived"):
        return "archived"
    if percentage >= 0.95:
        return "read"
    if percentage > 0:
        return "reading"
    return "to-read"


def is_read(bookmark):
    return bool(bookmark.get("archived")) or float(
        (bookmark.get("progress") or {}).get("percentage") or 0
    ) >= 0.95


def safe_filename(title, fallback="Untitled", max_length=120):
    name = (title or "").replace("\u00a0", " ")
    name = ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > max_length:
        cut = name[:max_length].rstrip(" .")
        name = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return name or fallback


# ------------------------------------------------------------------ frontmatter


def yaml_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if text == "":
        return '""'
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_frontmatter(fields):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            if not value:
                lines.append(f"{key}: []")
                continue
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {yaml_scalar(item)}")
        elif isinstance(value, dict):
            if not value:
                continue
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {yaml_scalar(sub_value)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------- tags


def note_tags(bookmark, category):
    tags = ["instapaper"]
    if category:
        tags.append(slugify(category))
    for tag in bookmark.get("tags") or []:
        if isinstance(tag, dict) and tag.get("name"):
            candidate = slugify(str(tag["name"]))
            if candidate and candidate not in tags:
                tags.append(candidate)
    return tags


# ---------------------------------------------------------------------- notes


def strip_images(markdown):
    markdown = re.sub(r"!\[[^\]]*\]\([^)]*\)\s*\n?", "", markdown)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def metadata_line(bookmark, config):
    parts = []
    author = bookmark.get("author")
    if author:
        parts.append(f"*{author}*")
    site = domain_of(bookmark.get("url"))
    url = bookmark.get("url")
    if site and url:
        parts.append(f"[{site}]({url})")
    elif url:
        parts.append(url)
    published = iso_date(bookmark.get("pubtime"))
    if published:
        parts.append(f"published {published}")
    saved = iso_date(bookmark.get("time"))
    if saved:
        parts.append(f"saved {saved}")
    return " · ".join(parts)


def word_count(text):
    """Words in the text we actually store, for Obsidian's `words:` searches."""
    return len(re.findall(r"[\w'\u2019-]+", text or ""))


def render_note(
    bookmark,
    body,
    category,
    config,
    highlights=None,
    synced_at=None,
    user_notes="",
    unavailable=False,
):
    status = status_of(bookmark)
    title = bookmark.get("title") or f"Instapaper item {bookmark.get('id')}"
    folder = bookmark.get("folder_title")

    fields = {
        "instapaper_id": bookmark.get("id"),
        "title": title,
        "url": bookmark.get("url"),
        "author": bookmark.get("author"),
        "site": domain_of(bookmark.get("url")) or None,
        "saved": iso_datetime(bookmark.get("time")),
        "published": iso_date(bookmark.get("pubtime")),
        "status": status,
        "read": is_read(bookmark),
        "archived": bool(bookmark.get("archived")),
        "reading_progress": round(float((bookmark.get("progress") or {}).get("percentage") or 0), 4),
        "liked": bool(bookmark.get("liked")),
        "folder": folder,
        "instapaper_tags": [
            str(t.get("name")) for t in bookmark.get("tags") or [] if isinstance(t, dict)
        ],
        "category": category,
        "tags": note_tags(bookmark, category),
        # The body is what search can actually find, so the count describes it
        # rather than Instapaper's estimate for the original page.
        "words": word_count(body) if body else None,
        "instapaper_url": f"https://www.instapaper.com/read/{bookmark.get('id')}",
        "synced": synced_at or now_iso(),
    }
    extra = (config.get("frontmatter") or {}).get("extra") or {}
    if extra:
        fields = {**fields, **extra}
    fields = {key: value for key, value in fields.items() if value is not None or key in (
        "url", "author", "published", "folder", "words"
    )}

    chunks = [render_frontmatter(fields), f"# {title}"]
    line = metadata_line(bookmark, config)
    if line:
        chunks.append(line)

    description = (bookmark.get("description") or "").strip()
    if config.get("frontmatter", {}).get("include_description", True) and len(description) > 20:
        flattened = re.sub(r"\s+", " ", description)
        if flattened.lower()[:120] not in (body or "").lower():
            chunks.append(f"> {flattened}")

    if highlights:
        quoted = []
        for highlight in highlights:
            text = (highlight.get("text") or "").strip()
            if not text:
                continue
            quoted.append(f"> {text}")
            note = (highlight.get("note") or "").strip()
            if note:
                quoted.append(f">\n> **Note:** {note}")
            quoted.append("")
        if quoted:
            chunks.append("## Highlights\n\n" + "\n".join(quoted).strip())

    if body:
        chunks.append("## Article\n\n" + body)
    elif unavailable:
        chunks.append(
            f"> [!warning] {UNAVAILABLE_NOTICE}\n"
            "> Instapaper could not extract this article (dead link, paywall, or a site "
            "that blocks it). The note still has the title, link and your highlights.\n"
            "> Retry from time to time: `python3 -m instapaper_sync sync --retry-failures`"
        )
    else:
        chunks.append(
            f"> [!warning] {PENDING_NOTICE}\n"
            "> Re-run the sync to fetch it: `python3 -m instapaper_sync sync --refresh`"
        )

    extra_notes = (user_notes or "").strip("\n")
    tail = ["---", USER_NOTES_MARKER, "## Notes", ""]
    if extra_notes:
        tail.append(extra_notes)
    chunks.append("\n".join(tail).rstrip() + "\n")
    return "\n\n".join(chunks)


def split_user_notes(text):
    index = text.find(USER_NOTES_MARKER)
    if index < 0:
        return ""
    remainder = text[index + len(USER_NOTES_MARKER):]
    remainder = re.sub(r"^\s*## Notes\s*", "", remainder.lstrip("\n"), count=1)
    return remainder.strip("\n")


def read_frontmatter_id(text):
    match = re.match(r"---\n(.*?)\n---", text, re.S)
    if not match:
        return None
    found = re.search(r"^instapaper_id:\s*(\d+)", match.group(1), re.M)
    return int(found.group(1)) if found else None


def relative_note_path(bookmark, config, filename=None):
    """Where the note for this bookmark belongs, relative to the vault root."""
    status = status_of(bookmark)
    template = (config.get("filename") or {}).get("template") or "{title}"
    title = bookmark.get("title") or f"Instapaper {bookmark.get('id')}"
    stem = filename or template.replace("{title}", title).replace("{id}", str(bookmark.get("id")))
    stem = safe_filename(stem, fallback=f"Instapaper {bookmark.get('id')}",
                         max_length=int((config.get("filename") or {}).get("max_length") or 120))

    notes_dir = config.get("notes_dir").strip("/")
    parts = [notes_dir] if notes_dir else []
    if config.get("layout") == "folders":
        if status == "archived":
            parts.append(config.get("archive_dir") or "Archived")
        else:
            parts.append(config.get("to_read_dir") or "To Read")
    parts.append(stem + ".md")
    return os.path.join(*parts)


def write_note(vault_path, relative_path, text):
    target = os.path.join(vault_path, relative_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.path.exists(target):
        os.replace(temporary, target)
    else:
        os.rename(temporary, target)
    return target


def read_note(vault_path, relative_path):
    target = os.path.join(vault_path, relative_path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


# ---------------------------------------------------------------------- index


def render_index(counts, recent, config, categories, generated_at):
    root = config.get("notes_dir").strip("/")
    to_read = f"{root}/{config.get('to_read_dir')}" if config.get("layout") == "folders" else root
    archived = f"{root}/{config.get('archive_dir')}" if config.get("layout") == "folders" else root

    lines = [
        "---",
        "title: \"Instapaper Index\"",
        "tags:",
        "  - instapaper",
        "  - index",
        f"generated: \"{generated_at}\"",
        "---",
        "",
        "# Instapaper Index",
        "",
        f"Synced **{counts['total']}** articles "
        f"(**{counts['to_read']}** to read, **{counts['archived']}** archived, "
        f"**{counts['reading']}** in progress).",
        "",
        "## Search",
        "",
        "| What | Query |",
        "| --- | --- |",
        f"| Everything synced | `path:\"{root}\"` |",
        f"| To read only | `path:\"{to_read}\"` |",
        f"| Archived only | `path:\"{archived}\"` |",
        "| Unread + a topic | `path:\"" + to_read + "\" [category:AI]` |",
        "| Anything with a tag | `tag:#instapaper` |",
        "| Downloaded long-form | `path:\"" + root + "\" [words:>1500]` |",
        "",
        "Tip: add `-[\"status\":read]` to hide anything Instapaper considers read.",
        "",
        "## Categories",
        "",
        "| Category | To read | Archived | Total |",
        "| --- | --- | --- | --- |",
    ]
    for category in categories:
        row = counts["by_category"].get(category, {"to_read": 0, "archived": 0})
        total = row["to_read"] + row["archived"]
        lines.append(f"| {category} | {row['to_read']} | {row['archived']} | {total} |")

    if recent:
        lines += ["", "## Recently saved", ""]
        for item in recent:
            label = (item.get("title") or "Untitled").replace("|", "\\|")
            path = item.get("path")
            status = item.get("status") or ""
            saved = item.get("saved") or ""
            entry = f"[{label}](<{path}>)" if path else label
            lines.append(f"- {saved} · {status} · {entry}")

    lines.append("")
    return "\n".join(lines)
