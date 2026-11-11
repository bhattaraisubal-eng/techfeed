import email.message
import gzip
import io
import unittest
import urllib.error

from ingest.http import HttpClient, HttpError, RequestError, Validators, redact


class FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = email.message.Message()   # same .get() interface as a real response's headers
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, headers: dict | None = None, body: bytes = b'{"error": "nope"}'):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://example.com", code, "error", message, io.BytesIO(body))


class ScriptedOpener:
    """Replaces urlopen: each call returns (or raises) the next scripted outcome."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if isinstance(outcome, FakeResponse) else FakeResponse(outcome)


class HttpClientTests(unittest.TestCase):
    def make_client(self, opener):
        self.sleeps = []
        return HttpClient(opener=opener, sleep=self.sleeps.append)

    def test_retries_temporary_failure_then_succeeds(self):
        opener = ScriptedOpener(http_error(503), b'{"ok": true}')
        self.assertEqual(self.make_client(opener).get_json("https://example.com/x"), {"ok": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_backoff_doubles_and_gives_up_after_max_attempts(self):
        opener = ScriptedOpener(TimeoutError(), TimeoutError(), TimeoutError())
        with self.assertRaises(RequestError):
            self.make_client(opener).get_bytes("https://example.com/x")
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_honours_retry_after_header(self):
        opener = ScriptedOpener(http_error(429, {"Retry-After": "5"}), b"{}")
        self.make_client(opener).get_json("https://example.com/x")
        self.assertEqual(self.sleeps, [5.0])

    def test_client_errors_are_not_retried(self):
        opener = ScriptedOpener(http_error(403))
        with self.assertRaises(HttpError) as caught:
            self.make_client(opener).get_json("https://example.com/x", params={"key": "SECRET123"})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn("SECRET123", str(caught.exception))

    def test_gzip_body_is_decompressed(self):
        # deepmind.google sends gzip even when we don't ask for it, and says so
        # in Content-Encoding. Without this, the XML parser receives binary data.
        compressed = FakeResponse(gzip.compress(b"<rss/>"), {"Content-Encoding": "gzip"})
        self.assertEqual(self.make_client(ScriptedOpener(compressed)).get_bytes("https://example.com/feed"), b"<rss/>")

    def test_body_without_content_encoding_is_left_alone(self):
        self.assertEqual(self.make_client(ScriptedOpener(b"<rss/>")).get_bytes("https://example.com/feed"), b"<rss/>")

    def test_corrupt_gzip_is_a_request_error(self):
        broken = FakeResponse(b"not really gzip", {"Content-Encoding": "gzip"})
        with self.assertRaisesRegex(RequestError, "gzip"):
            self.make_client(ScriptedOpener(broken)).get_bytes("https://example.com/feed")

    def test_conditional_get_sends_validators_and_returns_new_ones(self):
        ok = FakeResponse(b"<rss/>", {"ETag": '"v2"', "Last-Modified": "Wed, 11 Nov 2026 10:00:00 GMT"})
        opener = ScriptedOpener(ok)
        response = self.make_client(opener).get_conditional(
            "https://example.com/feed", Validators('"v1"', "Tue, 10 Nov 2026 10:00:00 GMT"))
        sent = opener.requests[0]
        self.assertEqual(sent.get_header("If-none-match"), '"v1"')    # urllib stores header names as "If-none-match"
        self.assertEqual(sent.get_header("If-modified-since"), "Tue, 10 Nov 2026 10:00:00 GMT")
        self.assertFalse(response.not_modified)
        self.assertEqual(response.body, b"<rss/>")
        self.assertEqual(response.validators, Validators('"v2"', "Wed, 11 Nov 2026 10:00:00 GMT"))

    def test_304_means_not_modified_and_is_not_retried(self):
        opener = ScriptedOpener(http_error(304, body=b""))
        response = self.make_client(opener).get_conditional("https://example.com/feed", Validators('"v1"'))
        self.assertTrue(response.not_modified)
        self.assertIsNone(response.body)
        self.assertEqual(response.validators.etag, '"v1"')   # kept, since the 304 didn't repeat it
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(self.sleeps, [])

    def test_first_conditional_get_sends_no_conditions(self):
        opener = ScriptedOpener(FakeResponse(b"<rss/>", {"ETag": '"v1"'}))
        response = self.make_client(opener).get_conditional("https://example.com/feed")
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))
        self.assertEqual(response.validators.etag, '"v1"')

    def test_304_on_a_normal_get_is_still_an_error(self):
        # Only get_conditional asked "has it changed?"; anywhere else a 304 is unexpected.
        with self.assertRaises(HttpError):
            self.make_client(ScriptedOpener(http_error(304, body=b""))).get_bytes("https://example.com/feed")

    def test_invalid_json_is_a_request_error(self):
        opener = ScriptedOpener(b"<html>not json</html>")
        with self.assertRaises(RequestError):
            self.make_client(opener).get_json("https://example.com/x")

    def test_sends_user_agent(self):
        opener = ScriptedOpener(b"{}")
        self.make_client(opener).get_json("https://example.com/x")
        self.assertIn("TechFeedIngest", opener.requests[0].get_header("User-agent"))

    def test_redact_hides_secret_params_only(self):
        result = redact("https://api.example.com/v1?id=42&key=SECRET&access_token=T0K")
        self.assertNotIn("SECRET", result)
        self.assertNotIn("T0K", result)
        self.assertIn("id=42", result)


if __name__ == "__main__":
    unittest.main()
