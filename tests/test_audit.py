import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import audit, config as config_module, notes  # noqa: E402
from instapaper_sync.sync import Sync  # noqa: E402

CATEGORIES = {
    "fallback": "Unsorted",
    "categories": [{"name": "Making & Craft", "keywords": ["3d printer"]}],
}

RICH_BODY = (
    "<h2>A subhead</h2>"
    "<p>First paragraph with a <strong>bold</strong> word and an "
    '<a href="https://example.com/source">outbound link</a>.</p>'
    "<ul><li>one item</li><li>another item</li></ul>"
    "<pre><code>print('hello')\n</code></pre>"
    "<blockquote><p>Quoted wisdom.</p></blockquote>"
)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def bookmark(identifier, title, archived=False, progress=0.0):
    return {
        "id": identifier,
        "title": title,
        "url": f"https://example.com/{identifier}",
        "author": "Author",
        "description": "A description that is long enough to be rendered in the note.",
        "time": 1700000000,
        "pubtime": 1690000000,
        "archived": archived,
        "liked": False,
        "progress": {"percentage": progress, "timestamp": 0},
        "tags": [],
        "folder_id": None,
    }


def word_count(html):
    return len(re.sub(r"<[^>]+>", " ", html).split())


def parse_payload(title, body, words=None):
    if words is None:
        words = word_count(body)
    return {
        "metadata": {"title": title, "author": {"name": "Author"}, "pubtime": 1690000000},
        "content": {"body": body, "words": words, "paywalled": False},
    }


class FakeClient:
    def __init__(self, bookmarks, parsed=None):
        self.bookmarks = bookmarks
        self.parsed = parsed or {}
        self.parse_calls = []

    def me(self):
        return {"id": 1, "username": "reader@example.com", "premium": True}

    def folders(self):
        return []

    def iter_bookmarks(self, section=None, folder_id=None, tag=None, page_size=500):
        for item in self.bookmarks:
            if section == "archive" and not item.get("archived"):
                continue
            if section in (None, "home") and item.get("archived"):
                continue
            yield item

    def parse(self, bookmark_id, use_cache=True):
        self.parse_calls.append(bookmark_id)
        if bookmark_id not in self.parsed:
            raise RuntimeError("Parsed content unavailable")
        return self.parsed[bookmark_id]

    def highlights(self, bookmark_id):
        return []


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(self.vault)
        categories = os.path.join(self.tmp, "categories.json")
        write(categories, json.dumps(CATEGORIES))
        self.config_path = os.path.join(self.tmp, "config.json")
        write(
            self.config_path,
            json.dumps(
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
                }
            ),
        )
        self.config = config_module.load(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def note_path(self, *parts):
        return os.path.join(self.vault, "Instapaper", *parts)

    def synced_files(self):
        return audit.scan(self.vault, "Instapaper", index_note="Instapaper Index")

    def severity(self, findings, level):
        return [message for severity, message in findings if severity == level]

    # ------------------------------------------------------- the markdown on disk

    def test_rendered_markdown_lands_in_the_file(self):
        client = FakeClient(
            [bookmark(1, "Rich piece")], parsed={1: parse_payload("Rich piece", RICH_BODY)}
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        text = read(self.note_path("To Read", "Rich piece.md"))

        self.assertIn("## Article", text)
        self.assertIn("## A subhead", text)
        self.assertIn("**bold**", text)
        self.assertIn("[outbound link](https://example.com/source)", text)
        self.assertIn("- one item", text)
        self.assertIn("- another item", text)
        self.assertIn("print('hello')", text)
        self.assertIn("> Quoted wisdom.", text)
        self.assertIn("instapaper_id: 1", text)
        self.assertIn('  - "instapaper"', text)

    def test_audit_accepts_every_note_the_sync_writes(self):
        client = FakeClient(
            [
                bookmark(1, "Has text"),
                bookmark(2, "No text"),
                bookmark(3, "Finished", progress=1.0),
                bookmark(4, "Old one", archived=True),
            ],
            parsed={1: parse_payload("Has text", RICH_BODY)},
        )
        Sync(self.config, client, logger=lambda *_: None).run()

        files, findings = self.synced_files()
        self.assertEqual(len(files), 4)
        self.assertEqual(self.severity(findings, "error"), [], findings)

        info = self.severity(findings, "info")
        self.assertEqual(len(info), 3)
        self.assertTrue(
            all("not downloaded" in message or "unavailable" in message for message in info)
        )

        summary, with_body = audit.summarise(files, findings)
        self.assertEqual(with_body, 1)
        self.assertIn("files          4", summary)

    def test_audit_flags_a_file_whose_body_disappeared(self):
        client = FakeClient(
            [bookmark(1, "Has text")], parsed={1: parse_payload("Has text", RICH_BODY)}
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        path = self.note_path("To Read", "Has text.md")

        mangled = read(path).replace("## Article\n\n", "")
        write(path, mangled)
        _files, findings = self.synced_files()
        messages = self.severity(findings, "error")
        self.assertTrue(any("no '## Article' section" in message for message in messages))

    def test_audit_flags_a_truncated_file(self):
        client = FakeClient(
            [bookmark(1, "Long one")],
            parsed={1: parse_payload("Long one", "<p>" + "word " * 2000 + "</p>", 2000)},
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        path = self.note_path("To Read", "Long one.md")

        text = read(path)
        write(path, text[: text.index("## Article") + 60])
        _files, findings = self.synced_files()
        messages = self.severity(findings, "error")
        self.assertTrue(
            any("frontmatter says" in message or "lost the user-notes marker" in message
                for message in messages),
            messages,
        )

    def test_audit_flags_placeholder_with_a_word_count(self):
        client = FakeClient([bookmark(1, "Half done")])
        Sync(self.config, client, logger=lambda *_: None).run()
        path = self.note_path("To Read", "Half done.md")

        text = read(path)
        self.assertIn("words: null", text, "an undownloaded note claims no word count")
        write(path, text.replace("words: null", "words: 1200"))
        _files, findings = self.synced_files()
        messages = self.severity(findings, "error")
        self.assertTrue(
            any("placeholder body but frontmatter claims" in message for message in messages),
            messages,
        )

    def test_audit_flags_status_that_contradicts_the_archived_flag(self):
        client = FakeClient([bookmark(1, "Moved")], parsed={1: parse_payload("Moved", RICH_BODY)})
        Sync(self.config, client, logger=lambda *_: None).run()
        path = self.note_path("To Read", "Moved.md")

        write(path, read(path).replace('status: "to-read"', 'status: "archived"'))
        _files, findings = self.synced_files()
        messages = self.severity(findings, "error")
        self.assertTrue(
            any("status is 'archived' but archived is false" in message for message in messages),
            messages,
        )

    def test_audit_skips_the_index_note(self):
        client = FakeClient(
            [bookmark(1, "Only one")], parsed={1: parse_payload("Only one", RICH_BODY)}
        )
        Sync(self.config, client, logger=lambda *_: None).run()
        self.assertTrue(os.path.exists(self.note_path("Instapaper Index.md")))

        files, findings = self.synced_files()
        self.assertFalse(any("Index" in path for path in files))
        self.assertEqual(self.severity(findings, "error"), [])

    def test_audit_reports_an_unreadable_file(self):
        os.makedirs(self.note_path("To Read"))
        write(self.note_path("To Read", "Broken.md"), "---\nnot: closed\n")
        _files, findings = self.synced_files()
        messages = self.severity(findings, "error")
        self.assertTrue(any("no parsable frontmatter block" in message for message in messages))


class FrontmatterTests(unittest.TestCase):
    def test_parse_frontmatter_handles_the_shapes_the_sync_emits(self):
        text = (
            "---\n"
            "instapaper_id: 123\n"
            'title: "A: quoted, title"\n'
            "reading_progress: 0.4200\n"
            "read: false\n"
            "author: null\n"
            "instapaper_tags:\n"
            '  - "ai"\n'
            "tags:\n"
            '  - "instapaper"\n'
            '  - "ai-machine-learning"\n'
            "---\n\n# Body\n"
        )
        data, ok = audit.parse_frontmatter(text)
        self.assertTrue(ok)
        self.assertEqual(data["instapaper_id"], 123)
        self.assertEqual(data["title"], "A: quoted, title")
        self.assertAlmostEqual(data["reading_progress"], 0.42)
        self.assertIs(data["read"], False)
        self.assertIsNone(data["author"])
        self.assertEqual(
            audit.frontmatter_tags(text), ["instapaper", "ai-machine-learning"]
        )

    def test_parse_frontmatter_rejects_a_missing_block(self):
        data, ok = audit.parse_frontmatter("# no frontmatter here\n")
        self.assertFalse(ok)
        self.assertEqual(data, {})


class ReportTests(unittest.TestCase):
    def test_report_groups_findings_and_limits_samples(self):
        findings = [("error", f"note{i}.md: has no '## Article' section") for i in range(5)]
        findings.append(("info", "a.md: article text not downloaded yet"))
        text = audit.report(findings, sample=2)
        self.assertIn("ERROR has no '## Article' section (5)", text)
        self.assertIn("note0.md", text)
        self.assertIn("... and 3 more", text)
        self.assertNotIn("a.md", text, "info findings are hidden unless verbose")
        self.assertIn("a.md", audit.report(findings, verbose=True))


class CompletenessTests(unittest.TestCase):
    """The stored text is checked against the page Instapaper parsed."""

    def inspect(self, body, payload):
        text = notes.render_note(
            {
                "id": 7,
                "title": "Sample",
                "url": "https://example.com/a",
                "time": 1700000000,
            },
            body,
            "Unsorted",
            {"categorize": {"fallback": "Unsorted"}, "frontmatter": {}},
        )
        return audit.inspect("Sample.md", text, payload=payload)

    def test_paywalled_parse_is_reported_as_partial(self):
        payload = {
            "content": {
                "body": "<p>" + "word " * 500 + "</p>",
                "words": 500,
                "paywalled": True,
            }
        }
        findings = self.inspect("word " * 500, payload)
        self.assertTrue(
            any("paywall" in message for _severity, message in findings), findings
        )

    def test_a_body_much_shorter_than_the_parsed_page_is_an_error(self):
        page = " ".join(f"word{i}" for i in range(1000))
        findings = self.inspect(
            "only a little text is here", {"content": {"body": f"<p>{page}</p>"}}
        )
        self.assertTrue(
            any(severity == "error" and "incomplete" in message for severity, message in findings),
            findings,
        )

    def test_a_subscription_prompt_at_the_end_is_a_warning(self):
        body = "An opening paragraph.\n\nSubscribe to continue reading."
        page = "<p>" + "word " * 40 + "</p><p>Subscribe to continue reading.</p>"
        findings = self.inspect(body, {"content": {"body": page}})
        self.assertTrue(
            any("subscription prompt" in message for _severity, message in findings),
            findings,
        )

    def test_an_embed_only_page_is_not_called_incomplete(self):
        payload = {"content": {"body": '<div><iframe src="https://youtube.com/embed/x"></iframe></div>', "words": 4862}}
        findings = self.inspect("[iframe](https://youtube.com/embed/x)", payload)
        self.assertFalse([f for f in findings if "incomplete" in f[1]], findings)


if __name__ == "__main__":
    unittest.main()
