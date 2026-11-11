"""Stage 4: store validated items in PostgreSQL.

Every item is upserted on (platform, source_item_id), the identity key:
  - a key we've never seen   -> INSERT  (a new item)
  - a key we already have    -> UPDATE  (seen again; edits to title etc. are picked up)

So running a worker twice never creates duplicates, and backfill and regular
runs are the same operation.
"""

from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from .models import ContentItem

# What an update changes, and what it leaves alone, is the important part:
#   updated:  the item's content, and last_seen_at
#   kept:     source_id      (the source that found the item first keeps the credit)
#             first_seen_at  (when we first saw it: how "new today" is computed)
UPSERT_SQL = """
INSERT INTO content_items (
    platform, source_item_id, source_id, content_type, title, url,
    published_at, author, description, duration_seconds, is_short, tags, raw
) VALUES (
    %(platform)s, %(source_item_id)s, %(source_id)s, %(content_type)s, %(title)s, %(url)s,
    %(published_at)s, %(author)s, %(description)s, %(duration_seconds)s, %(is_short)s, %(tags)s, %(raw)s
)
ON CONFLICT (platform, source_item_id) DO UPDATE
   SET content_type     = EXCLUDED.content_type,
       title            = EXCLUDED.title,
       url              = EXCLUDED.url,
       published_at     = EXCLUDED.published_at,
       author           = EXCLUDED.author,
       description      = EXCLUDED.description,
       duration_seconds = EXCLUDED.duration_seconds,
       is_short         = EXCLUDED.is_short,   -- updated, so better detection fixes stored rows too
       tags             = EXCLUDED.tags,
       raw              = EXCLUDED.raw,
       last_seen_at     = now()
-- xmax = 0 only on a freshly inserted row (a PostgreSQL internal detail):
-- it's how one statement tells us "inserted" from "updated".
RETURNING id, (xmax = 0) AS inserted
"""


@dataclass
class StoreResult:
    inserted: int = 0
    seen_again: int = 0
    new_item_ids: list[int] = field(default_factory=list)  # for later stages: full text, tags

    def summary(self) -> str:
        return f"stored {self.inserted} new, {self.seen_again} seen again"


def item_to_row(item: ContentItem, source_id: int) -> dict:
    """ContentItem -> the values for one content_items row.

    Python's `item.source` becomes the `platform` column ("source" in the
    database means a row in the sources table).
    """
    return {
        "platform": item.source,
        "source_item_id": item.source_item_id,
        "source_id": source_id,
        "content_type": item.content_type.value,
        "title": item.title,
        "url": item.url,
        "published_at": item.published_at,
        "author": item.author,
        "description": item.description,
        "duration_seconds": item.duration_seconds,
        "is_short": item.is_short,
        "tags": list(item.tags),   # tuple -> list: psycopg sends lists as PostgreSQL arrays
        "raw": Jsonb(item.raw),    # Jsonb(...) tells psycopg to send this dict as jsonb
    }


HEALTH_SQL = """
UPDATE sources
   SET last_fetched_at      = now(),
       last_success_at      = CASE WHEN %(error)s::text IS NULL THEN now() ELSE last_success_at END,
       last_new_item_at     = CASE WHEN %(new_items)s > 0 THEN now() ELSE last_new_item_at END,
       consecutive_failures = CASE WHEN %(error)s::text IS NULL THEN 0 ELSE consecutive_failures + 1 END,
       -- Keep the previous error after a success: it's still useful history,
       -- and consecutive_failures = 0 already says the source is healthy now.
       last_error           = COALESCE(%(error)s, last_error)
 WHERE id = %(source_id)s
"""


def record_health(conn, source_id: int, error: str | None, new_items: int) -> None:
    """Update a source's health columns after a run.

    error=None means the run succeeded. These columns are written only here,
    never by the seed script: each column has one owner.
    """
    conn.execute(HEALTH_SQL, {"source_id": source_id, "error": error, "new_items": new_items})


def record_validators(conn, source_id: int, etag: str | None, last_modified: str | None) -> None:
    """Remember a feed's ETag / Last-Modified for the next conditional GET.

    Call this only after the response's items were stored. Saving them first,
    then failing to store, would make the next run get 304 and never store
    those items: silent data loss.
    """
    conn.execute(
        "UPDATE sources SET http_etag = %s, http_last_modified = %s WHERE id = %s",
        (etag, last_modified, source_id),
    )


def upsert_items(conn, source_id: int, items: list[ContentItem]) -> StoreResult:
    """Upsert every item from one source, in one transaction: all or nothing.

    If any item fails (e.g. it breaks a database constraint), none of this
    batch is stored and the error is raised, so a half-stored batch can't happen.
    """
    result = StoreResult()
    with conn.transaction(), conn.cursor() as cur:
        for item in items:
            item_id, inserted = cur.execute(UPSERT_SQL, item_to_row(item, source_id)).fetchone()
            if inserted:
                result.inserted += 1
                result.new_item_ids.append(item_id)
            else:
                result.seen_again += 1
    return result
