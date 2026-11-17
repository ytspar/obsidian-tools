import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import notes  # noqa: E402

CONFIG = {
    "notes_dir": "Instapaper",
    "layout": "folders",
    "to_read_dir": "To Read",
    "archive_dir": "Archived",
    "filename": {"template": "{title}", "max_length": 60},
    "frontmatter": {"include_description": True},
}


def bookmark(**overrides):
    base = {
        "id": 42,
        "title": "A Title",
        "url": "https://example.com/post",
        "author": "Jane Doe",
        "description": "A short description of the piece that is long enough to show.",
        "time": 1700000000,
        "pubtime": 1690000000,
        "archived": False,
        "liked": False,
        "progress": {"percentage": 0.0, "timestamp": 0},
        "tags": [{"name": "Deep Work"}],
    }
    base.update(overrides)
    return base


class StatusTests(unittest.TestCase):
    def test_unread(self):
        self.assertEqual(notes.status_of(bookmark()), "to-read")
        self.assertFalse(notes.is_read(bookmark()))

    def test_archived(self):
        self.assertEqual(notes.status_of(bookmark(archived=True)), "archived")
        self.assertTrue(notes.is_read(bookmark(archived=True)))

    def test_in_progress(self):
        item = bookmark(progress={"percentage": 0.4, "timestamp": 1})
        self.assertEqual(notes.status_of(item), "reading")
        self.assertFalse(notes.is_read(item))

    def test_finished_but_not_archived(self):
        item = bookmark(progress={"percentage": 0.98, "timestamp": 1})
        self.assertEqual(notes.status_of(item), "read")
        self.assertTrue(notes.is_read(item))


class FilenameTests(unittest.TestCase):
    def test_illegal_characters_are_removed(self):
        self.assertEqual(notes.safe_filename('a/b:c*d?e"f<g>h|i'), "a b c d e f g h i")

    def test_length_is_capped_on_a_word_boundary(self):
        name = notes.safe_filename("word " * 40, max_length=20)
        self.assertLessEqual(len(name), 20)
        self.assertFalse(name.endswith(" "))

    def test_empty_title_falls_back(self):
        self.assertEqual(notes.safe_filename("", fallback="Instapaper 7"), "Instapaper 7")

    def test_path_uses_status_folder(self):
        path = notes.relative_note_path(bookmark(), CONFIG)
        self.assertEqual(path, os.path.join("Instapaper", "To Read", "A Title.md"))
        archived = notes.relative_note_path(bookmark(archived=True), CONFIG)
        self.assertEqual(archived, os.path.join("Instapaper", "Archived", "A Title.md"))

    def test_flat_layout(self):
        flat = dict(CONFIG, layout="flat")
        self.assertEqual(
            notes.relative_note_path(bookmark(archived=True), flat),
            os.path.join("Instapaper", "A Title.md"),
        )


class RenderTests(unittest.TestCase):
    def test_frontmatter_and_body(self):
        text = notes.render_note(bookmark(), "Body text.", "Product & Design", CONFIG)
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('instapaper_id: 42', text)
        self.assertIn('title: "A Title"', text)
        self.assertIn('status: "to-read"', text)
        self.assertIn("read: false", text)
        self.assertIn('category: "Product & Design"', text)
        self.assertIn('  - "product-design"', text)
        self.assertIn('  - "deep-work"', text)
        self.assertIn("# A Title", text)
        self.assertIn("## Article\n\nBody text.", text)
        self.assertIn(notes.USER_NOTES_MARKER, text)

    def test_missing_body_gets_a_warning(self):
        text = notes.render_note(bookmark(), "", "Unsorted", CONFIG)
        self.assertIn("Article text not downloaded", text)

    def test_user_notes_are_preserved_on_resync(self):
        first = notes.render_note(bookmark(), "Body.", "Unsorted", CONFIG)
        edited = first.replace("## Notes", "## Notes\n\nmy own thought")
        carried = notes.split_user_notes(edited)
        self.assertEqual(carried, "my own thought")
        second = notes.render_note(bookmark(), "Body v2.", "Unsorted", CONFIG, user_notes=carried)
        self.assertIn("my own thought", second)
        self.assertIn("Body v2.", second)
        self.assertEqual(second.count("my own thought"), 1)

    def test_frontmatter_id_roundtrip(self):
        text = notes.render_note(bookmark(), "Body.", "Unsorted", CONFIG)
        self.assertEqual(notes.read_frontmatter_id(text), 42)

    def test_null_fields_are_rendered(self):
        text = notes.render_note(
            bookmark(url=None, author=None, pubtime=None, tags=[]), "Body.", "Unsorted", CONFIG
        )
        self.assertIn("author: null", text)
        self.assertIn("published: null", text)

    def test_index_lists_search_queries(self):
        counts = {"total": 3, "to_read": 2, "archived": 1, "reading": 1,
                  "by_category": {"AI & Machine Learning": {"to_read": 2, "archived": 1}}}
        text = notes.render_index(counts, [], CONFIG, ["AI & Machine Learning"], "2024-01-01T00:00:00Z")
        self.assertIn('path:"Instapaper/To Read"', text)
        self.assertIn("AI & Machine Learning", text)
        self.assertIn("Synced **3** articles", text)


if __name__ == "__main__":
    unittest.main()
