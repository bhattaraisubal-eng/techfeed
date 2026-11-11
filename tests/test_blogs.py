import unittest
from datetime import UTC, datetime

from ingest.http import ConditionalResponse, HttpError, Validators
from ingest.parsing import FeedParseError
from ingest.workers.blogs import BlogsWorker, parse_feed
from tests.fakes import FakeHttp

RSS_URL = "https://feeds.example.com/rss"
ATOM_URL = "https://atom.example.com/feed.atom"

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Example Eng Blog</title>
    <item>
      <title>Scaling Postgres &amp; friends</title>
      <link>https://blog.example.com/postgres</link>
      <guid isPermaLink="false">post-42</guid>
      <pubDate>Wed, 01 May 2024 14:00:00 +0200</pubDate>
      <dc:creator>Ada</dc:creator>
      <description><![CDATA[<p>We moved to <b>partitioning</b>.</p>]]></description>
      <category>databases</category>
    </item>
    <item>
      <title>No date here</title>
      <link>https://blog.example.com/nodate</link>
    </item>
  </channel>
</rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Blog</title>
  <author><name>Feed Author</name></author>
  <entry>
    <id>tag:atom.example.com,2024:1</id>
    <title>Vector search notes</title>
    <link rel="self" href="https://atom.example.com/1.atom"/>
    <link rel="alternate" href="https://atom.example.com/vectors"/>
    <published>2024-05-01T12:00:00Z</published>
    <summary type="html">&lt;p&gt;HNSW &amp;amp; IVF&lt;/p&gt;</summary>
    <category term="search"/>
  </entry>
</feed>"""

NOON_UTC = datetime(2024, 5, 1, 12, tzinfo=UTC)


class BlogsWorkerTests(unittest.TestCase):
    def test_rss_item(self):
        result = BlogsWorker(FakeHttp([(RSS_URL, RSS)]), [RSS_URL]).run(limit=10)
        item = result.items[0]
        self.assertEqual(item.source_item_id, f"{RSS_URL}#post-42")  # bare guid gets the feed prefix
        self.assertEqual(item.title, "Scaling Postgres & friends")
        self.assertEqual(item.published_at, NOON_UTC)                # +0200 converted to UTC
        self.assertEqual(item.author, "Ada")
        self.assertEqual(item.description, "We moved to partitioning.")
        self.assertEqual(item.tags, ("databases",))
        self.assertIn("<guid", item.raw["xml"])                      # the original XML is kept

    def test_rss_item_without_date_is_rejected_with_reason(self):
        result = BlogsWorker(FakeHttp([(RSS_URL, RSS)]), [RSS_URL]).run(limit=10)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("published_at is missing", result.rejected[0].reasons)

    def test_atom_entry(self):
        item = BlogsWorker(FakeHttp([(ATOM_URL, ATOM)]), [ATOM_URL]).run(limit=10).items[0]
        self.assertEqual(item.source_item_id, "tag:atom.example.com,2024:1")
        self.assertEqual(item.url, "https://atom.example.com/vectors")  # rel=alternate, not rel=self
        self.assertEqual(item.author, "Feed Author")                    # falls back to the feed's author
        self.assertEqual(item.description, "HNSW & IVF")
        self.assertEqual(item.published_at, NOON_UTC)

    def test_broken_feeds_are_recorded_and_others_continue(self):
        http = FakeHttp([
            ("https://down.example.com", HttpError(404, "https://down.example.com/feed")),
            ("https://html.example.com", b"<html><body>not a feed"),
            (ATOM_URL, ATOM),
        ])
        result = BlogsWorker(http, ["https://down.example.com/feed", "https://html.example.com/feed", ATOM_URL]).run(limit=10)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(len(result.errors), 2)

    def test_limit_is_per_feed(self):
        result = BlogsWorker(FakeHttp([(RSS_URL, RSS)]), [RSS_URL]).run(limit=1)
        self.assertEqual(result.fetched, 1)

    def test_unknown_root_element(self):
        with self.assertRaises(FeedParseError):
            parse_feed(b"<html></html>", "https://x")

    def test_unchanged_feed_is_skipped(self):
        http = FakeHttp([(RSS_URL, ConditionalResponse(None, Validators('"v1"')))])   # a 304
        worker = BlogsWorker(http, [RSS_URL], {RSS_URL: Validators('"v1"')})
        result = worker.run(limit=10)
        self.assertEqual((result.fetched, result.errors), (0, []))   # nothing to parse, and not an error
        self.assertEqual(worker.not_modified, {RSS_URL})
        self.assertEqual(http.calls[0].headers, {"If-None-Match": '"v1"'})

    def test_new_validators_are_kept_for_the_pipeline(self):
        http = FakeHttp([(RSS_URL, ConditionalResponse(RSS, Validators('"v2"', "Wed, 11 Nov 2026 10:00:00 GMT")))])
        worker = BlogsWorker(http, [RSS_URL])
        worker.run(limit=10)
        self.assertEqual(worker.new_validators[RSS_URL], Validators('"v2"', "Wed, 11 Nov 2026 10:00:00 GMT"))

    def test_broken_feed_does_not_update_validators(self):
        http = FakeHttp([(RSS_URL, ConditionalResponse(b"<html>not a feed", Validators('"v2"')))])
        worker = BlogsWorker(http, [RSS_URL])
        worker.run(limit=10)
        self.assertNotIn(RSS_URL, worker.new_validators)


if __name__ == "__main__":
    unittest.main()
