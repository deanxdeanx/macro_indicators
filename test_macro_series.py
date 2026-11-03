"""Tests for the FRED API fallback and the derived macro series."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from build_data import build_snapshot, choose_dollar_series, net_liquidity_series
from data_sources import (
    MACRO_INDICATORS,
    Indicator,
    fetch_fred_series,
    series_from_fred_observations,
)


def _series(values: list[float], dates: list[str], name: str = "X") -> pd.Series:
    return pd.Series(values, index=pd.to_datetime(dates), name=name)


class FredApiTests(unittest.TestCase):
    def test_observations_skip_missing_values(self) -> None:
        payload = {
            "observations": [
                {"date": "2026-11-02", "value": "4.10"},
                {"date": "2026-11-03", "value": "."},
                {"date": "2026-11-04", "value": "4.25"},
            ]
        }

        series = series_from_fred_observations(payload, "DGS10")

        self.assertEqual(list(series.index.strftime("%Y-%m-%d")), ["2026-11-02", "2026-11-04"])
        self.assertAlmostEqual(float(series.iloc[-1]), 4.25)

    def test_api_key_uses_the_official_endpoint(self) -> None:
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"observations": [{"date": "2026-11-03", "value": "3.9"}]}

        with patch("data_sources.requests.get", return_value=response) as request:
            series = fetch_fred_series(
                "DFF",
                date(2026, 11, 1),
                end_date=date(2026, 11, 3),
                timeout=9,
                attempts=1,
                api_key="test-key",
            )

        self.assertEqual(request.call_args.args[0], "https://api.stlouisfed.org/fred/series/observations")
        self.assertEqual(request.call_args.kwargs["params"]["api_key"], "test-key")
        self.assertAlmostEqual(float(series.iloc[-1]), 3.9)

    def test_api_failure_falls_back_to_csv(self) -> None:
        api_response = Mock(status_code=500)
        api_response.raise_for_status.side_effect = __import__("requests").HTTPError(response=api_response)
        csv_response = Mock(status_code=200, text="observation_date,DFF\n2026-11-03,3.8\n")
        csv_response.raise_for_status.return_value = None

        with patch("data_sources.requests.get", side_effect=[api_response, csv_response]) as request, patch(
            "data_sources.time.sleep"
        ):
            series = fetch_fred_series(
                "DFF",
                date(2026, 11, 1),
                end_date=date(2026, 11, 3),
                timeout=9,
                attempts=1,
                api_key="test-key",
            )

        self.assertEqual(request.call_count, 2)
        self.assertIn("fredgraph.csv", request.call_args_list[1].args[0])
        self.assertAlmostEqual(float(series.iloc[-1]), 3.8)

    def test_without_a_key_the_csv_endpoint_is_used(self) -> None:
        response = Mock(status_code=200, text="observation_date,DFF\n2026-11-03,3.7\n")
        response.raise_for_status.return_value = None

        with patch("data_sources.requests.get", return_value=response) as request:
            fetch_fred_series(
                "DFF",
                date(2026, 11, 1),
                end_date=date(2026, 11, 3),
                timeout=9,
                attempts=1,
                api_key="",
            )

        self.assertEqual(request.call_count, 1)
        self.assertIn("fredgraph.csv", request.call_args.args[0])

    def test_retries_sleep_with_exponential_backoff(self) -> None:
        import requests

        failed = Mock(status_code=503)
        failed.raise_for_status.side_effect = requests.HTTPError(response=failed)
        recovered = Mock(status_code=200, text="observation_date,DFF\n2026-11-03,3.6\n")
        recovered.raise_for_status.return_value = None

        with patch("data_sources.requests.get", side_effect=[failed, failed, recovered]), patch(
            "data_sources.time.sleep"
        ) as sleeper:
            series = fetch_fred_series(
                "DFF",
                date(2026, 11, 1),
                end_date=date(2026, 11, 3),
                timeout=9,
                attempts=3,
                retry_backoff=1.0,
                api_key="",
            )

        self.assertEqual([call.args[0] for call in sleeper.call_args_list], [1.0, 2.0])
        self.assertAlmostEqual(float(series.iloc[-1]), 3.6)


class DerivedSeriesTests(unittest.TestCase):
    def test_net_liquidity_aligns_units_and_subtracts_tga_and_rrp(self) -> None:
        walcl = _series([1000.0, 1100.0], ["2026-11-04", "2026-11-11"])
        tga = _series([100.0, 150.0], ["2026-11-04", "2026-11-11"])
        rrp = _series([20.0, 20.0, 30.0, 10.0], ["2026-11-04", "2026-11-05", "2026-11-11", "2026-11-12"])

        net = net_liquidity_series(walcl, tga, rrp)

        self.assertAlmostEqual(float(net.loc["2026-11-04"]), 880.0)
        self.assertAlmostEqual(float(net.loc["2026-11-05"]), 880.0)
        self.assertAlmostEqual(float(net.loc["2026-11-11"]), 920.0)
        self.assertAlmostEqual(float(net.loc["2026-11-12"]), 940.0)

    def test_dollar_uses_yahoo_when_fred_lags(self) -> None:
        fred = _series([120.0], ["2026-10-20"], "DTWEXBGS")
        yahoo = _series([104.0, 105.0], ["2026-10-20", "2026-11-02"], "DX-Y.NYB")

        chosen, source = choose_dollar_series(fred, yahoo)

        self.assertEqual(source, "Yahoo Finance")
        self.assertAlmostEqual(float(chosen.iloc[-1]), 105.0)

    def test_dollar_keeps_fred_when_it_is_current(self) -> None:
        fred = _series([120.0, 121.0], ["2026-11-01", "2026-11-02"])
        yahoo = _series([104.0], ["2026-11-02"])

        chosen, source = choose_dollar_series(fred, yahoo)

        self.assertEqual(source, "FRED")
        self.assertAlmostEqual(float(chosen.iloc[-1]), 121.0)

    def test_macro_list_replaces_fed_funds_and_adds_the_new_blocks(self) -> None:
        keys = [indicator.key for indicator in MACRO_INDICATORS]
        sections = {indicator.key: indicator.section for indicator in MACRO_INDICATORS}

        self.assertNotIn("FEDFUNDS", keys)
        self.assertIn("DFF", keys)
        for key in ("WALCL", "WRESBAL", "RRPONTSYD", "WTREGEN", "NETLIQ", "DFII10", "T10YIE", "BAMLH0A0HYM2", "BAMLC0A0CM", "DTWEXBGS", "DGS2", "T10Y3M"):
            self.assertIn(key, keys)
        self.assertEqual(sections["NETLIQ"], "Liquidity")
        self.assertEqual(sections["DFF"], "Rates and the curve")
        self.assertEqual(sections["BAMLC0A0CM"], "Real rates and credit")
        self.assertEqual(sections["DTWEXBGS"], "Dollar")
        self.assertEqual(next(item.scale for item in MACRO_INDICATORS if item.key == "WALCL"), "millions_to_billions")
        self.assertEqual(next(item for item in MACRO_INDICATORS if item.key == "NETLIQ").source, "Computed")

    def test_snapshot_scales_balance_sheet_and_writes_net_liquidity(self) -> None:
        def indicator(key: str, scale: str = "none", source: str = "FRED") -> Indicator:
            return Indicator(key, key, source, "$ billions", "Weekly", section="Liquidity", scale=scale)

        indicators = [
            ("macro", indicator("WALCL", "millions_to_billions")),
            ("macro", indicator("WTREGEN", "millions_to_billions")),
            ("macro", indicator("RRPONTSYD")),
            ("macro", indicator("NETLIQ", source="Computed")),
            ("markets", Indicator("^GSPC", "S&P 500", "Yahoo Finance", "points", "Daily")),
        ]
        calls: list[str] = []

        def fetcher(item: Indicator, start: date, end: date) -> pd.Series:
            calls.append(item.key)
            rows = {
                "WALCL": _series([6_600_000.0], ["2026-11-04"]),
                "WTREGEN": _series([800_000.0], ["2026-11-04"]),
                "RRPONTSYD": _series([50.0], ["2026-11-04"]),
                "^GSPC": _series([5800.0], ["2026-11-04"]),
            }
            return rows[item.key]

        with tempfile.TemporaryDirectory() as tmp:
            report = build_snapshot(Path(tmp), indicators=indicators, fetcher=fetcher, workers=1)
            document = json.loads((Path(tmp) / "series" / "NETLIQ.json").read_text(encoding="utf-8"))
            walcl = json.loads((Path(tmp) / "series" / "WALCL.json").read_text(encoding="utf-8"))

        self.assertNotIn("NETLIQ", calls)
        self.assertEqual(walcl["observations"], [["2026-11-04", 6600.0]])
        self.assertEqual(document["observations"], [["2026-11-04", 5750.0]])
        self.assertEqual(document["overlay"], [["2026-11-04", 5800.0]])
        self.assertEqual(document["overlay_unit"], "points")
        self.assertEqual(report["counts"]["ok"], 5)


if __name__ == "__main__":
    unittest.main()
