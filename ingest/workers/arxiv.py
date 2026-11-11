"""arXiv worker: the newest research papers in chosen categories.

The arXiv API returns Atom XML. Its terms ask clients to wait 3 seconds
between requests, so paging sleeps between pages.

Which query to run comes from the `sources` table (worker = 'arxiv'): the
locator is an arXiv search query, e.g. "cat:cs.IR".
"""

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator

from ..http import RequestError
from ..models import ContentItem, ContentType
from ..parsing import FeedParseError, clean_description, clean_title, parse_iso_datetime, xml_text
from .base import Worker

API_URL = "https://export.arxiv.org/api/query"
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 3

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

# 'http://arxiv.org/abs/2405.01234v2' -> id '2405.01234', version '2'
_ID_RE = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<version>\d+))?$")


class ArxivWorker(Worker):
    source = "arxiv"

    def __init__(self, http, query: str, sleep: Callable[[float], None] = time.sleep):
        super().__init__(http)
        self.query = query
        self._sleep = sleep

    def fetch(self, limit: int) -> Iterator[dict]:
        """`limit` = max papers in total, newest submissions first."""
        start = 0
        while start < limit:
            if start:
                self._sleep(REQUEST_DELAY_SECONDS)
            size = min(PAGE_SIZE, limit - start)
            params = {"search_query": self.query, "start": start, "max_results": size,
                      "sortBy": "submittedDate", "sortOrder": "descending"}
            try:
                entries = parse_arxiv_feed(self.http.get_bytes(API_URL, params=params))
            except (RequestError, FeedParseError) as error:
                self.fetch_errors.append(f"page starting at {start}: {error}")
                return
            yield from entries
            if len(entries) < size:  # fewer than asked for: no more results
                return
            start += size

    def normalize(self, raw: dict) -> ContentItem:
        arxiv_id = raw.get("arxiv_id")
        if not arxiv_id:
            raise ValueError(f"could not read an arXiv ID from {raw.get('entry_id')!r}")
        authors = raw.get("authors") or []
        author = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "") if authors else None
        return ContentItem(
            source=self.source,
            # Versionless ID: v1 and v2 of a paper are the same item. The upsert
            # will update it when a new version appears; the version is in raw.
            source_item_id=arxiv_id,
            content_type=ContentType.PAPER,
            title=clean_title(raw.get("title")),
            url=f"https://arxiv.org/abs/{arxiv_id}",
            published_at=parse_iso_datetime(raw["published"]),
            author=author,
            # Abstracts are hard-wrapped at ~80 chars; join them into one paragraph.
            description=clean_description(clean_title(raw.get("summary"))),
            tags=tuple(raw.get("categories", [])),
            raw=raw,
        )


def parse_arxiv_feed(body: bytes) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise FeedParseError(f"not valid XML: {error}") from None

    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_id = xml_text(entry, f"{ATOM}id") or ""
        if "/api/errors" in entry_id:  # arXiv reports bad queries as a fake entry
            raise FeedParseError(f"arXiv returned an error: {xml_text(entry, f'{ATOM}summary')}")
        match = _ID_RE.search(entry_id)
        primary = entry.find(f"{ARXIV}primary_category")
        entries.append({
            "entry_id": entry_id,
            "arxiv_id": match["id"] if match else None,
            "version": int(match["version"]) if match and match["version"] else None,
            "title": xml_text(entry, f"{ATOM}title"),
            "summary": xml_text(entry, f"{ATOM}summary"),
            "published": xml_text(entry, f"{ATOM}published"),
            "updated": xml_text(entry, f"{ATOM}updated"),
            "authors": [name for a in entry.findall(f"{ATOM}author") if (name := xml_text(a, f"{ATOM}name"))],
            "categories": [c.get("term") for c in entry.findall(f"{ATOM}category") if c.get("term")],
            "primary_category": primary.get("term") if primary is not None else None,
            "pdf_url": next((l.get("href") for l in entry.findall(f"{ATOM}link") if l.get("title") == "pdf"), None),
            "xml": ET.tostring(entry, encoding="unicode"),
        })
    return entries
