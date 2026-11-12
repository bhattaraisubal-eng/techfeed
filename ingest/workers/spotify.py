"""Spotify worker: recent episodes of chosen podcast shows.

Authentication is OAuth 2.0 "client credentials": we trade our client ID and
secret for an access token that lasts about an hour, then send that token
with every API call. The token is cached and refreshed shortly before it expires.

Settings (.env), from https://developer.spotify.com/dashboard :
  SPOTIFY_CLIENT_ID      required
  SPOTIFY_CLIENT_SECRET  required
  SPOTIFY_MARKET         optional, two-letter country code (default US)

Which shows to read comes from the `sources` table (worker = 'spotify'):
the locator is the part after /show/ in a show URL.
"""

import base64
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from ..http import RequestError
from ..models import ContentItem, ContentType
from ..parsing import clean_description, clean_title
from .base import Worker

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
TOKEN_REFRESH_MARGIN_SECONDS = 60  # refresh a minute early, never mid-request


class SpotifyWorker(Worker):
    source = "spotify"

    def __init__(self, http, client_id: str, client_secret: str, show_ids: list[str],
                 market: str = "US", clock: Callable[[], float] = time.monotonic):
        super().__init__(http)
        self.client_id = client_id
        self.client_secret = client_secret
        self.show_ids = show_ids
        self.market = market
        self._clock = clock  # monotonic: unaffected by the system clock changing
        self._access_token: str | None = None
        self._expires_at = 0.0

    # ---------- auth ----------

    def _auth_headers(self) -> dict:
        if self._access_token is None or self._clock() >= self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            data = self.http.post_form(TOKEN_URL, {"grant_type": "client_credentials"},
                                       headers={"Authorization": f"Basic {credentials}"})
            self._access_token = data["access_token"]
            self._expires_at = self._clock() + float(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._access_token}"}

    # ---------- fetch ----------

    def fetch(self, limit: int) -> Iterator[dict]:
        """`limit` = max episodes per show."""
        # Get a token before the loop: bad credentials should fail the whole
        # worker once, loudly, not produce the same error for every show.
        self._auth_headers()
        for show_id in self.show_ids:
            try:
                episodes = self._show_episodes(show_id, limit)
            except RequestError as error:
                self.fetch_errors.append(f"show {show_id}: {error}")
                continue
            yield from episodes

    def _show_episodes(self, show_id: str, limit: int) -> list[dict]:
        # The show object includes its first page of episodes; `next` links to the rest.
        show = self.http.get_json(f"{API_URL}/shows/{show_id}", params={"market": self.market}, headers=self._auth_headers())
        show_info = {"id": show.get("id"), "name": show.get("name"), "publisher": show.get("publisher")}
        page = show.get("episodes") or {}
        episodes: list[dict] = []
        while True:
            for episode in page.get("items") or []:
                if episode is None:  # Spotify returns null for unavailable episodes
                    self.skipped += 1
                    continue
                # Simplified episode objects don't say which show they belong to,
                # so we attach it. The underscore marks it as added by us.
                episodes.append({**episode, "_show": show_info})
            if len(episodes) >= limit or not page.get("next"):
                break
            page = self.http.get_json(page["next"], headers=self._auth_headers())
        return episodes[:limit]

    # ---------- normalize ----------

    def normalize(self, raw: dict) -> ContentItem:
        show = raw.get("_show") or {}
        duration_ms = raw.get("duration_ms")
        return ContentItem(
            source=self.source,
            source_item_id=raw["id"],
            content_type=ContentType.PODCAST_EPISODE,
            title=clean_title(raw.get("name")),
            url=raw["external_urls"]["spotify"],
            published_at=parse_release_date(raw["release_date"]),
            author=show.get("name"),
            description=clean_description(raw.get("description")),
            duration_seconds=round(duration_ms / 1000) if duration_ms is not None else None,
            thumbnail_url=largest_image(raw.get("images")),
            raw=raw,
        )


def largest_image(images: list | None) -> str | None:
    """Spotify sends each episode's artwork in several sizes (640, 300, 64 px)."""
    sized = [img for img in images or [] if img and img.get("url")]
    if not sized:
        return None
    return max(sized, key=lambda img: img.get("width") or 0)["url"]


def parse_release_date(value: str) -> datetime:
    """'2024-05-01', '2024-05' or '2024' -> midnight UTC on the first matching day.

    Spotify sends a release_date_precision of day, month or year; it stays in raw.
    """
    numbers = [int(part) for part in value.split("-")]
    year, month, day = (numbers + [1, 1])[:3]
    return datetime(year, month, day, tzinfo=UTC)
