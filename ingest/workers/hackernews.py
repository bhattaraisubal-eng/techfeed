"""Hacker News worker: the current front page (or new/best stories).

Uses the official Firebase API, which needs no authentication:
  GET /v0/topstories.json  -> up to 500 story IDs, ranked
  GET /v0/item/<id>.json   -> one story (one request per story)

Which list to read comes from the `sources` table (worker = 'hackernews'):
the locator is topstories, newstories or beststories.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

from ..config import ConfigError
from ..http import RequestError
from ..models import ContentItem, ContentType
from ..parsing import clean_description, clean_title, strip_html
from .base import Worker

API_URL = "https://hacker-news.firebaseio.com/v0"
LISTS = ("topstories", "newstories", "beststories")


class HackerNewsWorker(Worker):
    source = "hackernews"

    def __init__(self, http, list_name: str = "topstories"):
        super().__init__(http)
        if list_name not in LISTS:
            raise ConfigError(f"the Hacker News list must be one of {', '.join(LISTS)}, not {list_name!r}")
        self.list_name = list_name

    def fetch(self, limit: int) -> Iterator[dict]:
        """`limit` = max stories in total."""
        story_ids = self.http.get_json(f"{API_URL}/{self.list_name}.json") or []
        for story_id in story_ids[:limit]:
            try:
                item = self.http.get_json(f"{API_URL}/item/{story_id}.json")
            except RequestError as error:
                self.fetch_errors.append(f"item {story_id}: {error}")
                continue
            # The API returns null for some deleted items, and flags others.
            # Jobs and polls aren't learning content, so only keep stories.
            if not item or item.get("deleted") or item.get("dead") or item.get("type") != "story":
                self.skipped += 1
                continue
            yield item

    def normalize(self, raw: dict) -> ContentItem:
        story_id = str(raw["id"])
        discussion_url = f"https://news.ycombinator.com/item?id={story_id}"
        return ContentItem(
            source=self.source,
            source_item_id=story_id,
            content_type=ContentType.DISCUSSION,
            title=clean_title(raw.get("title")),
            # "Ask HN" posts have no external link, so the discussion page is the content.
            url=raw.get("url") or discussion_url,
            published_at=datetime.fromtimestamp(raw["time"], UTC),  # Unix seconds, always UTC
            author=raw.get("by"),
            description=clean_description(strip_html(raw["text"])) if raw.get("text") else None,
            raw=raw,
        )
