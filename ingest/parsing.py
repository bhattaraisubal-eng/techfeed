"""Pure helpers that turn messy source values into clean Python values.

"Pure" = no network, no global state, same input -> same output. That makes
them the easiest code to test, so the fiddly logic (durations, dates, HTML,
XML) lives here instead of inside the workers.
"""

import email.utils
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html.parser import HTMLParser


class FeedParseError(ValueError):
    """An RSS/Atom/arXiv response could not be understood."""


# ---------- durations ----------

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_iso8601_duration(value: str | None) -> int | None:
    """'PT1H2M10S' -> 3730 seconds. YouTube uses this format.

    Returns None for a missing value; raises ValueError for a malformed one,
    so bad data is noticed instead of silently becoming 0.
    """
    if value is None or value == "":
        return None
    text = value.strip()
    match = _DURATION_RE.match(text)
    if not match or text in ("P", "PT") or text.endswith("T"):
        raise ValueError(f"not an ISO 8601 duration: {value!r}")
    part = {name: float(number) if number else 0.0 for name, number in match.groupdict().items()}
    return round(part["days"] * 86400 + part["hours"] * 3600 + part["minutes"] * 60 + part["seconds"])


# ---------- dates ----------

def to_utc(moment: datetime) -> datetime:
    """Make a datetime timezone-aware and convert it to UTC.

    A *naive* datetime (no timezone) is assumed to already be UTC. Every
    source we use documents UTC, and guessing "local time" is exactly the
    5-hours-off bug from our earlier discussion.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_iso_datetime(value: str) -> datetime:
    """'2024-05-01T12:00:00Z' or '...+02:00' -> aware UTC datetime. Used by YouTube, Atom, arXiv."""
    return to_utc(datetime.fromisoformat(value.strip()))


def parse_rfc822_datetime(value: str) -> datetime:
    """'Wed, 01 May 2024 12:00:00 GMT' -> aware UTC datetime. Used by RSS feeds."""
    return to_utc(email.utils.parsedate_to_datetime(value.strip()))


def parse_feed_date(value: str | None) -> datetime | None:
    """Try both formats feeds use. Returns None if neither works.

    Feeds are messy, so an unreadable date isn't an exception here; the
    validator then rejects the item with a clear reason.
    """
    if not value:
        return None
    for parse in (parse_iso_datetime, parse_rfc822_datetime):
        try:
            return parse(value)
        except (ValueError, TypeError, IndexError):
            continue
    return None


# ---------- text ----------

def clean_title(value: str | None) -> str:
    """Collapse all whitespace (including newlines) to single spaces."""
    return " ".join(value.split()) if value else ""


def clean_description(value: str | None, max_length: int = 5000) -> str | None:
    """Tidy multi-line text: keep paragraph breaks, drop extra blank lines, cap the length."""
    if not value:
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "blockquote", "pre", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)  # turns &amp; into & for us
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def strip_html(html: str | None) -> str:
    """'<p>Hello &amp; <b>world</b></p>' -> 'Hello & world'. Keeps paragraph breaks."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return clean_description("".join(parser.parts), max_length=10**9) or ""


# ---------- XML ----------

def xml_text(element: ET.Element, path: str) -> str | None:
    """Text inside the first element matching `path`, or None if missing or empty."""
    node = element.find(path)
    if node is None:
        return None
    text = "".join(node.itertext()).strip()
    return text or None
