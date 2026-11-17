"""One-time OAuth 2 authorization-code login.

Instapaper issues bearer tokens through the standard authorization code flow.
This walks through it on 127.0.0.1 so the token never has to be copy-pasted:

    python3 -m instapaper_sync.login --client-id ... --client-secret ...

Prerequisites: the callback URI (default http://127.0.0.1:8765/callback) must be
listed on the application's edit page, and the flow must finish in a browser that
is signed in to instapaper.com.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

AUTHORIZE_URL = "https://www.instapaper.com/oauth2/authorize"
TOKEN_URL = "https://www.instapaper.com/oauth2/token"
ME_URL = "https://www.instapaper.com/api/2/me"


class LoginError(RuntimeError):
    pass


def _post_form(url: str, data: dict, timeout: float = 60.0) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise LoginError(f"token exchange failed ({exc.code}): {detail}") from exc


def _verify(token: str, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(ME_URL, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LoginError(f"the new token was rejected ({exc.code})") from exc


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}
    done = threading.Event()

    def do_GET(self):  # noqa: N802 - required name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.redirect_path:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {key: values[0] for key, values in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in params:
            message = "Authorized. You can close this tab and go back to the terminal."
        else:
            message = "Authorization was not granted. You can close this tab."
        self.wfile.write(
            f"<!doctype html><meta charset=utf-8><title>Instapaper</title>"
            f"<body style='font:16px/1.5 -apple-system,sans-serif;margin:4rem auto;max-width:32rem'>"
            f"<h1 style='font-size:1.25rem'>{message}</h1></body>".encode()
        )
        _CallbackHandler.done.set()

    def log_message(self, *args):  # keep the terminal quiet
        pass


def login(
    client_id: str,
    client_secret: str,
    redirect_uri: str = "http://127.0.0.1:8765/callback",
    port: int = 8765,
    open_browser: bool = True,
    timeout: float = 300.0,
) -> dict:
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )
    authorize = f"{AUTHORIZE_URL}?{query}"

    _CallbackHandler.redirect_path = urllib.parse.urlparse(redirect_uri).path or "/"
    _CallbackHandler.result = {}
    _CallbackHandler.done = threading.Event()

    server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Open this URL and approve access (must be signed in to instapaper.com):\n")
    print(f"  {authorize}\n")
    if open_browser:
        try:
            webbrowser.open(authorize)
        except Exception:
            pass
    print(f"Waiting for the redirect to {redirect_uri} ...")

    try:
        if not _CallbackHandler.done.wait(timeout):
            raise LoginError(f"timed out after {int(timeout)}s without a redirect")
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if "error" in result:
        raise LoginError(
            f"authorization failed: {result.get('error')} "
            f"({result.get('error_description', 'no description')})"
        )
    code = result.get("code")
    if not code:
        raise LoginError("no authorization code in the redirect")
    if result.get("state") and result["state"] != state:
        raise LoginError("state mismatch in the redirect; refusing to continue")

    payload = _post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    token = payload.get("access_token")
    if not token:
        raise LoginError(f"no access_token in the response: {payload}")
    user = _verify(token)
    return {"token": token, "user": user, "account": payload.get("user")}


def save_token(token: str, path: str) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp"
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token.strip() + "\n")
    os.replace(tmp, path)
    return path


def _client_from_file(path: str) -> tuple:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return "", ""
    return str(data.get("client_id") or ""), str(data.get("client_secret") or "")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m instapaper_sync.login",
        description="Obtain an Instapaper API v2 bearer token via the OAuth 2 code flow.",
    )
    parser.add_argument("--client-id", default=os.environ.get("INSTAPAPER_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("INSTAPAPER_CLIENT_SECRET", ""))
    parser.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", default="~/.config/instapaper/token")
    parser.add_argument("--client-file", default="~/.config/instapaper/client.json")
    parser.add_argument("--no-browser", action="store_true", help="print the URL instead of opening it")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    client_id, client_secret = args.client_id, args.client_secret
    if not (client_id and client_secret):
        client_id, client_secret = _client_from_file(os.path.expanduser(args.client_file))
    if not (client_id and client_secret):
        print(
            "Need the application's client ID and secret:\n"
            "  python3 -m instapaper_sync.login --client-id KEY --client-secret SECRET\n"
            "or put them in " + args.client_file + ' as {"client_id": "...", "client_secret": "..."}',
            file=sys.stderr,
        )
        return 2

    try:
        outcome = login(
            client_id,
            client_secret,
            redirect_uri=args.redirect_uri,
            port=args.port,
            open_browser=not args.no_browser,
            timeout=args.timeout,
        )
    except LoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130

    path = save_token(outcome["token"], os.path.expanduser(args.token_file))
    user = outcome["user"]
    username = user.get("username") or outcome.get("account", {}).get("username") or "unknown"
    print(f"\nSigned in as {username} (id {user.get('id')}), premium={user.get('premium')}")
    print(f"Token saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
