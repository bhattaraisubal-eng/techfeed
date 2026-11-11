"""The common shape every source is normalized into."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ContentType(StrEnum):
    VIDEO = "video"
    PODCAST_EPISODE = "podcast_episode"
    DISCUSSION = "discussion"  # Hacker News stories
    ARTICLE = "article"        # blog posts
    PAPER = "paper"            # research papers


@dataclass(frozen=True)
class ContentItem:
    """One piece of content, whatever its source.

    Identity is (source, source_item_id): the ID the source itself assigns.
    It never changes, unlike titles and URLs. The database will use this pair
    as its unique key, which is what makes re-running a worker safe.

    frozen=True: once normalized, an item can't be changed by accident.
    """

    source: str                       # "youtube", "hackernews", ...
    source_item_id: str               # the source's own ID
    content_type: ContentType
    title: str
    url: str
    published_at: datetime | None     # always timezone-aware UTC after normalizing
    author: str | None = None         # channel, show, HN user, blog author, paper authors
    description: str | None = None    # plain text, no HTML
    duration_seconds: int | None = None
    is_short: bool = False            # a YouTube Short; always False for other platforms
    tags: tuple[str, ...] = ()
    # The untouched source data ("keep the raw, derive the rest"). Excluded
    # from repr and comparisons because it's large.
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self, include_raw: bool = False) -> dict:
        """A JSON-friendly dict (datetimes become ISO 8601 strings)."""
        data = {
            "source": self.source,
            "source_item_id": self.source_item_id,
            "content_type": self.content_type.value,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "author": self.author,
            "description": self.description,
            "duration_seconds": self.duration_seconds,
            "is_short": self.is_short,
            "tags": list(self.tags),
        }
        if include_raw:
            data["raw"] = self.raw
        return data
