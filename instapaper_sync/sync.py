"""Orchestration for the Instapaper -> Obsidian sync."""

from __future__ import annotations

import collections
import html
import json
import os
import re
import shutil
import threading
import time
from concurrent import futures

from . import notes
from .categorize import Categorizer
from .html2md import html_to_markdown


def log(message):
    print(message, flush=True)


# Instapaper answers "Parsed content unavailable" for an article it cannot parse
# right now - and often produces it on a later request, so it is not a verdict.
# A failed article is parked with a growing cooldown instead of being written
# off: cheap to ignore on the next run, cheap to retry on the tenth.
FAILURE_COOLDOWNS = (3600, 6 * 3600, 86400, 3 * 86400, 7 * 86400, 30 * 86400)


class Sync:
    def __init__(self, config, client, logger=None):
        self.config = config
        self.client = client
        self.log = logger or log
        self.state = self._load_state()
        self.categories = self.state.setdefault("categories", {})
        # Expensive LLM verdicts, kept apart from the rule cache: they are keyed
        # on metadata only, so downloading article text later does not discard
        # a paid classification.
        self.llm_categories = self.state.setdefault("llm", {})
        self.notes_state = self.state.setdefault("notes", {})
        # Bookmarks whose article text is not available yet, with the attempt
        # count and timestamp that decide when to try again.
        self.failures = self.state.setdefault("failures", {})
        self.retry_failures = False
        self.parse_cache_dir = os.path.join(config.cache_dir, "parse")
        self.failed_ids = set()
        os.makedirs(self.parse_cache_dir, exist_ok=True)
        os.makedirs(config.state_dir, exist_ok=True)

        categories_file = config.get("categorize", {}).get("categories_file") or "categories.json"
        path = config.resolve(categories_file)
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"no categories file at {path}; copy categories.example.json there and edit it"
            )
        with open(path, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
        self.categorizer = Categorizer(
            spec,
            mode=config.get("categorize", {}).get("mode") or "rules",
            llm_options=config.get("categorize", {}).get("llm") or {},
            log=self.log,
        )

    # ------------------------------------------------------------------- state

    @property
    def state_path(self):
        return os.path.join(self.config.state_dir, "state.json")

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def save_state(self):
        self.state["last_sync"] = int(time.time())
        temporary = self.state_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
        os.replace(temporary, self.state_path)

    # ------------------------------------------------------------------- cache

    def _cache_path(self, bookmark_id):
        return os.path.join(self.parse_cache_dir, f"{bookmark_id}.json")

    def load_parsed(self, bookmark_id):
        try:
            with open(self._cache_path(bookmark_id), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def store_parsed(self, bookmark_id, payload):
        temporary = self._cache_path(bookmark_id) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, self._cache_path(bookmark_id))

    # -------------------------------------------------------------- collecting

    def collect(self, sections=None, since=None):
        """Return (bookmarks, deleted_ids) for the whole account."""
        sections = sections or self.config.get("sections") or ["home", "archive"]
        page_size = int(self.config.get("page_size") or 500)
        bookmarks, deleted = {}, []

        if since:
            self.log(f"Fetching changes since {since} ...")
            for page, removed, _total in self.client.iter_bookmark_pages(
                page_size=page_size, since=since
            ):
                for bookmark in page:
                    bookmarks[bookmark.get("id")] = bookmark
                deleted.extend(removed)
            return list(bookmarks.values()), deleted

        for section in sections:
            if section == "folders":
                for folder in self.client.folders():
                    self.log(f"Fetching folder '{folder.get('title')}' ...")
                    for bookmark in self.client.iter_bookmarks(
                        folder_id=folder.get("id"), page_size=page_size
                    ):
                        bookmarks[bookmark.get("id")] = bookmark
                continue
            self.log(f"Fetching {section} ...")
            for bookmark in self.client.iter_bookmarks(section=section, page_size=page_size):
                bookmarks[bookmark.get("id")] = bookmark
        return list(bookmarks.values()), deleted

    def folder_titles(self):
        try:
            return {folder.get("id"): folder.get("title") for folder in self.client.folders()}
        except Exception:  # noqa: BLE001 - folder names are cosmetic
            return {}

    # ----------------------------------------------------------------- article

    def prefetch_bodies(self, bookmarks, refresh=False, limit=None, workers=None):
        """Download the article text that is still missing, using several threads.

        Populating the cache up front is what makes the rest of the run a local
        operation: the per-bookmark loop below then never waits on the network.
        """
        if not self.config.get("fetch_article_text", True):
            return 0
        workers = workers if workers is not None else self.config.get("workers") or 1
        workers = max(1, int(workers))

        todo = []
        for bookmark in bookmarks:
            identifier = bookmark.get("id")
            if identifier is None:
                continue
            if not refresh and self.load_parsed(identifier) is not None:
                continue
            if not refresh and not self.retry_ready(str(identifier)):
                continue
            if refresh:
                self.failures.pop(str(identifier), None)
            todo.append(identifier)
            if limit is not None and len(todo) >= limit:
                break
        if not todo:
            return 0

        self.log(
            f"Downloading {len(todo)} article bod{'ies' if len(todo) != 1 else 'y'} "
            f"with {workers} worker{'s' if workers != 1 else ''} ..."
        )
        lock = threading.Lock()

        def work(identifier):
            try:
                payload = self.client.parse(identifier, use_cache=False)
            except Exception as error:  # noqa: BLE001 - recorded, not fatal
                with lock:
                    self.failed_ids.add(identifier)
                    self.record_failure(identifier, error)
                self.log(f"    ! no text for {identifier}: {error}")
                return 0
            with lock:
                self.failures.pop(str(identifier), None)
            self.store_parsed(identifier, payload)
            return 1

        fetched = 0
        done = 0
        total = len(todo)
        step = 25 if workers == 1 else 100
        if workers == 1:
            results = (work(identifier) for identifier in todo)
            for fetched_increment in results:
                fetched += fetched_increment
                done += 1
                if done % step == 0 or done == total:
                    self.log(f"    fetched {done}/{total} ({fetched} usable)")
        else:
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for fetched_increment in pool.map(work, todo):
                    fetched += fetched_increment
                    done += 1
                    if done % step == 0 or done == total:
                        self.log(f"    fetched {done}/{total} ({fetched} usable)")
        self.log(f"Downloaded {fetched} of {total} article bodies, {total - fetched} unavailable")
        if self.failures:
            self.save_state()
        return fetched

    def retry_ready(self, key):
        """Is this parked article due for another attempt?"""
        if self.retry_failures:
            return True
        entry = self.failures.get(key) or {}
        attempts = int(entry.get("attempts") or 1)
        cooldown = FAILURE_COOLDOWNS[min(attempts - 1, len(FAILURE_COOLDOWNS) - 1)]
        return time.time() >= (entry.get("at") or 0) + cooldown

    def record_failure(self, identifier, error):
        key = str(identifier)
        previous = self.failures.get(key) or {}
        self.failures[key] = {
            "reason": str(error)[:200],
            "status": getattr(error, "status", None),
            "attempts": int(previous.get("attempts") or 0) + 1,
            "at": int(time.time()),
        }

    def article_for(self, bookmark, refresh=False, allow_fetch=True):
        """Return (markdown_body, metadata) from cache or the parse endpoint."""
        identifier = bookmark.get("id")
        payload = None if refresh else self.load_parsed(identifier)
        key = str(identifier)
        blocked = identifier in self.failed_ids or (
            key in self.failures and not refresh and not self.retry_ready(key)
        )
        if payload is None and allow_fetch and not blocked:
            try:
                payload = self.client.parse(identifier, use_cache=not refresh)
                self.store_parsed(identifier, payload)
            except Exception as error:  # noqa: BLE001 - one bad article shouldn't stop the run
                self.log(f"    ! could not fetch text for {identifier}: {error}")
                return "", {}
        if not payload:
            return "", {}
        content = payload.get("content") or {}
        metadata = payload.get("metadata") or {}
        body = html_to_markdown(content.get("body") or "")
        if not self.config.get("include_images"):
            body = notes.strip_images(body)
        return body, {
            "words": content.get("words"),
            "paywalled": content.get("paywalled"),
            "pubtime": metadata.get("pubtime"),
            "author": (metadata.get("author") or {}).get("name"),
            "thumbnail": metadata.get("thumbnail"),
        }

    # -------------------------------------------------------------------- note

    def target_path(self, bookmark, filename=None):
        path = notes.relative_note_path(bookmark, self.config.data, filename=filename)
        absolute = os.path.join(self.config.vault_path, path)
        if os.path.exists(absolute):
            existing = notes.read_note(self.config.vault_path, path)
            owner = notes.read_frontmatter_id(existing) if existing else None
            if owner is not None and owner != bookmark.get("id"):
                stem, extension = os.path.splitext(path)
                path = f"{stem} ({bookmark.get('id')}){extension}"
        return path

    def write(self, bookmark, path, text):
        absolute = notes.write_note(self.config.vault_path, path, text)
        return absolute

    def move(self, old_path, new_path):
        source = os.path.join(self.config.vault_path, old_path)
        target = os.path.join(self.config.vault_path, new_path)
        if os.path.abspath(source) == os.path.abspath(target):
            return
        if not os.path.exists(source):
            return
        if os.path.exists(target):
            os.remove(source)
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(source, target)
        self.log(f"    moved -> {new_path}")

    def discard(self, path):
        absolute = os.path.join(self.config.vault_path, path)
        if not os.path.exists(absolute):
            return
        if self.config.get("delete_mode") == "trash":
            trash = os.path.join(self.config.vault_path, ".trash")
            os.makedirs(trash, exist_ok=True)
            target = os.path.join(trash, os.path.basename(absolute))
            shutil.move(absolute, target)
        else:
            os.remove(absolute)
        self.log(f"    removed {path}")

    def quick_body(self, identifier, limit=4000):
        """Article text straight from the parse cache, without the Markdown pass.

        Categorization only looks at the opening of an article, so a cheap
        tag-strip is enough -- and it keeps a full recategorization of 10k
        bookmarks down to seconds instead of an hour of HTML conversion.
        """
        payload = self.load_parsed(identifier)
        if not payload:
            return ""
        raw = (payload.get("content") or {}).get("body") or ""
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        return re.sub(r"\s+", " ", text).strip()[:limit]

    def categorize_all(self, scope=None, refresh=False, limit=None):
        """Recompute the category of every bookmark and store it, rendering nothing."""
        if refresh:
            self.categories.clear()
            self.llm_categories.clear()
        bookmarks, _deleted = self.collect()
        if limit:
            bookmarks = bookmarks[:limit]
        folders = self.folder_titles()
        bodies = {}
        for position, bookmark in enumerate(bookmarks, start=1):
            folder_id = bookmark.get("folder_id")
            bookmark["folder_title"] = folders.get(folder_id) if folder_id else None
            identifier = bookmark.get("id")
            body = self.quick_body(identifier)
            bodies[identifier] = body
            self.categorizer.assign(
                bookmark, body, cache=self.categories,
                folder=bookmark.get("folder_title"),
                llm_cache=self.llm_categories,
            )
            if position % 500 == 0:
                self.log(f"[{position}/{len(bookmarks)}] categorized")

        if (self.config.get("categorize", {}).get("mode") or "rules") == "llm":
            self.categorizer.refine_with_llm(
                bookmarks, bodies, cache=self.categories,
                llm_cache=self.llm_categories, scope=scope,
                progress=self.save_state,
            )

        counts = collections.Counter()
        methods = collections.Counter()
        for bookmark in bookmarks:
            key = str(bookmark.get("id"))
            entry = self.categories.get(key) or {}
            counts[entry.get("category") or self.categorizer.fallback] += 1
            methods[entry.get("method") or "?"] += 1
        self.save_state()
        return counts, methods

    def refine_categories(self, bookmarks, bodies):
        """Second pass: ask the model, then re-render notes whose category moved."""
        settings = self.config.get("categorize", {}).get("llm", {}) or {}
        before = {
            str(b.get("id")): (self.categories.get(str(b.get("id"))) or {}).get("category")
            for b in bookmarks
        }
        assigned = self.categorizer.refine_with_llm(
            bookmarks, bodies, cache=self.categories,
            llm_cache=self.llm_categories, scope=settings.get("scope"),
        )
        if not assigned:
            return
        moved = 0
        for bookmark in bookmarks:
            identifier = bookmark.get("id")
            key = str(identifier)
            new_category = (self.categories.get(key) or {}).get("category")
            if not new_category or new_category == before.get(key):
                continue
            path = self.notes_state.get(key, {}).get("path")
            existing = notes.read_note(self.config.vault_path, path) if path else None
            if existing is None:
                continue
            user_notes = notes.split_user_notes(existing)
            text = notes.render_note(
                bookmark, bodies.get(identifier) or "", new_category, self.config.data,
                user_notes=user_notes,
                unavailable=str(identifier) in self.failures,
            )
            self.write(bookmark, path, text)
            moved += 1
        self.log(f"  llm pass moved {moved} notes to a different category")
        self.save_state()

    # --------------------------------------------------------------------- run

    def run(
        self,
        refresh=False,
        limit=None,
        dry_run=False,
        since=None,
        sections=None,
        workers=None,
        retry_failures=False,
    ):
        user = None
        try:
            user = self.client.me()
            self.log(
                f"Connected as {user.get('username')} "
                f"({'premium' if user.get('premium') else 'free'} account)"
            )
        except Exception as error:  # noqa: BLE001 - surfaced below
            raise SystemExit(f"cannot reach Instapaper: {error}")

        if retry_failures:
            self.log(f"Retrying {len(self.failures)} parked articles now")
        self.retry_failures = retry_failures

        bookmarks, deleted = self.collect(sections=sections, since=since)
        if not bookmarks and not deleted:
            self.log("Nothing to do.")
            return 0

        folders = self.folder_titles()
        for bookmark in bookmarks:
            folder_id = bookmark.get("folder_id")
            bookmark["folder_title"] = folders.get(folder_id) if folder_id else None

        self.log(f"{len(bookmarks)} bookmarks, {len(deleted)} deleted")
        fetched = 0
        chunk_size = max(1, int(self.config.get("chunk_size") or 250))
        written = 0
        created = 0
        bodies = {}
        pending_llm = []

        for position, bookmark in enumerate(bookmarks, start=1):
            if not dry_run and (position - 1) % chunk_size == 0:
                remaining = None if limit is None else max(limit - fetched, 0)
                if remaining is None or remaining > 0:
                    chunk = bookmarks[position - 1: position - 1 + chunk_size]
                    fetched += self.prefetch_bodies(
                        chunk, refresh=refresh, limit=remaining, workers=workers
                    )

            identifier = bookmark.get("id")
            title = (bookmark.get("title") or "untitled")[:70]
            previous = self.notes_state.get(str(identifier), {})
            path = self.target_path(bookmark)

            cached = self.load_parsed(identifier)
            exhausted = limit is not None and fetched >= limit
            allow_fetch = (
                bool(self.config.get("fetch_article_text", True)) and not exhausted and not dry_run
            )
            need_fetch = (refresh and allow_fetch) or (cached is None and allow_fetch)

            self.log(f"[{position}/{len(bookmarks)}] {title}")
            body, metadata = self.article_for(bookmark, allow_fetch=allow_fetch)
            if need_fetch and metadata:
                fetched += 1
            if metadata.get("words"):
                bookmark["word_count"] = metadata["words"]
            if metadata.get("pubtime") and not bookmark.get("pubtime"):
                bookmark["pubtime"] = metadata["pubtime"]
            if metadata.get("author") and not bookmark.get("author"):
                bookmark["author"] = metadata["author"]
            bodies[identifier] = body

            category, method = self.categorizer.assign(
                bookmark, body, cache=self.categories,
                folder=bookmark.get("folder_title"),
                llm_cache=self.llm_categories,
            )
            if method == "fallback":
                pending_llm.append(bookmark)

            highlights = None
            if self.config.get("fetch_highlights"):
                try:
                    highlights = self.client.highlights(identifier)
                except Exception:  # noqa: BLE001
                    highlights = None

            old_path = previous.get("path")
            existing = None
            for candidate in (old_path, path):
                if candidate:
                    existing = notes.read_note(self.config.vault_path, candidate)
                    if existing:
                        break
            user_notes = notes.split_user_notes(existing) if existing else ""
            text = notes.render_note(
                bookmark, body, category, self.config.data,
                highlights=highlights, user_notes=user_notes,
                unavailable=str(identifier) in self.failures,
            )

            if dry_run:
                self.log(f"    would write {path}")
                written += 1
                continue

            if previous.get("path") and previous["path"] != path:
                self.move(previous["path"], path)
            if not existing:
                created += 1
            self.write(bookmark, path, text)
            self.notes_state[str(identifier)] = {
                "path": path,
                "title": bookmark.get("title"),
                "status": notes.status_of(bookmark),
                "saved": bookmark.get("time"),
                "synced": int(time.time()),
            }
            written += 1
            if position % 25 == 0:
                self.save_state()

        if self.config.get("categorize", {}).get("mode") == "llm" and not dry_run:
            self.refine_categories(bookmarks, bodies)


        for identifier in deleted:
            entry = self.notes_state.pop(str(identifier), None)
            if not entry:
                continue
            if dry_run:
                self.log(f"    would remove {entry.get('path')}")
                continue
            self.discard(entry.get("path"))

        if not dry_run:
            self.write_index()
            self.save_state()

        self.log(
            f"Done: {written} notes written ({created} new, {fetched} articles downloaded), "
            f"{len(deleted)} removed"
        )
        if self.failures and not dry_run:
            self.log(
                f"{len(self.failures)} bookmarks have no article text yet; they are "
                f"retried on a backoff (force it with --retry-failures)"
            )
        return 0

    # ------------------------------------------------------------------- index

    def write_index(self):
        cutoff = int(time.time()) - 60 * 60 * 24 * 90
        by_category = {}
        counts = {"total": 0, "to_read": 0, "archived": 0, "reading": 0, "by_category": by_category}
        recent = []

        for identifier, entry in self.notes_state.items():
            status = entry.get("status") or "to-read"
            category = (self.categories.get(identifier) or {}).get("category") or self.categorizer.fallback
            counts["total"] += 1
            if status == "archived":
                counts["archived"] += 1
            else:
                counts["to_read"] += 1
                if status == "reading":
                    counts["reading"] += 1
            bucket = by_category.setdefault(category, {"to_read": 0, "archived": 0})
            bucket["archived" if status == "archived" else "to_read"] += 1
            if int(entry.get("synced") or 0) >= cutoff:
                recent.append({
                    "title": entry.get("title"),
                    "path": entry.get("path"),
                    "status": status,
                    "saved": notes.iso_date(entry.get("saved") or entry.get("synced")),
                })

        recent.sort(key=lambda item: item.get("saved") or "", reverse=True)
        categories = sorted(by_category, key=lambda name: -(by_category[name]["to_read"] + by_category[name]["archived"]))
        text = notes.render_index(counts, recent[:30], self.config.data, categories, notes.now_iso())

        name = self.config.get("index_note") or "Instapaper Index"
        path = os.path.join(self.config.get("notes_dir").strip("/"), name + ".md")
        notes.write_note(self.config.vault_path, path, text)
        self.log(f"Index updated: {path}")
