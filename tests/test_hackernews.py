import unittest
from datetime import UTC, datetime

from ingest.config import ConfigError
from ingest.http import HttpError
from ingest.workers.hackernews import HackerNewsWorker
from tests.fakes import FakeHttp

NOON = int(datetime(2024, 5, 1, 12, tzinfo=UTC).timestamp())

LINK_STORY = {"id": 1, "type": "story", "by": "pg", "time": NOON, "title": "Show HN: A vector DB in 500 lines", "url": "https://example.com/vdb"}
DEAD_STORY = {"id": 2, "type": "story", "dead": True, "time": NOON, "title": "spam"}
ASK_STORY = {"id": 4, "type": "story", "by": "dang", "time": NOON, "title": "Ask HN: How do you evaluate RAG?",
             "text": "I built a thing &amp; it works.<p>What do you think?"}
JOB = {"id": 5, "type": "job", "time": NOON, "title": "We're hiring"}


def routes(**overrides):
    items = {"1": LINK_STORY, "2": DEAD_STORY, "3": None, "4": ASK_STORY, "5": JOB, **overrides}
    return [("/topstories.json", [1, 2, 3, 4, 5])] + [(f"/item/{key}.json", value) for key, value in items.items()]


class HackerNewsWorkerTests(unittest.TestCase):
    def test_keeps_only_live_stories(self):
        result = HackerNewsWorker(FakeHttp(routes())).run(limit=5)
        self.assertEqual([i.source_item_id for i in result.items], ["1", "4"])
        self.assertEqual(result.skipped, 3)  # dead, deleted (null), job

    def test_link_story(self):
        item = HackerNewsWorker(FakeHttp([])).normalize(LINK_STORY)
        self.assertEqual(item.url, "https://example.com/vdb")
        self.assertEqual(item.published_at, datetime(2024, 5, 1, 12, tzinfo=UTC))
        self.assertEqual(item.author, "pg")
        self.assertIsNone(item.description)

    def test_ask_hn_links_to_discussion_and_strips_html(self):
        item = HackerNewsWorker(FakeHttp([])).normalize(ASK_STORY)
        self.assertEqual(item.url, "https://news.ycombinator.com/item?id=4")
        self.assertEqual(item.description, "I built a thing & it works.\n\nWhat do you think?")

    def test_limit_caps_requests(self):
        http = FakeHttp(routes())
        HackerNewsWorker(http).run(limit=2)
        self.assertEqual(len(http.calls), 3)  # the list + 2 items

    def test_one_failing_item_does_not_stop_the_run(self):
        result = HackerNewsWorker(FakeHttp(routes(**{"1": HttpError(500, "https://hn/item/1.json")}))).run(limit=5)
        self.assertEqual([i.source_item_id for i in result.items], ["4"])
        self.assertIn("item 1", result.errors[0])

    def test_rejects_unknown_list_name(self):
        with self.assertRaises(ConfigError):
            HackerNewsWorker(FakeHttp([]), "worststories")


if __name__ == "__main__":
    unittest.main()
