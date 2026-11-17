import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync.html2md import html_to_markdown  # noqa: E402


class HtmlToMarkdownTests(unittest.TestCase):
    def test_paragraphs_and_headings(self):
        html = "<h1>Title</h1><p>First <strong>bold</strong> and <em>italic</em>.</p><p>Second.</p>"
        self.assertEqual(
            html_to_markdown(html),
            "# Title\n\nFirst **bold** and *italic*.\n\nSecond.",
        )

    def test_links_and_images(self):
        html = '<p><a href="https://a.test/x">label</a> <img src="i.png" alt="pic"></p>'
        self.assertEqual(html_to_markdown(html), "[label](https://a.test/x) ![pic](i.png)")

    def test_list_nesting(self):
        html = "<ul><li>one</li><li>two<ul><li>two-a</li></ul></li></ul>"
        self.assertEqual(html_to_markdown(html), "- one\n- two\n  - two-a")

    def test_ordered_list(self):
        html = "<ol><li>alpha</li><li>beta</li></ol>"
        self.assertEqual(html_to_markdown(html), "1. alpha\n2. beta")

    def test_pre_block_preserves_text(self):
        html = '<pre><code class="language-python">def f():\n    return 1\n</code></pre>'
        self.assertEqual(html_to_markdown(html), "```python\ndef f():\n    return 1\n```")

    def test_inline_code_with_backtick(self):
        html = "<p>use <code>a`b</code> here</p>"
        self.assertEqual(html_to_markdown(html), "use ``a`b`` here")

    def test_blockquote(self):
        html = "<blockquote><p>quoted words</p></blockquote>"
        self.assertEqual(html_to_markdown(html), "> quoted words")

    def test_table(self):
        html = (
            "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        )
        self.assertEqual(html_to_markdown(html), "| A | B |\n| --- | --- |\n| 1 | 2 |")

    def test_scripts_and_styles_are_dropped(self):
        html = "<style>p{color:red}</style><p>keep</p><script>alert(1)</script>"
        self.assertEqual(html_to_markdown(html), "keep")

    def test_escaping_of_markdown_characters(self):
        html = "<p>5 * 3 = 15 and [brackets]</p>"
        self.assertEqual(html_to_markdown(html), "5 \\* 3 = 15 and \\[brackets\\]")

    def test_hard_break(self):
        html = "<p>line one<br>line two</p>"
        self.assertEqual(html_to_markdown(html), "line one  \nline two")

    def test_empty_input(self):
        self.assertEqual(html_to_markdown(""), "")
        self.assertEqual(html_to_markdown("   "), "")

    def test_paragraph_between_list_items_keeps_its_text(self):
        html = (
            "<ol><li>First</li>"
            "<p>Prose that a browser shows inside the list.</p>"
            "<li>Second</li></ol>"
        )
        markdown = html_to_markdown(html)
        self.assertIn("1. First", markdown)
        self.assertIn("2. Second", markdown)
        self.assertIn("Prose that a browser shows inside the list.", markdown)

    def test_consent_script_dumped_as_text_is_dropped(self):
        junk = "(() => { var x = window.y; function f() { return; } })" * 40
        html = f"<div>real prose here</div><div>{junk}</div>"
        self.assertEqual(html_to_markdown(html), "real prose here")

    def test_code_inside_pre_is_kept(self):
        junk = "(() => { var x = window.y; function f() { return; } })" * 40
        self.assertIn("() =>", html_to_markdown(f"<pre><code>{junk}</code></pre>"))

    def test_unclosed_tags_do_not_crash(self):
        self.assertEqual(html_to_markdown("<p>text<div>more"), "text\n\nmore")

    def test_nested_div_paragraph_collapse(self):
        html = "<div><p>a</p><p>b</p></div>"
        self.assertEqual(html_to_markdown(html), "a\n\nb")


if __name__ == "__main__":
    unittest.main()
