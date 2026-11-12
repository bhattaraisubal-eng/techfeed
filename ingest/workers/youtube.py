"""YouTube worker: each channel's most recent uploads.

Three API calls per channel (each costs 1 unit of the default 10,000/day quota):
  1. channels.list       -> the channel's "uploads" playlist ID
  2. playlistItems.list  -> video IDs in that playlist, newest first, 50 per page
  3. videos.list         -> full details (duration, tags) for up to 50 IDs per call

Settings (.env):
  YOUTUBE_API_KEY  required

Which channels to read comes from the `sources` table (worker = 'youtube').
"""

from collections.abc import Iterator

from ..http import RequestError
from ..models import ContentItem, ContentType
from ..parsing import clean_description, clean_title, parse_iso8601_duration, parse_iso_datetime
from .base import Worker

API_URL = "https://www.googleapis.com/youtube/v3"
PAGE_SIZE = 50  # the API's maximum for playlistItems and videos

# Shorts. The API has no "is this a Short?" field, so we infer it. Tested
# against YouTube itself (youtube.com/shorts/<id> loads for Shorts and
# redirects for normal videos):
#   * duration alone is wrong for short horizontal videos (channel announcements),
#   * "#shorts" in the title is missing on most Shorts,
#   * vertical AND <= 3 minutes matched every video we checked.
SHORT_MAX_SECONDS = 180      # YouTube's limit for Shorts since October 2024
# The player's width/height are only returned when a max size is requested.
# Any value works; only the ratio matters.
PLAYER_MAX_WIDTH = 1000


class ChannelNotFoundError(LookupError):
    pass


class YouTubeWorker(Worker):
    source = "youtube"

    def __init__(self, http, api_key: str, channel_ids: list[str]):
        super().__init__(http)
        self.api_key = api_key
        self.channel_ids = channel_ids

    # ---------- fetch ----------

    def fetch(self, limit: int) -> Iterator[dict]:
        """`limit` = max videos per channel."""
        for channel_id in self.channel_ids:
            try:
                playlist_id = self._uploads_playlist_id(channel_id)
                video_ids = self._recent_video_ids(playlist_id, limit)
                videos = self._video_details(video_ids)
            except (RequestError, ChannelNotFoundError, KeyError) as error:
                # One broken channel must not stop the others.
                self.fetch_errors.append(f"channel {channel_id}: {error}")
                continue
            yield from videos

    def _get(self, endpoint: str, **params):
        return self.http.get_json(f"{API_URL}/{endpoint}", params={**params, "key": self.api_key})

    def _uploads_playlist_id(self, channel_id: str) -> str:
        data = self._get("channels", part="contentDetails", id=channel_id)
        items = data.get("items") or []
        if not items:
            raise ChannelNotFoundError(f"no channel with id {channel_id!r}")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def _recent_video_ids(self, playlist_id: str, limit: int) -> list[str]:
        """Follow nextPageToken until we have `limit` IDs or the playlist ends."""
        video_ids: list[str] = []
        page_token = None
        while len(video_ids) < limit:
            params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": min(PAGE_SIZE, limit - len(video_ids))}
            if page_token:
                params["pageToken"] = page_token
            data = self._get("playlistItems", **params)
            video_ids.extend(item["contentDetails"]["videoId"] for item in data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return video_ids[:limit]

    def _video_details(self, video_ids: list[str]) -> list[dict]:
        """Batch IDs 50 at a time: 1 call per 50 videos instead of 1 per video."""
        videos: list[dict] = []
        for start in range(0, len(video_ids), PAGE_SIZE):
            batch = video_ids[start:start + PAGE_SIZE]
            # "player" adds the embed size (for Shorts detection) in the same call: no extra quota.
            data = self._get("videos", part="snippet,contentDetails,statistics,player",
                             id=",".join(batch), maxWidth=PLAYER_MAX_WIDTH)
            videos.extend(data.get("items", []))
        return videos

    # ---------- normalize ----------

    def normalize(self, raw: dict) -> ContentItem:
        snippet = raw["snippet"]
        video_id = raw["id"]
        duration = parse_iso8601_duration(raw.get("contentDetails", {}).get("duration"))
        return ContentItem(
            source=self.source,
            source_item_id=video_id,
            content_type=ContentType.VIDEO,
            title=clean_title(snippet.get("title")),
            url=f"https://www.youtube.com/watch?v={video_id}",
            published_at=parse_iso_datetime(snippet["publishedAt"]),
            author=snippet.get("channelTitle"),
            description=clean_description(snippet.get("description")),
            duration_seconds=duration,
            is_short=is_youtube_short(raw, duration),
            thumbnail_url=youtube_thumbnail(snippet),
            tags=tuple(snippet.get("tags", [])),
            raw=raw,
        )


# Card-sized first: 640px ("standard") is sharp enough and exists for almost
# every video; "maxres" (1280px+) is heavier than a card needs.
THUMBNAIL_SIZES = ("standard", "high", "medium", "default")


def youtube_thumbnail(snippet: dict) -> str | None:
    thumbnails = snippet.get("thumbnails") or {}
    for size in THUMBNAIL_SIZES:
        url = (thumbnails.get(size) or {}).get("url")
        if url:
            return url
    return None


def is_youtube_short(raw: dict, duration_seconds: int | None) -> bool:
    """Vertical (taller than wide) and at most 3 minutes.

    Without player dimensions we can't tell, so we say False: a Short missing
    from the Shorts section is a smaller mistake than a 40-minute talk in it.
    """
    player = raw.get("player") or {}
    width, height = player.get("embedWidth"), player.get("embedHeight")
    if not width or not height or duration_seconds is None:
        return False
    return int(height) > int(width) and duration_seconds <= SHORT_MAX_SECONDS
