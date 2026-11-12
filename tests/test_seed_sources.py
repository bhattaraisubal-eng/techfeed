"""Tests for turning CSV rows into `sources` values.

These test the pure conversion only: no database needed. The upsert itself
will be tested against a real test database in the store-stage step.
"""

import tempfile
import unittest
from pathlib import Path

from ingest.seed_sources import csv_row_to_source, read_sources

ROW = {
    "worker": "blog", "locator": "https://netflixtechblog.com/feed", "name": "Netflix Tech Blog",
    "homepage_url": "https://netflixtechblog.com/", "tier": "2", "official": "true",
    "status": "active", "notes": "",
}


class CsvRowToSourceTests(unittest.TestCase):
    def test_converts_types(self):
        source = csv_row_to_source(ROW)
        self.assertEqual(source["tier"], 2)            # "2" -> 2
        self.assertIs(source["official"], True)        # "true" -> True
        self.assertIsNone(source["notes"])             # "" -> None (NULL), not an empty string

    def test_optional_cells_can_be_empty(self):
        source = csv_row_to_source({**ROW, "tier": "", "homepage_url": "", "official": "", "status": ""})
        self.assertIsNone(source["tier"])
        self.assertIsNone(source["homepage_url"])
        self.assertIs(source["official"], True)        # default
        self.assertEqual(source["status"], "active")   # default

    def test_unofficial(self):
        self.assertIs(csv_row_to_source({**ROW, "official": "false"})["official"], False)

    def test_whitespace_is_trimmed(self):
        self.assertEqual(csv_row_to_source({**ROW, "name": "  Netflix  "})["name"], "Netflix")

    def test_required_columns(self):
        for column in ("worker", "locator", "name"):
            with self.subTest(column=column), self.assertRaisesRegex(ValueError, column):
                csv_row_to_source({**ROW, column: " "})

    def test_bad_values_are_rejected(self):
        with self.assertRaises(ValueError):
            csv_row_to_source({**ROW, "tier": "two"})
        with self.assertRaisesRegex(ValueError, "official"):
            csv_row_to_source({**ROW, "official": "yes"})


    def test_logo_override(self):
        self.assertIsNone(csv_row_to_source(ROW)["logo_override"])     # the column is optional
        url = "https://netflixtechblog.com/logo.png"
        self.assertEqual(csv_row_to_source({**ROW, "logo_override": url})["logo_override"], url)
        with self.assertRaisesRegex(ValueError, "logo_override"):
            csv_row_to_source({**ROW, "logo_override": "logo.png"})    # must be a full http(s) URL

class ReadSourcesTests(unittest.TestCase):
    def test_error_names_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.csv"
            path.write_text(
                "worker,locator,name,homepage_url,tier,official,status,notes\n"
                "blog,https://a.com/feed,A,,1,true,active,\n"
                "blog,https://b.com/feed,,,1,true,active,\n"   # line 3: no name
            )
            with self.assertRaisesRegex(ValueError, "line 3: name is empty"):
                read_sources(path)

    def test_the_real_file_is_valid(self):
        # Catches a typo in data/sources.csv before it reaches the database.
        sources = read_sources(Path(__file__).parent.parent / "data" / "sources.csv")
        self.assertGreater(len(sources), 0)


if __name__ == "__main__":
    unittest.main()
