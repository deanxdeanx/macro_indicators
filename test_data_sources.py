"""Unit tests for data transformation and metric calculations."""

from __future__ import annotations

import unittest

import pandas as pd

from data_sources import _extract_close_prices, latest_metrics


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


if __name__ == "__main__":
    unittest.main()
