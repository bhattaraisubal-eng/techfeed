"""Stage 3: decide whether a normalized item is good enough to store.

`validate` returns a list of human-readable reasons. An empty list means the
item is valid. Returning reasons (instead of raising on the first problem)
means a rejected item reports everything wrong with it at once.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .models import ContentItem

MAX_TITLE_LENGTH = 500
MAX_DESCRIPTION_LENGTH = 5000
EARLIEST_DATE = datetime(1990, 1, 1, tzinfo=UTC)
FUTURE_TOLERANCE = timedelta(days=1)  # small clock differences between servers are normal


def validate(item: ContentItem, now: datetime | None = None) -> list[str]:
    # `now` is a parameter so tests can pin the clock instead of depending on today's date.
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    if item.description is not None and len(item.description) > MAX_DESCRIPTION_LENGTH:
        reasons.append(f"description is longer than {MAX_DESCRIPTION_LENGTH} characters")
    if not item.source:
        reasons.append("source is empty")
    if not item.source_item_id or not item.source_item_id.strip():
        reasons.append("source_item_id is empty")

    if not item.title:
        reasons.append("title is empty")
    elif len(item.title) > MAX_TITLE_LENGTH:
        reasons.append(f"title is longer than {MAX_TITLE_LENGTH} characters")

    url = urlsplit(item.url or "")
    if url.scheme not in ("http", "https") or not url.netloc:
        reasons.append(f"url is not an http(s) URL: {item.url!r}")

    # A date is required: a feed needs to sort by recency.
    if item.published_at is None:
        reasons.append("published_at is missing")
    elif item.published_at.tzinfo is None:
        reasons.append("published_at has no timezone")
    elif item.published_at > now + FUTURE_TOLERANCE:
        reasons.append("published_at is in the future")
    elif item.published_at < EARLIEST_DATE:
        reasons.append("published_at is before 1990")

    if item.duration_seconds is not None and item.duration_seconds < 0:
        reasons.append("duration_seconds is negative")

    return reasons
