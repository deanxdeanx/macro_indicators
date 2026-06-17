"""Unit tests for data transformation and metric calculations."""

from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

import pandas as pd

from data_sources import (
    DataSourceError,
    Indicator,
    _extract_close_prices,
    fetch_macro_data,
    frame_to_series,
    latest_metrics,
    series_freshness,
    validate_fred_response,
)


class DataSourceTests(unittest.TestCase):
    def test_latest_metrics_percent_change(self) -> None:
        series = pd.Series(
            [100.0, 110.0, 121.0],
            index=pd.to_datetime(["2025-12-31", "2026-01-01", "2026-02-01"]),
        )

        metrics = latest_metrics(series)

        self.assertAlmostEqual(metrics["previous_change"], 10.0)
        self.assertAlmostEqual(metrics["ytd_change"], 10.0)

    def test_latest_metrics_point_change(self) -> None:
        series = pd.Series(
            [4.0, 4.25, 4.5],
            index=pd.to_datetime(["2025-12-01", "2026-01-01", "2026-02-01"]),
        )

        metrics = latest_metrics(series, "points")

        self.assertAlmostEqual(metrics["previous_change"], 0.25)
        self.assertAlmostEqual(metrics["ytd_change"], 0.25)

    def test_extract_single_ticker_close_prices(self) -> None:
        download = pd.DataFrame(
            {"Close": [10.0, 11.0]},
            index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
        )

        close = _extract_close_prices(download, ["TEST"])

        self.assertEqual(list(close.columns), ["TEST"])
        self.assertEqual(close.iloc[-1, 0], 11.0)

    def test_indicator_defaults_to_percent_change(self) -> None:
        indicator = Indicator("TEST", "Test", "Test source", "points", "Daily")

        self.assertEqual(indicator.change_mode, "percent")

    def test_validate_fred_response_rejects_missing_series_column(self) -> None:
        frame = pd.DataFrame({"observation_date": ["2026-01-01"], "OTHER": [1.0]})

        with self.assertRaisesRegex(DataSourceError, "missing TEST"):
            validate_fred_response(frame, "TEST")

    def test_validate_fred_response_rejects_malformed_dates(self) -> None:
        frame = pd.DataFrame({"observation_date": ["not-a-date"], "TEST": [1.0]})

        with self.assertRaisesRegex(DataSourceError, "malformed observation dates"):
            validate_fred_response(frame, "TEST")

    def test_validate_fred_response_rejects_non_numeric_observations(self) -> None:
        frame = pd.DataFrame({"observation_date": ["2026-01-01"], "TEST": ["."]})

        with self.assertRaisesRegex(DataSourceError, "no numeric observations"):
            validate_fred_response(frame, "TEST")

    def test_series_freshness_uses_frequency_aware_thresholds(self) -> None:
        series = pd.Series([1.0], index=pd.to_datetime(["2026-04-01"]))

        monthly = series_freshness(series, "Monthly", as_of=date(2026, 6, 14))
        daily = series_freshness(series, "Daily", as_of=date(2026, 6, 14))

        self.assertFalse(monthly["is_stale"])
        self.assertTrue(daily["is_stale"])
        self.assertEqual(monthly["age_days"], 74)

    def test_frame_to_series_isolates_malformed_columns(self) -> None:
        frame = pd.DataFrame({"GOOD": [1.0], "BAD": [2.0]}, index=["not-a-date"])
        indicators = (
            Indicator("GOOD", "Good", "Test", "points", "Daily"),
            Indicator("MISSING", "Missing", "Test", "points", "Daily"),
        )

        with self.assertLogs("data_sources", level="ERROR"):
            data, errors = frame_to_series(frame, indicators)

        self.assertEqual(data, {})
        self.assertEqual(errors["GOOD"], "The source returned malformed price data.")
        self.assertEqual(errors["MISSING"], "No data returned.")

    def test_fetch_macro_data_isolates_indicator_failures(self) -> None:
        sample = pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))

        def fake_fetch(series_id, start_date, transform="none", end_date=None):
            if series_id == "UNRATE":
                raise TypeError("temporary failure")
            return sample.rename(series_id)

        with patch("data_sources.fetch_fred_series", side_effect=fake_fetch):
            data, errors = fetch_macro_data(date(2026, 1, 1), date(2026, 2, 1))

        self.assertNotIn("UNRATE", data)
        self.assertEqual(errors["UNRATE"], "temporary failure")
        self.assertIn("CPIAUCSL", data)


if __name__ == "__main__":
    unittest.main()
