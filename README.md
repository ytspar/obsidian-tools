# instapaper-obsidian

Turn your whole Instapaper library — to-read *and* archive — into an Obsidian
vault of full-text, searchable, auto-categorized Markdown notes.

The official [Instapaper plugin](https://github.com/instapaper/obsidian-instapaper)
only pulls notes and highlights. This tool pulls the articles themselves over the
[Instapaper API v2](https://www.instapaper.com/developers/v2/reference/bookmarks)
and writes one note per bookmark:

- **Full article text**, converted from Instapaper's parsed body to Markdown.
- **Read state in the frontmatter** (`status`, `read`, `archived`, `reading_progress`)
  and in the folder layout (`To Read/` vs `Archived/`).
- **Auto-categorization** from rules you control, optionally refined by a local LLM.
- **Fully searchable**: every note is plain Markdown, so Obsidian's search covers
  titles, bodies, tags, and your own notes below the sync marker.

## Requirements

- Python 3.8+ (standard library only, no pip installs)
- An Instapaper account and an API token (below)

## 1. Get an access token

1. Sign in at <https://www.instapaper.com/developers/applications>.
2. Create an application ("New App", any name/description) — this is your own
   developer app, not a store listing.
3. On that app's page choose **Generate access token**, then copy the token.

Save it somewhere private, either:

```bash
mkdir -p ~/.config/instapaper && chmod 700 ~/.config/instapaper
printf '%s' 'YOUR_TOKEN' > ~/.config/instapaper/token && chmod 600 ~/.config/instapaper/token
```

or export it: `export INSTAPAPER_TOKEN=...`

If you already connected the Instapaper Obsidian plugin (v1.3+), its token is
reused automatically from `.obsidian/plugins/instapaper/data.json`.

## 2. Configure

```bash
cp config.example.json config.json
cp categories.example.json categories.json   # then edit it to match your interests
```

Edit `config.json`:

| Key | Meaning |
| --- | --- |
| `vault_path` | absolute path to the Obsidian vault |
| `notes_dir` | folder inside the vault that receives the notes (default `Instapaper`) |
| `layout` | `folders` → `To Read/` + `Archived/`; `flat` → one folder |
| `sections` | which Instapaper lists to pull: `home`, `archive`, `liked`, `folders` |
| `fetch_article_text` | download and convert the article body |
| `fetch_highlights` | also inline the highlights you made in Instapaper |
| `categorize.mode` | `rules` (offline, keyword/tag rules) or `llm` (rules + a model) |
| `categorize.llm.provider` | `deepseek`, `openai`, `openrouter`, `ollama`, or your own OpenAI-compatible server |
| `categorize.llm.scope` | `fallback` (only unplaced articles) or `all` (re-judge everything) |
| `categorize.llm.workers` | batches asked at once (default 1; 6 makes 10k articles take ~10 minutes) |
| `categorize.categories_file` | path to your category taxonomy |
| `request_delay` | seconds between API calls (be polite; 0.3–0.5 is fine) |
| `workers` | parallel article downloads (default 4; 6–8 is a good ceiling) |
| `chunk_size` | notes are written this many at a time, so the vault fills up while it runs |
| `parse_timeout` | seconds to wait for one article's text before giving up (default 20) |
| `delete_mode` | `trash` (move to `.trash/`) or `delete` when a bookmark disappears |

`vault_path` etc. accept `~` and `$VARS`. Relative paths resolve next to `config.json`.

## 3. Run

```bash
python3 -m instapaper_sync check                # verify the token, show counts
python3 -m instapaper_sync sync --dry-run       # preview
python3 -m instapaper_sync sync --limit 20      # first 20 article downloads
python3 -m instapaper_sync sync                 # full sync
python3 -m instapaper_sync sync --since-last    # later, incremental only
```

Useful flags: `--refresh` (re-download article text), `--recategorize` (re-run
categorization), `--sections home,archive`, `--limit N`, `--dry-run`,
`--workers N`, `--retry-failures` (see below).

Other commands: `folders`, `tags`, `list --section archive`, `index`, `audit`,
`verify`, `categorize`.

`audit` reads the notes actually on disk and reports anything inconsistent:
unparsable frontmatter, a missing article body, a word count that does not match
the text, a `status` that contradicts `archived`, a lost user-notes marker. It
exits non-zero when it finds errors, so it works as a check after a sync:

```bash
python3 -m instapaper_sync audit --verbose      # list every finding
python3 -m instapaper_sync audit --lenient      # exit 0 even with errors
python3 -m instapaper_sync audit --no-cache     # structure only, skip completeness
```

### Verifying against the live web

`audit` proves we rendered what Instapaper returned; it cannot see what
Instapaper itself missed. `verify` closes that gap: it samples notes, loads the
original URLs in a real browser (Playwright/Chromium), and measures how much of
each stored body actually appears on the page.

`verify` is the only command that needs a browser, so it is the only one with an
install step. Playwright ships its own Chromium build, pinned to the package
version, and it fails with `Executable doesn't exist at .../chromium-<build>`
when the package and the downloaded build have drifted apart — keep both in step:

```bash
python3 -m pip install -U playwright
python3 -m playwright install chromium
```

```bash
python3 -m instapaper_sync verify --sample 100 --out verify.json
```

Two numbers per note:

- **ratio** — stored words / page words. Cheap, but a page padded with
  navigation or comments punishes us unfairly.
- **coverage** — fraction of 20-word shingles in the stored body that appear
  verbatim in the page text. This is the one that matters: a body that stops
  halfway keeps a decent ratio but collapses here.

A note is `ok` when the ratio is at or above `--min-ratio` (0.85) or when its
coverage is at least half — the second rescues notes whose page carries extra
matter we deliberately do not store. Storing *more* than the page shows (a
paywall went up, the piece was edited) is not a loss, so ratio alone can pass.
The summary reports the partial rate with a 95% confidence interval, and pages
that cannot be read at all (paywalls, consent walls, JavaScript shells, dead
links) are counted separately — they say nothing about the archive.

Sampling is reproducible (`--seed`), needs the network, and takes about 2-4
minutes for 100 notes at `--workers 5`. `--strict` exits 1 if anything is partial
or dropped.

### Cost and rate limits

One API request per bookmarks page (500 items) plus one per article for the text.
A 1,000-article library is roughly 1,000 requests; with `request_delay: 0.4` that
is a few minutes. Downloads are cached in `cache_dir`, so re-runs only fetch what
is new or `--refresh`ed.

Not every article yields text. Instapaper parses the page itself, so a dead link,
a hard paywall or a site that blocks it comes back as
`400 Parsed content unavailable`, and a hanging page costs one `parse_timeout`
and then gives up. Those bookmarks still get a note (title, link, metadata,
highlights) with a callout saying the text is unavailable; the bookmark is
remembered in the state file so later runs do not pay the timeout again.
Changed your mind, or Instapaper fixed something? `sync --retry-failures`
forgets those verdicts and tries once more. Notes that simply have not been
downloaded yet say so instead, which is the state you see while a large library
is still filling in.

## What a note looks like

```markdown
---
instapaper_id: 12345
title: "An Article"
url: "https://example.com/article"
author: "Jane Doe"
site: "example.com"
saved: "2025-03-01T12:00:00Z"
published: "2024-01-15"
status: "to-read"        # to-read | reading | read | archived
read: false
archived: false
reading_progress: 0.0
liked: false
folder: null
instapaper_tags: []
category: "AI & Machine Learning"
tags:
  - "instapaper"
  - "ai-machine-learning"
words: 1234
instapaper_url: "https://www.instapaper.com/read/12345"
synced: "2025-03-02T09:00:00Z"
---

# An Article

*Jane Doe* · [example.com](https://example.com/article) · published 2024-01-15 · saved 2025-03-01

## Article

…the article, as Markdown…

---
<!-- instapaper-sync:user-notes -->
## Notes

Anything you write below this marker is preserved across syncs.
```

`words` is the number of words in the article text stored in the note, so
`[words:>2000]` finds long reads you can actually search. Notes without text
carry `words: null`.

`status` is derived from Instapaper: `archived` when the bookmark is archived,
`read` when it is ≥95% read but not archived, `reading` once you have started,
`to-read` otherwise.

## Searching

Everything lands as Markdown, so Obsidian's search already covers the full text.
Handy queries:

| Goal | Query |
| --- | --- |
| Everything from Instapaper | `path:"Instapaper"` |
| Only what you have not read | `path:"Instapaper/To Read" -["status":read]` |
| Only the archive | `path:"Instapaper/Archived"` |
| One topic, unread | `path:"Instapaper/To Read" [category:"AI & Machine Learning"]` |
| Long reads | `path:"Instapaper" [words:>2000]` |
| Downloaded full text | `path:"Instapaper" [words:>0]` |
| Tag-based | `tag:#ai-machine-learning` |

`python3 -m instapaper_sync sync` also regenerates `Instapaper/Instapaper Index.md`
with live counts per category and these queries ready to copy.

## Categorization

`categories.json` is a plain list of categories with matching signals:

```json
{
  "fallback": "Unsorted",
  "categories": [
    {
      "name": "AI & Machine Learning",
      "keywords": ["llm", "transformer", "fine-tuning"],
      "body_keywords": ["inference", "attention"],
      "tags": ["ai", "ml"],
      "domains": ["openai.com", "anthropic.com"]
    }
  ]
}
```

Scoring: an Instapaper tag match is worth 6, a folder or domain match 4, a
title/description keyword 3, a body keyword 1. Single words match on word
boundaries; multi-word phrases match as substrings. Highest score wins, ties go to
the earlier category, and no match means `fallback`.

The taxonomy is per-article, not per-source, so add keywords that describe
*subjects* you actually save. Categories also become Obsidian tags, so
`[category:"Product & Design"]` and `tag:#product-design` both work.

`categories.example.json` ships a general starter set you are meant to rewrite.
Your own `categories.json` is gitignored along with `config.json`, since a
reading taxonomy says quite a lot about you.

### Letting a model categorize (DeepSeek, OpenAI, Ollama)

Rules get you a rough sort; a model gets you an accurate one. Set
`"mode": "llm"` and pick a provider:

```json
"categorize": {
  "mode": "llm",
  "llm": {
    "provider": "deepseek",
    "model": "deepseek-chat",
    "scope": "all",
    "batch_size": 25,
    "api_key_env": "DEEPSEEK_API_KEY",
    "api_key_file": "~/.config/instapaper/llm-key"
  }
}
```

Presets: `deepseek` (`https://api.deepseek.com`, `deepseek-chat`), `openai`
(`gpt-4o-mini`), `openrouter` (`deepseek/deepseek-chat`), and `ollama`
(`http://localhost:11434`, `gpt-oss:20b`, the default — pull it once with
`ollama pull gpt-oss:20b`). Any other OpenAI-compatible server works by setting
`provider` to anything you like plus `base_url` and `model`; the version path
belongs in `base_url` (`https://api.deepseek.com/v1` and
`http://localhost:8080/v1` both work).

The key is looked up in this order: `api_key` in the config, `api_key_env`
(`DEEPSEEK_API_KEY`, or the provider's own variable), then `api_key_file`
(default `~/.config/instapaper/llm-key`, `chmod 600`). The token is never written
to the repo or the state file:

```bash
printf '%s\n' "$DEEPSEEK_API_KEY" > ~/.config/instapaper/llm-key
chmod 600 ~/.config/instapaper/llm-key
```

`scope` decides what the model is asked about: `fallback` (default) only fills in
the articles the rules could not place, while `all` re-judges everything — that
is how rule-based misplacements get corrected, and it costs one batch per 25
articles. Batches are retried with backoff on 429/5xx and on the occasional 404
the DeepSeek gateway returns; if the provider is unreachable the run still
finishes on rule-based categories.

Model verdicts are cached in the state file keyed on title/description/tags/url,
so they survive later article downloads and only re-run when an article's
metadata changes. That makes a two-step workflow cheap:

```bash
python3 -m instapaper_sync categorize --recategorize --scope all   # minutes, no rendering
python3 -m instapaper_sync sync                                    # re-render from cache
```

`categorize` prints the resulting distribution and the method that decided each
note (`rules`, `fallback`, `llm`), without touching a single note. The full
`sync --recategorize` does both in one pass and also re-renders the notes.

## Re-running safely

- Notes are keyed by `instapaper_id`, so re-runs update in place instead of duplicating.
- When an article moves between to-read and archive, its note moves between folders.
- Anything below `<!-- instapaper-sync:user-notes -->` is kept verbatim; the rest is
  regenerated from Instapaper, so write under `## Notes` rather than inside the article.
- Title collisions append the Instapaper id: `Title (12345).md`.
- `--since-last` uses the API's `since` mode and also removes notes for bookmarks
  deleted in Instapaper.

## Layout

```
instapaper_sync/
  api.py          Instapaper API v2 client (pagination, retries, caching hooks)
  config.py       config loading + token discovery
  html2md.py      dependency-free HTML -> Markdown converter
  categorize.py   rule engine + DeepSeek/OpenAI/Ollama classifier
  notes.py        frontmatter, note bodies, file naming, index note
  sync.py         orchestration: fetch, render, move, index, state
  audit.py        checks the notes already on disk (+ completeness vs cached text)
  verify.py       samples notes and diffs them against the live web page
  login.py        OAuth helper that writes the Instapaper token
  cli.py          argparse entry point
tests/            unit + end-to-end tests against a fake API
```

Run the tests with `python3 -m unittest discover -s tests`.
