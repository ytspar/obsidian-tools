"""Rule-based (and optionally LLM-assisted) categorization of saved articles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent import futures
import urllib.error
import urllib.request

WEIGHTS = {"tag": 6, "folder": 4, "domain": 4, "title": 3, "description": 1, "body": 1}

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you",
    "how", "why", "what", "when", "who", "are", "was", "were", "his", "her",
}


def slugify(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-") or "uncategorized"


def domain_of(url):
    if not url:
        return ""
    match = re.match(r"https?://([^/]+)", url.strip(), re.I)
    if not match:
        return ""
    host = match.group(1).lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _compile_keyword(keyword):
    keyword = keyword.strip().lower()
    if not keyword:
        return None
    if re.search(r"\s", keyword):
        return ("contains", keyword)
    return ("word", re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"))


class RuleSet:
    def __init__(self, spec):
        self.fallback = spec.get("fallback") or "Unsorted"
        self.categories = []
        for entry in spec.get("categories") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"].strip()
            self.categories.append({
                "name": name,
                "slug": entry.get("slug") or slugify(name),
                "tags": {t.strip().lower() for t in entry.get("tags") or [] if t.strip()},
                "domains": {d.strip().lower() for d in entry.get("domains") or [] if d.strip()},
                "keywords": [
                    compiled
                    for compiled in (_compile_keyword(k) for k in entry.get("keywords") or [])
                    if compiled
                ],
                "body_keywords": [
                    compiled
                    for compiled in (_compile_keyword(k) for k in entry.get("body_keywords") or [])
                    if compiled
                ],
            })

    @staticmethod
    def _matches(compiled, text):
        kind, value = compiled
        if kind == "contains":
            return value in text
        return bool(value.search(text))

    def score(self, category, signals):
        score = 0
        for tag in signals["tags"]:
            if tag in category["tags"]:
                score += WEIGHTS["tag"]
        if signals["folder"] and signals["folder"].lower() in category["tags"]:
            score += WEIGHTS["folder"]
        if signals["domain"] and signals["domain"].lower() in category["domains"]:
            score += WEIGHTS["domain"]
        for compiled in category["keywords"]:
            if self._matches(compiled, signals["title"]):
                score += WEIGHTS["title"]
            elif self._matches(compiled, signals["description"]):
                score += WEIGHTS["description"]
        for compiled in category["body_keywords"]:
            if self._matches(compiled, signals["body"]):
                score += WEIGHTS["body"]
        return score

    def classify(self, signals):
        best, best_score = None, 0
        for category in self.categories:
            score = self.score(category, signals)
            if score > best_score:
                best, best_score = category, score
        return (best, best_score) if best else (None, 0)


def signals_for(bookmark, body="", folder=None):
    tags = [str(t.get("name", "")).lower() for t in bookmark.get("tags") or [] if isinstance(t, dict)]
    return {
        "tags": tags,
        "folder": (folder or bookmark.get("folder_title") or "").lower() or None,
        "domain": domain_of(bookmark.get("url")),
        "title": (bookmark.get("title") or "").lower(),
        "description": (bookmark.get("description") or "").lower(),
        "body": body[:4000].lower(),
    }


def content_hash(bookmark, body=""):
    payload = json.dumps(
        [
            bookmark.get("title"),
            bookmark.get("description"),
            sorted(str(t.get("name")) for t in bookmark.get("tags") or [] if isinstance(t, dict)),
            bookmark.get("url"),
            len(body),
        ],
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


PROVIDERS = {
    "ollama": {
        "style": "ollama",
        "base_url": "http://localhost:11434",
        "model": "gpt-oss:20b",
        "key_env": None,
    },
    "deepseek": {
        "style": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "style": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "openrouter": {
        "style": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "key_env": "OPENROUTER_API_KEY",
    },
}

DEFAULT_KEY_FILE = "~/.config/instapaper/llm-key"


def _expand(path):
    return os.path.expanduser(os.path.expandvars(path))


def resolve_api_key(options, provider):
    """Find an API key: explicit option, environment, then a key file."""
    explicit = (options.get("api_key") or "").strip()
    if explicit:
        return explicit

    env_name = options.get("api_key_env") or PROVIDERS.get(provider, {}).get("key_env")
    if env_name and os.environ.get(env_name):
        value = os.environ[env_name].strip()
        if value:
            return value

    key_file = options.get("api_key_file")
    if key_file is None and PROVIDERS.get(provider, {}).get("key_env"):
        key_file = DEFAULT_KEY_FILE
    if key_file:
        try:
            with open(_expand(key_file), "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            value = ""
        if value:
            return value
    return ""


def _extract_json(text):
    """Pull the first JSON object out of a model reply (tolerates code fences)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for position in range(start, len(text)):
            if text[position] == "{":
                depth += 1
            elif text[position] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:position + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return {}


class LlmClassifier:
    """Batched second-opinion pass against a local or hosted chat model.

    Two wire formats are supported: Ollama's native ``/api/chat`` and the
    OpenAI-compatible ``/chat/completions`` spoken by DeepSeek, OpenRouter and
    friends.  Anything that speaks the latter works by setting provider/base_url
    /model/api_key.
    """

    def __init__(self, options, categories, fallback, log=None):
        options = options or {}
        provider = (options.get("provider") or "ollama").strip().lower()
        preset = PROVIDERS.get(provider, {})
        style = options.get("style") or preset.get("style") or "openai"
        self.provider = provider
        self.style = style
        self.base_url = (options.get("base_url") or preset.get("base_url") or "").rstrip("/")
        self.model = options.get("model") or preset.get("model") or "gpt-oss:20b"
        self.api_key = resolve_api_key(options, provider)
        self.batch_size = max(1, int(options.get("batch_size") or 12))
        self.timeout = int(options.get("timeout") or 300)
        self.delay = float(options.get("delay") or 0)
        self.max_retries = int(options.get("max_retries") or 3)
        self.temperature = float(options.get("temperature") or 0.1)
        self.scope = (options.get("scope") or "fallback").lower()
        self.workers = max(1, int(options.get("workers") or 1))
        self.progress = None
        self.categories = categories
        self.fallback = fallback
        self.log = log or (lambda *a, **k: None)

    @property
    def needed_key(self):
        return PROVIDERS.get(self.provider, {}).get("key_env") or "an API key"

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def available(self):
        if self.style == "ollama":
            try:
                with urllib.request.urlopen(self.base_url + "/api/tags", timeout=5) as response:
                    return response.status == 200
            except (urllib.error.URLError, OSError, ValueError):
                return False
        if not self.api_key:
            self.log(f"  no API key for {self.provider} ({self.needed_key}); skipping LLM pass")
            return False
        request = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status == 200
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self.log(f"  {self.provider} rejected the API key (HTTP {exc.code})")
                return False
            return True  # gateway without a model listing still serves chat
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _prompt(self):
        names = ", ".join(self.categories)
        return (
            "You are sorting a personal reading list into fixed categories.\n"
            f"Allowed categories: {names}.\n"
            "For each article, choose exactly one allowed category. "
            f"If nothing fits, use {self.fallback}.\n"
            "Reply with a json object only, of the form "
            '{"results": [{"id": <article id>, "category": "<category>", "confidence": 0.0-1.0}]}.\n'
            "Use the abstract topic of the article, not the source website."
        )

    def _items(self, batch):
        return [
            {
                "id": item["id"],
                "title": item.get("title") or "",
                "description": (item.get("description") or "")[:300],
                "tags": item.get("tags") or [],
                "site": item.get("domain") or "",
                "opening": (item.get("body") or "")[:900],
            }
            for item in batch
        ]

    def _payload(self, batch):
        messages = [
            {"role": "system", "content": self._prompt()},
            {
                "role": "user",
                "content": json.dumps(self._items(batch), ensure_ascii=False),
            },
        ]
        if self.style == "ollama":
            return {
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": self.temperature},
                "messages": messages,
            }
        return {
            "model": self.model,
            "stream": False,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }

    def _endpoint(self):
        if self.style == "ollama":
            return self.base_url + "/api/chat"
        return self.base_url + "/chat/completions"

    def _content(self, data):
        if self.style == "ollama":
            return data.get("message", {}).get("content", "")
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content", "")

    def _ask(self, batch):
        body = json.dumps(self._payload(batch), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(), data=body, headers=self._headers(), method="POST"
        )
        wait = 0.0
        for attempt in range(self.max_retries + 1):
            if wait:
                self.log(f"  llm retry in {wait:.0f}s")
                time.sleep(wait)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return _extract_json(self._content(data))
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                # 404 has been seen from the DeepSeek gateway itself, not just
                # from a mistyped path, so it is worth one more try.
                if exc.code in (404, 408, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                    wait = min(float(retry_after or 2 ** (attempt + 1)), 60.0)
                    continue
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                self.log(
                    f"  llm batch failed (HTTP {exc.code} from {self._endpoint()}) {detail}; "
                    "keeping rule-based categories"
                )
                return {}
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt < self.max_retries:
                    wait = min(2 ** (attempt + 1), 60.0)
                    continue
                self.log(f"  llm batch failed ({exc}); keeping rule-based categories")
                return {}
        return {}

    def classify(self, items):
        """items: list of dicts with id/title/description/tags/domain/body."""
        results = {}
        allowed = {name.lower(): name for name in self.categories}
        batches = [
            items[start:start + self.batch_size]
            for start in range(0, len(items), self.batch_size)
        ]
        total = len(items)
        lock = threading.Lock()
        done = [0]

        def work(batch):
            if self.delay:
                time.sleep(self.delay)
            parsed = self._ask(batch)
            found = {}
            for row in parsed.get("results") or []:
                if not isinstance(row, dict):
                    continue
                category = str(row.get("category", "")).strip()
                resolved = allowed.get(category.lower())
                if resolved and row.get("id") is not None:
                    found[row["id"]] = resolved
            if not found:
                return
            with lock:
                results.update(found)
                done[0] += len(batch)
                if done[0] % 250 < len(batch) or done[0] >= total:
                    self.log(f"  llm categorized {done[0]}/{total}")
                    if self.progress:
                        self.progress()

        workers = max(1, min(self.workers, len(batches) or 1))
        if workers == 1:
            for batch in batches:
                work(batch)
        else:
            self.log(f"  asking {workers} batches at a time")
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(work, batches))
        return results


class Categorizer:
    def __init__(self, spec, mode="rules", llm_options=None, log=None):
        self.rules = RuleSet(spec)
        self.mode = mode
        self.log = log or (lambda *a, **k: None)
        names = [category["name"] for category in self.rules.categories]
        if self.rules.fallback not in names:
            names.append(self.rules.fallback)
        self.names = names
        self.llm = None
        if mode == "llm":
            self.llm = LlmClassifier(llm_options or {}, names, self.rules.fallback, log=log)

    @property
    def fallback(self):
        return self.rules.fallback

    def llm_category(self, bookmark, llm_cache=None):
        """Return a still-valid LLM verdict for this bookmark, if there is one.

        The LLM cache is keyed on metadata only (title/description/tags/url), so
        fetching the article text later does not throw the verdict away.
        """
        if not llm_cache:
            return None
        entry = llm_cache.get(str(bookmark.get("id")))
        if not entry:
            return None
        if entry.get("hash") != content_hash(bookmark, ""):
            return None
        return entry.get("category") or None

    def assign(self, bookmark, body="", cache=None, folder=None, llm_cache=None):
        """Return (category, method) for a bookmark, using the cache when valid."""
        identifier = bookmark.get("id")
        from_llm = self.llm_category(bookmark, llm_cache)
        if from_llm:
            return from_llm, "llm"

        fingerprint = content_hash(bookmark, body)
        if cache is not None:
            cached = cache.get(str(identifier))
            if cached and cached.get("hash") == fingerprint:
                return cached.get("category") or self.fallback, cached.get("method") or "cache"

        signals = signals_for(bookmark, body, folder=folder)
        best, score = self.rules.classify(signals)
        category = best["name"] if best else self.fallback
        method = "rules" if best else "fallback"
        if cache is not None:
            cache[str(identifier)] = {"hash": fingerprint, "category": category, "method": method}
        return category, method

    def refine_with_llm(self, bookmarks, bodies, cache=None, llm_cache=None, scope=None,
                        progress=None):
        """One batched LLM pass over the bookmarks the rules could not place.

        ``scope="all"`` re-asks the model about every bookmark instead of only
        the fallback ones -- more expensive, but it lets the model fix
        rule-based misplacements rather than just filling holes.
        """
        if not self.llm:
            return {}
        if not self.llm.available():
            self.log(f"  {self.llm.provider} not usable at {self.llm.base_url}; skipping LLM pass")
            return {}

        scope = (scope or self.llm.scope or "fallback").lower()
        candidates = []
        for bookmark in bookmarks:
            identifier = str(bookmark.get("id"))
            if self.llm_category(bookmark, llm_cache):
                continue
            current = (cache or {}).get(identifier, {}).get("category", self.fallback)
            if scope != "all" and current != self.fallback:
                continue
            candidates.append(bookmark)
        if not candidates:
            self.log("  llm pass: nothing left to categorize")
            return {}

        self.log(
            f"  llm pass over {len(candidates)} bookmarks ({self.llm.provider}/{self.llm.model}, "
            f"scope {scope})"
        )
        items = []
        for bookmark in candidates:
            identifier = bookmark.get("id")
            items.append({
                "id": identifier,
                "title": bookmark.get("title"),
                "description": bookmark.get("description"),
                "tags": [str(t.get("name")) for t in bookmark.get("tags") or [] if isinstance(t, dict)],
                "domain": domain_of(bookmark.get("url")),
                "body": bodies.get(identifier) or "",
            })

        self.llm.progress = progress
        try:
            assigned = self.llm.classify(items)
        finally:
            self.llm.progress = None
        for bookmark in candidates:
            identifier = bookmark.get("id")
            category = assigned.get(identifier)
            if not category:
                continue
            keep = category == self.fallback and scope != "all"
            if llm_cache is not None and not keep:
                llm_cache[str(identifier)] = {
                    "category": category,
                    "hash": content_hash(bookmark, ""),
                }
            if cache is None:
                continue
            entry = cache.get(str(identifier)) or {}
            entry["category"] = category
            entry["method"] = "llm"
            cache[str(identifier)] = entry
        return assigned
