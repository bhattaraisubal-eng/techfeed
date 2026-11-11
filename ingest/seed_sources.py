"""Load data/sources.csv into the `sources` table.

Run:  .venv/bin/python -m ingest.seed_sources [path/to/sources.csv]

Safe to rerun after editing the CSV: it upserts on (worker, locator), so new
rows are added, changed rows are updated, and nothing is duplicated. It only
touches the curation columns (name, tier, status...). The health columns
(last_success_at, consecutive_failures...) belong to the workers and are left alone.

Rows removed from the CSV are NOT deleted from the table: sources are
disabled, never deleted (see ON DELETE RESTRICT in db/schema.sql).
"""

import csv
import sys
from pathlib import Path

from .config import PROJECT_ROOT, load_env_file
from .db import connect

SOURCES_FILE = PROJECT_ROOT / "data" / "sources.csv"

# Only these columns come from the CSV. The upsert updates exactly these.
CURATED_COLUMNS = ("worker", "locator", "name", "homepage_url", "tier", "official", "status", "notes")

UPSERT_SQL = """
INSERT INTO sources (worker, locator, name, homepage_url, tier, official, status, notes)
VALUES (%(worker)s, %(locator)s, %(name)s, %(homepage_url)s, %(tier)s, %(official)s, %(status)s, %(notes)s)
ON CONFLICT (worker, locator) DO UPDATE
   SET name = EXCLUDED.name,
       homepage_url = EXCLUDED.homepage_url,
       tier = EXCLUDED.tier,
       official = EXCLUDED.official,
       status = EXCLUDED.status,
       notes = EXCLUDED.notes
   -- Skip the write when nothing changed, so a rerun reports "unchanged"
   -- instead of rewriting every row. IS DISTINCT FROM treats NULL = NULL as equal.
 WHERE (sources.name, sources.homepage_url, sources.tier, sources.official, sources.status, sources.notes)
       IS DISTINCT FROM
       (EXCLUDED.name, EXCLUDED.homepage_url, EXCLUDED.tier, EXCLUDED.official, EXCLUDED.status, EXCLUDED.notes)
-- xmax = 0 only on a freshly inserted row (a PostgreSQL internal detail):
-- it's how one statement tells us "inserted" from "updated".
RETURNING (xmax = 0) AS inserted
"""


def csv_row_to_source(row: dict) -> dict:
    """Turn one CSV row (all strings) into values for the `sources` table.

    Empty cells become None (NULL), `tier` becomes an int, `official` a bool.
    Raises ValueError for a row that can't be stored, so a bad CSV fails
    before anything is written.
    """
    values = {col: (row.get(col) or "").strip() or None for col in CURATED_COLUMNS}

    for required in ("worker", "locator", "name"):
        if values[required] is None:
            raise ValueError(f"{required} is empty")

    if values["tier"] is not None:
        values["tier"] = int(values["tier"])  # raises ValueError for e.g. "two"

    official = (values["official"] or "true").lower()
    if official not in ("true", "false"):
        raise ValueError(f"official must be true or false, got {values['official']!r}")
    values["official"] = official == "true"

    values["status"] = values["status"] or "active"
    return values


def read_sources(path: Path) -> list[dict]:
    """Read and convert every row. Line numbers in errors match the file."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    sources = []
    for line_number, row in enumerate(rows, start=2):  # line 1 is the header
        try:
            sources.append(csv_row_to_source(row))
        except ValueError as error:
            raise ValueError(f"{path.name} line {line_number}: {error}") from error
    return sources


def seed(conn, sources: list[dict]) -> dict:
    """Upsert every source in one transaction. Returns counts."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    with conn.transaction():
        for source in sources:
            result = conn.execute(UPSERT_SQL, source).fetchone()
            if result is None:              # the WHERE clause skipped it
                counts["unchanged"] += 1
            elif result[0]:
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
    return counts


def main() -> None:
    load_env_file()
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCES_FILE
    sources = read_sources(path)  # validate the whole file before touching the database
    with connect() as conn:
        counts = seed(conn, sources)
    print(f"{path.name}: {len(sources)} rows -> "
          f"{counts['inserted']} inserted, {counts['updated']} updated, {counts['unchanged']} unchanged")


if __name__ == "__main__":
    main()
