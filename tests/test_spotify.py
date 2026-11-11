import base64
import unittest
from datetime import UTC, datetime

from ingest.http import HttpError
from ingest.workers.spotify import SpotifyWorker, parse_release_date
from tests.fakes import FakeHttp


def episode(episode_id, release_date="2024-05-01", precision="day"):
    return {
        "id": episode_id,
        "name": f"Episode {episode_id}",
        "description": "We talk about agents.",
        "release_date": release_date,
        "release_date_precision": precision,
        "duration_ms": 3723400,
        "external_urls": {"spotify": f"https://open.spotify.com/episode/{episode_id}"},
    }


NEXT = "https://api.spotify.com/v1/shows/S1/episodes?offset=2&limit=2"
SHOW = {"id": "S1", "name": "The Inference Hour", "publisher": "Example",
        "episodes": {"items": [episode("e1"), None], "next": NEXT}}
PAGE2 = {"items": [episode("e2", "2024-03", "month")], "next": None}
TOKEN = {"access_token": "TOKEN", "expires_in": 3600}


def routes():
    # The episodes URL contains "/shows/S1", so it must be listed first.
    return [("accounts.spotify.com", TOKEN), ("/shows/S1/episodes", PAGE2), ("/shows/S1", SHOW),
            ("/shows/S2", {**SHOW, "id": "S2", "episodes": {"items": [episode("e9")], "next": None}})]


class SpotifyWorkerTests(unittest.TestCase):
    def make(self, http, show_ids=("S1",), clock=lambda: 0.0):
        return SpotifyWorker(http, "my-id", "my-secret", list(show_ids), clock=clock)

    def test_authenticates_then_reads_all_episode_pages(self):
        http = FakeHttp(routes())
        result = self.make(http).run(limit=10)

        token_call = http.calls[0]
        self.assertEqual(token_call.method, "POST")
        self.assertEqual(token_call.params, {"grant_type": "client_credentials"})
        expected = base64.b64encode(b"my-id:my-secret").decode()
        self.assertEqual(token_call.headers["Authorization"], f"Basic {expected}")

        api_calls = http.calls[1:]
        self.assertTrue(all(c.headers["Authorization"] == "Bearer TOKEN" for c in api_calls))
        self.assertEqual(api_calls[0].params, {"market": "US"})
        self.assertEqual(api_calls[1].url, NEXT)

        self.assertEqual([i.source_item_id for i in result.items], ["e1", "e2"])
        self.assertEqual(result.skipped, 1)  # the null episode

    def test_normalizes_an_episode(self):
        item = self.make(FakeHttp([])).normalize({**episode("e1"), "_show": {"name": "The Inference Hour"}})
        self.assertEqual(item.title, "Episode e1")
        self.assertEqual(item.author, "The Inference Hour")
        self.assertEqual(item.duration_seconds, 3723)
        self.assertEqual(item.published_at, datetime(2024, 5, 1, tzinfo=UTC))
        self.assertEqual(item.url, "https://open.spotify.com/episode/e1")

    def test_token_is_reused_across_shows(self):
        http = FakeHttp(routes())
        self.make(http, show_ids=("S1", "S2")).run(limit=10)
        self.assertEqual(sum(c.method == "POST" for c in http.calls), 1)

    def test_token_is_refreshed_when_nearly_expired(self):
        now = [0.0]
        http = FakeHttp(routes())
        worker = self.make(http, clock=lambda: now[0])
        worker.run(limit=1)
        now[0] = 3600 - 30  # inside the 60-second refresh margin
        worker.run(limit=1)
        self.assertEqual(sum(c.method == "POST" for c in http.calls), 2)

    def test_limit_is_per_show_and_stops_paging(self):
        http = FakeHttp(routes())
        result = self.make(http).run(limit=1)
        self.assertEqual(len(result.items), 1)
        self.assertFalse(any(c.url == NEXT for c in http.calls))

    def test_bad_credentials_fail_the_whole_worker(self):
        http = FakeHttp([("accounts.spotify.com", HttpError(400, "https://accounts.spotify.com/api/token", "invalid_client"))])
        with self.assertRaises(HttpError):
            self.make(http, show_ids=("S1", "S2")).run(limit=5)

    def test_release_date_precisions(self):
        self.assertEqual(parse_release_date("2024-05-07"), datetime(2024, 5, 7, tzinfo=UTC))
        self.assertEqual(parse_release_date("2024-05"), datetime(2024, 5, 1, tzinfo=UTC))
        self.assertEqual(parse_release_date("2024"), datetime(2024, 1, 1, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
