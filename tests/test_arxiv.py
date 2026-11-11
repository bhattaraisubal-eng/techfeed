import unittest
from datetime import UTC, datetime

from ingest.workers.arxiv import ArxivWorker, parse_arxiv_feed
from tests.fakes import FakeHttp

QUERY = "cat:cs.IR"
HEADER = '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'

ENTRY = """
<entry>
  <id>http://arxiv.org/abs/2405.01234v2</id>
  <updated>2024-05-03T10:00:00Z</updated>
  <published>2024-05-01T12:00:00Z</published>
  <title>Retrieval at Scale:
    A Study</title>
  <summary>  We study retrieval
  at scale. It works.  </summary>
  <author><name>A. One</name></author>
  <author><name>B. Two</name></author>
  <author><name>C. Three</name></author>
  <author><name>D. Four</name></author>
  <link href="http://arxiv.org/abs/2405.01234v2" rel="alternate" type="text/html"/>
  <link title="pdf" href="http://arxiv.org/pdf/2405.01234v2" rel="related" type="application/pdf"/>
  <arxiv:primary_category term="cs.IR"/>
  <category term="cs.IR"/>
  <category term="cs.CL"/>
</entry>"""


def feed(*entries: str) -> bytes:
    return (HEADER + "".join(entries) + "</feed>").encode()


def numbered_entry(n: int) -> str:
    return ENTRY.replace("2405.01234", f"2405.{n:05d}")


class ArxivWorkerTests(unittest.TestCase):
    def test_parses_and_normalizes_a_paper(self):
        result = ArxivWorker(FakeHttp([("arxiv.org", feed(ENTRY))]), QUERY).run(limit=5)
        item = result.items[0]
        self.assertEqual(item.source_item_id, "2405.01234")       # version stripped
        self.assertEqual(item.raw["version"], 2)                  # ...but kept in raw
        self.assertEqual(item.url, "https://arxiv.org/abs/2405.01234")
        self.assertEqual(item.title, "Retrieval at Scale: A Study")
        self.assertEqual(item.description, "We study retrieval at scale. It works.")
        self.assertEqual(item.author, "A. One, B. Two, C. Three et al.")
        self.assertEqual(item.published_at, datetime(2024, 5, 1, 12, tzinfo=UTC))
        self.assertEqual(item.tags, ("cs.IR", "cs.CL"))
        self.assertEqual(item.raw["pdf_url"], "http://arxiv.org/pdf/2405.01234v2")

    def test_sends_query_sorted_newest_first(self):
        http = FakeHttp([("arxiv.org", feed(ENTRY))])
        ArxivWorker(http, query="cat:cs.IR").run(limit=5)
        params = http.calls[0].params
        self.assertEqual(params["search_query"], "cat:cs.IR")
        self.assertEqual((params["sortBy"], params["sortOrder"]), ("submittedDate", "descending"))
        self.assertEqual(params["max_results"], 5)

    def test_pages_and_waits_between_requests(self):
        def pages(url, params):
            count = 100 if params["start"] == 0 else 30
            return feed(*(numbered_entry(params["start"] + n) for n in range(count)))

        sleeps = []
        http = FakeHttp([("arxiv.org", pages)])
        result = ArxivWorker(http, QUERY, sleep=sleeps.append).run(limit=150)
        self.assertEqual(len(result.items), 130)
        self.assertEqual([c.params["start"] for c in http.calls], [0, 100])
        self.assertEqual(sleeps, [3])  # polite delay before the second page only

    def test_api_error_entry_is_recorded(self):
        error_entry = "<entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><summary>bad query</summary></entry>"
        result = ArxivWorker(FakeHttp([("arxiv.org", feed(error_entry))]), QUERY).run(limit=5)
        self.assertEqual(result.items, [])
        self.assertIn("bad query", result.errors[0])

    def test_old_style_ids(self):
        old = ENTRY.replace("2405.01234v2", "hep-th/9901001v1")
        self.assertEqual(parse_arxiv_feed(feed(old))[0]["arxiv_id"], "hep-th/9901001")


if __name__ == "__main__":
    unittest.main()
