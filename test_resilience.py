"""Tests for release-aware staleness, backups, and the cross-asset series."""

from __future__ import annotations

from datetime import date
import unittest

import pandas as pd

from backup_sources import (
    cftc_history,
    choose_eps,
    daily_earnings_yield,
    earnings_yield_minus_real,
    eps_is_old,
    parse_dts_tga,
    parse_ecb_csv,
    parse_h41_preview,
    parse_jgb_csv,
    parse_multpl_earnings,
    parse_multpl_earnings_yield,
    parse_nyfed_rates,
    parse_nyfed_rrp,
    parse_treasury_real_yield_csv,
    parse_treasury_yield_csv,
    position_percentile,
    price_ratio,
    speculative_net,
    yield_spread,
)
from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    company_indicators,
    regime_inputs_stale,
    regime_readout,
    release_is_stale,
    stale_input_reason,
    week_move_leaders,
    weekly_zscore,
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


class DirectLiquidityTests(unittest.TestCase):
    def test_h41_preview_reads_millions(self) -> None:
        html = """
        <tr><td>H41/H41/RESPPA_N.WW</td><td>2026-11-04</td><td>6748145</td></tr>
        <tr><td>H41/H41/RESPPA_N.WW</td><td>2026-10-28</td><td>6,745,635</td></tr>
        """
        series = parse_h41_preview(html, "WALCL")
        self.assertEqual(series.index[-1].strftime("%Y-%m-%d"), "2026-11-04")
        self.assertAlmostEqual(float(series.iloc[-1]), 6748145)

    def test_dts_uses_the_opening_field_when_close_is_null(self) -> None:
        payload = {"data": [
            {"record_date": "2026-11-09", "close_today_bal": "null", "open_today_bal": "922633"},
            {"record_date": "2026-11-06", "close_today_bal": None, "open_today_bal": "910313"},
        ]}
        series = parse_dts_tga(payload)
        self.assertAlmostEqual(float(series.loc["2026-11-09"]), 922.633)
        self.assertAlmostEqual(float(series.loc["2026-11-06"]), 910.313)

    def test_rrp_amount_is_billions(self) -> None:
        payload = {"repo": {"operations": [
            {"operationDate": "2022-12-05", "totalAmtAccepted": 2093647000000},
            {"operationDate": "2026-11-10", "totalAmtAccepted": 152000000},
        ]}}
        series = parse_nyfed_rrp(payload)
        self.assertAlmostEqual(float(series.loc["2022-12-05"]), 2093.647, places=3)
        self.assertAlmostEqual(float(series.loc["2026-11-10"]), 0.152, places=3)

    def test_a_one_day_rrp_hole_does_not_become_a_spike(self) -> None:
        payload = {"repo": {"operations": [
            {"operationDate": "2022-11-16", "totalAmtAccepted": 2_100_000_000_000},
            {"operationDate": "2022-11-17", "totalAmtAccepted": 100_000_000},
            {"operationDate": "2022-11-18", "totalAmtAccepted": 2_110_000_000_000},
        ]}}
        series = parse_nyfed_rrp(payload)
        self.assertGreater(float(series.loc["2022-11-17"]), 2000)


class TreasuryBackupTests(unittest.TestCase):
    def test_spreads_and_real_yield_and_breakeven(self) -> None:
        nominal = parse_treasury_yield_csv(
            'Date,"3 Mo","2 Yr","10 Yr"\n11/10/2026,4.20,4.10,4.50\n11/09/2026,4.18,4.08,4.40\n'
        )
        curve = yield_spread(nominal["DGS10"], nominal["DGS2"], "T10Y2Y")
        self.assertAlmostEqual(float(curve.iloc[-1]), 0.40)
        bill = yield_spread(nominal["DGS10"], nominal["DGS3MO"], "T10Y3M")
        self.assertAlmostEqual(float(bill.iloc[-1]), 0.30)
        real = parse_treasury_real_yield_csv('Date,"5 YR","10 YR"\n11/10/2026,1.80,2.10\n')
        self.assertAlmostEqual(float(real.iloc[-1]), 2.10)
        breakeven = yield_spread(nominal["DGS10"], real, "T10YIE")
        self.assertAlmostEqual(float(breakeven.iloc[-1]), 2.40)

    def test_global_yield_parsers(self) -> None:
        ecb = parse_ecb_csv(
            "TIME_PERIOD,OBS_VALUE\n2026-11-10,2.71\n2026-11-09,2.68\n",
            "EUR10Y",
        )
        self.assertAlmostEqual(float(ecb.iloc[-1]), 2.71)
        jgb = parse_jgb_csv("Interest Rate\nDate,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y\n2026/10/30,1,1,1,1,1,1,1,1,1,2.817\n")
        self.assertAlmostEqual(float(jgb.iloc[-1]), 2.817)


class StalenessInheritanceTests(unittest.TestCase):
    def test_derived_series_is_stale_when_an_input_is_held_over(self) -> None:
        entries = {
            "WALCL": {"key": "WALCL", "status": "preserved", "latest_date": "2026-11-04", "frequency": "Weekly"},
            "WTREGEN": {"key": "WTREGEN", "status": "ok", "latest_date": "2026-11-10", "frequency": "Daily"},
        }
        reason = stale_input_reason(entries, ["WALCL", "WTREGEN"], date(2026, 11, 12))
        self.assertIn("WALCL is held over", reason)

        regime = []
        for key in ("DFII10", "T10Y2Y", "BAMLH0A0HYM2", "DTWEXBGS", "NETLIQ", "NFCI", "^VIX"):
            regime.append({
                "key": key,
                "status": "ok",
                "latest_date": "2026-11-10",
                "frequency": "Daily",
                "inputs_stale": key in {"NETLIQ", "DFII10", "T10Y2Y", "BAMLH0A0HYM2", "DTWEXBGS"},
                "week_change": 0.0,
                "range_percentile": 50,
            })
        self.assertTrue(regime_inputs_stale(regime, date(2026, 11, 12)))
        self.assertIn("stale — no call", regime_readout(regime, as_of=date(2026, 11, 12)))

    def test_a_fresh_input_is_not_a_reason(self) -> None:
        entries = {
            "HG=F": {"key": "HG=F", "status": "ok", "latest_date": "2026-11-10", "frequency": "Daily"},
            "GC=F": {"key": "GC=F", "status": "ok", "latest_date": "2026-11-10", "frequency": "Daily"},
        }
        self.assertIsNone(stale_input_reason(entries, ["HG=F", "GC=F"], date(2026, 11, 12)))


class EarningsAndMoversTests(unittest.TestCase):
    def test_daily_yield_carries_the_latest_eps_and_flags_an_old_print(self) -> None:
        trailing = pd.Series([200.0], index=pd.to_datetime(["2026-06-30"]))
        forward = pd.Series([210.0], index=pd.to_datetime(["2026-09-30"]))
        eps, kind, stamp = choose_eps(trailing, forward, date(2026, 11, 12))
        self.assertEqual(kind, "forward")
        self.assertEqual(stamp.strftime("%Y-%m-%d"), "2026-09-30")
        kept, kept_kind, _kept_stamp = choose_eps(trailing, pd.Series(dtype=float), date(2026, 7, 15))
        self.assertEqual(kept_kind, "trailing")
        self.assertTrue(eps_is_old(kept.index[-1], date(2026, 11, 12)))

        price = pd.Series([5000.0, 5200.0], index=pd.to_datetime(["2026-11-10", "2026-11-11"]))
        ey = daily_earnings_yield(price, eps)
        self.assertEqual(list(ey.index.strftime("%Y-%m-%d")), ["2026-11-10", "2026-11-11"])
        self.assertAlmostEqual(float(ey.iloc[-1]), 210.0 / 5200.0 * 100)
        real = pd.Series([2.0], index=pd.to_datetime(["2026-11-11"]))
        spread = earnings_yield_minus_real(ey, real)
        self.assertAlmostEqual(float(spread.iloc[-1]), 210.0 / 5200.0 * 100 - 2.0)

    def test_multpl_earnings_splits_estimates(self) -> None:
        html = """
        <tr><td>Sep 30, 2026</td><td><abbr title="Estimate">†</abbr> 310.00</td></tr>
        <tr><td>Jun 30, 2026</td><td>298.03</td></tr>
        """
        trailing, forward = parse_multpl_earnings(html)
        self.assertEqual(trailing.index[-1].strftime("%Y-%m-%d"), "2026-06-30")
        self.assertEqual(forward.index[-1].strftime("%Y-%m-%d"), "2026-09-30")

    def test_week_movers_skip_stale_series_and_rank_by_z(self) -> None:
        dates = pd.bdate_range("2026-01-02", periods=80)
        calm = pd.Series(100.0, index=dates)
        jump = calm.copy()
        jump.iloc[-1] = 130.0
        calm_entry = {"key": "CALM", "name": "Calm", "status": "ok", "latest_date": "2026-04-24", "frequency": "Daily", "change_mode": "percent", "visible": True}
        jump_entry = {"key": "JUMP", "name": "Jump", "status": "ok", "latest_date": "2026-04-24", "frequency": "Daily", "change_mode": "percent", "visible": True, "higher_is_bad": True}
        stale_entry = {"key": "OLD", "name": "Old", "status": "preserved", "latest_date": "2026-04-24", "frequency": "Daily", "change_mode": "percent", "visible": True}
        leaders = week_move_leaders(
            [(jump_entry, jump), (calm_entry, calm), (stale_entry, jump)],
            date(2026, 4, 24),
        )
        self.assertEqual(leaders[0]["key"], "JUMP")
        self.assertNotIn("OLD", [item["key"] for item in leaders])
        self.assertGreater(abs(leaders[0]["z"]), 1)
        self.assertIsNone(weekly_zscore(calm, "percent"))


if __name__ == "__main__":
    unittest.main()
