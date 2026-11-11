"""Connecting to PostgreSQL.

Settings (.env):
  DATABASE_URL  required, e.g. "dbname=techfeed" or "postgresql://user@host/techfeed"

The connection string lives in config, like the API keys, because it differs
between your laptop, a test database, and a future server.
"""

import psycopg

from .config import require


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Open a connection. Use it in a `with` block:

        with connect() as conn:
            conn.execute(...)

    Leaving the block commits if no exception was raised, rolls back if one
    was, and closes the connection either way.

    autocommit=True: every statement, and every `with conn.transaction():`
    block, commits as soon as it finishes. Long runs use this so that work
    already done is saved even if a later step crashes.
    """
    return psycopg.connect(require("DATABASE_URL"), autocommit=autocommit)
