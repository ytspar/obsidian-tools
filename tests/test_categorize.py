import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import categorize  # noqa: E402

SPEC = {
    "fallback": "Unsorted",
    "categories": [
        {
            "name": "Software & Engineering",
            "keywords": ["software", "engineering", "api", "open source"],
            "tags": ["dev"],
            "domains": ["github.com"],
        },
        {
            "name": "Media & Writing",
            "keywords": ["magazine", "essay", "newsletter"],
            "tags": ["writing"],
            "domains": ["newyorker.com"],
        },
        {
            "name": "History & Culture",
            "keywords": ["history", "victorian"],
            "body_keywords": ["empire"],
        },
    ],
}


def bookmark(title="", url="https://example.com/x", description="", tags=(), folder_title=None):
    return {
        "id": 1,
        "title": title,
        "url": url,
        "description": description,
        "tags": [{"name": name} for name in tags],
        "folder_title": folder_title,
    }


def classify(item, body="", folder=None):
    categorizer = categorize.Categorizer(SPEC)
    return categorizer.assign(item, body, folder=folder)


class ClassificationTests(unittest.TestCase):
    def test_title_keyword_matches(self):
        self.assertEqual(classify(bookmark("Why software is eating the world"))[0], "Software & Engineering")

    def test_word_boundaries_prevent_substring_matches(self):
        # "api" must not match inside "capital"
        self.assertEqual(classify(bookmark("Capital allocation in practice"))[0], "Unsorted")

    def test_multi_word_keyword_matches_as_substring(self):
        self.assertEqual(classify(bookmark("The open source funding problem"))[0], "Software & Engineering")

    def test_tags_outweigh_title_keywords(self):
        item = bookmark("A magazine essay about newsletters", tags=["dev"])
        self.assertEqual(classify(item)[0], "Software & Engineering")

    def test_domain_matches(self):
        item = bookmark("Some repository", url="https://github.com/example-org/example-repo")
        self.assertEqual(classify(item)[0], "Software & Engineering")

    def test_folder_matches_when_title_slug_is_a_tag(self):
        item = bookmark("Quarterly notes", folder_title="Writing")
        self.assertEqual(classify(item)[0], "Media & Writing")

    def test_description_hit_loses_to_title_hit(self):
        # the regression that put "Why were Victorian elites so effective?" in Software
        item = bookmark(
            "Why were Victorian elites so effective? - Works in Progress Magazine",
            description="Education should be intensive and rigorous, focused on engineering and mathematics.",
        )
        self.assertEqual(classify(item)[0], "Media & Writing")

    def test_body_keywords_apply(self):
        item = bookmark("Notes on Rome")
        self.assertEqual(classify(item, body="The empire expanded quickly.")[0], "History & Culture")

    def test_unmatched_uses_fallback(self):
        self.assertEqual(classify(bookmark("A quiet walk"))[0], "Unsorted")

    def test_tie_goes_to_the_earlier_category(self):
        item = bookmark("software magazine")
        self.assertEqual(classify(item)[0], "Software & Engineering")


class CacheTests(unittest.TestCase):
    def test_cache_reuses_result_until_content_changes(self):
        categorizer = categorize.Categorizer(SPEC)
        cache = {}
        item = bookmark("Why software matters")
        self.assertEqual(categorizer.assign(item, "", cache=cache), ("Software & Engineering", "rules"))
        self.assertEqual(cache["1"]["category"], "Software & Engineering")

        # a categorizer with no rules at all still returns the cached answer
        blind = categorize.Categorizer({"fallback": "Unsorted", "categories": []})
        self.assertEqual(blind.assign(item, "", cache=dict(cache))[0], "Software & Engineering")

        item["title"] = "A magazine essay"
        self.assertEqual(categorizer.assign(item, "", cache=cache)[0], "Media & Writing")
        self.assertEqual(cache["1"]["category"], "Media & Writing")

    def test_custom_fallback(self):
        spec = {"fallback": "Unclassified", "categories": SPEC["categories"]}
        categorizer = categorize.Categorizer(spec)
        self.assertEqual(categorizer.assign(bookmark("nothing relevant"), "")[0], "Unclassified")


class HelperTests(unittest.TestCase):
    def test_domain_of_strips_www_and_port(self):
        self.assertEqual(categorize.domain_of("https://www.example.com:8080/x"), "example.com")
        self.assertEqual(categorize.domain_of("not a url"), "")

    def test_slugify(self):
        self.assertEqual(categorize.slugify("AI & Machine Learning"), "ai-machine-learning")
        self.assertEqual(categorize.slugify(""), "uncategorized")


if __name__ == "__main__":
    unittest.main()
