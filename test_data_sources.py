"""Unit tests for data transformation and metric calculations."""

from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from data_sources import (
    DataSourceError,
    Indicator,
    fetch_fred_series,
    fetch_macro_data,
    fetch_yahoo_chart_series,
    latest_metrics,
    observation_window,
    parse_yahoo_chart,
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

    def test_observation_window_keeps_six_years_and_defaults_to_five(self) -> None:
        history, default, today = observation_window(date(2026, 11, 2))

        self.assertEqual(history, date(2020, 1, 1))
        self.assertEqual(default, date(2021, 1, 1))
        self.assertEqual(today, date(2026, 11, 2))

    def test_fetch_fred_series_passes_the_explicit_timeout(self) -> None:
        response = Mock(status_code=200, text="observation_date,TEST\n2026-01-01,1.5\n")
        response.raise_for_status.return_value = None

        with patch("data_sources.requests.get", return_value=response) as request:
            series = fetch_fred_series(
                "TEST",
                date(2026, 1, 1),
                end_date=date(2026, 2, 1),
                timeout=7,
                attempts=1,
            )

        self.assertEqual(request.call_args.kwargs["timeout"], (5.0, 7))
        self.assertEqual(float(series.iloc[-1]), 1.5)

    def test_parse_yahoo_chart_prefers_adjusted_close(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1761000000, 1761086400],
                        "indicators": {
                            "quote": [{"close": [10.0, 11.0]}],
                            "adjclose": [{"adjclose": [9.5, 10.5]}],
                        },
                    }
                ],
                "error": None,
            }
        }

        series = parse_yahoo_chart(payload, "TEST")

        self.assertAlmostEqual(float(series.iloc[-1]), 10.5)
        self.assertEqual(series.index[-1].hour, 0)
        self.assertEqual(len(series), 2)

    def test_parse_yahoo_chart_drops_null_closes_and_rejects_errors(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1761000000, 1761086400],
                        "indicators": {"quote": [{"close": [None, 4.0]}]},
                    }
                ],
                "error": None,
            }
        }

        series = parse_yahoo_chart(payload, "TEST")

        self.assertEqual(len(series), 1)
        self.assertEqual(float(series.iloc[-1]), 4.0)
        with self.assertRaisesRegex(DataSourceError, "rejected the request"):
            parse_yahoo_chart({"chart": {"result": None, "error": {"description": "No data found"}}}, "TEST")

    def test_yahoo_chart_tries_the_next_host_after_a_rate_limit(self) -> None:
        import requests

        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1761000000],
                        "indicators": {"quote": [{"close": [3.0]}]},
                    }
                ],
                "error": None,
            }
        }

        class Response:
            def __init__(self, status: int, body: dict | None = None) -> None:
                self.status_code = status
                self._body = body or {}
                self.ok = 200 <= status < 300
                self.text = ""

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    error = requests.HTTPError(response=self)
                    raise error

            def json(self) -> dict:
                return self._body

        responses = [Response(429), Response(200, payload)]

        def fake_get(url, params=None, timeout=None):
            return responses.pop(0)

        with patch("data_sources.requests.Session.get", side_effect=fake_get), patch("data_sources.time.sleep"):
            series = fetch_yahoo_chart_series("TEST", date(2026, 1, 1), date(2026, 1, 3), attempts=1, timeout=1)

        self.assertEqual(float(series.iloc[-1]), 3.0)
        self.assertEqual(responses, [])


if __name__ == "__main__":
    unittest.main()
