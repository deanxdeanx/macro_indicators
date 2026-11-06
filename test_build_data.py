"""Tests for the static snapshot builder."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from build_data import (
    build_snapshot,
    default_fetcher,
    series_file_id,
)
from data_sources import DataSourceError, Indicator


def _series(key: str, values: list[float], dates: list[str]) -> pd.Series:
    return pd.Series(values, index=pd.to_datetime(dates), name=key)


class ScriptedFetch:
    def __init__(self, mapping: dict[str, pd.Series | Exception]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, indicator: Indicator, start: date, end: date) -> pd.Series:
        self.calls.append(indicator.key)
        result = self.mapping[indicator.key]
        if isinstance(result, Exception):
            raise result
        return result


def _indicators() -> list[tuple[str, Indicator]]:
    return [
        ("macro", Indicator("UNRATE", "US Unemployment Rate", "FRED", "%", "Monthly", change_mode="points")),
        ("markets", Indicator("^GSPC", "S&P 500 Index", "Yahoo Finance", "points", "Daily")),
    ]


class BuildSnapshotTests(unittest.TestCase):
    def test_series_file_id_strips_ticker_punctuation(self) -> None:
        self.assertEqual(series_file_id("^GSPC"), "GSPC")
        self.assertEqual(series_file_id("GC=F"), "GC-F")
        self.assertEqual(series_file_id("CL=F"), "CL-F")
        self.assertEqual(series_file_id("BRK-B"), "BRK-B")
        self.assertEqual(series_file_id("BTC-USD"), "BTC-USD")
        with self.assertRaises(ValueError):
            series_file_id("^")

    def test_duplicate_file_ids_abort_before_fetching(self) -> None:
        indicators = [
            ("markets", Indicator("^GSPC", "A", "Yahoo Finance", "points", "Daily")),
            ("markets", Indicator("GSPC", "B", "Yahoo Finance", "points", "Daily")),
        ]
        called = False

        def fetcher(indicator, start, end):
            nonlocal called
            called = True
            return _series(indicator.key, [1.0], ["2026-01-01"])

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "collide"):
                build_snapshot(Path(tmp), indicators=indicators, fetcher=fetcher, workers=1)
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertFalse(called)

    def test_build_isolates_failures_and_preserves_previous_observations(self) -> None:
        first = ScriptedFetch(
            {
                "UNRATE": _series("UNRATE", [4.0, 4.2], ["2026-01-01", "2026-02-01"]),
                "^GSPC": _series("^GSPC", [100.0, 110.0], ["2026-01-02", "2026-01-03"]),
            }
        )
        moment = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first_report = build_snapshot(
                directory,
                indicators=_indicators(),
                fetcher=first,
                workers=1,
                now=moment,
                start=date(2026, 1, 1),
                end=date(2026, 11, 1),
            )
            unrate_path = directory / "series" / "UNRATE.json"
            preserved_bytes = unrate_path.read_bytes()
            self.assertEqual(first_report["counts"], {"ok": 2, "preserved": 0, "error": 0})

            second = ScriptedFetch(
                {
                    "UNRATE": DataSourceError("FRED timed out while fetching UNRATE. Try refreshing the data."),
                    "^GSPC": _series("^GSPC", [100.0, 120.0], ["2026-01-02", "2026-01-03"]),
                }
            )
            later = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
            report = build_snapshot(
                directory,
                indicators=_indicators(),
                fetcher=second,
                workers=1,
                now=later,
                start=date(2026, 1, 1),
                end=date(2026, 11, 2),
            )

            self.assertEqual(unrate_path.read_bytes(), preserved_bytes)
            stored = json.loads(preserved_bytes)
            self.assertEqual(stored["observations"], [["2026-01-01", 4.0], ["2026-02-01", 4.2]])
            self.assertEqual(stored["fetched_at"], "2026-11-01T12:00:00Z")

            catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
            by_key = {item["key"]: item for item in catalog["series"]}
            self.assertEqual(by_key["UNRATE"]["status"], "preserved")
            self.assertEqual(by_key["UNRATE"]["fetched_at"], "2026-11-01T12:00:00Z")
            self.assertEqual(by_key["UNRATE"]["last_attempt_at"], "2026-11-02T12:00:00Z")
            self.assertIn("timed out", by_key["UNRATE"]["last_error"])
            self.assertEqual(by_key["UNRATE"]["observation_count"], 2)
            self.assertNotIn("observations", by_key["UNRATE"])

            prices = json.loads((directory / "series" / "GSPC.json").read_text(encoding="utf-8"))
            self.assertEqual(prices["observations"][-1], ["2026-01-03", 120.0])
            self.assertEqual(prices["fetched_at"], "2026-11-02T12:00:00Z")
            self.assertEqual(by_key["^GSPC"]["status"], "ok")
            self.assertIsNone(by_key["^GSPC"]["last_error"])
            self.assertEqual(report["counts"], {"ok": 1, "preserved": 1, "error": 0})
            self.assertEqual(report["with_data"], 2)
            self.assertEqual([item["key"] for item in catalog["series"]], ["UNRATE", "^GSPC"])

    def test_failed_fetch_without_history_does_not_invent_observations(self) -> None:
        fetcher = ScriptedFetch(
            {"UNRATE": DataSourceError("FRED is temporarily unavailable for UNRATE (HTTP 503).")}
        )
        indicators = [_indicators()[0]]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            report = build_snapshot(
                directory,
                indicators=indicators,
                fetcher=fetcher,
                workers=1,
                now=datetime(2026, 11, 2, tzinfo=timezone.utc),
                start=date(2026, 1, 1),
                end=date(2026, 11, 2),
            )
            document = json.loads((directory / "series" / "UNRATE.json").read_text(encoding="utf-8"))
            self.assertEqual(document["observations"], [])
            self.assertEqual(document["status"], "error")
            self.assertIsNone(document["fetched_at"])
            self.assertEqual(report["with_data"], 0)
            self.assertEqual(report["counts"]["error"], 1)

            again = build_snapshot(
                directory,
                indicators=indicators,
                fetcher=fetcher,
                workers=1,
                now=datetime(2026, 11, 3, tzinfo=timezone.utc),
                start=date(2026, 1, 1),
                end=date(2026, 11, 3),
            )
            self.assertEqual(again["counts"]["preserved"], 0)
            self.assertEqual(again["counts"]["error"], 1)
            reread = json.loads((directory / "series" / "UNRATE.json").read_text(encoding="utf-8"))
            self.assertEqual(reread["observations"], [])

    def test_empty_series_keeps_the_previous_file(self) -> None:
        indicators = [_indicators()[0]]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            build_snapshot(
                directory,
                indicators=indicators,
                fetcher=ScriptedFetch({"UNRATE": _series("UNRATE", [3.5], ["2026-03-01"])}),
                workers=1,
                now=datetime(2026, 11, 1, tzinfo=timezone.utc),
                start=date(2026, 1, 1),
                end=date(2026, 11, 1),
            )
            path = directory / "series" / "UNRATE.json"
            original = path.read_bytes()
            build_snapshot(
                directory,
                indicators=indicators,
                fetcher=ScriptedFetch({"UNRATE": pd.Series(dtype=float)}),
                workers=1,
                now=datetime(2026, 11, 2, tzinfo=timezone.utc),
                start=date(2026, 1, 1),
                end=date(2026, 11, 2),
            )
            self.assertEqual(path.read_bytes(), original)

    def test_identical_success_does_not_rewrite_files(self) -> None:
        indicators = [_indicators()[0]]
        fetcher = ScriptedFetch({"UNRATE": _series("UNRATE", [4.0], ["2026-01-01"])})
        moment = datetime(2026, 11, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            kwargs = {
                "indicators": indicators,
                "fetcher": fetcher,
                "workers": 1,
                "now": moment,
                "start": date(2026, 1, 1),
                "end": date(2026, 11, 1),
            }
            first = build_snapshot(directory, **kwargs)
            second = build_snapshot(directory, **kwargs)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])

    def test_observations_are_rounded_and_dated(self) -> None:
        indicators = [_indicators()[0]]
        fetcher = ScriptedFetch({"UNRATE": _series("UNRATE", [1.23456789], ["2026-05-01"])})
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            build_snapshot(
                directory,
                indicators=indicators,
                fetcher=fetcher,
                workers=1,
                now=datetime(2026, 11, 2, 15, 4, tzinfo=timezone.utc),
                start=date(2026, 1, 1),
                end=date(2026, 11, 2),
            )
            document = json.loads((directory / "series" / "UNRATE.json").read_text(encoding="utf-8"))
            self.assertEqual(document["observations"], [["2026-05-01", 1.234568]])
            self.assertEqual(document["latest_date"], "2026-05-01")
            self.assertEqual(document["change_mode"], "points")

    def test_default_fetcher_uses_short_timeouts_per_source(self) -> None:
        sample = _series("X", [1.0], ["2026-01-01"])
        fred = Indicator("UNRATE", "Unemployment", "FRED", "%", "Monthly", transform="yoy", change_mode="points")
        yahoo = Indicator("^GSPC", "S&P 500", "Yahoo Finance", "points", "Daily")
        unknown = Indicator("OTHER", "Other", "Somewhere", "points", "Daily")
        with (
            patch("build_data.fetch_fred_series", return_value=sample) as fred_fetch,
            patch("build_data.fetch_yahoo_chart_series", return_value=sample) as yahoo_fetch,
        ):
            fetcher = default_fetcher(9)
            self.assertEqual(float(fetcher(fred, date(2026, 1, 1), date(2026, 2, 1)).iloc[-1]), 1.0)
            self.assertEqual(float(fetcher(yahoo, date(2026, 1, 1), date(2026, 2, 1)).iloc[-1]), 1.0)
            with self.assertRaisesRegex(DataSourceError, "Unknown source"):
                fetcher(unknown, date(2026, 1, 1), date(2026, 2, 1))

        self.assertEqual(fred_fetch.call_args.args[2], "yoy")
        self.assertEqual(fred_fetch.call_args.kwargs["timeout"], 30)
        self.assertEqual(fred_fetch.call_args.kwargs["attempts"], 5)
        self.assertEqual(fred_fetch.call_args.kwargs["retry_backoff"], 2.0)
        self.assertEqual(yahoo_fetch.call_args.kwargs["timeout"], 9)
        self.assertEqual(yahoo_fetch.call_args.kwargs["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
