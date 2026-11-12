import unittest
from datetime import UTC, datetime

from ingest.parsing import (
    absolute_http_url,
    clean_description,
    clean_title,
    first_image_in_html,
    parse_feed_date,
    parse_iso8601_duration,
    parse_iso_datetime,
    parse_rfc822_datetime,
    strip_html,
)


class DurationTests(unittest.TestCase):
    def test_hours_minutes_seconds(self):
        self.assertEqual(parse_iso8601_duration("PT1H2M10S"), 3730)

    def test_partial_forms(self):
        self.assertEqual(parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(parse_iso8601_duration("PT10M"), 600)
        self.assertEqual(parse_iso8601_duration("P1DT2H"), 93600)

    def test_zero_duration_for_live_streams(self):
        self.assertEqual(parse_iso8601_duration("P0D"), 0)

    def test_missing_is_none(self):
        self.assertIsNone(parse_iso8601_duration(None))
        self.assertIsNone(parse_iso8601_duration(""))

    def test_malformed_raises_instead_of_guessing(self):
        for bad in ("garbage", "PT", "P", "1H2M", "P1DT"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_iso8601_duration(bad)


class DateTests(unittest.TestCase):
    NOON_UTC = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)

    def test_iso_with_z(self):
        self.assertEqual(parse_iso_datetime("2024-05-01T12:00:00Z"), self.NOON_UTC)

    def test_iso_offset_is_converted_to_utc(self):
        # The "5 hours off" bug from our discussion: an offset must shift the time.
        self.assertEqual(parse_iso_datetime("2024-05-01T14:00:00+02:00"), self.NOON_UTC)

    def test_naive_iso_is_treated_as_utc_not_local_time(self):
        result = parse_iso_datetime("2024-05-01T12:00:00")
        self.assertEqual(result, self.NOON_UTC)
        self.assertEqual(result.tzinfo, UTC)

    def test_rfc822(self):
        self.assertEqual(parse_rfc822_datetime("Wed, 01 May 2024 12:00:00 GMT"), self.NOON_UTC)
        self.assertEqual(parse_rfc822_datetime("Wed, 01 May 2024 14:00:00 +0200"), self.NOON_UTC)

    def test_feed_date_accepts_both_formats(self):
        self.assertEqual(parse_feed_date("2024-05-01T12:00:00Z"), self.NOON_UTC)
        self.assertEqual(parse_feed_date("Wed, 01 May 2024 12:00:00 GMT"), self.NOON_UTC)

    def test_feed_date_unreadable_is_none(self):
        self.assertIsNone(parse_feed_date("sometime last week"))
        self.assertIsNone(parse_feed_date(None))


class TextTests(unittest.TestCase):
    def test_strip_html_removes_tags_and_decodes_entities(self):
        self.assertEqual(strip_html("<p>Hello &amp; <b>world</b></p>"), "Hello & world")

    def test_strip_html_keeps_paragraphs_and_drops_scripts(self):
        html = "<p>One</p><script>alert(1)</script><p>Two<br>Three</p>"
        self.assertEqual(strip_html(html), "One\n\nTwo\nThree")

    def test_clean_title_collapses_whitespace(self):
        self.assertEqual(clean_title("  Retrieval at Scale:\n   A Study "), "Retrieval at Scale: A Study")

    def test_clean_description_limits_blank_lines_and_length(self):
        self.assertEqual(clean_description("a\r\n\r\n\r\n\r\nb"), "a\n\nb")
        self.assertEqual(clean_description("x" * 20, max_length=10), "x" * 9 + "…")
        self.assertIsNone(clean_description("   \n  "))


class ImageTests(unittest.TestCase):
    POST = "https://blog.example.com/2026/11/post/"

    def test_relative_urls_are_resolved_against_the_post(self):
        self.assertEqual(absolute_http_url("/img/hero.png", self.POST), "https://blog.example.com/img/hero.png")
        self.assertEqual(absolute_http_url("hero.png", self.POST), "https://blog.example.com/2026/11/post/hero.png")

    def test_only_http_urls_are_kept(self):
        for bad in ("", None, "data:image/png;base64,AAAA", "javascript:alert(1)", "ftp://x/y.png"):
            with self.subTest(value=bad):
                self.assertIsNone(absolute_http_url(bad, self.POST))

    def test_first_image_skips_tracking_pixels(self):
        # Medium appends an invisible 1x1 image to every post; the real image comes first or after.
        html = '<img src="https://medium.com/_/stat?event=post" width="1" height="1"><p>Hi</p><img src="https://cdn.example.com/hero.jpg">'
        self.assertEqual(first_image_in_html(html), "https://cdn.example.com/hero.jpg")

    def test_first_image_skips_inline_data_images(self):
        html = '<img src="data:image/gif;base64,R0lGOD"><img src="/hero.jpg">'
        self.assertEqual(first_image_in_html(html, self.POST), "https://blog.example.com/hero.jpg")

    def test_first_image_skips_emoji(self):
        # WordPress turns an emoji in a post into an <img>: found as the "first image"
        # of AWS Architecture and Slack Engineering posts.
        html = ('<img draggable="false" class="emoji" alt="✅" src="https://s.w.org/images/core/emoji/14.0.0/72x72/2705.png">'
                '<img class="wp-smiley" src="https://example.com/smile.png"><img src="https://cdn.example.com/diagram.png">')
        self.assertEqual(first_image_in_html(html), "https://cdn.example.com/diagram.png")

    def test_no_image(self):
        self.assertIsNone(first_image_in_html("<p>Just text</p>"))
        self.assertIsNone(first_image_in_html(None))


if __name__ == "__main__":
    unittest.main()
