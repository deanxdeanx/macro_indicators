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
    observation_window,
)


logger = logging.getLogger("build_data")

DEFAULT_DATA_DIR = Path("site/data")


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


def catalog_entry(
    document: dict,
    *,
    status: str,
    last_attempt_at: str,
    last_error: str | None,
) -> dict:
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
        "group": document["group"],
        "file": document["file"],
        "status": status,
        "observation_count": document.get("observation_count") or 0,
        "latest_date": document.get("latest_date"),
        "fetched_at": document.get("fetched_at"),
        "last_attempt_at": last_attempt_at,
        "last_error": last_error,
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
    }


def _public_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = f"{type(exc).__name__}: the source request failed."
    return message[:300]


def default_fetcher(timeout: float):
    """Fetch one indicator with a short timeout and two attempts."""

    def fetch(indicator: Indicator, start: date, end: date) -> pd.Series:
        if indicator.source == "FRED":
            return fetch_fred_series(
                indicator.key,
                start,
                indicator.transform,
                end,
                timeout=timeout,
                attempts=2,
                retry_backoff=0.5,
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

    def run_one(indicator: Indicator) -> tuple[str, tuple[str, object]]:
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

    overall_timeout = max(60.0, float(timeout) * 6)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(run_one, indicator) for _, indicator in grouped]
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
            if status != "ok":
                if isinstance(payload, BaseException):
                    raise payload
                raise DataSourceError(str(payload) or f"No data returned for {indicator.key}.")
            observations = observations_from_series(payload)
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
    }


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
    parser.add_argument("--timeout", type=float, default=12, help="Per-request read timeout in seconds.")
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
    if report["with_data"] == 0:
        print("No series has usable observations.")
        return 1
    from build_analysis import build_analysis_snapshot

    analysis = build_analysis_snapshot(args.data_dir / "analysis", timeout=args.timeout)
    print(
        f"Analysis tickers written: {', '.join(analysis['written']) or 'none'}"
        + (f"  errors: {analysis['errors']}" if analysis["errors"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
