"""Tests for Worker.run(): the stage wiring shared by every source."""

import unittest
from datetime import UTC, datetime

from ingest.models import ContentItem, ContentType
from ingest.workers.base import Worker


class DummyWorker(Worker):
    """A worker whose 'source' is a hard-coded list, to test run() in isolation."""

    source = "dummy"

    def __init__(self, raws):
        super().__init__(http=None)
        self.raws = raws

    def fetch(self, limit):
        self.fetch_errors.append("one feed was down")
        yield from self.raws[:limit]

    def normalize(self, raw):
        return ContentItem(
            source=self.source,
            source_item_id=raw["id"],  # KeyError if missing -> rejection
            content_type=ContentType.ARTICLE,
            title=raw.get("title", ""),
            url="https://example.com/" + raw["id"],
            published_at=datetime(2024, 5, 1, tzinfo=UTC),
        )


class RunTests(unittest.TestCase):
    def test_bad_items_are_rejected_without_stopping_the_run(self):
        worker = DummyWorker([
            {"id": "1", "title": "Good"},
            {"title": "No id"},            # normalize raises KeyError
            {"id": "3", "title": ""},      # normalizes, fails validation
            {"id": "4", "title": "Also good"},
        ])
        result = worker.run(limit=10)
        self.assertEqual([i.source_item_id for i in result.items], ["1", "4"])
        self.assertEqual(result.fetched, 4)
        self.assertEqual(len(result.rejected), 2)
        self.assertIn("could not normalize", result.rejected[0].reasons[0])
        self.assertEqual(result.rejected[1].reasons, ["title is empty"])

    def test_duplicates_within_a_run_are_skipped(self):
        result = DummyWorker([{"id": "1", "title": "A"}, {"id": "1", "title": "A"}]).run(limit=10)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.skipped, 1)

    def test_fetch_errors_are_reported_and_reset_between_runs(self):
        worker = DummyWorker([{"id": "1", "title": "A"}])
        worker.run(limit=10)
        result = worker.run(limit=10)
        self.assertEqual(result.errors, ["one feed was down"])

    def test_limit_is_passed_to_fetch(self):
        result = DummyWorker([{"id": str(n), "title": "T"} for n in range(5)]).run(limit=2)
        self.assertEqual(result.fetched, 2)


if __name__ == "__main__":
    unittest.main()
