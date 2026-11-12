"""Store-stage tests, against a real PostgreSQL test database (see tests/database.py).

Why a real database and not a fake: what we're testing (the unique key,
ON CONFLICT, constraints, jsonb) happens inside PostgreSQL. A fake would only
test our assumptions about PostgreSQL.
"""

import unittest
from dataclasses import replace
from datetime import UTC, datetime

import psycopg

from ingest.models import ContentItem, ContentType
from ingest.store import upsert_items
from tests.database import DatabaseTestCase

ITEM = ContentItem(
    source="blog",
    source_item_id="https://openai.com/index/scaling-git",
    content_type=ContentType.ARTICLE,
    title="Scaling Git at OpenAI",
    url="https://openai.com/index/scaling-git",
    published_at=datetime(2026, 11, 2, 17, tzinfo=UTC),
    author="OpenAI",
    tags=("Engineering",),
    raw={"guid": "https://openai.com/index/scaling-git", "feed_url": "https://openai.com/news/rss.xml"},
)


class StoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.news = self.add_source(locator="https://openai.com/news/rss.xml", name="OpenAI News")
        self.research = self.add_source(locator="https://example.com/research.xml", name="OpenAI Research")

    def test_new_items_are_inserted(self):
        second = replace(ITEM, source_item_id="https://openai.com/index/other", title="Other")
        result = upsert_items(self.conn, self.news, [ITEM, second])
        self.assertEqual((result.inserted, result.seen_again), (2, 0))
        self.assertEqual(len(result.new_item_ids), 2)
        self.assertEqual(self.count_items(), 2)

    def test_rerun_creates_no_duplicates(self):
        upsert_items(self.conn, self.news, [ITEM])
        result = upsert_items(self.conn, self.news, [ITEM])     # the next scheduled run
        self.assertEqual((result.inserted, result.seen_again), (0, 1))
        self.assertEqual(result.new_item_ids, [])               # nothing for later stages to do
        self.assertEqual(self.count_items(), 1)

    def test_same_article_from_another_source_keeps_first_source(self):
        # The OpenAI News + OpenAI Research overlap from our real data.
        upsert_items(self.conn, self.news, [ITEM])
        upsert_items(self.conn, self.research, [replace(ITEM, title="Scaling Git at OpenAI (edited)")])
        title, source_id = self.conn.execute("SELECT title, source_id FROM content_items").fetchone()
        self.assertEqual(title, "Scaling Git at OpenAI (edited)")   # content is updated...
        self.assertEqual(source_id, self.news)                      # ...but the first source keeps the credit
        self.assertEqual(self.count_items(), 1)

    def test_same_id_on_different_platforms_are_different_items(self):
        upsert_items(self.conn, self.news, [replace(ITEM, source_item_id="abc123")])
        upsert_items(self.conn, self.news, [replace(ITEM, source="youtube", content_type=ContentType.VIDEO, source_item_id="abc123")])
        self.assertEqual(self.count_items(), 2)

    def test_fields_are_stored_as_postgres_types(self):
        upsert_items(self.conn, self.news, [ITEM])
        row = self.conn.execute(
            "SELECT platform, content_type, published_at, tags, raw ->> 'feed_url' FROM content_items"
        ).fetchone()
        self.assertEqual(row, ("blog", "article", ITEM.published_at, ["Engineering"], "https://openai.com/news/rss.xml"))

    def test_is_short_is_stored_and_updated(self):
        video = replace(ITEM, source="youtube", source_item_id="v1", content_type=ContentType.VIDEO)
        upsert_items(self.conn, self.news, [video])
        stored = lambda: self.conn.execute("SELECT is_short FROM content_items").fetchone()[0]
        self.assertIs(stored(), False)                           # the default
        upsert_items(self.conn, self.news, [replace(video, is_short=True)])
        self.assertIs(stored(), True)                            # the backfill path: an update changes it

    def test_thumbnail_is_stored_and_updated(self):
        upsert_items(self.conn, self.news, [ITEM])
        stored = lambda: self.conn.execute("SELECT thumbnail_url FROM content_items").fetchone()[0]
        self.assertIsNone(stored())
        upsert_items(self.conn, self.news, [replace(ITEM, thumbnail_url="https://cdn.example.com/t.jpg")])
        self.assertEqual(stored(), "https://cdn.example.com/t.jpg")

    def test_a_bad_item_stores_nothing_from_its_batch(self):
        # A negative duration passes this test's ContentItem but breaks the
        # CHECK constraint: the database is the last line of defence.
        bad = replace(ITEM, source_item_id="bad", duration_seconds=-5)
        with self.assertRaises(psycopg.errors.CheckViolation):
            upsert_items(self.conn, self.news, [ITEM, bad])
        self.assertEqual(self.count_items(), 0)   # ITEM was rolled back too: all or nothing


if __name__ == "__main__":
    unittest.main()
