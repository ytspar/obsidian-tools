"""Ground truth: does a note hold the whole article the original page has?

The sync stores what Instapaper's parser returned, so ``audit`` can only prove
we rendered that faithfully -- it cannot see what Instapaper itself missed.  This
module closes that gap by sampling notes, loading the original URLs in a real
browser, and measuring the overlap between the page's article text and the text
we stored.

Two numbers per note:

``ratio``
    stored words / page words.  Cheap, but it punishes us for pages that pad the
    article with navigation, comments, or a consent wall.

``coverage``
    fraction of word shingles (default 20 words) in the stored body that appear
    verbatim in the page text.  This is the honest one: a body that stops halfway
    keeps a high ratio but drops sharply here.

Both come from the live web, so a note can also fail because the page is gone,
paywalled, or renders behind JavaScript.  Those are reported separately as
``unreadable`` and excluded from the partial rate, since they say nothing about
the quality of the archive.

Playwright is imported lazily and only by :func:`playwright_fetcher`, so the
comparison helpers stay stdlib-only and testable without a browser.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time

from . import audit, notes

WORD = re.compile(r"[\w'\u2019-]+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_LEAD = re.compile(r"(?m)^\s*(?:[#>|]+|[-*+]|\d+\.)\s+")

# A page holding less than half of what we stored cannot confirm anything: the
# site now shows a paywall, a consent wall, or a JavaScript stub instead of the
# article.  A high ratio against such a page is arithmetic, not evidence.
STUB_RATIO = 2.0

# Pages under this many words are treated as unreadable rather than short: a
# consent wall, a bot check, or a JavaScript shell all look like this.
MIN_PAGE_WORDS = 200

# Extracted text containers, best first.  The last entry is a fallback for sites
# with no semantic markup at all.
CONTAINERS = (
    "article",
    "[itemprop='articleBody']",
    "main",
    ".article-body, .article__body, .post-content, .entry-content",
    ".story-body, .story-content, #article-body",
)

EXTRACT_JS = """
(selectors) => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const picked = [];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      picked.push({how: selector, text: clean(el.innerText)});
    }
  }
  // Densest prose container, for sites without semantic markup.
  let best = null, bestWords = 0;
  for (const el of document.querySelectorAll("div,section,td")) {
    const paras = el.querySelectorAll("p");
    if (paras.length < 3) continue;
    let len = 0;
    for (const p of paras) len += (p.innerText || "").length;
    if (len > bestWords) { bestWords = len; best = el; }
  }
  if (best) picked.push({how: "densest", text: clean(best.innerText)});
  picked.push({how: "body", text: clean(document.body ? document.body.innerText : "")});
  return {
    title: document.title,
    candidates: picked,
  };
}
"""


def words(text):
    return WORD.findall((text or "").lower())


def plain(markdown):
    """Strip the Markdown our renderer added, keeping the prose.

    Link destinations matter: they are words in the file but never on the page,
    so leaving them in would understate the overlap.
    """
    text = _MD_LINK.sub(r"\1", markdown or "")
    text = _MD_LEAD.sub(" ", text)
    text = text.replace("**", "").replace("`", "").replace("\u00a0", " ")
    return text


def ratio(body, page_text):
    """Stored words / page words, as a float (page empty -> None)."""
    page = notes.word_count(page_text)
    if not page:
        return None
    return notes.word_count(plain(body)) / page


def shingle_coverage(body, page_text, size=20):
    """Fraction of the stored body's shingles that appear in the page text.

    Catches truncation that a length ratio hides: half an article can still be
    within 20% of a page whose useful text we over-estimated.
    """
    page = words(page_text)
    stored = words(plain(body))
    if len(page) < size or len(stored) < size:
        return None
    index = {tuple(page[i : i + size]) for i in range(len(page) - size + 1)}
    step = max(1, size // 2)
    total = hits = 0
    for i in range(0, len(stored) - size + 1, step):
        total += 1
        hits += tuple(stored[i : i + size]) in index
    return hits / total if total else None


def verdict(ratio_value, coverage, min_ratio=0.85):
    """ok / partial / dropped, from the length ratio and shingle coverage.

    A ratio at or above ``min_ratio`` is enough: storing more than the page
    currently shows (a paywall went up, the piece was edited) is not a loss.
    Below that, only coverage can rescue the note -- if nearly everything we
    stored appears on the page, the page's extra words were navigation, related
    links, or comments.

    Verdicts that say nothing about the archive are reported separately:
    ``unverified`` when the page is a stub many times smaller than our note, so
    the comparison has no bearing either way.
    """
    if ratio_value is None and coverage is None:
        return "unknown"
    if coverage is not None and coverage >= 0.5:
        return "ok"
    if ratio_value is not None and ratio_value >= min_ratio:
        return "unverified" if ratio_value > STUB_RATIO else "ok"
    if (ratio_value is not None and ratio_value >= 0.5) or (coverage is not None and coverage >= 0.2):
        return "partial"
    return "dropped"


def wilson(hits, total, z=1.96):
    """95% confidence interval for a proportion (stdlib, small samples included)."""
    if not total:
        return (0.0, 0.0)
    p = hits / total
    d = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, (centre - margin) / d), min(1.0, (centre + margin) / d))


def pick_candidate(candidates, body):
    """Choose the extracted container that best matches our stored body.

    Preference order: the container whose text is not absurdly smaller than the
    body, then the largest of those.  Falling back to the whole body keeps sites
    with no markup from being scored against a nav-heavy page.
    """
    stored = notes.word_count(body)
    usable = [c for c in candidates if notes.word_count(c.get("text")) >= MIN_PAGE_WORDS]
    if not usable:
        return max(candidates, key=lambda c: notes.word_count(c.get("text")), default=None)
    # Candidates arrive in preference order (semantic markup first, whole body
    # last), so take the first that is not suspiciously smaller than what we
    # stored -- that avoids scoring us against a nav-heavy <body>.
    for candidate in usable:
        if notes.word_count(candidate["text"]) >= stored * 0.5:
            return candidate
    return max(usable, key=lambda c: notes.word_count(c["text"]))


def sample_notes(vault_path, notes_dir="Instapaper", index_note="Instapaper Index", count=30, seed=0):
    """Pick notes to check: only files that hold article text and a web URL."""
    root = os.path.join(os.path.expanduser(vault_path), notes_dir)
    paths = []
    for directory, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            if name in (f"{index_note}.md", "index.md", "Index.md"):
                continue
            paths.append(os.path.join(directory, name))
    paths.sort()
    rng = random.Random(seed)
    rng.shuffle(paths)
    chosen = []
    for path in paths:
        if len(chosen) >= count:
            break
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        body = audit.article_section(text)
        if not body or audit.PLACEHOLDER in body or audit.UNAVAILABLE in body:
            continue
        front, _ok = audit.parse_frontmatter(text)
        url = front.get("url") or ""
        if not url.startswith("http"):
            continue
        chosen.append(
            {
                "path": path,
                "name": os.path.basename(path)[: -len(".md")],
                "url": url,
                "body": body,
                "words": notes.word_count(body),
            }
        )
    return chosen


def compare(note, page, min_ratio=0.85):
    """Merge one note with one fetched page into a finding."""
    result = {
        "name": note["name"],
        "url": note["url"],
        "stored_words": note["words"],
        "page_words": 0,
        "ratio": None,
        "coverage": None,
        "container": "",
        "page_title": "",
        "error": "",
    }
    if not page:
        result["error"] = "not fetched"
        return result
    if page.get("error"):
        result["error"] = page["error"]
        return result
    candidate = pick_candidate(page.get("candidates") or [], note["body"])
    if not candidate:
        result["error"] = "no text on page"
        return result
    text = candidate.get("text") or ""
    result["container"] = candidate.get("how") or ""
    result["page_title"] = page.get("title") or ""
    result["page_words"] = notes.word_count(text)
    result["ratio"] = ratio(note["body"], text)
    result["coverage"] = shingle_coverage(note["body"], text)
    if result["page_words"] < MIN_PAGE_WORDS:
        result["error"] = "page unreadable (paywall, consent wall, or JavaScript)"
        return result
    result["verdict"] = verdict(result["ratio"], result["coverage"], min_ratio)
    return result


def summarise(findings):
    usable = [
        f
        for f in findings
        if not f["error"] and f.get("verdict") in ("ok", "partial", "dropped")
    ]
    tally = {"ok": 0, "partial": 0, "dropped": 0}
    for finding in usable:
        tally[finding["verdict"]] = tally.get(finding["verdict"], 0) + 1
    bad = tally["partial"] + tally["dropped"]
    low, high = wilson(bad, len(usable))
    ratios = sorted(f["ratio"] for f in usable if f["ratio"] is not None)
    coverages = sorted(f["coverage"] for f in usable if f["coverage"] is not None)
    unverified = sum(1 for f in findings if f.get("verdict") == "unverified")
    return {
        "sampled": len(findings),
        "compared": len(usable),
        "unverified": unverified,
        "unreadable": len(findings) - len(usable) - unverified,
        "ok": tally["ok"],
        "partial": tally["partial"],
        "dropped": tally["dropped"],
        "bad": bad,
        "bad_rate": (bad / len(usable)) if usable else None,
        "bad_ci": (low, high),
        "median_ratio": ratios[len(ratios) // 2] if ratios else None,
        "median_coverage": coverages[len(coverages) // 2] if coverages else None,
    }


def report(findings, sample=10):
    """Human-readable summary plus the worst offenders."""
    stats = summarise(findings)
    lines = []
    lines.append(f"sampled         {stats['sampled']}")
    lines.append(f"compared        {stats['compared']}")
    if stats["compared"]:
        lines.append(
            "verdicts        "
            f"ok {stats['ok']}, partial {stats['partial']}, dropped {stats['dropped']}"
        )
        low, high = stats["bad_ci"]
        lines.append(
            "partial rate    "
            f"{stats['bad_rate'] * 100:.1f}% (95% CI {low * 100:.1f}-{high * 100:.1f}%)"
        )
        if stats["median_ratio"] is not None:
            lines.append(f"median ratio    {stats['median_ratio']:.2f} stored/page words")
        if stats["median_coverage"] is not None:
            lines.append(f"median overlap  {stats['median_coverage'] * 100:.0f}% of shingles on page")
    lines.append(f"unverified      {stats['unverified']}  (page now a stub or paywalled shell)")
    lines.append(f"unreadable      {stats['unreadable']}")
    bad = [f for f in findings if f.get("verdict") in ("partial", "dropped")]
    if bad:
        lines.append("")
        lines.append("worst offenders")
        for finding in sorted(bad, key=lambda f: (f["coverage"] if f["coverage"] is not None else 0))[:sample]:
            ratio_text = f"{finding['ratio']:.2f}" if finding["ratio"] is not None else "n/a"
            coverage_text = f"{finding['coverage'] * 100:.0f}%" if finding["coverage"] is not None else "n/a"
            lines.append(
                f"  {finding['verdict']:8s} ratio {ratio_text} overlap {coverage_text:>4s} "
                f"{finding.get('stored_words', 0):5d}/{finding.get('page_words', 0):5d} words  "
                f"{finding.get('name', '')[:70]}"
            )
    if bad and any(f.get("stored_words", 0) < 50 for f in bad):
        lines.append("")
        lines.append(
            "  notes this short usually mean Instapaper's own parser returned almost "
            "nothing; re-fetch them with `sync --refresh`"
        )
    errors = {}
    for finding in findings:
        if finding["error"]:
            errors.setdefault(finding["error"], []).append(finding["name"])
    if errors:
        lines.append("")
        lines.append("pages that could not be read")
        for reason, names in sorted(errors.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {len(names):3d}  {reason}")
            for name in names[:2]:
                lines.append(f"        {name[:70]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fetching (the only part that needs a browser)
# --------------------------------------------------------------------------- #


RETRIES = 3


def playwright_fetcher(workers=4, timeout=30, log=None):
    """Return fetch_many(urls) -> {url: extraction} backed by Chromium."""
    from playwright.async_api import async_playwright

    def fetch_many(urls):
        async def run():
            out = {}
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(headless=True)
                except Exception as exc:
                    if log:
                        log(f"Could not start Chromium: {exc}")
                    return {url: {"error": "browser unavailable"} for url in urls}
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                semaphore = asyncio.Semaphore(max(1, workers))

                async def attempt(url):
                    page = await context.new_page()
                    try:
                        await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        return await page.evaluate(EXTRACT_JS, list(CONTAINERS))
                    finally:
                        await page.close()

                async def one(url):
                    async with semaphore:
                        for attempt_number in range(RETRIES):
                            try:
                                return await attempt(url)
                            except Exception as exc:
                                first = str(exc).splitlines()[0][:120]
                                message = f"{type(exc).__name__}: {first}"
                                transient = any(
                                    marker in first
                                    for marker in (
                                        "ERR_NETWORK_CHANGED",
                                        "ERR_ABORTED",
                                        "ERR_CONNECTION_RESET",
                                        "ERR_NAME_NOT_RESOLVED",
                                        "ERR_HTTP2_PROTOCOL_ERROR",
                                        "Timeout",
                                    )
                                )
                                if not transient or attempt_number == RETRIES - 1:
                                    return {"error": message}
                                # This machine's network drops connections under load;
                                # a flap says nothing about the article.
                                await asyncio.sleep(2 + 2 * attempt_number)

                results = await asyncio.gather(*(one(url) for url in urls))
                await context.close()
                await browser.close()
            return dict(zip(urls, results))

        return asyncio.run(run())

    return fetch_many


def run(
    vault_path,
    notes_dir="Instapaper",
    index_note="Instapaper Index",
    sample=30,
    seed=0,
    min_ratio=0.85,
    fetch_many=None,
    workers=4,
    timeout=30,
    log=print,
):
    """Sample notes, fetch their pages, and report how much text is missing."""
    chosen = sample_notes(vault_path, notes_dir, index_note, sample, seed)
    if not chosen:
        raise RuntimeError("no notes with article text to check -- run the sync first")
    log(f"Checking {len(chosen)} notes against the live pages (seed {seed})")
    if fetch_many is None:
        fetch_many = playwright_fetcher(workers=workers, timeout=timeout, log=log)
    started = time.time()
    pages = fetch_many([note["url"] for note in chosen])
    findings = [compare(note, pages.get(note["url"]), min_ratio) for note in chosen]
    log(f"Fetched {len(pages)} pages in {time.time() - started:.0f}s")
    return findings


def write_report(findings, path):
    payload = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "findings": findings}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
