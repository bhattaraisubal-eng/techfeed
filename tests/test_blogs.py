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


def rss_item(inner: str) -> bytes:
    """A one-item RSS feed; `inner` is extra markup inside <item>."""
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel><title>Images Blog</title>
    <item>
      <title>Post</title><link>https://blog.example.com/posts/1/</link><guid>https://blog.example.com/posts/1/</guid>
      <pubDate>Wed, 01 May 2024 14:00:00 +0000</pubDate>
      {inner}
    </item>
  </channel>
</rss>""".encode()


class FeedImageTests(unittest.TestCase):
    def image(self, inner: str):
        return BlogsWorker(FakeHttp([(RSS_URL, rss_item(inner))]), [RSS_URL]).run(limit=10).items[0].thumbnail_url

    def test_media_thumbnail(self):
        self.assertEqual(self.image('<media:thumbnail url="https://cdn.example.com/t.jpg"/>'), "https://cdn.example.com/t.jpg")

    def test_media_content_image(self):
        self.assertEqual(self.image('<media:content url="https://cdn.example.com/c.jpg" medium="image"/>'),
                         "https://cdn.example.com/c.jpg")

    def test_media_content_video_is_ignored(self):
        self.assertIsNone(self.image('<media:content url="https://cdn.example.com/v.mp4" medium="video"/>'))

    def test_image_enclosure(self):
        self.assertEqual(self.image('<enclosure url="https://cdn.example.com/e.png" type="image/png" length="1"/>'),
                         "https://cdn.example.com/e.png")

    def test_podcast_enclosure_is_not_an_image(self):
        self.assertIsNone(self.image('<enclosure url="https://cdn.example.com/ep.mp3" type="audio/mpeg" length="1"/>'))

    def test_falls_back_to_first_img_in_the_post_html(self):
        html = '<![CDATA[<p>Intro</p><img src="/images/hero.png"><img src="/images/second.png">]]>'
        self.assertEqual(self.image(f"<content:encoded>{html}</content:encoded>"), "https://blog.example.com/images/hero.png")

    def test_thumbnail_wins_over_body_image(self):
        inner = ('<media:thumbnail url="https://cdn.example.com/t.jpg"/>'
                 '<content:encoded><![CDATA[<img src="https://cdn.example.com/body.jpg">]]></content:encoded>')
        self.assertEqual(self.image(inner), "https://cdn.example.com/t.jpg")

    def test_no_image(self):
        self.assertIsNone(self.image("<description>Just text</description>"))

    def test_atom_enclosure_link(self):
        atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
          <entry><id>tag:a,2024:1</id><title>T</title><published>2024-05-01T12:00:00Z</published>
            <link rel="alternate" href="https://a.example.com/1"/>
            <link rel="enclosure" type="image/jpeg" href="https://a.example.com/1.jpg"/></entry></feed>"""
        item = BlogsWorker(FakeHttp([(ATOM_URL, atom)]), [ATOM_URL]).run(limit=10).items[0]
        self.assertEqual(item.thumbnail_url, "https://a.example.com/1.jpg")


if __name__ == "__main__":
    unittest.main()
