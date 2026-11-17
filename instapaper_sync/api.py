"""Minimal, dependency-free client for the Instapaper API v2.

Docs: https://www.instapaper.com/developers/v2/reference/bookmarks
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://www.instapaper.com/api/2"
USER_AGENT = "obsidian-tools/instapaper-sync (personal archive)"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
# Instapaper's parser gives up on a page with a gateway timeout (504).  Retrying
# that costs a minute of waiting for an answer that will not change, so parse
# requests only retry on throttling.  A 504 is reported as a permanent miss.
PARSE_RETRY_STATUS = {408, 429}


class ApiError(RuntimeError):
    """A non-retryable API failure."""

    def __init__(self, status, message, payload=None):
        self.status = status
        self.payload = payload
        prefix = f"HTTP {status}: " if status else ""
        super().__init__(prefix + str(message))


class Instapaper:
    def __init__(
        self,
        token,
        base_url=BASE_URL,
        delay=0.3,
        timeout=120,
        max_retries=6,
        parse_timeout=45,
        log=None,
    ):
        if not token:
            raise ValueError("an access token is required")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.delay = float(delay)
        self.timeout = timeout
        self.max_retries = max_retries
        self.parse_timeout = parse_timeout
        self.log = log or (lambda *a, **k: None)
        self.requests = 0

    # ---------------------------------------------------------------- transport

    def request(
        self,
        method,
        path,
        params=None,
        payload=None,
        retries=None,
        timeout=None,
        retry_status=None,
    ):
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        retries = self.max_retries if retries is None else retries
        timeout = self.timeout if timeout is None else timeout
        retry_status = RETRY_STATUS if retry_status is None else retry_status

        last_error = None
        for attempt in range(retries + 1):
            if attempt:
                wait = min(2 ** attempt, 60)
                self.log(f"retrying {method} {path} in {wait}s ({last_error})")
                time.sleep(wait)
            elif self.delay:
                time.sleep(self.delay)

            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read()
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                continue

            if 200 <= status < 300:
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    raise ApiError(status, "response was not valid JSON", raw[:400])

            message, parsed = self._error_message(raw, status)
            if status in retry_status and attempt < retries:
                last_error = message
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), 300))
                    except ValueError:
                        pass
                continue
            raise ApiError(status, message, parsed)

        raise ApiError(None, f"giving up after {retries + 1} attempts: {last_error}")

    @staticmethod
    def _error_message(raw, status):
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw[:300].decode("utf-8", "replace") or f"HTTP {status}", None
        if isinstance(parsed, dict):
            for key in ("message", "error_description", "error"):
                if isinstance(parsed.get(key), str):
                    return parsed[key], parsed
        return json.dumps(parsed)[:300], parsed

    # ------------------------------------------------------------------ account

    def me(self):
        return self.request("GET", "/me")

    # ---------------------------------------------------------------- bookmarks

    def list_bookmarks(self, section=None, folder_id=None, tag=None, limit=500, offset=0, since=None):
        return self.request(
            "GET",
            "/bookmarks",
            params={
                "section": section,
                "folder_id": folder_id,
                "tag": tag,
                "limit": limit,
                "offset": offset,
                "since": since,
            },
        )

    def iter_bookmark_pages(self, section=None, folder_id=None, tag=None, page_size=500, since=None):
        """Yield (bookmarks, deleted_ids, total) until the section is exhausted."""
        offset = 0
        while True:
            page = self.list_bookmarks(
                section=None if since else section,
                folder_id=None if since else folder_id,
                tag=None if since else tag,
                limit=page_size,
                offset=offset,
                since=since,
            )
            bookmarks = page.get("bookmarks") or []
            deleted = page.get("deleted_ids") or []
            total = page.get("total")
            received = len(bookmarks) + len(deleted)
            yield bookmarks, deleted, total

            offset += received
            if received == 0:
                return
            if isinstance(total, int) and offset >= total:
                return
            if received < page_size:
                return

    def iter_bookmarks(self, section=None, folder_id=None, tag=None, page_size=500):
        for bookmarks, _deleted, _total in self.iter_bookmark_pages(
            section=section, folder_id=folder_id, tag=tag, page_size=page_size
        ):
            for bookmark in bookmarks:
                yield bookmark

    # ------------------------------------------------------------------- extras

    def parse(self, bookmark_id, use_cache=True, timeout=None, retries=0):
        """Fetch Instapaper's cleaned article body.

        The parser fetches the page upstream, so a dead or hanging source costs
        the whole timeout and then answers 504 (or nothing at all).  One attempt
        and a short timeout keep a bad article from blocking the run; the caller
        decides whether a failure is worth remembering.
        """
        return self.request(
            "GET",
            f"/bookmarks/{bookmark_id}/parse",
            params=None if use_cache else {"use_cache": 0},
            timeout=timeout or getattr(self, "parse_timeout", 45),
            retries=retries,
            retry_status=PARSE_RETRY_STATUS,
        )

    def highlights(self, bookmark_id):
        data = self.request("GET", f"/bookmarks/{bookmark_id}/highlights")
        return data.get("highlights") or []

    def folders(self):
        return self.request("GET", "/folders").get("folders") or []

    def tags(self):
        return self.request("GET", "/tags").get("tags") or []

    # -------------------------------------------------------------- convenience

    def all_sections(self, page_size=500, log=None):
        """Every bookmark on the account: home, archive, liked and every folder."""
        log = log or self.log
        seen = set()
        results = []

        def collect(label, **kwargs):
            count = 0
            for bookmark in self.iter_bookmarks(page_size=page_size, **kwargs):
                identifier = bookmark.get("id")
                if identifier in seen:
                    continue
                seen.add(identifier)
                results.append(bookmark)
                count += 1
            log(f"  {label}: {count} new")

        collect("home")
        collect("archive", section="archive")
        collect("liked", section="liked")
        for folder in self.folders():
            if folder.get("count"):
                collect(f"folder:{folder.get('title')}", folder_id=folder.get("id"))
        return results
