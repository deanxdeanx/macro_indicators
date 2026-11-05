"""Tests for honest as-of dates, the regime line, and semiconductor P/E."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from build_data import fred_failure_summary, funds_spread, main
from data_sources import as_of_label, regime_readout, source_dates
from semis import (
    earnings_series,
    market_cap,
    monthly_price_and_pe,
    pe_summary,
    rank_by_market_cap,
    split_adjust_earnings,
    splits_from_chart,
    to_usd_per_listed_share,
    trailing_twelve_months,
)


def _entry(**kwargs):
    base = {
        "source": "FRED",
        "frequency": "Daily",
        "group": "macro",
        "latest_date": "2026-11-03",
        "status": "ok",
    }
    base.update(kwargs)
    return base


class FreshnessTests(unittest.TestCase):
    def test_header_dates_come_from_observations_not_the_run(self) -> None:
        entries = [
            _entry(key="DGS10", latest_date="2026-11-03"),
            _entry(key="THREEFYTP10", latest_date="2026-10-30"),
            _entry(key="WALCL", frequency="Weekly", latest_date="2026-10-28"),
            _entry(key="UNRATE", frequency="Monthly", latest_date="2026-09-01"),
            _entry(key="^GSPC", source="Yahoo Finance", group="markets", latest_date="2026-11-04"),
            _entry(key="NVDA", source="Yahoo Finance", group="semis", latest_date="2026-08-31"),
        ]

        dates = source_dates(entries)
        label = as_of_label(dates)

        self.assertEqual(dates["fred_as_of"], "2026-11-03")
        self.assertEqual(dates["fred_oldest"], "2026-10-30")
        self.assertEqual(dates["yahoo_as_of"], "2026-11-04")
        self.assertEqual(dates["data_through"], "2026-11-04")
        self.assertIn("30 Oct 2026", label)
        self.assertIn("03 Nov 2026", label)
        self.assertIn("Yahoo as of 04 Nov 2026", label)
        self.assertNotIn("05 Nov", label)
        self.assertNotIn("01 Sep", label)

    def test_every_fred_series_failing_exits_nonzero(self) -> None:
        keys = [f"FRED{index}" for index in range(22)]
        entries = [
            {"key": key, "status": "preserved", "last_error": "FRED timed out while fetching.", "observation_count": 10, "latest_date": "2026-10-30"}
            for key in keys
        ]

        summary = fred_failure_summary(entries, keys)

        self.assertIn("22 of 22 FRED series failed", summary)
        self.assertIn("Previous observations were kept", summary)
        report = {
            "generated_at": "2026-11-04T20:37:19Z",
            "history_start": "2020-01-01",
            "history_end": "2026-11-04",
            "counts": {"ok": 27, "preserved": 22, "error": 0},
            "entries": entries,
            "with_data": 49,
            "fred_failure": summary,
        }
        with patch("build_data.build_snapshot", return_value=report):
            self.assertEqual(main([]), 2)


class RegimeAndSpreadTests(unittest.TestCase):
    def test_readout_names_a_tightening_mix(self) -> None:
        entries = [
            {"key": "NETLIQ", "week_change": -0.96},
            {"key": "DFII10", "range_percentile": 92},
            {"key": "BAMLH0A0HYM2", "week_change": 0.13},
            {"key": "DTWEXBGS", "week_change": 0.28},
        ]

        text = regime_readout(entries)

        self.assertEqual(
            text,
            "liquidity falling · real rates high · credit widening · dollar firm → tightening",
        )

    def test_funds_spread_subtracts_the_policy_rate(self) -> None:
        two_year = pd.Series([4.5, 4.6], index=pd.to_datetime(["2026-11-02", "2026-11-03"]))
        funds = pd.Series([3.88, 3.87], index=pd.to_datetime(["2026-11-02", "2026-11-03"]))

        spread = funds_spread(two_year, funds)

        self.assertAlmostEqual(float(spread.iloc[-1]), 0.73)


class SemiconductorTests(unittest.TestCase):
    def test_trailing_eps_requires_consecutive_quarters_and_skips_negative_pe(self) -> None:
        quarterly = pd.Series(
            [1.0, 1.1, 1.2, -5.0],
            index=pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]),
        )

        trailing = trailing_twelve_months(quarterly)
        prices = pd.Series(
            [10.0, 10.0],
            index=pd.to_datetime(["2025-09-30", "2025-12-31"]),
        )
        price_rows, pe_rows = monthly_price_and_pe(prices, trailing, years=10)

        self.assertLess(float(trailing.loc["2025-12-31"]), 0)
        self.assertTrue(all(row[1] > 0 for row in pe_rows))
        december = [row for row in pe_rows if row[0].startswith("2025-12")]
        self.assertEqual(december, [])
        summary = pe_summary(price_rows, pe_rows)
        self.assertIsNone(summary["pe"])

    def test_adr_ratio_scales_eps_and_market_cap(self) -> None:
        earnings = pd.Series([1.36], index=pd.to_datetime(["2024-12-31"]))

        per_adr = to_usd_per_listed_share(earnings, "USD/shares", 5, None)

        self.assertAlmostEqual(float(per_adr.iloc[-1]), 6.8)
        self.assertAlmostEqual(market_cap(25_930_000_000, 395.75, 5), 25_930_000_000 * 395.75 / 5)

    def test_annual_eps_is_used_when_quarters_are_absent(self) -> None:
        rows = [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 1.36, "filed": "2025-04-17", "form": "20-F"},
        ]

        series, basis = earnings_series(rows)

        self.assertIn("annual", basis)
        self.assertAlmostEqual(float(series.iloc[-1]), 1.36)

    def test_later_splits_scale_historical_eps_down(self) -> None:
        payload = {
            "chart": {
                "result": [{
                    "events": {
                        "splits": {
                            "1": {"date": int(pd.Timestamp("2024-06-10").timestamp()), "numerator": 10, "denominator": 1},
                        }
                    }
                }]
            }
        }
        earnings = pd.Series([40.0, 1.0], index=pd.to_datetime(["2024-03-31", "2024-09-30"]))

        adjusted = split_adjust_earnings(earnings, splits_from_chart(payload))

        self.assertAlmostEqual(float(adjusted.iloc[0]), 4.0)
        self.assertAlmostEqual(float(adjusted.iloc[1]), 1.0)

    def test_rank_keeps_the_ten_largest(self) -> None:
        candidates = [{"ticker": f"T{index}", "market_cap": float(index)} for index in range(1, 15)]

        ranked = rank_by_market_cap(candidates)

        self.assertEqual(len(ranked), 10)
        self.assertEqual(ranked[0]["ticker"], "T14")
        self.assertEqual(ranked[-1]["ticker"], "T5")


if __name__ == "__main__":
    unittest.main()
