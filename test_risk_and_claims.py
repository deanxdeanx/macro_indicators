"""Tests for risk-off colour flags, claims counts, and a failed FRED refresh."""

from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

from build_data import build_snapshot, fred_failure_summary, main
from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    format_claims_change,
    format_claims_level,
    lagged_change,
    range_percentile,
)


def _series(values: list[float], dates: list[str]) -> pd.Series:
    return pd.Series(values, index=pd.to_datetime(dates))


class RiskFlagTests(unittest.TestCase):
    def test_higher_values_are_flagged_as_risk_off(self) -> None:
        flagged = {item.key for item in (*MACRO_INDICATORS, *MARKET_INDICATORS) if item.higher_is_bad}

        for key in (
            "BAMLH0A0HYM2",
            "BAMLC0A0CM",
            "^VIX",
            "ICSA",
            "IC4WSA",
            "UNRATE",
            "DFII10",
            "DTWEXBGS",
            "DTWEXAFEGS",
        ):
            self.assertIn(key, flagged)
        for key in ("^GSPC", "NETLIQ", "A191RL1Q225SBEA", "UMCSENT", "T10Y2Y", "WALCL"):
            self.assertNotIn(key, flagged)

    def test_claims_are_counts_and_the_dollar_section_has_two_series(self) -> None:
        by_key = {item.key: item for item in MACRO_INDICATORS}

        self.assertEqual(by_key["ICSA"].change_mode, "count")
        self.assertEqual(by_key["IC4WSA"].change_mode, "count")
        self.assertEqual(by_key["ICSA"].unit, "claims")
        self.assertEqual(by_key["DTWEXAFEGS"].section, "Dollar")
        self.assertEqual(by_key["DTWEXBGS"].section, "Dollar")


class ClaimsFormatTests(unittest.TestCase):
    def test_level_uses_thousands_and_not_percentage_points(self) -> None:
        self.assertEqual(format_claims_level(218_000), "218K")
        self.assertEqual(format_claims_level(218_400), "218K")
        self.assertEqual(format_claims_level(85_000), "85K")
        self.assertEqual(format_claims_level(900), "900")
        self.assertNotIn("pp", format_claims_level(218_000))

    def test_change_is_a_signed_count(self) -> None:
        self.assertEqual(format_claims_change(4_200), "+4.2K")
        self.assertEqual(format_claims_change(-1_500), "-1.5K")
        self.assertEqual(format_claims_change(0), "0")
        self.assertNotIn("pp", format_claims_change(4_200))
        self.assertNotIn("%", format_claims_change(4_200))


class MoveTests(unittest.TestCase):
    def test_week_and_month_changes_use_the_prior_print(self) -> None:
        series = _series(
            [100.0, 110.0, 104.0],
            ["2026-10-01", "2026-10-26", "2026-11-02"],
        )

        self.assertAlmostEqual(lagged_change(series, 7, "points"), -6.0)
        self.assertAlmostEqual(lagged_change(series, 30, "percent"), 4.0)

    def test_percentile_uses_the_trailing_five_years(self) -> None:
        series = _series([1.0, 3.0, 2.0], ["2022-01-01", "2024-01-01", "2026-01-01"])

        self.assertAlmostEqual(range_percentile(series), 50.0)


class FredFailureTests(unittest.TestCase):
    def test_more_than_half_of_the_fred_series_is_a_failure(self) -> None:
        entries = [
            {"key": "UNRATE", "status": "preserved", "last_error": "FRED timed out while fetching UNRATE."},
            {"key": "DFF", "status": "preserved", "last_error": "FRED timed out while fetching DFF."},
            {"key": "DGS10", "status": "ok"},
        ]

        summary = fred_failure_summary(entries, ["UNRATE", "DFF", "DGS10"])

        self.assertIn("2 of 3 FRED series failed", summary)
        self.assertIn("Previous observations were kept", summary)
        self.assertIn("UNRATE:", summary)

    def test_half_of_the_fred_series_still_counts_as_a_success(self) -> None:
        entries = [
            {"key": "UNRATE", "status": "preserved", "last_error": "timed out"},
            {"key": "DFF", "status": "ok"},
        ]

        self.assertIsNone(fred_failure_summary(entries, ["UNRATE", "DFF"]))

    def test_main_exits_nonzero_but_keeps_the_report(self) -> None:
        report = {
            "generated_at": "2026-11-03T20:16:24Z",
            "history_start": "2020-01-01",
            "history_end": "2026-11-03",
            "counts": {"ok": 1, "preserved": 2, "error": 0},
            "entries": [],
            "with_data": 3,
            "fred_failure": "2 of 3 FRED series failed. Previous observations were kept.\nUNRATE: timed out",
        }

        with patch("build_data.build_snapshot", return_value=report):
            code = main([])

        self.assertEqual(code, 2)

    def test_snapshot_stores_week_change_and_the_risk_flag(self) -> None:
        from data_sources import Indicator

        indicator = Indicator(
            "DFF",
            "Effective Federal Funds Rate",
            "FRED",
            "%",
            "Daily",
            change_mode="points",
            higher_is_bad=True,
        )
        series = _series([4.0, 4.2, 4.5], ["2026-10-01", "2026-10-26", "2026-11-02"])

        def fetcher(item, start, end):
            return series

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            report = build_snapshot(Path(tmp), indicators=[("macro", indicator)], fetcher=fetcher, workers=1)

        entry = report["entries"][0]
        self.assertTrue(entry["higher_is_bad"])
        self.assertAlmostEqual(entry["week_change"], 0.3)
        self.assertAlmostEqual(entry["month_change"], 0.5)
        self.assertAlmostEqual(entry["range_percentile"], 100.0)


if __name__ == "__main__":
    unittest.main()
