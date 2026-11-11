import unittest
from datetime import UTC, datetime

from ingest.http import HttpError
from ingest.workers.youtube import YouTubeWorker
from tests.fakes import FakeHttp


def channels(url, params):
    if params["id"] == "UCmissing":
        return {"items": []}
    return {"items": [{"id": params["id"], "contentDetails": {"relatedPlaylists": {"uploads": "UU" + params["id"]}}}]}


def playlist_items(url, params):
    if params.get("pageToken") == "PAGE2":
        return {"items": [{"contentDetails": {"videoId": "v3"}}]}
    return {"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}], "nextPageToken": "PAGE2"}


def video(video_id):
    return {
        "id": video_id,
        "snippet": {
            "title": f"  Video {video_id}  ",
            "publishedAt": "2024-05-01T12:00:00Z",
            "channelTitle": "Hello Interview",
            "description": "Line one\r\n\r\n\r\nLine two",
            "tags": ["system design"],
        },
        "contentDetails": {"duration": "PT1H2M10S"},
    }


def videos(url, params):
    return {"items": [video(v) for v in params["id"].split(",")]}


ROUTES = [("/channels", channels), ("/playlistItems", playlist_items), ("/videos", videos)]


class YouTubeWorkerTests(unittest.TestCase):
    def test_follows_channel_to_playlist_to_videos_across_pages(self):
        http = FakeHttp(ROUTES)
        result = YouTubeWorker(http, "KEY", ["UCabc"]).run(limit=3)

        self.assertEqual([i.source_item_id for i in result.items], ["v1", "v2", "v3"])
        endpoints = [c.url.rsplit("/", 1)[1] for c in http.calls]
        self.assertEqual(endpoints, ["channels", "playlistItems", "playlistItems", "videos"])
        self.assertEqual(http.calls[1].params["playlistId"], "UUUCabc")
        self.assertEqual(http.calls[2].params["maxResults"], 1)   # only asks for what's still needed
        self.assertEqual(http.calls[3].params["id"], "v1,v2,v3")  # one batched details call
        self.assertTrue(all(c.params["key"] == "KEY" for c in http.calls))

    def test_normalizes_a_video(self):
        item = YouTubeWorker(FakeHttp([]), "KEY", []).normalize(video("v1"))
        self.assertEqual(item.title, "Video v1")
        self.assertEqual(item.url, "https://www.youtube.com/watch?v=v1")
        self.assertEqual(item.published_at, datetime(2024, 5, 1, 12, tzinfo=UTC))
        self.assertEqual(item.duration_seconds, 3730)
        self.assertEqual(item.description, "Line one\n\nLine two")
        self.assertEqual(item.author, "Hello Interview")
        self.assertEqual(item.tags, ("system design",))
        self.assertEqual(item.raw["id"], "v1")

    def test_limit_stops_paging_early(self):
        http = FakeHttp(ROUTES)
        result = YouTubeWorker(http, "KEY", ["UCabc"]).run(limit=2)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(sum("/playlistItems" in c.url for c in http.calls), 1)

    def test_missing_channel_is_recorded_and_others_continue(self):
        result = YouTubeWorker(FakeHttp(ROUTES), "KEY", ["UCmissing", "UCabc"]).run(limit=1)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("UCmissing", result.errors[0])

    def test_api_error_for_a_channel_is_recorded(self):
        http = FakeHttp([("/channels", HttpError(403, "https://x?key=KEY", "quotaExceeded"))])
        result = YouTubeWorker(http, "KEY", ["UCabc"]).run(limit=5)
        self.assertEqual(result.items, [])
        self.assertIn("403", result.errors[0])

    def test_details_call_asks_for_player_size(self):
        # Shorts detection needs the player's width/height, which the API only
        # returns for part=player together with a maxWidth.
        http = FakeHttp(ROUTES)
        YouTubeWorker(http, "KEY", ["UCabc"]).run(limit=3)
        details = http.calls[-1].params
        self.assertIn("player", details["part"].split(","))
        self.assertIn("maxWidth", details)


def sized(duration: str, width: int | None, height: int | None) -> dict:
    """A video with a given duration and player size."""
    raw = {**video("v1"), "contentDetails": {"duration": duration}}
    if width is not None:
        raw["player"] = {"embedWidth": str(width), "embedHeight": str(height)}   # the API sends strings
    return raw


class ShortsTests(unittest.TestCase):
    def is_short(self, raw):
        return YouTubeWorker(FakeHttp([]), "KEY", []).normalize(raw).is_short

    def test_vertical_and_short_is_a_short(self):
        self.assertTrue(self.is_short(sized("PT47S", 563, 1000)))

    def test_exactly_three_minutes_is_still_a_short(self):
        self.assertTrue(self.is_short(sized("PT3M", 563, 1000)))

    def test_one_second_over_is_not(self):
        self.assertFalse(self.is_short(sized("PT3M1S", 563, 1000)))

    def test_short_but_horizontal_is_not(self):
        # Like "Thankful for you all!" (155 s, 1000x563): short, but a normal video.
        self.assertFalse(self.is_short(sized("PT2M35S", 1000, 563)))

    def test_vertical_but_long_is_not(self):
        # e.g. a phone-filmed livestream.
        self.assertFalse(self.is_short(sized("PT45M", 563, 1000)))

    def test_unknown_size_is_not(self):
        self.assertFalse(self.is_short(sized("PT30S", None, None)))


if __name__ == "__main__":
    unittest.main()
