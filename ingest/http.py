"""A small HTTP client: timeouts, retries with backoff, and secret redaction.

Every worker talks to the network only through HttpClient. That gives us:
  * one place for the retry policy (instead of five slightly different ones),
  * one exception family (RequestError) that workers catch,
  * an easy seam for tests, which pass a fake client instead of a real one.
"""

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

USER_AGENT = "TechFeedIngest/0.1 (learning project)"

# Temporary problems worth retrying. 4xx errors such as 400/401/403/404 mean
# "your request is wrong" — retrying would just fail again (and burn quota).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Query parameters that carry secrets. They are hidden in every error message
# so an API key never ends up in logs or terminal output.
SECRET_PARAMS = {"key", "api_key", "access_token", "client_secret", "token"}

MAX_RETRY_AFTER_SECONDS = 60


class RequestError(Exception):
    """A request failed for good: network down, timed out, bad status or bad body."""


class HttpError(RequestError):
    """The server answered with an HTTP error status."""

    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = redact(url)
        self.body = body
        super().__init__(f"HTTP {status} for {self.url}: {body[:300]}")


@dataclass(frozen=True)
class Validators:
    """What a server told us about a response, so we can ask "has it changed?" next time.

    ETag is an opaque version label ("abc123"); Last-Modified is a date string.
    Both are sent back exactly as received.
    """

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ConditionalResponse:
    body: bytes | None       # None means "304 Not Modified": nothing changed since last time
    validators: Validators   # what to send next time

    @property
    def not_modified(self) -> bool:
        return self.body is None


def redact(url: str) -> str:
    """Replace secret query-parameter values with REDACTED."""
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, "REDACTED" if key in SECRET_PARAMS else value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


class HttpClient:
    def __init__(
        self,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable = urllib.request.urlopen,
    ):
        # `sleep` and `opener` are parameters so tests can replace them:
        # no real waiting, no real network.
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._opener = opener

    def get_bytes(self, url: str, params: dict | None = None, headers: dict | None = None) -> bytes:
        return self._request(url, params=params, headers=headers)

    def get_conditional(self, url: str, validators: Validators | None = None) -> ConditionalResponse:
        """GET that can answer "not modified".

        With validators from a previous response, the server may reply 304 and
        send no body. Without them, this is a normal GET that also returns the
        validators to remember for next time.
        """
        headers = {}
        if validators and validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators and validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified

        body, response_headers = self._send(url, headers=headers, allow_not_modified=True)
        new = Validators(response_headers.get("ETag"), response_headers.get("Last-Modified"))
        if body is None and validators:
            # A 304 doesn't have to repeat the validators; keep the ones that still apply.
            new = Validators(new.etag or validators.etag, new.last_modified or validators.last_modified)
        return ConditionalResponse(body, new)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self._parse_json(self._request(url, params=params, headers=headers), url)

    def post_form(self, url: str, form: dict, headers: dict | None = None):
        """POST an application/x-www-form-urlencoded body and parse a JSON reply."""
        data = urllib.parse.urlencode(form).encode()
        body = self._request(url, headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}, data=data)
        return self._parse_json(body, url)

    def _request(self, url: str, params: dict | None = None, headers: dict | None = None, data: bytes | None = None) -> bytes:
        body, _ = self._send(url, params=params, headers=headers, data=data)
        return body

    def _send(self, url: str, params: dict | None = None, headers: dict | None = None,
              data: bytes | None = None, allow_not_modified: bool = False):
        """The retry loop. Returns (body, response headers).

        allow_not_modified: treat 304 as an answer, returning (None, headers).
        urllib raises 304 as an HTTPError, but for a conditional GET it means
        "unchanged", not "failed", and it must never be retried.
        """
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        all_headers = {"User-Agent": USER_AGENT, **(headers or {})}

        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(url, data=data, headers=all_headers)
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    return _decode_body(response.read(), response.headers, url), response.headers
            except urllib.error.HTTPError as error:  # must come before URLError (it's a subclass)
                if error.code == 304 and allow_not_modified:
                    return None, (error.headers or {})
                body = _read_error_body(error)
                if error.code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                    raise HttpError(error.code, url, body) from None
                delay = _retry_after(error) or self._backoff(attempt)
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == self.max_attempts:
                    reason = getattr(error, "reason", error)
                    raise RequestError(f"could not reach {redact(url)}: {reason}") from None
                delay = self._backoff(attempt)
            self._sleep(delay)

        raise AssertionError("unreachable")  # the loop always returns or raises

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff: 1s, 2s, 4s, ... so a struggling server gets breathing room."""
        return self.backoff_seconds * 2 ** (attempt - 1)

    @staticmethod
    def _parse_json(body: bytes, url: str):
        try:
            return json.loads(body)
        except ValueError:
            raise RequestError(f"response from {redact(url)} is not valid JSON") from None


def _decode_body(body: bytes, headers, url: str) -> bytes:
    """Undo Content-Encoding: gzip.

    We never ask for compression (no Accept-Encoding header), but some servers
    compress anyway: deepmind.google does for our User-Agent. urllib doesn't
    decompress on its own, so the caller would get binary data instead of XML.
    """
    if (headers.get("Content-Encoding") or "").strip().lower() != "gzip":
        return body
    try:
        return gzip.decompress(body)
    except (OSError, EOFError):  # gzip.BadGzipFile is an OSError
        raise RequestError(f"response from {redact(url)} says gzip but isn't valid gzip") from None


def _read_error_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """Honour a numeric Retry-After header (servers send it with 429/503)."""
    value = error.headers.get("Retry-After") if error.headers else None
    if value and value.strip().isdigit():
        return min(float(value), MAX_RETRY_AFTER_SECONDS)
    return None
