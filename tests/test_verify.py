"""Tests for verify: sampling, comparison maths and reporting (no browser)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from instapaper_sync import notes, verify

PROSE = (
    "The quick brown fox jumps over the lazy dog while the neighbours watch from "
    "their windows and wonder what the commotion is about this time of night. "
)

FULL_BODY = " ".join(f"Paragraph {i}: {PROSE}" for i in range(12))
HALF_BODY = " ".join(f"Paragraph {i}: {PROSE}" for i in range(6))


def write_note(root, name, body, url="https://example.com/a", words=None):
    path = os.path.join(root, name + ".md")
    text = (
        "---\n"
        'instapaper_id: 1\n'
        f'title: "{name}"\n'
        f'url: "{url}"\n'
        "saved: 2026-01-01T00:00:00Z\n"
        'status: "to-read"\n'
        "archived: false\n"
        f'words: {words if words is not None else notes.word_count(body)}\n'
        "---\n\n"
        f"# {name}\n\n## Article\n\n{body}\n\n---\n<!-- instapaper-sync:user-notes -->\n\n## Notes\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class RatioAndCoverage(unittest.TestCase):
    def test_identical_text_scores_one(self):
        self.assertAlmostEqual(verify.ratio(FULL_BODY, FULL_BODY), 1.0, places=3)
        self.assertAlmostEqual(verify.shingle_coverage(FULL_BODY, FULL_BODY), 1.0, places=3)

    def test_truncated_body_keeps_ratio_but_loses_coverage(self):
        # Half the body against the whole page: ratio 0.5, coverage 1.0 because
        # every shingle we kept is genuinely on the page.
        self.assertAlmostEqual(verify.ratio(HALF_BODY, FULL_BODY), 0.5, places=2)
        self.assertAlmostEqual(verify.shingle_coverage(HALF_BODY, FULL_BODY), 1.0, places=2)

    def test_body_with_invented_text_scores_low_coverage(self):
        invented = " ".join(f"Fabricated line {i} appears nowhere on the page at all." for i in range(40))
        self.assertEqual(verify.shingle_coverage(invented, FULL_BODY), 0.0)

    def test_coverage_is_none_for_tiny_texts(self):
        self.assertIsNone(verify.shingle_coverage("three words only", FULL_BODY))
        self.assertIsNone(verify.ratio(FULL_BODY, ""))

    def test_markdown_link_destinations_are_not_counted(self):
        stored = "A piece of prose with [a link](https://example.com/very/long/path)."
        self.assertEqual(verify.words(verify.plain(stored)), ["a", "piece", "of", "prose", "with", "a", "link"])
        self.assertNotIn("example", verify.words(verify.plain(stored)))

    def test_headings_and_list_markers_are_stripped(self):
        self.assertEqual(
            verify.words(verify.plain("## Article\n- one\n> quoted\n**bold**")),
            ["article", "one", "quoted", "bold"],
        )

    def test_coverage_ignores_punctuation_and_case(self):
        page = FULL_BODY.upper().replace(" ", "  \n ")
        self.assertAlmostEqual(verify.shingle_coverage(FULL_BODY, page), 1.0, places=2)


class Verdicts(unittest.TestCase):
    def test_whole_article_is_ok(self):
        self.assertEqual(verify.verdict(1.0, 1.0), "ok")

    def test_high_coverage_rescues_a_low_ratio(self):
        # Page padded with navigation: small ratio, but everything we stored is
        # really there.
        self.assertEqual(verify.verdict(0.3, 0.95), "ok")

    def test_a_stub_page_proves_nothing(self):
        # Our note is 7x the page: the site now serves a paywall stub, so the
        # comparison cannot say whether the archive is complete either way.
        self.assertEqual(verify.verdict(7.1, 0.0), "unverified")

    def test_a_page_with_a_paywall_over_our_text_is_still_ok(self):
        # Page is somewhat shorter but recognisably the same article.
        self.assertEqual(verify.verdict(1.4, 0.1), "ok")

    def test_midway_is_partial(self):
        self.assertEqual(verify.verdict(0.62, 0.1), "partial")

    def test_nothing_in_common_is_dropped(self):
        self.assertEqual(verify.verdict(0.1, 0.0), "dropped")

    def test_min_ratio_is_honoured(self):
        self.assertEqual(verify.verdict(0.7, None, min_ratio=0.6), "ok")
        self.assertEqual(verify.verdict(0.7, None, min_ratio=0.9), "partial")

    def test_missing_numbers_are_unknown(self):
        self.assertEqual(verify.verdict(None, None), "unknown")


class WilsonInterval(unittest.TestCase):
    def test_brackets_the_estimate(self):
        low, high = verify.wilson(10, 100)
        self.assertLess(low, 0.1)
        self.assertGreater(high, 0.1)
        self.assertTrue(0 <= low <= high <= 1)

    def test_small_samples_are_wide(self):
        low, high = verify.wilson(0, 5)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.4)

    def test_no_samples(self):
        self.assertEqual(verify.wilson(0, 0), (0.0, 0.0))


class CandidateChoice(unittest.TestCase):
    def test_prefers_the_container_closest_to_the_stored_body(self):
        body = " ".join(["word"] * 800)
        candidates = [
            {"how": "article", "text": " ".join(["word"] * 780)},
            {"how": "body", "text": " ".join(["word"] * 9000)},
            {"how": "densest", "text": " ".join(["word"] * 3000)},
        ]
        best = verify.pick_candidate(candidates, body)
        self.assertEqual(best["how"], "article")

    def test_falls_back_to_largest_when_nothing_matches(self):
        body = " ".join(["word"] * 4000)
        candidates = [
            {"how": "article", "text": " ".join(["word"] * 250)},
            {"how": "body", "text": " ".join(["word"] * 900)},
        ]
        self.assertEqual(verify.pick_candidate(candidates, body)["how"], "body")

    def test_survives_an_empty_page(self):
        self.assertIsNone(verify.pick_candidate([], "body"))


class Compare(unittest.TestCase):
    def note(self, body=FULL_BODY):
        return {
            "name": "A note",
            "url": "https://example.com/a",
            "body": body,
            "words": notes.word_count(body),
        }

    def test_page_matching_the_note_is_ok(self):
        page = {"title": "T", "candidates": [{"how": "article", "text": FULL_BODY}]}
        finding = verify.compare(self.note(), page)
        self.assertEqual(finding["verdict"], "ok")
        self.assertEqual(finding["container"], "article")
        self.assertGreater(finding["coverage"], 0.9)

    def test_body_that_drifts_from_the_page_is_flagged(self):
        # One real paragraph buried in invented text, against a much longer page:
        # neither the length ratio nor the overlap supports "ok".
        drifted = "Paragraph 0: " + PROSE
        drifted += " ".join(f"Invented {i}: nothing here resembles the page at all." for i in range(40))
        page = {"title": "T", "candidates": [{"how": "article", "text": FULL_BODY * 2}]}
        finding = verify.compare(self.note(drifted), page)
        self.assertNotEqual(finding["verdict"], "ok")

    def test_storing_more_than_the_page_is_not_a_loss(self):
        # Pages shrink (paywall added, article edited); our copy being longer is
        # not a defect, so a healthy ratio stands on its own.
        abridged = " ".join(f"Paragraph {i}: {PROSE}" for i in range(8))
        page = {"title": "T", "candidates": [{"how": "article", "text": abridged}]}
        finding = verify.compare(self.note(), page)
        self.assertEqual(finding["verdict"], "ok")
        self.assertGreater(finding["ratio"], 1.0)

    def test_repeated_page_text_is_not_treated_as_a_loss(self):
        # A page that repeats our text (pagination, duplicates) is not a defect
        # in our copy: every shingle we stored really is there.
        page = {"title": "T", "candidates": [{"how": "article", "text": HALF_BODY * 4}]}
        self.assertEqual(verify.compare(self.note(), page)["verdict"], "ok")

    def test_short_page_is_unreadable_not_missing(self):
        page = {"title": "T", "candidates": [{"how": "body", "text": "Please enable JavaScript."}]}
        finding = verify.compare(self.note(), page)
        self.assertIn("unreadable", finding["error"])
        self.assertNotIn("verdict", finding)

    def test_fetch_error_is_reported(self):
        finding = verify.compare(self.note(), {"error": "TimeoutError: 30000ms"})
        self.assertEqual(finding["error"], "TimeoutError: 30000ms")
        self.assertIsNone(finding["ratio"])


class Sampling(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(os.path.join(self.vault, "Instapaper", "To Read"))
        for i in range(6):
            write_note(os.path.join(self.vault, "Instapaper", "To Read"), f"Note {i}", FULL_BODY)
        # Not eligible: no article text, no URL, or the index.
        write_note(
            os.path.join(self.vault, "Instapaper", "To Read"),
            "Empty",
            "> [!warning] Article text not downloaded",
            words=0,
        )
        write_note(os.path.join(self.vault, "Instapaper", "To Read"), "No URL", FULL_BODY, url="")
        write_note(os.path.join(self.vault, "Instapaper"), "Instapaper Index", FULL_BODY)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_notes_with_text_and_url_are_sampled(self):
        chosen = verify.sample_notes(self.vault, "Instapaper", "Instapaper Index", count=10, seed=1)
        self.assertEqual(len(chosen), 6)
        self.assertTrue(all(n["url"].startswith("http") for n in chosen))
        self.assertTrue(all(n["words"] > 0 for n in chosen))

    def test_sampling_is_reproducible_and_capped(self):
        first = [n["name"] for n in verify.sample_notes(self.vault, "Instapaper", "Instapaper Index", 3, seed=7)]
        second = [n["name"] for n in verify.sample_notes(self.vault, "Instapaper", "Instapaper Index", 3, seed=7)]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_different_seeds_pick_different_notes(self):
        seen = {
            tuple(n["name"] for n in verify.sample_notes(self.vault, "Instapaper", "Instapaper Index", 3, seed=s))
            for s in range(4)
        }
        self.assertGreater(len(seen), 1)

    def test_run_uses_the_injected_fetcher(self):
        def fetch_many(urls):
            return {url: {"title": "T", "candidates": [{"how": "article", "text": FULL_BODY}]} for url in urls}

        findings = verify.run(
            self.vault, "Instapaper", sample=4, seed=2, fetch_many=fetch_many, log=lambda *_: None
        )
        self.assertEqual(len(findings), 4)
        self.assertTrue(all(f["verdict"] == "ok" for f in findings))


class Reporting(unittest.TestCase):
    def test_summary_counts_and_confidence_interval(self):
        findings = [
            {"name": "a", "error": "", "verdict": "ok", "ratio": 1.0, "coverage": 1.0},
            {"name": "b", "error": "", "verdict": "ok", "ratio": 0.95, "coverage": 0.9},
            {"name": "c", "error": "", "verdict": "partial", "ratio": 0.6, "coverage": 0.3},
            {"name": "d", "error": "", "verdict": "dropped", "ratio": 0.1, "coverage": 0.0},
            {"name": "e", "error": "page unreadable (paywall)", "ratio": None, "coverage": None},
        ]
        stats = verify.summarise(findings)
        self.assertEqual((stats["sampled"], stats["compared"], stats["unreadable"]), (5, 4, 1))
        self.assertEqual(stats["unverified"], 0)
        self.assertEqual(stats["bad"], 2)
        self.assertAlmostEqual(stats["bad_rate"], 0.5)
        self.assertEqual(stats["median_ratio"], 0.95)
        text = verify.report(findings)
        self.assertIn("partial rate    50.0%", text)
        self.assertIn("95% CI", text)
        self.assertIn("pages that could not be read", text)
        self.assertIn("worst offenders", text)
        self.assertIn("ratio 0.10", text)

    def test_report_survives_an_empty_run(self):
        text = verify.report([])
        self.assertIn("sampled         0", text)

    def test_json_report_round_trips(self):
        findings = [{"name": "a", "verdict": "ok", "ratio": 1.0, "coverage": 1.0, "error": ""}]
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        try:
            verify.write_report(findings, handle.name)
            with open(handle.name, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload["findings"], findings)
            self.assertIn("generated", payload)
        finally:
            os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
