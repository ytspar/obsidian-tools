import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync.api import ApiError, Instapaper  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    routes = {}
    calls = []

    def do_GET(self):
        parsed = urlparse(self.path)
        Handler.calls.append((parsed.path, parse_qs(parsed.query), self.headers.get("Authorization")))
        route = Handler.routes.get(parsed.path)
        if callable(route):
            status, payload = route(parse_qs(parsed.query), len(Handler.calls))
        elif route is not None:
            status, payload = route
        else:
            status, payload = 404, {"message": "not found"}
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.routes = {}
        Handler.calls = []
        self.client = Instapaper("tok", base_url=self.base, delay=0, max_retries=2)

    def test_pagination_walks_every_page(self):
        pages = {
            "0": {"bookmarks": [{"id": 1}, {"id": 2}], "total": 5},
            "2": {"bookmarks": [{"id": 3}, {"id": 4}], "total": 5},
            "4": {"bookmarks": [{"id": 5}], "total": 5},
        }
        Handler.routes["/bookmarks"] = lambda query, _n: (200, pages[query.get("offset", ["0"])[0]])
        ids = [bookmark["id"] for bookmark in self.client.iter_bookmarks(page_size=2)]
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(len(Handler.calls), 3)

    def test_since_mode_reports_deletions(self):
        Handler.routes["/bookmarks"] = (
            200,
            {"bookmarks": [{"id": 9}], "deleted_ids": [8, 7], "total": 3},
        )
        pages = list(self.client.iter_bookmark_pages(since=1700000000, page_size=500))
        self.assertEqual(pages[0][1], [8, 7])
        query = Handler.calls[0][1]
        self.assertEqual(query["since"], ["1700000000"])
        self.assertNotIn("section", query)

    def test_authorization_header_is_sent(self):
        Handler.routes["/me"] = (200, {"id": 1, "username": "a@b.c", "premium": False})
        self.client.me()
        self.assertEqual(Handler.calls[-1][2], "Bearer tok")

    def test_retry_on_429_then_success(self):
        def flaky(_query, attempt):
            if attempt == 1:
                return 429, {"message": "slow down"}
            return 200, {"id": 1, "username": "a@b.c", "premium": True}

        Handler.routes["/me"] = flaky
        self.assertEqual(self.client.me()["id"], 1)
        self.assertEqual(len(Handler.calls), 2)

    def test_client_error_is_not_retried(self):
        Handler.routes["/bookmarks/1/parse"] = (400, {"message": "Parsed content unavailable"})
        with self.assertRaises(ApiError) as caught:
            self.client.parse(1)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("Parsed content unavailable", str(caught.exception))
        self.assertEqual(len(Handler.calls), 1)

    def test_parse_gateway_timeout_is_not_retried(self):
        Handler.routes["/bookmarks/7/parse"] = (504, {"message": "gateway timeout"})
        with self.assertRaises(ApiError) as caught:
            self.client.parse(7)
        self.assertEqual(caught.exception.status, 504)
        self.assertEqual(len(Handler.calls), 1, "a parser timeout is not worth retrying")

    def test_parse_makes_a_single_attempt_when_throttled(self):
        Handler.routes["/bookmarks/7/parse"] = (429, {"message": "slow down"})
        with self.assertRaises(ApiError) as caught:
            self.client.parse(7)
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(Handler.calls), 1, "a throttled parse is retried by the next run")

    def test_unauthorized_raises_immediately(self):
        Handler.routes["/me"] = (401, {"message": "invalid token"})
        with self.assertRaises(ApiError) as caught:
            self.client.me()
        self.assertEqual(caught.exception.status, 401)

    def test_empty_body_returns_empty_dict(self):
        Handler.routes["/folders"] = (200, {})
        self.assertEqual(self.client.folders(), [])


if __name__ == "__main__":
    unittest.main()
