import json
import os
import time
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import config as config_module  # noqa: E402
from instapaper_sync import notes  # noqa: E402
from instapaper_sync.api import ApiError
from instapaper_sync.sync import Sync  # noqa: E402

CATEGORIES = {
    "fallback": "Unsorted",
    "categories": [
        {"name": "AI & Machine Learning", "keywords": ["llm", "gpt"], "tags": ["ai"]},
        {"name": "Making & Craft", "keywords": ["3d printer"], "tags": []},
    ],
}


def slurp(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FakeClient:
    def __init__(self, bookmarks, folders=None, parsed=None):
        self.bookmarks = bookmarks
        self._folders = folders or []
        self.parsed = parsed or {}
        self.parse_calls = []

    def me(self):
        return {"id": 1, "username": "reader@example.com", "premium": True}

    def folders(self):
        return self._folders

    def iter_bookmarks(self, section=None, folder_id=None, tag=None, page_size=500):
        for bookmark in self.bookmarks:
            if section == "archive" and not bookmark.get("archived"):
                continue
            if section in (None, "home") and bookmark.get("archived"):
                continue
            if folder_id is not None and bookmark.get("folder_id") != folder_id:
                continue
            yield bookmark

    def parse(self, bookmark_id, use_cache=True):
        self.parse_calls.append(bookmark_id)
        if bookmark_id not in self.parsed:
            raise ApiError(400, "Parsed content unavailable")
        return self.parsed[bookmark_id]

    def highlights(self, bookmark_id):
        return []


def make_parse(title, body, words=120):
    return {
        "metadata": {"title": title, "author": {"name": "Author"}, "pubtime": 1690000000},
        "content": {"body": body, "words": words, "paywalled": False},
    }


def bookmark(identifier, title, archived=False, **extra):
    item = {
        "id": identifier,
        "title": title,
        "url": f"https://example.com/{identifier}",
        "author": "Author",
        "description": "A description that is long enough to be rendered in the note.",
        "time": 1700000000,
        "pubtime": 1690000000,
        "archived": archived,
        "liked": False,
        "progress": {"percentage": 0.0, "timestamp": 0},
        "tags": [],
        "folder_id": None,
    }
    item.update(extra)
    return item


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        categories = os.path.join(self.tmp, "categories.json")
        with open(categories, "w", encoding="utf-8") as handle:
            json.dump(CATEGORIES, handle)
        self.config_path = os.path.join(self.tmp, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vault_path": self.vault,
                    "notes_dir": "Instapaper",
                    "layout": "folders",
                    "sections": ["home", "archive"],
                    "state_dir": os.path.join(self.tmp, "state"),
                    "cache_dir": os.path.join(self.tmp, "cache"),
                    "categorize": {
                        "mode": "rules",
                        "categories_file": categories,
                        "fallback": "Unsorted",
                    },
                    "request_delay": 0,
                },
                handle,
            )
        self.config = config_module.load(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note_path(self, *parts):
        return os.path.join(self.vault, "Instapaper", *parts)

    def test_first_sync_writes_notes_and_index(self):
        client = FakeClient(
            [bookmark(1, "Unread LLM piece"), bookmark(2, "Archived thing", archived=True)],
            parsed={1: make_parse("Unread LLM piece", "<p>Body one</p>")},
        )
        Sync(self.config, client, logger=lambda *_: None).run()

        unread = self.note_path("To Read", "Unread LLM piece.md")
        archived = self.note_path("Archived", "Archived thing.md")
        self.assertTrue(os.path.exists(unread))
        self.assertTrue(os.path.exists(archived))
        self.assertTrue(os.path.exists(self.note_path("Instapaper Index.md")))

        text = slurp(unread)
        self.assertIn('status: "to-read"', text)
        self.assertIn("read: false", text)
        self.assertIn('category: "AI & Machine Learning"', text)
        self.assertIn("Body one", text)

        archived_text = slurp(archived)
        self.assertIn('status: "archived"', archived_text)
        self.assertIn("read: true", archived_text)
        self.assertIn("Article text unavailable", archived_text)

    def test_second_run_uses_the_cache_and_moves_archived_notes(self):
        client = FakeClient(
            [bookmark(1, "A piece")],
            parsed={1: make_parse("A piece", "<p>Body</p>")},
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertEqual(client.parse_calls, [1])

        client.bookmarks = [bookmark(1, "A piece", archived=True)]
        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertEqual(client.parse_calls, [1], "cached article must not be re-downloaded")
        self.assertFalse(os.path.exists(self.note_path("To Read", "A piece.md")))
        moved = self.note_path("Archived", "A piece.md")
        self.assertTrue(os.path.exists(moved))
        self.assertIn('status: "archived"', slurp(moved))

    def test_user_notes_survive_resync(self):
        client = FakeClient([bookmark(1, "A piece")], parsed={1: make_parse("A piece", "<p>Body</p>")})
        Sync(self.config, client, logger=lambda *_: None).run()

        path = self.note_path("To Read", "A piece.md")
        text = slurp(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text + "\nmy own thought\n")

        Sync(self.config, client, logger=lambda *_: None).run()
        refreshed = slurp(path)
        self.assertIn("my own thought", refreshed)
        self.assertEqual(refreshed.count("my own thought"), 1)

    def test_user_notes_survive_a_folder_move(self):
        client = FakeClient([bookmark(1, "A piece")], parsed={1: make_parse("A piece", "<p>Body</p>")})
        Sync(self.config, client, logger=lambda *_: None).run()

        source = self.note_path("To Read", "A piece.md")
        text = slurp(source)
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(text + "\nkept across the move\n")

        client.bookmarks = [bookmark(1, "A piece", archived=True)]
        Sync(self.config, client, logger=lambda *_: None).run()
        moved = slurp(self.note_path("Archived", "A piece.md"))
        self.assertIn("kept across the move", moved)
        self.assertEqual(moved.count("kept across the move"), 1)

    def test_parallel_prefetch_downloads_every_missing_body(self):
        self.config.data["workers"] = 4
        client = FakeClient(
            [bookmark(i, f"Piece {i}") for i in (1, 2, 3, 4, 5)],
            parsed={i: make_parse(f"Piece {i}", f"<p>Body {i}</p>") for i in (1, 2, 3, 4, 5)},
        )
        synced = Sync(self.config, client, logger=lambda *_: None)
        self.assertEqual(synced.prefetch_bodies(client.bookmarks, workers=4), 5)
        self.assertEqual(sorted(client.parse_calls), [1, 2, 3, 4, 5])
        for i in (1, 2, 3, 4, 5):
            self.assertIsNotNone(synced.load_parsed(i))

        # a second pass finds everything cached and makes no requests
        client.parse_calls.clear()
        self.assertEqual(synced.prefetch_bodies(client.bookmarks, workers=4), 0)
        self.assertEqual(client.parse_calls, [])

    def test_prefetch_records_failures_and_skips_them_afterwards(self):
        client = FakeClient([bookmark(1, "Paywalled"), bookmark(2, "Fine")],
                            parsed={2: make_parse("Fine", "<p>Body</p>")})
        synced = Sync(self.config, client, logger=lambda *_: None)
        self.assertEqual(synced.prefetch_bodies(client.bookmarks, workers=1), 1)
        self.assertEqual(synced.failed_ids, {1})
        self.assertEqual(synced.article_for(client.bookmarks[0]), ("", {}))

    def test_chunked_run_writes_notes_before_the_downloads_finish(self):
        self.config.data["chunk_size"] = 2
        ids = [1, 2, 3, 4, 5]
        client = FakeClient(
            [bookmark(i, f"Piece {i}") for i in ids],
            parsed={i: make_parse(f"Piece {i}", f"<p>Body {i}</p>") for i in ids},
        )
        original = client.parse
        seen = []

        def parse(bookmark_id, use_cache=True):
            seen.append(bookmark_id)
            if len(seen) > 4:
                raise KeyboardInterrupt
            return original(bookmark_id, use_cache=use_cache)

        client.parse = parse
        with self.assertRaises(KeyboardInterrupt):
            Sync(self.config, client, logger=lambda *_: None).run()

        # the first two chunks were written to the vault before the interruption
        for i in (1, 2, 3, 4):
            self.assertTrue(
                os.path.exists(self.note_path("To Read", f"Piece {i}.md")), f"note {i}"
            )
            self.assertIn("Body %d" % i, slurp(self.note_path("To Read", f"Piece {i}.md")))
        self.assertFalse(os.path.exists(self.note_path("To Read", "Piece 5.md")))

    def test_unparseable_article_is_remembered_and_not_refetched(self):
        self.config.data["chunk_size"] = 1
        client = FakeClient([bookmark(1, "Dead link")], parsed={})
        synced = Sync(self.config, client, logger=lambda *_: None)
        synced.run()
        self.assertEqual(client.parse_calls, [1])
        self.assertIn("1", synced.failures)
        self.assertIn("Article text unavailable", slurp(self.note_path("To Read", "Dead link.md")))

        again = Sync(self.config, client, logger=lambda *_: None)
        again.run()
        self.assertEqual(client.parse_calls, [1], "the second run should not re-ask")

        retried = Sync(self.config, client, logger=lambda *_: None)
        retried.run(retry_failures=True)
        self.assertEqual(client.parse_calls, [1, 1], "--retry-failures asks again")

    def test_a_parked_article_is_retried_after_its_cooldown(self):
        self.config.data["chunk_size"] = 1
        client = FakeClient([bookmark(1, "Parked")])
        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertEqual(client.parse_calls, [1])
        self.assertEqual(Sync(self.config, client, logger=lambda *_: None).run() or client.parse_calls,
                         [1], "a fresh failure is left alone")

        # Age the record past the first cooldown and it gets another chance.
        statefile = os.path.join(self.config.data["state_dir"], "state.json")
        with open(statefile) as handle:
            state = json.load(handle)
        state["failures"]["1"]["at"] = int(time.time()) - 7200
        with open(statefile, "w") as handle:
            json.dump(state, handle)

        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertEqual(client.parse_calls, [1, 1], "due for a retry")
        with open(statefile) as handle:
            self.assertEqual(json.load(handle)["failures"]["1"]["attempts"], 2)

    def test_notes_awaiting_download_read_differently_from_dead_articles(self):
        self.config.data["chunk_size"] = 1
        client = FakeClient(
            [bookmark(1, "Pending"), bookmark(2, "Dead")],
            parsed={1: make_parse("Pending", "<p>Body</p>")},
        )
        Sync(self.config, client, logger=lambda *_: None).run(limit=1)
        waiting = slurp(self.note_path("To Read", "Dead.md"))
        self.assertIn("Article text not downloaded", waiting)
        self.assertNotIn("Article text unavailable", waiting)

        # The next run does try it, Instapaper refuses, and the note says so.
        Sync(self.config, client, logger=lambda *_: None).run()
        dead = slurp(self.note_path("To Read", "Dead.md"))
        self.assertIn("Article text unavailable", dead)
        self.assertNotIn("Article text not downloaded", dead)
        self.assertIn("Body", slurp(self.note_path("To Read", "Pending.md")))

    def test_frontmatter_word_count_describes_the_stored_body(self):
        client = FakeClient(
            [bookmark(1, "Long read")],
            parsed={1: make_parse("Long read", "<p>" + "word " * 400 + "</p>", words=9000)},
        )
        Sync(self.config, client, logger=lambda _: None).run()
        text = slurp(self.note_path("To Read", "Long read.md"))
        self.assertIn("words: 400", text, "Instapaper's estimate must not leak into the note")

    def test_a_later_success_forgets_the_recorded_failure(self):
        self.config.data["chunk_size"] = 1
        client = FakeClient([bookmark(1, "Flaky")], parsed={})
        Sync(self.config, client, logger=lambda *_: None).run()

        good_client = FakeClient([bookmark(1, "Flaky")], parsed={1: make_parse("Flaky", "<p>Body</p>")})
        synced = Sync(self.config, good_client, logger=lambda *_: None)
        synced.run(retry_failures=True)
        self.assertEqual(synced.failures, {})
        self.assertIn("Body", slurp(self.note_path("To Read", "Flaky.md")))

    def test_dry_run_writes_nothing(self):
        client = FakeClient([bookmark(1, "A piece")], parsed={1: make_parse("A piece", "<p>Body</p>")})
        Sync(self.config, client, logger=lambda *_: None).run(dry_run=True)
        self.assertFalse(os.path.exists(self.note_path("To Read")))
        self.assertEqual(client.parse_calls, [], "dry run should not touch the API")

    def test_limit_skips_downloads_but_still_writes_notes(self):
        client = FakeClient(
            [bookmark(1, "One"), bookmark(2, "Two")],
            parsed={1: make_parse("One", "<p>Body one</p>"), 2: make_parse("Two", "<p>Body two</p>")},
        )
        Sync(self.config, client, logger=lambda *_: None).run(limit=1)
        self.assertEqual(client.parse_calls, [1])
        first = slurp(self.note_path("To Read", "One.md"))
        second = slurp(self.note_path("To Read", "Two.md"))
        self.assertIn("Body one", first)
        self.assertIn("Article text not downloaded", second)

    def test_title_collision_between_two_bookmarks(self):
        client = FakeClient(
            [bookmark(1, "Same title"), bookmark(2, "Same title")],
            parsed={
                1: make_parse("Same title", "<p>One</p>"),
                2: make_parse("Same title", "<p>Two</p>"),
            },
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertTrue(os.path.exists(self.note_path("To Read", "Same title.md")))
        self.assertTrue(os.path.exists(self.note_path("To Read", "Same title (2).md")))

    def test_refresh_refetches_article_text(self):
        client = FakeClient([bookmark(1, "A piece")], parsed={1: make_parse("A piece", "<p>Body</p>")})
        Sync(self.config, client, logger=lambda *_: None).run()
        Sync(self.config, client, logger=lambda *_: None).run(refresh=True)
        self.assertEqual(client.parse_calls, [1, 1])

    def test_failed_parse_does_not_abort_the_run(self):
        client = FakeClient([bookmark(1, "Broken"), bookmark(2, "Good")],
                            parsed={2: make_parse("Good", "<p>Fine</p>")})
        Sync(self.config, client, logger=lambda *_: None).run()
        broken = slurp(self.note_path("To Read", "Broken.md"))
        self.assertIn("Article text unavailable", broken)
        self.assertTrue(os.path.exists(self.note_path("To Read", "Good.md")))

    def test_illegal_title_characters_produce_a_valid_filename(self):
        client = FakeClient([bookmark(1, 'Why/What: "This"?')])
        Sync(self.config, client, logger=lambda *_: None).run()
        names = os.listdir(self.note_path("To Read"))
        self.assertEqual(names, ["Why What This.md"])

    def test_tags_and_folder_come_from_instapaper(self):
        client = FakeClient(
            [bookmark(1, "Tagged", tags=[{"name": "Deep Work"}], folder_id=9)],
            folders=[{"id": 9, "title": "Reading list", "count": 1}],
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        text = slurp(self.note_path("To Read", "Tagged.md"))
        self.assertIn('folder: "Reading list"', text)
        self.assertIn('  - "Deep Work"', text)
        self.assertIn('  - "deep-work"', text)


if __name__ == "__main__":
    unittest.main()
