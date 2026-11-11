"""Pipeline tests: sources table -> worker -> content_items + health columns.

Uses the real test database (tests/database.py) and FakeHttp, so no network.
"""

import unittest
from unittest import mock

import psycopg

from ingest.http import ConditionalResponse, HttpError, Validators
from ingest.pipeline import Source, active_sources, run_source, store_run
from tests.database import DatabaseTestCase
from tests.fakes import FakeHttp
from tests.test_blogs import RSS, RSS_URL   # one valid item, one without a date


class PipelineTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.source_id = self.add_source(locator=RSS_URL, name="Example Eng Blog")
        self.source = Source(self.source_id, "blog", RSS_URL, "Example Eng Blog")

    def health(self):
        return self.conn.execute(
            "SELECT last_success_at IS NOT NULL, last_new_item_at IS NOT NULL, consecutive_failures, last_error"
            " FROM sources WHERE id = %s",
            (self.source_id,),
        ).fetchone()

    def test_stores_items_with_the_source_id_and_records_success(self):
        outcome = run_source(self.conn, FakeHttp([(RSS_URL, RSS)]), self.source, limit=100)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.stored.inserted, 1)           # the dated item; the undated one was rejected
        self.assertEqual(len(outcome.result.rejected), 1)
        stored_source = self.conn.execute("SELECT source_id FROM content_items").fetchone()[0]
        self.assertEqual(stored_source, self.source_id)
        self.assertEqual(self.health(), (True, True, 0, None))   # succeeded, found something new

    def test_second_run_stores_nothing_new(self):
        http = FakeHttp([(RSS_URL, RSS)])
        run_source(self.conn, http, self.source, limit=100)
        outcome = run_source(self.conn, http, self.source, limit=100)
        self.assertEqual((outcome.stored.inserted, outcome.stored.seen_again), (0, 1))
        self.assertEqual(self.count_items(), 1)

    def test_failing_feed_is_recorded_and_counted(self):
        http = FakeHttp([(RSS_URL, HttpError(500, RSS_URL, "oops"))])
        run_source(self.conn, http, self.source, limit=100)
        outcome = run_source(self.conn, http, self.source, limit=100)
        self.assertIn("500", outcome.error)
        succeeded, _, failures, last_error = self.health()
        self.assertFalse(succeeded)
        self.assertEqual(failures, 2)                          # two failures in a row
        self.assertIn("500", last_error)
        self.assertEqual(self.count_items(), 0)

    def test_success_resets_the_failure_count(self):
        run_source(self.conn, FakeHttp([(RSS_URL, HttpError(500, RSS_URL, "oops"))]), self.source, limit=100)
        run_source(self.conn, FakeHttp([(RSS_URL, RSS)]), self.source, limit=100)
        succeeded, _, failures, last_error = self.health()
        self.assertTrue(succeeded)
        self.assertEqual(failures, 0)
        self.assertIn("500", last_error)                       # kept as history

    def test_disabled_sources_are_not_run(self):
        self.add_source(locator="https://dead.example.com/feed", name="Dead Blog", status="disabled")
        self.assertEqual([s.name for s in active_sources(self.conn, "blog")], ["Example Eng Blog"])

    # --- conditional GET -------------------------------------------------

    def validators_in_db(self):
        return self.conn.execute(
            "SELECT http_etag, http_last_modified FROM sources WHERE id = %s", (self.source_id,)
        ).fetchone()

    def fresh_source(self):
        """The Source as the pipeline would read it now, saved validators included."""
        return next(s for s in active_sources(self.conn, "blog") if s.id == self.source_id)

    def test_validators_are_saved_and_sent_on_the_next_run(self):
        first = FakeHttp([(RSS_URL, ConditionalResponse(RSS, Validators('"v1"', "Tue, 10 Nov 2026 10:00:00 GMT")))])
        run_source(self.conn, first, self.fresh_source(), limit=100)
        self.assertEqual(self.validators_in_db(), ('"v1"', "Tue, 10 Nov 2026 10:00:00 GMT"))

        second = FakeHttp([(RSS_URL, ConditionalResponse(None, Validators('"v1"')))])   # the feed answers 304
        outcome = run_source(self.conn, second, self.fresh_source(), limit=100)
        self.assertEqual(second.calls[0].headers["If-None-Match"], '"v1"')
        self.assertTrue(outcome.not_modified)
        self.assertIsNone(outcome.error)                          # unchanged is healthy, not a failure
        self.assertEqual(self.health()[2], 0)
        self.assertEqual(self.count_items(), 1)                   # the item from run 1 is untouched

    def test_validators_are_not_saved_when_the_store_fails(self):
        # The data-loss case: if these were saved, the next run would get 304
        # and the items of this response would never be stored.
        http = FakeHttp([(RSS_URL, ConditionalResponse(RSS, Validators('"v1"')))])
        with mock.patch("ingest.pipeline.upsert_items", side_effect=psycopg.errors.CheckViolation("simulated")):
            outcome = run_source(self.conn, http, self.fresh_source(), limit=100)
        self.assertIn("store failed", outcome.error)
        self.assertEqual(self.validators_in_db(), (None, None))

    def test_refetch_ignores_saved_validators(self):
        self.conn.execute("UPDATE sources SET http_etag = '\"v1\"' WHERE id = %s", (self.source_id,))
        http = FakeHttp([(RSS_URL, RSS)])
        outcome = run_source(self.conn, http, self.fresh_source(), limit=100, refetch=True)
        self.assertEqual(http.calls[0].headers, {})               # no If-None-Match: a full download
        self.assertEqual(outcome.stored.inserted, 1)

    # --- dry run -------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        self.conn.execute("UPDATE sources SET http_etag = '\"v1\"' WHERE id = %s", (self.source_id,))
        before = self.conn.execute("SELECT * FROM sources WHERE id = %s", (self.source_id,)).fetchone()
        http = FakeHttp([(RSS_URL, RSS)])
        outcome = run_source(self.conn, http, self.fresh_source(), limit=100, store=False)
        self.assertEqual(len(outcome.result.items), 1)            # it did fetch and validate...
        self.assertIsNone(outcome.stored)
        self.assertEqual(self.count_items(), 0)                   # ...but stored no items,
        after = self.conn.execute("SELECT * FROM sources WHERE id = %s", (self.source_id,)).fetchone()
        self.assertEqual(before, after)                           # and changed no health or validators
        self.assertEqual(http.calls[0].headers, {})               # a full download, not a 304

    def test_dry_run_of_a_locator_not_in_the_table(self):
        # --locator: no database row, so no id and no connection needed.
        source = Source(None, "blog", RSS_URL, "trying a new feed")
        outcome = run_source(None, FakeHttp([(RSS_URL, RSS)]), source, limit=100, store=False)
        self.assertIsNone(outcome.error)
        self.assertEqual(len(outcome.result.items), 1)

    def test_one_failing_source_does_not_stop_the_others(self):
        other = "https://other.example.com/rss"
        self.add_source(locator=other, name="Other Blog")
        http = FakeHttp([(RSS_URL, HttpError(500, RSS_URL, "oops")), (other, RSS.replace(b"post-42", b"post-43"))])
        outcomes = store_run(self.conn, http, ["blogs"], limit=None)
        self.assertEqual([o.error is None for o in outcomes], [False, True])
        self.assertEqual(self.count_items(), 1)


if __name__ == "__main__":
    unittest.main()
