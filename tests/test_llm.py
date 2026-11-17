"""Tests for the LLM categorization provider (DeepSeek / OpenAI-compatible / Ollama)."""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import categorize  # noqa: E402

CATEGORIES = ["Essays & Ideas", "Money & Investing", "Unsorted"]


class FakeModel:
    """A tiny HTTP server standing in for the chat completions endpoint."""

    def __init__(self, replies=None, statuses=None):
        self.requests = []
        self.replies = list(replies or [])
        self.statuses = list(statuses or [])
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), None))
                if self.path.endswith("/models"):
                    self._reply(200, {"data": [{"id": "fake"}]})
                else:
                    self._reply(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(("POST", self.path, dict(self.headers), payload))
                status = outer.statuses.pop(0) if outer.statuses else 200
                if status != 200:
                    self._reply(status, {"error": "nope"}, {"Retry-After": "0"})
                    return
                content = outer.replies.pop(0) if outer.replies else '{"results": []}'
                if self.path.endswith("/api/chat"):
                    self._reply(200, {"message": {"content": content}})
                else:
                    self._reply(200, {"choices": [{"message": {"content": content}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def bookmark(identifier, title, description="", url="https://example.com/x", tags=None):
    return {
        "id": identifier,
        "title": title,
        "description": description,
        "url": url,
        "tags": [{"name": t} for t in (tags or [])],
    }


class ProviderPresets(unittest.TestCase):
    def test_deepseek_preset_is_openai_shaped(self):
        llm = categorize.LlmClassifier({"provider": "deepseek"}, CATEGORIES, "Unsorted")
        self.assertEqual(llm.style, "openai")
        self.assertEqual(llm.base_url, "https://api.deepseek.com")
        self.assertEqual(llm.model, "deepseek-chat")
        self.assertEqual(llm.needed_key, "DEEPSEEK_API_KEY")
        self.assertEqual(llm._endpoint(), "https://api.deepseek.com/chat/completions")

    def test_ollama_stays_the_default(self):
        llm = categorize.LlmClassifier({}, CATEGORIES, "Unsorted")
        self.assertEqual(llm.style, "ollama")
        self.assertEqual(llm._endpoint(), "http://localhost:11434/api/chat")

    def test_unknown_provider_falls_back_to_openai_wire_format(self):
        llm = categorize.LlmClassifier(
            {"provider": "llamacpp", "base_url": "http://localhost:8080/v1", "model": "local"},
            CATEGORIES, "Unsorted",
        )
        self.assertEqual(llm.style, "openai")
        self.assertEqual(llm._endpoint(), "http://localhost:8080/v1/chat/completions")


class ConfigDefaultsDoNotHijackTheProvider(unittest.TestCase):
    def test_merged_config_defaults_keep_the_deepseek_endpoint(self):
        from instapaper_sync import config as config_module

        merged = config_module._merge(
            config_module.DEFAULTS,
            {"categorize": {"llm": {"provider": "deepseek", "model": "deepseek-chat"}}},
        )
        llm = categorize.LlmClassifier(
            merged["categorize"]["llm"], CATEGORIES, "Unsorted"
        )
        self.assertEqual(llm.base_url, "https://api.deepseek.com")
        self.assertEqual(llm._endpoint(), "https://api.deepseek.com/chat/completions")


class KeyResolution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "llm-key")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("file-key\n")

    def tearDown(self):
        self.dir.cleanup()
        os.environ.pop("TEST_LLM_KEY", None)

    def test_explicit_key_wins(self):
        llm = categorize.LlmClassifier(
            {"provider": "deepseek", "api_key": "explicit", "api_key_file": self.path},
            CATEGORIES, "Unsorted",
        )
        self.assertEqual(llm.api_key, "explicit")

    def test_environment_beats_the_file(self):
        os.environ["TEST_LLM_KEY"] = "from-env"
        llm = categorize.LlmClassifier(
            {"provider": "deepseek", "api_key_env": "TEST_LLM_KEY", "api_key_file": self.path},
            CATEGORIES, "Unsorted",
        )
        self.assertEqual(llm.api_key, "from-env")

    def test_file_is_the_last_resort(self):
        llm = categorize.LlmClassifier(
            {"provider": "deepseek", "api_key_file": self.path}, CATEGORIES, "Unsorted"
        )
        self.assertEqual(llm.api_key, "file-key")

    def test_missing_key_is_empty_not_an_error(self):
        llm = categorize.LlmClassifier(
            {"provider": "deepseek", "api_key_file": os.path.join(self.dir.name, "nope")},
            CATEGORIES, "Unsorted",
        )
        self.assertEqual(llm.api_key, "")


class JsonExtraction(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(categorize._extract_json('{"results": []}'), {"results": []})

    def test_fenced_object(self):
        text = '```json\n{"results": [{"id": 1, "category": "Unsorted"}]}\n```'
        self.assertEqual(categorize._extract_json(text)["results"][0]["category"], "Unsorted")

    def test_object_buried_in_prose(self):
        text = 'Sure! Here you go:\n{"results": [{"id": 7, "category": "Essays & Ideas"}]}\nHope that helps.'
        self.assertEqual(categorize._extract_json(text)["results"][0]["id"], 7)

    def test_garbage_yields_empty_mapping(self):
        self.assertEqual(categorize._extract_json("I cannot help with that."), {})


class OpenAIWireFormat(unittest.TestCase):
    def setUp(self):
        self.reply = '{"results": [{"id": 1, "category": "Essays & Ideas", "confidence": 0.9}]}'
        self.model = FakeModel(replies=[self.reply])

    def tearDown(self):
        self.model.close()

    def classifier(self, **extra):
        options = {
            "provider": "deepseek",
            "base_url": self.model.url,
            "model": "deepseek-chat",
            "api_key": "sk-test",
            "batch_size": 5,
            "max_retries": 1,
        }
        options.update(extra)
        return categorize.LlmClassifier(options, CATEGORIES, "Unsorted")

    def test_posts_openai_style_with_bearer_auth(self):
        llm = self.classifier()
        self.assertTrue(llm.available())
        results = llm.classify([{"id": 1, "title": "Why the west is tired"}])
        self.assertEqual(results, {1: "Essays & Ideas"})

        posts = [r for r in self.model.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        method, path, headers, payload = posts[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual(headers.get("Authorization"), "Bearer sk-test")
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("Essays & Ideas", payload["messages"][0]["content"])
        self.assertEqual(json.loads(payload["messages"][1]["content"])[0]["id"], 1)

    def test_unknown_category_in_reply_is_ignored(self):
        self.model.replies[:] = ['{"results": [{"id": 1, "category": "Astrology"}]}']
        self.assertEqual(self.classifier().classify([{"id": 1}]), {})

    def test_throttling_is_retried_then_succeeds(self):
        self.model.statuses[:] = [429]
        results = self.classifier(max_retries=2).classify([{"id": 1}])
        self.assertEqual(results, {1: "Essays & Ideas"})
        self.assertEqual(len([r for r in self.model.requests if r[0] == "POST"]), 2)

    def test_batches_are_split_by_batch_size(self):
        self.model.replies[:] = [self.reply, self.reply]
        items = [{"id": n, "title": f"item {n}"} for n in range(1, 8)]
        self.classifier(batch_size=4).classify(items)
        posts = [r for r in self.model.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 2)

    def test_missing_key_reports_unavailable_without_calling_out(self):
        llm = self.classifier(api_key="", api_key_file="/nonexistent/key", api_key_env="NOPE_KEY")
        self.assertFalse(llm.available())
        self.assertEqual([r for r in self.model.requests if r[0] == "POST"], [])


class Concurrency(unittest.TestCase):
    def test_batches_can_run_at_once_and_still_all_land(self):
        reply = '{"results": [{"id": 1, "category": "Essays & Ideas"}]}'
        model = FakeModel(replies=[reply] * 6)
        try:
            llm = categorize.LlmClassifier(
                {"provider": "deepseek", "base_url": model.url, "api_key": "k",
                 "batch_size": 2, "workers": 4, "max_retries": 0},
                CATEGORIES, "Unsorted",
            )
            items = [{"id": n} for n in range(1, 13)]
            llm.classify(items)
            self.assertEqual(len([r for r in model.requests if r[0] == "POST"]), 6)
        finally:
            model.close()

    def test_progress_hook_fires_so_state_can_be_saved_mid_run(self):
        reply = '{"results": [{"id": 1, "category": "Essays & Ideas"}]}'
        model = FakeModel(replies=[reply] * 5)
        try:
            llm = categorize.LlmClassifier(
                {"provider": "deepseek", "base_url": model.url, "api_key": "k",
                 "batch_size": 5, "workers": 1, "max_retries": 0},
                CATEGORIES, "Unsorted",
            )
            calls = []
            llm.progress = lambda: calls.append(1)
            llm.classify([{"id": n} for n in range(1, 26)])
            self.assertGreaterEqual(len(calls), 1)
        finally:
            model.close()


class OllamaWireFormat(unittest.TestCase):
    def test_posts_native_chat_with_json_format(self):
        model = FakeModel(replies=['{"results": [{"id": 2, "category": "Money & Investing"}]}'])
        try:
            llm = categorize.LlmClassifier(
                {"provider": "ollama", "base_url": model.url, "model": "gpt-oss:20b"},
                CATEGORIES, "Unsorted",
            )
            results = llm.classify([{"id": 2, "title": "Bond yields"}])
            self.assertEqual(results, {2: "Money & Investing"})
            method, path, headers, payload = [r for r in model.requests if r[0] == "POST"][0]
            self.assertEqual(path, "/api/chat")
            self.assertEqual(payload["format"], "json")
            self.assertNotIn("Authorization", headers)
        finally:
            model.close()


class LlmVerdictCache(unittest.TestCase):
    def categorizer(self, mode="rules"):
        spec = {
            "fallback": "Unsorted",
            "categories": [
                {"name": "Money & Investing", "keywords": ["bond"]},
                {"name": "Essays & Ideas"},
            ],
        }
        return categorize.Categorizer(spec, mode=mode)

    def test_a_cached_verdict_is_reused_without_rules(self):
        cat = self.categorizer()
        item = bookmark(5, "Bond yields and you")
        llm_cache = {"5": {"category": "Essays & Ideas", "hash": categorize.content_hash(item, "")}}
        self.assertEqual(cat.assign(item, "", llm_cache=llm_cache), ("Essays & Ideas", "llm"))

    def test_a_stale_verdict_is_discarded_when_the_title_changes(self):
        cat = self.categorizer()
        item = bookmark(5, "Bond yields and you")
        llm_cache = {"5": {"category": "Essays & Ideas", "hash": "stale"}}
        self.assertEqual(cat.assign(item, "", llm_cache=llm_cache), ("Money & Investing", "rules"))

    def test_verdict_survives_downloading_the_article_text(self):
        cat = self.categorizer()
        item = bookmark(5, "Bond yields and you")
        llm_cache = {"5": {"category": "Essays & Ideas", "hash": categorize.content_hash(item, "")}}
        self.assertEqual(cat.assign(item, "x" * 5000, llm_cache=llm_cache), ("Essays & Ideas", "llm"))

    def test_scope_all_asks_about_items_the_rules_already_placed(self):
        cat = self.categorizer(mode="llm")
        asked = []

        class Recorder:
            provider = "fake"
            model = "fake"
            scope = "all"

            def available(self):
                return True

            def classify(self, items):
                asked.extend(item["id"] for item in items)
                return {item["id"]: "Essays & Ideas" for item in items}

        cat.llm = Recorder()
        items = [bookmark(1, "Bond yields"), bookmark(2, "Something else entirely")]
        cat.refine_with_llm(items, {}, cache={}, llm_cache={}, scope="all")
        self.assertEqual(sorted(asked), [1, 2])

    def test_scope_fallback_only_asks_about_unplaced_items(self):
        cat = self.categorizer(mode="llm")
        asked = []
        cache = {"1": {"category": "Money & Investing", "method": "rules"}}
        rules = cat.assign(bookmark(2, "Something else entirely"), "", cache=cache)

        class Recorder:
            provider = "fake"
            model = "fake"
            scope = "fallback"

            def available(self):
                return True

            def classify(self, items):
                asked.extend(item["id"] for item in items)
                return {}

        cat.llm = Recorder()
        items = [bookmark(1, "Bond yields"), bookmark(2, "Something else entirely")]
        cat.refine_with_llm(items, {}, cache=cache, llm_cache={}, scope="fallback")
        self.assertEqual(asked, [2])
        self.assertEqual(rules[0], "Unsorted")

    def test_a_verdict_is_written_back_to_the_llm_cache(self):
        cat = self.categorizer(mode="llm")

        class Recorder:
            provider = "fake"
            model = "fake"
            scope = "all"

            def available(self):
                return True

            def classify(self, items):
                return {item["id"]: "Essays & Ideas" for item in items}

        cat.llm = Recorder()
        item = bookmark(9, "Some essay")
        llm_cache = {}
        cat.refine_with_llm([item], {}, cache={}, llm_cache=llm_cache, scope="all")
        self.assertEqual(llm_cache["9"]["category"], "Essays & Ideas")
        self.assertEqual(llm_cache["9"]["hash"], categorize.content_hash(item, ""))

    def test_already_verdicted_items_are_not_asked_again(self):
        cat = self.categorizer(mode="llm")
        asked = []
        item = bookmark(4, "Some essay")
        llm_cache = {"4": {"category": "Essays & Ideas", "hash": categorize.content_hash(item, "")}}

        class Recorder:
            provider = "fake"
            model = "fake"
            scope = "all"

            def available(self):
                return True

            def classify(self, items):
                asked.extend(item["id"] for item in items)
                return {}

        cat.llm = Recorder()
        cat.refine_with_llm([item], {}, cache={}, llm_cache=llm_cache, scope="all")
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
