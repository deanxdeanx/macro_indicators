"""Build the static JSON snapshot consumed by the Macro Terminal site.

Each indicator is fetched on its own. A failed fetch keeps the previous good
series file and records the error on the catalog entry instead of writing an
empty series over real observations.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import json
import logging
import math
import os
import sys
import threading
from pathlib import Path
import time

import pandas as pd

from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    DataSourceError,
    Indicator,
    company_indicators,
    fetch_fred_series,
    fetch_yahoo_chart_series,
    lagged_change,
    observation_window,
    range_percentile,
)


logger = logging.getLogger("build_data")

DEFAULT_DATA_DIR = Path("site/data")
# The 3 Nov 2026 GitHub run opened six FRED CSV connections together. Every one
# raised ReadTimeout at 30 seconds, including the last pair, while Yahoo on the
# same runner succeeded. fred.stlouisfed.org is behind Akamai and was accepting
# the connection without sending a body. Keep FRED to two at a time.
FRED_WORKERS = 2
FRED_START_GAP_SECONDS = 0.4


def series_file_id(key: str) -> str:
    """Return a stable filename stem for a FRED id or Yahoo ticker."""
    cleaned = (
        key.replace("^", "")
        .replace("=", "-")
        .replace("/", "-")
        .replace("\\", "-")
        .replace(" ", "")
    )
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"Cannot derive a file id from {key!r}.")
    return cleaned


def utc_now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def indicator_groups() -> list[tuple[str, Indicator]]:
    grouped = [("macro", item) for item in MACRO_INDICATORS]
    grouped.extend(("markets", item) for item in MARKET_INDICATORS)
    grouped.extend(("companies", item) for item in company_indicators())
    return grouped


def dumps_json(payload: object) -> str:
    return json.dumps(payload, indent=2, allow_nan=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, payload: object) -> bool:
    """Write JSON when the bytes would change. Return whether the file changed."""
    text = dumps_json(payload)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            logger.warning("unreadable_existing_file path=%s", path)
    atomic_write_text(path, text)
    return True


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("unreadable_json path=%s", path)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def observations_from_series(series: pd.Series) -> list[list[str | float]]:
    rows: list[list[str | float]] = []
    clean = series.dropna().sort_index()
    for timestamp, value in clean.items():
        number = float(value)
        if not math.isfinite(number):
            continue
        rows.append([pd.Timestamp(timestamp).strftime("%Y-%m-%d"), round(number, 6)])
    return rows


def metadata_for(indicator: Indicator, group: str) -> dict:
    return {
        "key": indicator.key,
        "name": indicator.name,
        "source": indicator.source,
        "unit": indicator.unit,
        "frequency": indicator.frequency,
        "change_mode": indicator.change_mode,
        "description": indicator.description,
        "transform": indicator.transform,
        "max_age_days": indicator.max_age_days,
        "section": indicator.section,
        "higher_is_bad": indicator.higher_is_bad,
        "group": group,
        "file": f"series/{series_file_id(indicator.key)}.json",
    }


def series_document(
    meta: dict,
    observations: list[list[str | float]],
    *,
    status: str,
    fetched_at: str | None,
) -> dict:
    latest = observations[-1][0] if observations else None
    return {
        **meta,
        "status": status,
        "observation_count": len(observations),
        "latest_date": latest,
        "fetched_at": fetched_at,
        "observations": observations,
    }


def previous_is_usable(document: dict | None) -> bool:
    if not isinstance(document, dict):
        return False
    observations = document.get("observations")
    if not isinstance(observations, list) or not observations:
        return False
    first = observations[0]
    return isinstance(first, (list, tuple)) and len(first) >= 2


def _json_number(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def moves_from_observations(observations: list, change_mode: str) -> dict[str, float | None]:
    """1-week change, 1-month change, and 5-year percentile for one stored series."""
    empty = {"week_change": None, "month_change": None, "range_percentile": None}
    if not observations:
        return empty
    try:
        series = pd.Series(
            [float(row[1]) for row in observations],
            index=pd.to_datetime([row[0] for row in observations]),
        )
    except (TypeError, ValueError, IndexError):
        return empty
    return {
        "week_change": _json_number(lagged_change(series, 7, change_mode), 4),
        "month_change": _json_number(lagged_change(series, 30, change_mode), 4),
        "range_percentile": _json_number(range_percentile(series), 1),
    }


def catalog_entry(
    document: dict,
    *,
    status: str,
    last_attempt_at: str,
    last_error: str | None,
) -> dict:
    moves = moves_from_observations(document.get("observations") or [], document.get("change_mode") or "percent")
    return {
        "key": document["key"],
        "name": document["name"],
        "source": document["source"],
        "unit": document["unit"],
        "frequency": document["frequency"],
        "change_mode": document["change_mode"],
        "description": document.get("description") or "",
        "transform": document.get("transform") or "none",
        "max_age_days": document.get("max_age_days"),
        "section": document.get("section") or "",
        "higher_is_bad": bool(document.get("higher_is_bad")),
        "group": document["group"],
        "file": document["file"],
        "status": status,
        "observation_count": document.get("observation_count") or 0,
        "latest_date": document.get("latest_date"),
        "fetched_at": document.get("fetched_at"),
        "last_attempt_at": last_attempt_at,
        "last_error": last_error,
        **moves,
    }


def _catalog_source_from_previous(meta: dict, previous: dict) -> dict:
    observations = previous.get("observations") or []
    latest = previous.get("latest_date")
    if not latest and observations:
        latest = observations[-1][0]
    count = previous.get("observation_count")
    if not isinstance(count, int):
        count = len(observations)
    return {
        **meta,
        "observation_count": count,
        "latest_date": latest,
        "fetched_at": previous.get("fetched_at"),
        "observations": previous.get("observations") or [],
    }


def _public_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = f"{type(exc).__name__}: the source request failed."
    return message[:300]


def default_fetcher(timeout: float):
    """Fetch one indicator. FRED gets a longer timeout and more retries than Yahoo."""

    fred_timeout = max(float(timeout), 30.0)

    def fetch(indicator: Indicator, start: date, end: date) -> pd.Series:
        if indicator.source == "Computed":
            raise DataSourceError(f"{indicator.key} is computed after the source series are stored.")
        if indicator.source == "FRED":
            return fetch_fred_series(
                indicator.key,
                start,
                indicator.transform,
                end,
                timeout=fred_timeout,
                attempts=4,
                retry_backoff=1.0,
            )
        if indicator.source == "Yahoo Finance":
            return fetch_yahoo_chart_series(
                indicator.key,
                start,
                end,
                timeout=timeout,
                attempts=2,
                retry_backoff=0.4,
            )
        raise DataSourceError(f"Unknown source for {indicator.key}: {indicator.source}.")

    return fetch


def apply_scale(series: pd.Series, scale: str) -> pd.Series:
    if scale == "millions_to_billions":
        return series / 1000.0
    return series


def net_liquidity_series(walcl_billions: pd.Series, tga_billions: pd.Series, rrp_billions: pd.Series) -> pd.Series:
    """Fed assets minus the Treasury general account minus reverse repo.

    All three inputs are billions of dollars. Weekly levels are carried forward
    onto each daily reverse-repo date, including dates after the latest weekly
    print.
    """
    calendar = rrp_billions.dropna().sort_index().index
    frame = pd.DataFrame(
        {
            "walcl": walcl_billions.dropna().sort_index().reindex(calendar, method="ffill"),
            "tga": tga_billions.dropna().sort_index().reindex(calendar, method="ffill"),
            "rrp": rrp_billions.dropna().sort_index().reindex(calendar),
        }
    ).dropna(how="any")
    net = (frame["walcl"] - frame["tga"] - frame["rrp"]).dropna()
    net.name = "NETLIQ"
    return net


def choose_dollar_series(
    fred: pd.Series | None,
    yahoo: pd.Series | None,
    lag_days: int = 5,
) -> tuple[pd.Series, str]:
    """Prefer FRED's broad dollar index unless ICE's print is newer."""
    fred_clean = None if fred is None else fred.dropna().sort_index()
    yahoo_clean = None if yahoo is None else yahoo.dropna().sort_index()
    if fred_clean is None or fred_clean.empty:
        if yahoo_clean is None or yahoo_clean.empty:
            raise DataSourceError("No dollar index data is available.")
        return yahoo_clean, "Yahoo Finance"
    if yahoo_clean is None or yahoo_clean.empty:
        return fred_clean, "FRED"
    gap_days = (yahoo_clean.index[-1] - fred_clean.index[-1]).days
    if gap_days > lag_days:
        return yahoo_clean, "Yahoo Finance"
    return fred_clean, "FRED"


def build_snapshot(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    start: date | None = None,
    end: date | None = None,
    timeout: float = 12,
    workers: int = 6,
    indicators: list[tuple[str, Indicator]] | None = None,
    fetcher=None,
    now: datetime | None = None,
) -> dict:
    """Fetch every dashboard series and write ``catalog.json`` plus series files.

    ``fetcher`` is ``(indicator, start, end) -> Series``. Tests inject one.
    The default fetcher calls FRED and the Yahoo chart API directly.
    """
    data_path = Path(data_dir)
    grouped = indicator_groups() if indicators is None else list(indicators)
    file_ids = [series_file_id(indicator.key) for _, indicator in grouped]
    if len(file_ids) != len(set(file_ids)):
        raise ValueError("Series file ids collide. Refusing to overwrite one series with another.")

    window_end = end or date.today()
    history_start, default_view, today = observation_window(window_end)
    fetch_start = start or history_start
    fetch_end = end or today
    if fetch_start > fetch_end:
        raise ValueError("The start date must be on or before the end date.")
    view_start = default_view
    if view_start < fetch_start or view_start > fetch_end:
        view_start = fetch_start

    moment = utc_now_iso(now)
    fetch = fetcher or default_fetcher(timeout)
    outcomes: dict[str, tuple[str, object]] = {}
    pace_fred = fetcher is None
    fred_start = threading.Lock()

    def run_one(indicator: Indicator) -> tuple[str, tuple[str, object]]:
        if indicator.source == "Computed":
            return indicator.key, ("defer", None)
        if pace_fred and indicator.source == "FRED":
            with fred_start:
                time.sleep(FRED_START_GAP_SECONDS)
        try:
            series = fetch(indicator, fetch_start, fetch_end)
            if series is None or series.dropna().empty:
                raise DataSourceError(f"No data returned for {indicator.key}.")
            return indicator.key, ("ok", series)
        except Exception as exc:
            logger.error(
                "series_fetch_failed key=%s error=%s message=%s",
                indicator.key,
                type(exc).__name__,
                _public_error(exc),
            )
            return indicator.key, ("error", exc)

    overall_timeout = max(600.0, float(timeout) * 20)
    fred_items = [indicator for _, indicator in grouped if indicator.source == "FRED"]
    other_items = [indicator for _, indicator in grouped if indicator.source != "FRED"]
    fred_workers = max(1, min(FRED_WORKERS, workers))
    with (
        ThreadPoolExecutor(max_workers=fred_workers) as fred_pool,
        ThreadPoolExecutor(max_workers=max(1, workers)) as other_pool,
    ):
        futures = [fred_pool.submit(run_one, indicator) for indicator in fred_items]
        futures.extend(other_pool.submit(run_one, indicator) for indicator in other_items)
        try:
            for future in as_completed(futures, timeout=overall_timeout):
                key, outcome = future.result()
                outcomes[key] = outcome
        except TimeoutError:
            logger.error("build_timed_out timeout_seconds=%s", overall_timeout)
            for future in futures:
                future.cancel()

    entries: list[dict] = []
    counts = {"ok": 0, "preserved": 0, "error": 0}
    changed = False

    for group, indicator in grouped:
        meta = metadata_for(indicator, group)
        path = data_path / meta["file"]
        previous = load_json(path)
        try:
            outcome = outcomes.get(indicator.key)
            if outcome is None:
                raise DataSourceError(
                    f"{indicator.source} timed out while fetching {indicator.key}. "
                    "Try refreshing the data."
                )
            status, payload = outcome
            if status == "defer":
                continue
            if status != "ok":
                if isinstance(payload, BaseException):
                    raise payload
                raise DataSourceError(str(payload) or f"No data returned for {indicator.key}.")
            observations = observations_from_series(apply_scale(payload, indicator.scale))
            if not observations:
                raise DataSourceError(f"No data returned for {indicator.key}.")
            document = series_document(meta, observations, status="ok", fetched_at=moment)
            if write_json(path, document):
                changed = True
            entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
            counts["ok"] += 1
        except Exception as exc:
            message = _public_error(exc)
            if previous_is_usable(previous):
                entry = catalog_entry(
                    _catalog_source_from_previous(meta, previous),
                    status="preserved",
                    last_attempt_at=moment,
                    last_error=message,
                )
                counts["preserved"] += 1
            else:
                document = series_document(meta, [], status="error", fetched_at=None)
                if write_json(path, document):
                    changed = True
                entry = catalog_entry(
                    document,
                    status="error",
                    last_attempt_at=moment,
                    last_error=message,
                )
                counts["error"] += 1
        entries.append(entry)

    _add_derived_series(data_path, grouped, entries, counts, moment, timeout)
    fred_keys = [indicator.key for _, indicator in grouped if indicator.source == "FRED"]
    fred_failure = fred_failure_summary(entries, fred_keys)
    catalog = {
        "generator": "build_data.py",
        "generated_at": moment,
        "history_start": fetch_start.isoformat(),
        "history_end": fetch_end.isoformat(),
        "default_start": view_start.isoformat(),
        "counts": counts,
        "series": entries,
    }
    if write_json(data_path / "catalog.json", catalog):
        changed = True

    return {
        "generated_at": moment,
        "history_start": fetch_start.isoformat(),
        "history_end": fetch_end.isoformat(),
        "default_start": view_start.isoformat(),
        "counts": counts,
        "changed": changed,
        "with_data": counts["ok"] + counts["preserved"],
        "entries": entries,
        "data_dir": str(data_path),
        "fred_failure": fred_failure,
    }


def _series_from_document(document: dict | None) -> pd.Series | None:
    if not previous_is_usable(document):
        return None
    rows = document["observations"]
    series = pd.Series(
        [float(row[1]) for row in rows],
        index=pd.to_datetime([row[0] for row in rows]),
        name=document.get("key"),
    )
    return series.sort_index()


def _scaled_component(data_path: Path, key: str, scale: str) -> pd.Series | None:
    """Load a component in its published units (already scaled when the file was written)."""
    return _series_from_document(load_json(data_path / "series" / f"{series_file_id(key)}.json"))


def _add_derived_series(
    data_path: Path,
    grouped: list[tuple[str, Indicator]],
    entries: list[dict],
    counts: dict[str, int],
    moment: str,
    timeout: float,
) -> None:
    by_key = {item["key"]: item for item in entries}
    indicators = {indicator.key: (group, indicator) for group, indicator in grouped}
    if "NETLIQ" in indicators:
        _write_net_liquidity(data_path, indicators["NETLIQ"], entries, by_key, counts, moment)
    if "DTWEXBGS" in indicators:
        _refresh_dollar(data_path, indicators["DTWEXBGS"], entries, by_key, counts, moment, timeout)


def _write_net_liquidity(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        walcl = _scaled_component(data_path, "WALCL", "millions_to_billions")
        tga = _scaled_component(data_path, "WTREGEN", "millions_to_billions")
        rrp = _scaled_component(data_path, "RRPONTSYD", "none")
        spx = _scaled_component(data_path, "^GSPC", "none")
        if walcl is None or tga is None or rrp is None:
            raise DataSourceError("Net liquidity needs Fed assets, the Treasury general account, and reverse repo.")
        net = net_liquidity_series(walcl, tga, rrp)
        observations = observations_from_series(net)
        if not observations:
            raise DataSourceError("Net liquidity had no overlapping dates.")
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        if spx is not None:
            aligned = spx.reindex(net.index).ffill().dropna()
            document["overlay_name"] = "S&P 500"
            document["overlay_unit"] = "points"
            document["overlay"] = observations_from_series(aligned)
        if write_json(path, document):
            pass
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        counts["ok"] += 1
    except Exception as exc:
        message = _public_error(exc)
        if previous_is_usable(previous):
            entry = catalog_entry(
                _catalog_source_from_previous(meta, previous),
                status="preserved",
                last_attempt_at=moment,
                last_error=message,
            )
            counts["preserved"] += 1
        else:
            document = series_document(meta, [], status="error", fetched_at=None)
            write_json(path, document)
            entry = catalog_entry(document, status="error", last_attempt_at=moment, last_error=message)
            counts["error"] += 1
    entries.append(entry)
    by_key[indicator.key] = entry


def _refresh_dollar(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    timeout: float,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    current = _series_from_document(load_json(path))
    try:
        yahoo = fetch_yahoo_chart_series("DX-Y.NYB", date(date.today().year - 6, 1, 1), date.today(), timeout=timeout, attempts=2)
    except Exception as exc:
        logger.warning("dollar_yahoo_failed error=%s", type(exc).__name__)
        return
    try:
        chosen, source = choose_dollar_series(current, yahoo)
    except DataSourceError:
        return
    if source != "Yahoo Finance":
        return
    observations = observations_from_series(chosen)
    yahoo_meta = {**meta, "source": "Yahoo Finance", "description": "ICE US dollar index (DX-Y.NYB), used because it is newer than FRED's broad dollar index."}
    document = series_document(yahoo_meta, observations, status="ok", fetched_at=moment)
    write_json(path, document)
    entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
    if indicator.key in by_key:
        previous_status = by_key[indicator.key]["status"]
        if previous_status in counts:
            counts[previous_status] = max(0, counts[previous_status] - 1)
        counts["ok"] += 1
        index = entries.index(by_key[indicator.key])
        entries[index] = entry
        by_key[indicator.key] = entry


def fred_failure_summary(entries: list[dict], fred_keys: list[str]) -> str | None:
    """Describe a refresh where more than half of the FRED series did not update.

    Preserved and missing series both count as failures. The caller still keeps
    the previous observations; this summary is why the job should exit non-zero.
    """
    if not fred_keys:
        return None
    by_key = {entry["key"]: entry for entry in entries}
    failed: list[str] = []
    for key in fred_keys:
        entry = by_key.get(key)
        if entry is not None and entry.get("status") == "ok":
            continue
        reason = "missing"
        if entry is not None:
            reason = entry.get("last_error") or entry.get("status") or "missing"
        failed.append(f"{key}: {reason}")
    if len(failed) * 2 <= len(fred_keys):
        return None
    lines = [
        f"{len(failed)} of {len(fred_keys)} FRED series failed. Previous observations were kept.",
        *failed,
    ]
    return "\n".join(lines)


def format_report(report: dict) -> str:
    counts = report["counts"]
    lines = [
        f"Snapshot {report['generated_at']}",
        f"Window {report['history_start']} to {report['history_end']}",
        f"ok {counts['ok']}  preserved {counts['preserved']}  failed {counts['error']}",
    ]
    for entry in report["entries"]:
        latest = entry.get("latest_date") or "-"
        detail = entry.get("last_error") or ""
        lines.append(
            f"  {entry['status']:9} {entry['key']:18} {latest:10} {entry['observation_count']:5}  {detail}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch every Macro Terminal series into static JSON files."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--timeout", type=float, default=30, help="Per-request read timeout in seconds. FRED uses at least 30.")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    started = time.perf_counter()
    try:
        report = build_snapshot(
            args.data_dir,
            start=args.start,
            end=args.end,
            timeout=args.timeout,
            workers=args.workers,
        )
    except ValueError as exc:
        print(exc)
        return 1
    print(format_report(report))
    print(f"Finished in {time.perf_counter() - started:.1f}s")
    summary = report.get("fred_failure")
    if summary:
        print(summary, file=sys.stderr)
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            Path(step_summary).write_text("# FRED refresh failed\n\n```\n" + summary + "\n```\n", encoding="utf-8")
        return 2
    if report["with_data"] == 0:
        print("No series has usable observations.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
