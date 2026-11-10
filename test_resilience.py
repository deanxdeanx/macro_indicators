"""Tests for release-aware staleness, backups, and the cross-asset series."""

from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from backup_sources import (
    cftc_history,
    earnings_yield_minus_real,
    parse_multpl_earnings_yield,
    parse_nyfed_rates,
    parse_treasury_yield_csv,
    position_percentile,
    price_ratio,
    speculative_net,
)
from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    company_indicators,
    regime_readout,
    release_is_stale,
)


class ReleaseScheduleTests(unittest.TestCase):
    def test_monthly_and_quarterly_wait_for_the_next_release(self) -> None:
        self.assertFalse(release_is_stale(date(2026, 9, 1), "Monthly", date(2026, 11, 10)))
        self.assertTrue(release_is_stale(date(2026, 9, 1), "Monthly", date(2026, 11, 25)))
        self.assertFalse(release_is_stale(date(2026, 7, 1), "Quarterly", date(2026, 11, 10)))

    def test_daily_allows_a_weekend_and_one_grace_session(self) -> None:
        friday = date(2026, 11, 6)
        self.assertFalse(release_is_stale(friday, "Daily", date(2026, 11, 9)))
        self.assertTrue(release_is_stale(friday, "Daily", date(2026, 11, 11)))

    def test_regime_verdict_is_withheld_when_most_inputs_are_held_over(self) -> None:
        entries = []
        for key in ("DFII10", "T10Y2Y", "BAMLH0A0HYM2", "DTWEXBGS", "NETLIQ", "NFCI", "^VIX"):
            entries.append({
                "key": key,
                "status": "ok" if key == "NETLIQ" else "preserved",
                "latest_date": "2026-11-09",
                "frequency": "Daily",
                "week_change": -1.0 if key == "NETLIQ" else 0.2,
                "range_percentile": 90 if key == "DFII10" else 20,
            })

        text = regime_readout(entries, as_of=date(2026, 11, 10))

        self.assertIn("stale — no call", text)
        self.assertNotIn("tightening", text)
        self.assertNotIn("easing", text)


class BackupParserTests(unittest.TestCase):
    def test_treasury_csv_maps_the_named_tenors(self) -> None:
        text = 'Date,"3 Mo","2 Yr","10 Yr"\n11/09/2026,4.24,4.56,4.80\n11/06/2026,4.20,4.50,4.75\n'
        parsed = parse_treasury_yield_csv(text)
        self.assertAlmostEqual(float(parsed["DGS10"].iloc[0]), 4.75)
        self.assertAlmostEqual(float(parsed["DGS3MO"].iloc[-1]), 4.24)
        self.assertIn("DGS2", parsed)

    def test_nyfed_reads_percent_rate(self) -> None:
        payload = {"refRates": [
            {"effectiveDate": "2026-11-06", "percentRate": 3.89},
            {"effectiveDate": "2026-11-05", "percentRate": 3.91},
        ]}
        series = parse_nyfed_rates(payload, "SOFR")
        self.assertEqual(series.index[-1].strftime("%Y-%m-%d"), "2026-11-06")
        self.assertAlmostEqual(float(series.iloc[-1]), 3.89)

    def test_speculative_net_and_percentile(self) -> None:
        row = {"lev_money_positions_long": "10", "lev_money_positions_short": "40"}
        self.assertEqual(speculative_net(row), -30)
        history = cftc_history([
            {"report_date_as_yyyy_mm_dd": "2024-01-02", "lev_money_positions_long": 1, "lev_money_positions_short": 2},
            {"report_date_as_yyyy_mm_dd": "2026-11-03", "lev_money_positions_long": 10, "lev_money_positions_short": 4},
        ])
        self.assertAlmostEqual(float(history.iloc[-1]), 6)
        self.assertEqual(position_percentile(history), 100.0)


class CrossAssetTests(unittest.TestCase):
    def test_copper_gold_and_earnings_spread(self) -> None:
        copper = pd.Series([4.0, 4.4], index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
        gold = pd.Series([4000.0, 4400.0], index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
        ratio = price_ratio(copper, gold, "COPPERGOLD")
        self.assertAlmostEqual(float(ratio.iloc[-1]), 0.001)

        earnings = pd.Series([4.0, 4.2], index=pd.to_datetime(["2026-10-01", "2026-11-01"]))
        real = pd.Series([1.5, 2.0], index=pd.to_datetime(["2026-10-01", "2026-11-03"]))
        spread = earnings_yield_minus_real(earnings, real)
        self.assertAlmostEqual(float(spread.loc["2026-11-01"]), 2.7)

    def test_sections_keep_stocks_and_cross_asset_out_of_growth(self) -> None:
        self.assertTrue(all(item.section == "Companies" for item in company_indicators()))
        sections = {item.key: item.section for item in (*MACRO_INDICATORS, *MARKET_INDICATORS)}
        for key in ("^VIX", "GC=F", "CL=F", "BTC-USD", "^MOVE", "CFTC"):
            self.assertEqual(sections[key], "Cross-asset")
        self.assertEqual(sections["COPPERGOLD"], "Rates and the curve")
        self.assertEqual(sections["SPEARN10"], "Real rates and credit")
        self.assertEqual(sections["DGS3MO"], "Rates and the curve")
        growth = [item.key for item in MACRO_INDICATORS if item.section == "Growth and labor"]
        self.assertEqual(growth, ["UNRATE", "CPIAUCSL", "A191RL1Q225SBEA", "ICSA", "IC4WSA", "UMCSENT"])

    def test_multpl_skips_estimate_rows(self) -> None:
        html = """
        <tr><td>Nov 9, 2026</td><td><abbr title="Estimate">†</abbr> 3.88%</td></tr>
        <tr><td>Sep 1, 2026</td><td>3.50%</td></tr>
        """
        series = parse_multpl_earnings_yield(html)
        self.assertEqual(list(series.index.strftime("%Y-%m-%d")), ["2026-09-01"])
        self.assertAlmostEqual(float(series.iloc[-1]), 3.5)


if __name__ == "__main__":
    unittest.main()
