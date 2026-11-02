"""Tests for the Analysis snapshot builder."""

from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from build_analysis import (
    ANALYSIS_TICKERS,
    assemble_document,
    earnings_releases,
    parse_prospectus,
    parse_share_counts,
    prices_from_series,
    quarterly_facts,
    trailing_four_quarters,
)


SPCX = ANALYSIS_TICKERS[0]
PROSPECTUS = """
<html><body>
Founded in 2002, SpaceX is the only company building the integrated hardware and
software infrastructure of the future across space, connectivity, and AI. At our
core, we are builders. We design, manufacture, launch, and operate products and
services built on cutting-edge technologies. We safely and reliably transport
astronauts, satellites, and other payloads on missions that benefit life on Earth.
Elon Musk, our founder, Chief Executive Officer, will hold the voting power.
The initial public offering price of $135.00 per share.
</body></html>
"""
TENQ = """
<html><body>
As of October&nbsp;15, 2026, the registrant had 8,246,541,218 shares of Class A
common stock and 5,334,522,491 shares of Class B common stock outstanding.
</body></html>
"""


def _eps(end: str, start: str, value: float, frame: str | None = None) -> dict:
    return {"end": end, "start": start, "val": value, "frame": frame, "form": "10-Q"}


class BuildAnalysisTests(unittest.TestCase):
    def test_prices_before_ipo_are_dropped(self) -> None:
        series = pd.Series(
            [40.0, 135.0, 150.0],
            index=pd.to_datetime(["2024-01-02", "2026-06-11", "2026-06-12"]),
        )

        rows = prices_from_series(series, "2026-06-12")

        self.assertEqual(rows, [["2026-06-12", 150.0]])

    def test_earnings_releases_keep_only_item_2_02(self) -> None:
        submissions = {
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K", "10-Q"],
                    "items": ["5.02", "2.02,9.01", ""],
                    "filingDate": ["2026-06-17", "2026-10-20", "2026-10-22"],
                    "reportDate": ["2026-06-16", "2026-10-20", "2026-09-30"],
                }
            }
        }

        events = earnings_releases(submissions)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2026-10-20")
        self.assertTrue(events[0]["confirmed"])

    def test_trailing_eps_requires_four_quarters(self) -> None:
        short = [
            _eps("2026-06-30", "2026-04-01", -0.09, "CY2026Q2"),
            _eps("2026-09-30", "2026-07-01", 0.35, "CY2026Q3"),
        ]
        self.assertIsNone(trailing_four_quarters(short))
        full = short + [
            _eps("2025-12-31", "2025-10-01", 0.1, "CY2025Q4"),
            _eps("2026-03-31", "2026-01-01", 0.2, "CY2026Q1"),
            _eps("2026-09-30", "2026-01-01", -0.15),
        ]
        quarters = quarterly_facts(full)
        self.assertEqual(len(quarters), 4)
        self.assertNotIn(-0.15, [row["val"] for row in quarters])
        self.assertIsNotNone(trailing_four_quarters(full))
        gapped = [
            _eps("2025-09-30", "2025-07-01", -0.36, "CY2025Q3"),
            _eps("2026-06-30", "2026-04-01", -0.09, "CY2026Q2"),
            _eps("2026-09-30", "2026-07-01", 0.35, "CY2026Q3"),
            _eps("2025-06-30", "2025-04-01", -0.34, "CY2025Q2"),
        ]
        self.assertIsNone(trailing_four_quarters(gapped))

    def test_assemble_does_not_invent_forward_estimates(self) -> None:
        document = assemble_document(
            SPCX,
            [["2026-06-12", 160.0], ["2026-10-30", 134.72]],
            {
                "filings": {
                    "recent": {
                        "form": ["8-K"],
                        "items": ["2.02,9.01"],
                        "filingDate": ["2026-10-20"],
                        "reportDate": ["2026-10-20"],
                    }
                }
            },
            PROSPECTUS,
            TENQ,
            facts={"facts": {"us-gaap": {}}},
        )
        fundamentals = document["fundamentals"]

        self.assertIsNone(fundamentals["pe_ntm"]["value"])
        self.assertIsNone(fundamentals["eps_ntm"]["value"])
        self.assertIsNone(fundamentals["next_earnings_date"]["value"])
        self.assertIsNone(fundamentals["eps_ttm"]["value"])
        self.assertIsNone(fundamentals["pe_ttm"]["value"])
        self.assertEqual(fundamentals["ceo"]["value"], "Elon Musk")
        self.assertIn("Founded in 2002", fundamentals["description"]["value"])
        self.assertEqual(fundamentals["last_earnings_date"]["value"], "2026-10-20")
        self.assertEqual(document["prices"][0][0], "2026-06-12")
        self.assertAlmostEqual(fundamentals["market_cap"]["value"], round(8246541218 * 134.72, 2))
        self.assertIn("Class A", fundamentals["market_cap"]["source"])
        self.assertTrue(document["prompt"].startswith("Write a comprehensive"))

    def test_share_count_parser_reads_the_cover_sentence(self) -> None:
        parsed = parse_share_counts(TENQ)

        self.assertEqual(parsed["as_of"], "2026-10-15")
        self.assertEqual(parsed["class_a"], 8246541218)
        self.assertEqual(parsed["class_b"], 5334522491)

    def test_prospectus_parser_ignores_pages_without_the_sentences(self) -> None:
        self.assertEqual(parse_prospectus("<html>no overview</html>")["ceo"], None)
        self.assertIsNone(parse_prospectus("<html>no overview</html>")["description"])


if __name__ == "__main__":
    unittest.main()
