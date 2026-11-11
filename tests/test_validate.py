import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ingest.models import ContentItem, ContentType
from ingest.validate import validate

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

GOOD = ContentItem(
    source="youtube",
    source_item_id="abc123",
    content_type=ContentType.VIDEO,
    title="Design a rate limiter",
    url="https://www.youtube.com/watch?v=abc123",
    published_at=datetime(2024, 5, 1, tzinfo=UTC),
    duration_seconds=600,
)


class ValidateTests(unittest.TestCase):
    def assertRejectedFor(self, item, fragment):
        reasons = validate(item, now=NOW)
        self.assertTrue(any(fragment in r for r in reasons), f"expected a reason containing {fragment!r}, got {reasons}")

    def test_good_item_has_no_reasons(self):
        self.assertEqual(validate(GOOD, now=NOW), [])

    def test_empty_title(self):
        self.assertRejectedFor(replace(GOOD, title=""), "title is empty")
    
    

    def test_overlong_title(self):
        self.assertRejectedFor(replace(GOOD, title="x" * 501), "longer than")

    def test_overlong_description(self):
        self.assertRejectedFor(replace(GOOD, description = "x" * 5001), "description")

    def test_description_at_limit_is_allowed(self):
        self.assertEqual(validate(replace(GOOD, description ="x" * 5000), now = NOW), [])

    def test_non_http_urls(self):
        for bad in ("", "not a url", "ftp://example.com/file", "/relative/path"):
            with self.subTest(url=bad):
                self.assertRejectedFor(replace(GOOD, url=bad), "url is not")

    def test_missing_date(self):
        self.assertRejectedFor(replace(GOOD, published_at=None), "missing")

    def test_naive_date(self):
        self.assertRejectedFor(replace(GOOD, published_at=datetime(2024, 5, 1)), "no timezone")

    def test_future_date_beyond_tolerance(self):
        self.assertRejectedFor(replace(GOOD, published_at=NOW + timedelta(days=2)), "future")
        self.assertEqual(validate(replace(GOOD, published_at=NOW + timedelta(hours=1)), now=NOW), [])

    def test_ancient_date(self):
        self.assertRejectedFor(replace(GOOD, published_at=datetime(1970, 1, 1, tzinfo=UTC)), "before 1990")

    def test_negative_duration(self):
        self.assertRejectedFor(replace(GOOD, duration_seconds=-1), "negative")

    def test_reports_every_problem_at_once(self):
        reasons = validate(replace(GOOD, title="", url="nope", published_at=None), now=NOW)
        self.assertEqual(len(reasons), 3)


if __name__ == "__main__":
    unittest.main()
