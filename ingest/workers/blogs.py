"""Tech blogs worker: reads RSS 2.0 and Atom feeds.

Almost every engineering blog publishes a feed, which is far more reliable
than scraping HTML pages (and respects what the site chose to publish).

Which feeds to read comes from the `sources` table (worker = 'blog'), via
ingest/pipeline.py. This class only knows how to read the feeds it's given.
"""

import xml.etree.ElementTree as ET
from collections.abc import Iterator

from ..http import RequestError, Validators
from ..models import ContentItem, ContentType
from ..parsing import (FeedParseError, absolute_http_url, clean_description, clean_title, first_image_in_html,
                       parse_feed_date, strip_html, xml_text)
from .base import Worker

# XML namespaces: Atom elements are written {namespace}name by ElementTree.
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
MEDIA = "{http://search.yahoo.com/mrss/}"   # "Media RSS": media:thumbnail, media:content


class BlogsWorker(Worker):
    source = "blog"

    def __init__(self, http, feed_urls: list[str], validators: dict[str, Validators] | None = None):
        """`validators`: optional {feed_url: Validators} from the previous run.
        With them, an unchanged feed answers 304 and is skipped (conditional GET)."""
        super().__init__(http)
        self.feed_urls = feed_urls
        self.validators = validators or {}
        # Filled by fetch(), read by the pipeline after the run:
        self.new_validators: dict[str, Validators] = {}   # what to send next time
        self.not_modified: set[str] = set()               # feeds that answered 304

    def fetch(self, limit: int) -> Iterator[dict]:
        """`limit` = max posts per feed."""
        self.new_validators, self.not_modified = {}, set()
        for feed_url in self.feed_urls:
            try:
                response = self.http.get_conditional(feed_url, self.validators.get(feed_url))
                if response.not_modified:
                    self.not_modified.add(feed_url)
                    self.new_validators[feed_url] = response.validators
                    continue
                root = parse_xml(response.body)
                entries = feed_entries(root, feed_url)
            except (RequestError, FeedParseError) as error:
                self.fetch_errors.append(f"feed {feed_url}: {error}")
                continue
            # Only after the body parsed: a broken feed must not update the validators.
            self.new_validators[feed_url] = response.validators
            if logo := feed_logo(root, feed_url):
                self.source_logos[feed_url] = logo
            yield from entries[:limit]

    def normalize(self, raw: dict) -> ContentItem:
        return ContentItem(
            source=self.source,
            source_item_id=blog_item_id(raw),
            content_type=ContentType.ARTICLE,
            title=clean_title(raw.get("title")),
            url=raw.get("link") or "",
            published_at=parse_feed_date(raw.get("published")),
            author=raw.get("author") or raw.get("feed_title"),
            description=clean_description(strip_html(raw["summary"])) if raw.get("summary") else None,
            thumbnail_url=raw.get("image"),
            tags=tuple(raw.get("categories", [])),
            raw=raw,
        )


def blog_item_id(raw: dict) -> str:
    """A stable, globally unique ID for a feed entry.

    Feeds give each entry a guid (RSS) or id (Atom). Usually it's a URL or a
    'tag:'/'urn:' identifier, which is unique worldwide. But some blogs use
    bare values like '42', which would collide across blogs, so those get
    the feed URL as a prefix.
    """
    guid = raw.get("guid") or raw.get("link")
    if not guid:
        raise ValueError("entry has neither a guid/id nor a link")
    if "://" in guid or guid.startswith(("tag:", "urn:")):
        return guid
    return f"{raw['feed_url']}#{guid}"


def parse_feed(body: bytes, feed_url: str) -> list[dict]:
    """Parse RSS or Atom XML into plain dicts (our raw format for feeds)."""
    return feed_entries(parse_xml(body), feed_url)


def parse_xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as error:
        raise FeedParseError(f"not valid XML: {error}") from None


def feed_entries(root: ET.Element, feed_url: str) -> list[dict]:
    """The feed's entries as plain dicts.

    Each dict also keeps the entry's original XML under "xml", so the raw
    record is genuinely raw, not just the fields we chose to read.
    """
    if root.tag == f"{ATOM}feed":
        return [_atom_entry(entry, root, feed_url) for entry in root.findall(f"{ATOM}entry")]
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise FeedParseError("RSS feed has no <channel>")
        return [_rss_item(item, channel, feed_url) for item in channel.findall("item")]
    raise FeedParseError(f"unsupported feed type <{root.tag}>")


def feed_logo(root: ET.Element, feed_url: str) -> str | None:
    """The feed's own logo: one per feed, not per post.

      RSS:  <channel><image><url>
      Atom: <logo> (a wide image) first, else <icon> (a small square one)

    Relative URLs are resolved against the feed's URL.
    """
    if root.tag == f"{ATOM}feed":
        candidates = [xml_text(root, f"{ATOM}logo"), xml_text(root, f"{ATOM}icon")]
    else:
        candidates = [xml_text(root, "channel/image/url")]
    for candidate in candidates:
        if url := absolute_http_url(candidate, feed_url):
            return url
    return None


def _rss_item(item: ET.Element, channel: ET.Element, feed_url: str) -> dict:
    return {
        "format": "rss",
        "feed_url": feed_url,
        "feed_title": xml_text(channel, "title"),
        "guid": xml_text(item, "guid"),
        "title": xml_text(item, "title"),
        "link": xml_text(item, "link"),
        "published": xml_text(item, "pubDate") or xml_text(item, f"{DC}date"),
        "author": xml_text(item, f"{DC}creator") or xml_text(item, "author"),
        "summary": xml_text(item, "description") or xml_text(item, f"{CONTENT}encoded"),
        "categories": [c.text.strip() for c in item.findall("category") if c.text and c.text.strip()],
        "image": feed_image(item, xml_text(item, "link"),
                            xml_text(item, f"{CONTENT}encoded") or xml_text(item, "description")),
        "xml": ET.tostring(item, encoding="unicode"),
    }


def _atom_entry(entry: ET.Element, feed: ET.Element, feed_url: str) -> dict:
    return {
        "format": "atom",
        "feed_url": feed_url,
        "feed_title": xml_text(feed, f"{ATOM}title"),
        "guid": xml_text(entry, f"{ATOM}id"),
        "title": xml_text(entry, f"{ATOM}title"),
        "link": _atom_link(entry),
        "published": xml_text(entry, f"{ATOM}published") or xml_text(entry, f"{ATOM}updated"),
        "author": xml_text(entry, f"{ATOM}author/{ATOM}name") or xml_text(feed, f"{ATOM}author/{ATOM}name"),
        "summary": xml_text(entry, f"{ATOM}summary") or xml_text(entry, f"{ATOM}content"),
        "categories": [c.get("term") for c in entry.findall(f"{ATOM}category") if c.get("term")],
        "image": feed_image(entry, _atom_link(entry),
                            xml_text(entry, f"{ATOM}content") or xml_text(entry, f"{ATOM}summary")),
        "xml": ET.tostring(entry, encoding="unicode"),
    }


def feed_image(element: ET.Element, page_url: str | None, html: str | None) -> str | None:
    """The entry's image, checking the most deliberate markup first:

      1. <media:thumbnail>               a thumbnail, by definition
      2. <media:content medium="image">  an image attached to the entry
      3. <enclosure type="image/...">    RSS's attachment (Atom: <link rel="enclosure">)
      4. the first real <img> in the post's HTML (often the hero image)

    Relative URLs are resolved against the post's own URL.
    """
    for thumb in element.iter(f"{MEDIA}thumbnail"):
        if url := absolute_http_url(thumb.get("url"), page_url):
            return url
    for media in element.iter(f"{MEDIA}content"):
        if media.get("medium") == "image" or (media.get("type") or "").startswith("image/"):
            if url := absolute_http_url(media.get("url"), page_url):
                return url
    for enclosure in [*element.findall("enclosure"), *element.findall(f"{ATOM}link[@rel='enclosure']")]:
        if (enclosure.get("type") or "").startswith("image/"):
            if url := absolute_http_url(enclosure.get("url") or enclosure.get("href"), page_url):
                return url
    return first_image_in_html(html, page_url)


def _atom_link(entry: ET.Element) -> str | None:
    """Atom entries can have several <link>s; the article is rel="alternate" (the default)."""
    for link in entry.findall(f"{ATOM}link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href")
    return None
