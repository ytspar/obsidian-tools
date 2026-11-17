import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instapaper_sync import login as login_module  # noqa: E402


class FakeOAuth:
    """Local stand-in for the Instapaper token and /me endpoints."""

    def __init__(self):
        self.requests = []
        self.fail = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                outer.requests.append(("POST", self.path, form))
                if outer.fail:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error":"invalid_grant"}')
                    return
                payload = {
                    "token_type": "Bearer",
                    "access_token": "token-123",
                    "user": {"id": 7, "username": "me@example.com"},
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, {}))
                authorized = self.headers.get("Authorization") == "Bearer token-123"
                body = json.dumps(
                    {"id": 7, "username": "me@example.com", "premium": True}
                ).encode()
                self.send_response(200 if authorized else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def hit(url, attempts=50):
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            return exc.read()
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"could not reach {url}")


class LoginTests(unittest.TestCase):
    callback_port = 8766

    def setUp(self):
        self.callback_url = f"http://127.0.0.1:{self.callback_port}/callback"
        self.saved = (login_module.TOKEN_URL, login_module.ME_URL)

    def tearDown(self):
        login_module.TOKEN_URL, login_module.ME_URL = self.saved

    def run_login(self, oauth, expect_error=None, state="state-1", reply=None):
        login_module.TOKEN_URL = f"http://127.0.0.1:{oauth.port}/oauth2/token"
        login_module.ME_URL = f"http://127.0.0.1:{oauth.port}/api/2/me"

        outcome = {}
        original = login_module.secrets.token_urlsafe
        login_module.secrets.token_urlsafe = lambda _n: state
        try:
            def worker():
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        outcome["value"] = login_module.login(
                            "client-1",
                            "secret-1",
                            redirect_uri=self.callback_url,
                            port=self.callback_port,
                            open_browser=False,
                            timeout=5,
                        )
                except Exception as exc:  # captured for the assertions below
                    outcome["error"] = exc

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            query = reply if reply is not None else f"code=auth-code&state={state}"
            hit(f"{self.callback_url}?{query}")
            thread.join(10)
        finally:
            login_module.secrets.token_urlsafe = original

        if expect_error:
            self.assertIsInstance(outcome.get("error"), expect_error)
            return outcome.get("error")
        self.assertNotIn("error", outcome, outcome.get("error"))
        return outcome["value"]

    def test_full_flow_returns_verified_token(self):
        with FakeOAuth() as oauth:
            outcome = self.run_login(oauth)
        self.assertEqual(outcome["token"], "token-123")
        self.assertEqual(outcome["user"]["username"], "me@example.com")
        self.assertTrue(outcome["user"]["premium"])
        posts = [r for r in oauth.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        form = posts[0][2]
        self.assertEqual(form["code"], ["auth-code"])
        self.assertEqual(form["redirect_uri"], [self.callback_url])
        self.assertEqual(form["client_secret"], ["secret-1"])

    def test_state_mismatch_is_refused(self):
        with FakeOAuth() as oauth:
            self.run_login(
                oauth,
                expect_error=login_module.LoginError,
                state="expected",
                reply="code=auth-code&state=something-else",
            )
        self.assertEqual([r for r in oauth.requests if r[0] == "POST"], [])

    def test_denied_authorization_raises(self):
        with FakeOAuth() as oauth:
            error = self.run_login(
                oauth,
                expect_error=login_module.LoginError,
                reply="error=access_denied&error_description=user+denied",
            )
        self.assertIn("access_denied", str(error))

    def test_token_endpoint_failure_raises(self):
        with FakeOAuth() as oauth:
            oauth.fail = True
            self.run_login(oauth, expect_error=login_module.LoginError)

    def test_unrelated_path_is_ignored(self):
        with FakeOAuth() as oauth:
            login_module.TOKEN_URL = f"http://127.0.0.1:{oauth.port}/oauth2/token"
            login_module.ME_URL = f"http://127.0.0.1:{oauth.port}/api/2/me"
            original = login_module.secrets.token_urlsafe
            login_module.secrets.token_urlsafe = lambda _n: "state-1"
            outcome = {}
            try:
                def worker():
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            outcome["value"] = login_module.login(
                                "id", "secret",
                                redirect_uri=self.callback_url,
                                port=self.callback_port,
                                open_browser=False,
                                timeout=5,
                            )
                    except Exception as exc:
                        outcome["error"] = exc

                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                hit(f"http://127.0.0.1:{self.callback_port}/favicon.ico")
                # the real callback still completes the flow afterwards
                hit(f"{self.callback_url}?code=auth-code&state=state-1")
                thread.join(10)
            finally:
                login_module.secrets.token_urlsafe = original
        self.assertEqual(outcome["value"]["token"], "token-123")


class TokenFileTests(unittest.TestCase):
    def test_save_token_restricts_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "token")
            saved = login_module.save_token("  token-abc\n", path)
            self.assertEqual(saved, path)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "token-abc\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_client_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "client.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"client_id": "key", "client_secret": "sec"}, handle)
            self.assertEqual(login_module._client_from_file(path), ("key", "sec"))

    def test_client_from_missing_file(self):
        self.assertEqual(login_module._client_from_file("/nope/client.json"), ("", ""))


if __name__ == "__main__":
    unittest.main()
