"""Shared setup for tests that need a real PostgreSQL database.

Setup, once:
    /opt/homebrew/opt/postgresql@17/bin/createdb techfeed_test

Each test runs inside a transaction that is rolled back afterwards, so tests
can't affect each other and the test database stays empty. Code under test
that uses `with conn.transaction():` gets a savepoint inside that transaction,
so its "commits" are rolled back too.
"""

import os
import unittest
from pathlib import Path

import psycopg

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "dbname=techfeed_test")
SCHEMA = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()


def connect_or_skip():
    try:
        return psycopg.connect(TEST_DATABASE_URL)
    except psycopg.OperationalError as error:
        raise unittest.SkipTest(f"no test database ({error}). Create it: createdb techfeed_test")


class DatabaseTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Rebuild the tables from db/schema.sql, so tests always run against the current schema.
        with connect_or_skip() as conn:
            # Safety guard: this drops tables, so never run it against a real database.
            if not conn.info.dbname.endswith("_test"):
                raise RuntimeError(f"refusing to run database tests against {conn.info.dbname!r}: name must end in _test")
            conn.execute("DROP TABLE IF EXISTS content_items, sources")
            conn.execute(SCHEMA)

    def setUp(self):
        self.conn = connect_or_skip()

    def tearDown(self):
        self.conn.rollback()   # undo everything this test did
        self.conn.close()

    def add_source(self, worker="blog", locator="https://openai.com/news/rss.xml", name="OpenAI News", status="active") -> int:
        return self.conn.execute(
            "INSERT INTO sources (worker, locator, name, status) VALUES (%s, %s, %s, %s) RETURNING id",
            (worker, locator, name, status),
        ).fetchone()[0]

    def count_items(self) -> int:
        return self.conn.execute("SELECT count(*) FROM content_items").fetchone()[0]
