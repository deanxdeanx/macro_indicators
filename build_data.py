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

from backup_sources import (
    ECB_SOURCE,
    H41_SERIES,
    H41_SOURCE,
    MOF_SOURCE,
    NYFED_SOURCE,
    TREASURY_SOURCE,
    choose_eps,
    daily_earnings_yield,
    earnings_yield_minus_real,
    eps_is_old,
    fetch_cftc_positions,
    fetch_dts_tga,
    fetch_ecb_aaa_yield,
    fetch_h41_series,
    fetch_jgb_yield,
    fetch_nyfed_rate,
    fetch_nyfed_rrp,
    fetch_spx_earnings,
    fetch_treasury_real_yield,
    fetch_treasury_yields,
    price_ratio,
    yield_spread,
)
from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    DataSourceError,
    Indicator,
    company_indicators,
    fetch_fred_series,
    as_of_label,
    fetch_yahoo_chart_series,
    fred_refresh_chip,
    lagged_change,
    observation_window,
    range_percentile,
    MACRO_SECTION_ORDER,
    regime_readout,
    regime_words,
    release_is_stale,
    source_dates,
    stale_input_reason,
    week_move_leaders,
)


logger = logging.getLogger("build_data")

DEFAULT_DATA_DIR = Path("site/data")
# GitHub runners time out on parallel reads to fred.stlouisfed.org. FRED is
# fetched one series at a time. After a timeout, the next series waits longer
# and retries fewer times so a dead host does not fill the whole job.
FRED_WORKERS = 1
FRED_RETRY_BACKOFF = 2.0
TREASURY_BACKUP_KEYS = {"DGS3MO", "DGS2", "DGS10"}
TREASURY_SPREAD_KEYS = {"T10Y2Y": ("DGS10", "DGS2"), "T10Y3M": ("DGS10", "DGS3MO")}
NYFED_BACKUP_KEYS = {"SOFR", "DFF"}
H41_BACKUP_KEYS = set(H41_SERIES)
COPPER_GOLD_FACTOR = 1000.0


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
        "visible": indicator.visible,
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
    entry = {
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
        "visible": document.get("visible", True) is not False,
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
    if document.get("eps_date"):
        entry["eps_date"] = document["eps_date"]
        entry["eps_kind"] = document.get("eps_kind") or "trailing"
    return entry


def _catalog_source_from_previous(meta: dict, previous: dict) -> dict:
    observations = previous.get("observations") or []
    latest = previous.get("latest_date")
    if not latest and observations:
        latest = observations[-1][0]
    count = previous.get("observation_count")
    if not isinstance(count, int):
        count = len(observations)
    carried = {
        **meta,
        "observation_count": count,
        "latest_date": latest,
        "fetched_at": previous.get("fetched_at"),
        "observations": previous.get("observations") or [],
    }
    if previous.get("eps_date"):
        carried["eps_date"] = previous["eps_date"]
        carried["eps_kind"] = previous.get("eps_kind") or "trailing"
    return carried


def _public_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = f"{type(exc).__name__}: the source request failed."
    return message[:300]


def fred_attempt_plan(consecutive_timeouts: int) -> tuple[int, float]:
    """Return FRED attempts and the pause before the next series starts.

    The first series retries five times. After one timeout, later series use
    three attempts and a longer gap. After two, they use two attempts.
    """
    if consecutive_timeouts <= 0:
        return 5, 0.8
    if consecutive_timeouts == 1:
        return 3, 2.0
    return 2, 2.5


def default_fetcher(timeout: float, pace: dict | None = None):
    """Fetch one indicator. FRED gets a longer timeout and more retries than Yahoo."""

    fred_timeout = max(float(timeout), 30.0)
    fred_pace = pace if pace is not None else {"consecutive_timeouts": 0}

    def fetch(indicator: Indicator, start: date, end: date) -> pd.Series:
        if indicator.source == "Computed":
            raise DataSourceError(f"{indicator.key} is computed after the source series are stored.")
        if indicator.source == "FRED":
            attempts, _gap = fred_attempt_plan(int(fred_pace.get("consecutive_timeouts") or 0))
            return fetch_fred_series(
                indicator.key,
                start,
                indicator.transform,
                end,
                timeout=fred_timeout,
                attempts=attempts,
                retry_backoff=FRED_RETRY_BACKOFF,
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


def merge_levels(previous: pd.Series | None, update: pd.Series) -> pd.Series:
    """Keep older history and let the newer source replace overlapping dates."""
    incoming = update.dropna().sort_index()
    if previous is None or previous.dropna().empty:
        incoming.name = update.name
        return incoming
    combined = pd.concat([previous.dropna().sort_index(), incoming]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.name = update.name or previous.name
    return combined


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
    include_semis: bool | None = None,
) -> dict:
    """Fetch every dashboard series and write ``catalog.json`` plus series files.

    ``fetcher`` is ``(indicator, start, end) -> Series``. Tests inject one.
    The default fetcher calls FRED and the Yahoo chart API directly.
    """
    data_path = Path(data_dir)
    full_snapshot = indicators is None
    grouped = indicator_groups() if indicators is None else list(indicators)
    if include_semis is None:
        include_semis = full_snapshot
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
    _backup_fred_series.cache = {}
    fred_pace = {"consecutive_timeouts": 0}
    fetch = fetcher or default_fetcher(timeout, fred_pace)
    outcomes: dict[str, tuple[str, object]] = {}
    pace_fred = fetcher is None
    fred_start = threading.Lock()

    def run_one(indicator: Indicator) -> tuple[str, tuple[str, object]]:
        if indicator.source == "Computed":
            return indicator.key, ("defer", None)
        if pace_fred and indicator.source == "FRED":
            _attempts, gap = fred_attempt_plan(int(fred_pace.get("consecutive_timeouts") or 0))
            with fred_start:
                time.sleep(gap)
        try:
            series = fetch(indicator, fetch_start, fetch_end)
            if series is None or series.dropna().empty:
                raise DataSourceError(f"No data returned for {indicator.key}.")
            if pace_fred and indicator.source == "FRED":
                fred_pace["consecutive_timeouts"] = 0
            return indicator.key, ("ok", series)
        except Exception as exc:
            message = _public_error(exc)
            if pace_fred and indicator.source == "FRED" and "timed out" in message.lower():
                fred_pace["consecutive_timeouts"] = int(fred_pace.get("consecutive_timeouts") or 0) + 1
            logger.error(
                "series_fetch_failed key=%s error=%s message=%s",
                indicator.key,
                type(exc).__name__,
                message,
            )
            return indicator.key, ("error", exc)

    overall_timeout = max(2400.0, float(timeout) * 40)
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
            backup = _backup_fred_series(indicator, fetch_start, fetch_end, timeout)
            if backup is not None:
                series, source_name = backup
                if previous_is_usable(previous):
                    series = merge_levels(_series_from_document(previous), series)
                observations = observations_from_series(series)
                if observations:
                    backed = {**meta, "source": source_name}
                    if indicator.key == "WTREGEN" and source_name == TREASURY_SOURCE:
                        backed["frequency"] = "Daily"
                        backed["description"] = (
                            "Daily Treasury general account closing balance from the Daily Treasury Statement, in billions."
                        )
                    document = series_document(backed, observations, status="ok", fetched_at=moment)
                    if write_json(path, document):
                        changed = True
                    entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
                    counts["ok"] += 1
                    entries.append(entry)
                    continue
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

    as_of = date.fromisoformat(moment[:10])
    fetch_live = fetcher is None
    _add_derived_series(
        data_path,
        grouped,
        entries,
        counts,
        moment,
        timeout,
        as_of=as_of,
        fetch_start=fetch_start,
        fetch_end=fetch_end,
        fetch_live=fetch_live,
    )
    if include_semis:
        _add_semiconductors(data_path, entries, counts, moment, timeout)
    fred_keys = [indicator.key for _, indicator in grouped if indicator.source == "FRED"]
    fred_failure = fred_failure_summary(entries, fred_keys)
    dates = source_dates(entries)
    catalog = {
        "generator": "build_data.py",
        "generated_at": moment,
        "history_start": fetch_start.isoformat(),
        "history_end": fetch_end.isoformat(),
        "default_start": view_start.isoformat(),
        "fred_oldest": dates["fred_oldest"],
        "fred_as_of": dates["fred_as_of"],
        "yahoo_oldest": dates["yahoo_oldest"],
        "yahoo_as_of": dates["yahoo_as_of"],
        "data_through": dates["data_through"],
        "as_of_label": as_of_label(dates),
        "regime_readout": regime_readout(entries, as_of=moment[:10]),
        "regime_words": regime_words(entries, as_of=moment[:10]),
        "fred_chip": fred_refresh_chip(entries),
        "week_movers": _week_movers(entries, data_path, date.fromisoformat(moment[:10])),
        "section_order": list(MACRO_SECTION_ORDER),
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


def _backup_cache() -> dict:
    cache = getattr(_backup_fred_series, "cache", None)
    if not isinstance(cache, dict):
        cache = {}
        _backup_fred_series.cache = cache
    return cache


def _cache_load(cache: dict, name: str, loader):
    """Run a backup fetch once. A failed fetch stays failed for the rest of the build."""
    if name in cache:
        cached = cache[name]
        if isinstance(cached, BaseException):
            raise cached
        return cached
    try:
        value = loader()
    except Exception as exc:
        cache[name] = exc
        raise
    cache[name] = value
    return value


def _backup_fred_series(
    indicator: Indicator,
    start: date,
    end: date,
    timeout: float,
) -> tuple[pd.Series, str] | None:
    """Public backup after a FRED miss. None when this series has no backup or the backup is down."""
    key = indicator.key
    supported = (
        TREASURY_BACKUP_KEYS
        | set(TREASURY_SPREAD_KEYS)
        | NYFED_BACKUP_KEYS
        | H41_BACKUP_KEYS
        | {"RRPONTSYD", "DFII10", "T10YIE"}
    )
    if key not in supported:
        return None
    cache = _backup_cache()
    try:
        series, source_name = _load_backup(cache, key, start, end, timeout)
    except Exception as exc:
        logger.warning("backup_failed key=%s error=%s", key, type(exc).__name__)
        return None
    if series is None or series.dropna().empty:
        return None
    return series, source_name


def _load_backup(cache: dict, key: str, start: date, end: date, timeout: float) -> tuple[pd.Series | None, str]:
    if key in TREASURY_BACKUP_KEYS or key in TREASURY_SPREAD_KEYS or key == "T10YIE":
        book = _cache_load(cache, "treasury", lambda: fetch_treasury_yields(start, end, timeout))
    else:
        book = {}
    if key in TREASURY_BACKUP_KEYS:
        return book.get(key), TREASURY_SOURCE
    if key in TREASURY_SPREAD_KEYS:
        long_key, short_key = TREASURY_SPREAD_KEYS[key]
        if long_key not in book or short_key not in book:
            return None, TREASURY_SOURCE
        return yield_spread(book[long_key], book[short_key], key), TREASURY_SOURCE
    if key == "DFII10":
        real = _cache_load(cache, "real", lambda: fetch_treasury_real_yield(start, end, timeout))
        return real, TREASURY_SOURCE
    if key == "T10YIE":
        real = _cache_load(cache, "real", lambda: fetch_treasury_real_yield(start, end, timeout))
        nominal = book.get("DGS10")
        if nominal is None:
            return None, TREASURY_SOURCE
        return yield_spread(nominal, real, "T10YIE"), TREASURY_SOURCE
    if key in NYFED_BACKUP_KEYS:
        return fetch_nyfed_rate(key, start, end, timeout), NYFED_SOURCE
    if key == "RRPONTSYD":
        return _cache_load(cache, "rrp", lambda: fetch_nyfed_rrp(start, end, timeout)), NYFED_SOURCE
    if key == "WTREGEN":
        try:
            return _cache_load(cache, "dts", lambda: fetch_dts_tga(start, end, timeout)), TREASURY_SOURCE
        except Exception:
            logger.warning("dts_tga_failed")
        levels = _cache_load(cache, f"h41:{key}", lambda: fetch_h41_series(key, timeout) / 1000.0)
        return levels, H41_SOURCE
    if key in H41_BACKUP_KEYS:
        levels = _cache_load(cache, f"h41:{key}", lambda: fetch_h41_series(key, timeout) / 1000.0)
        return levels, H41_SOURCE
    return None, ""


def _place_entry(entries: list[dict], entry: dict, after_key: str | None) -> None:
    if after_key:
        for index, existing in enumerate(entries):
            if existing.get("key") == after_key:
                entries.insert(index + 1, entry)
                return
    entries.append(entry)


def _write_preserved_or_error(
    path: Path,
    meta: dict,
    previous: dict | None,
    entries: list[dict],
    counts: dict[str, int],
    moment: str,
    message: str,
    after_key: str | None = None,
) -> None:
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
    _place_entry(entries, entry, after_key)


def _write_price_ratio(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    numerator_key: str,
    denominator_key: str,
    after_key: str,
    as_of: date,
    factor: float = 1.0,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        numerator = _scaled_component(data_path, numerator_key, "none")
        denominator = _scaled_component(data_path, denominator_key, "none")
        if numerator is None or denominator is None:
            raise DataSourceError(f"{indicator.key} needs {numerator_key} and {denominator_key}.")
        ratio = price_ratio(numerator, denominator, indicator.key) * factor
        observations = observations_from_series(ratio)
        if not observations:
            raise DataSourceError(f"{indicator.key} had no overlapping prices.")
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        _note_stale(entry, by_key, [numerator_key, denominator_key], as_of)
        counts["ok"] += 1
        _place_entry(entries, entry, after_key)
        by_key[indicator.key] = entry
    except Exception as exc:
        message = _public_error(exc)
        _write_preserved_or_error(path, meta, previous, entries, counts, moment, message, after_key)


def _write_earnings_spread(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    timeout: float,
    as_of: date,
    fetch_live: bool,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    if not fetch_live:
        _write_preserved_or_error(
            path, meta, previous, entries, counts, moment,
            "Earnings yield was not refreshed in this offline build.", "DFII10",
        )
        return
    try:
        real_yield = _scaled_component(data_path, "DFII10", "none")
        price = _scaled_component(data_path, "^GSPC", "none")
        if real_yield is None or price is None:
            raise DataSourceError("The earnings-yield spread needs the S&P price and the 10-year real yield.")
        trailing, forward = fetch_spx_earnings(timeout)
        eps, kind, eps_stamp = choose_eps(trailing, forward, as_of)
        earnings_yield = daily_earnings_yield(price, eps)
        spread = earnings_yield_minus_real(earnings_yield, real_yield)
        observations = observations_from_series(spread)
        if not observations:
            raise DataSourceError("The earnings-yield spread had no overlapping dates.")
        eps_day = pd.Timestamp(eps_stamp).strftime("%Y-%m-%d")
        label = "Forward" if kind == "forward" else "Trailing"
        meta = {
            **meta,
            "description": (
                f"Daily S&P earnings yield minus the 10-year real yield. "
                f"{label} twelve-month EPS as of {eps_day}, carried onto the S&P 500 price."
            ),
        }
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        document["eps_date"] = eps_day
        document["eps_kind"] = kind
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        extra = None
        if eps_is_old(eps_stamp, as_of):
            extra = f"{label} EPS is as of {eps_day}."
        _note_stale(entry, by_key, ["DFII10", "^GSPC"], as_of, extra)
        counts["ok"] += 1
        _place_entry(entries, entry, "DFII10")
        by_key[indicator.key] = entry
    except Exception as exc:
        message = _public_error(exc)
        logger.warning("earnings_spread_failed error=%s", type(exc).__name__)
        _write_preserved_or_error(path, meta, previous, entries, counts, moment, message, "DFII10")


def _write_cftc(
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
    previous = load_json(path)
    try:
        start = date(date.today().year - 3, 1, 1)
        markets = fetch_cftc_positions(start, timeout)
        observations = markets[0]["observations"]
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        document["markets"] = markets
        document["display"] = "positions"
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        counts["ok"] += 1
    except Exception as exc:
        message = _public_error(exc)
        logger.warning("cftc_failed error=%s", type(exc).__name__)
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


def _note_stale(entry: dict, by_key: dict[str, dict], keys: list[str], as_of: date, extra: str | None = None) -> None:
    """Mark a derived series when an input is held over or past its release window."""
    reason = stale_input_reason(by_key, keys, as_of)
    parts = [part for part in (reason, extra) if part]
    if not parts:
        return
    entry["inputs_stale"] = True
    entry["last_error"] = " ".join(parts)[:300]


def _add_derived_series(
    data_path: Path,
    grouped: list[tuple[str, Indicator]],
    entries: list[dict],
    counts: dict[str, int],
    moment: str,
    timeout: float,
    *,
    as_of: date,
    fetch_start: date,
    fetch_end: date,
    fetch_live: bool,
) -> None:
    by_key = {item["key"]: item for item in entries}
    indicators = {indicator.key: (group, indicator) for group, indicator in grouped}
    if "NETLIQ" in indicators:
        _write_net_liquidity(
            data_path, indicators["NETLIQ"], entries, by_key, counts, moment,
            as_of=as_of, fetch_start=fetch_start, fetch_end=fetch_end, fetch_live=fetch_live, timeout=timeout,
        )
    if "DTWEXBGS" in indicators and fetch_live:
        _refresh_dollar(data_path, indicators["DTWEXBGS"], entries, by_key, counts, moment, timeout)
    if "DGS2DFF" in indicators:
        _write_funds_spread(data_path, indicators["DGS2DFF"], entries, by_key, counts, moment, as_of)
    if "SOFRIORB" in indicators:
        _write_funding_spread(data_path, indicators["SOFRIORB"], entries, by_key, counts, moment, as_of)
    if "COPPERGOLD" in indicators:
        _write_price_ratio(
            data_path, indicators["COPPERGOLD"], entries, by_key, counts, moment,
            "HG=F", "GC=F", "DGS10", as_of, COPPER_GOLD_FACTOR,
        )
    if "SPEARN10" in indicators:
        _write_earnings_spread(
            data_path, indicators["SPEARN10"], entries, by_key, counts, moment, timeout, as_of, fetch_live,
        )
    if "RSPSPY" in indicators:
        _write_price_ratio(
            data_path, indicators["RSPSPY"], entries, by_key, counts, moment,
            "RSP", "SPY", "IWM", as_of, 1.0,
        )
    if "KRESPY" in indicators:
        _write_price_ratio(
            data_path, indicators["KRESPY"], entries, by_key, counts, moment,
            "KRE", "SPY", "KRE", as_of, 1.0,
        )
    if "HYGIEF" in indicators:
        _write_price_ratio(
            data_path, indicators["HYGIEF"], entries, by_key, counts, moment,
            "HYG", "IEF", "BAMLC0A0CM", as_of, 1.0,
        )
    if "LQDIEF" in indicators:
        _write_price_ratio(
            data_path, indicators["LQDIEF"], entries, by_key, counts, moment,
            "LQD", "IEF", "HYGIEF", as_of, 1.0,
        )
    if "EUR10Y" in indicators and fetch_live:
        _write_fetched_series(
            data_path, indicators["EUR10Y"], entries, by_key, counts, moment,
            lambda: fetch_ecb_aaa_yield(fetch_start, fetch_end, timeout), ECB_SOURCE,
        )
    if "JGB10Y" in indicators and fetch_live:
        _write_fetched_series(
            data_path, indicators["JGB10Y"], entries, by_key, counts, moment,
            lambda: fetch_jgb_yield(fetch_start, fetch_end, timeout), MOF_SOURCE,
        )
    if "CFTC" in indicators and fetch_live:
        _write_cftc(data_path, indicators["CFTC"], entries, by_key, counts, moment, timeout)


def _direct_late(series: pd.Series | None, frequency: str, as_of: date) -> bool:
    if series is None or series.dropna().empty:
        return True
    return release_is_stale(series.dropna().index[-1], frequency, as_of)


def _prefer_daily(stored: pd.Series | None, daily: pd.Series | None) -> pd.Series | None:
    """Use the daily print where it exists and keep older stored history before it starts."""
    if daily is None or daily.dropna().empty:
        return stored
    daily = daily.dropna().sort_index()
    if stored is None or stored.dropna().empty:
        return daily
    earlier = stored.dropna().sort_index()
    earlier = earlier[earlier.index < daily.index[0]]
    combined = pd.concat([earlier, daily]).sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def _write_net_liquidity(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    *,
    as_of: date,
    fetch_start: date,
    fetch_end: date,
    fetch_live: bool,
    timeout: float,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        walcl = _scaled_component(data_path, "WALCL", "millions_to_billions")
        tga_file = _scaled_component(data_path, "WTREGEN", "millions_to_billions")
        rrp_file = _scaled_component(data_path, "RRPONTSYD", "none")
        spx = _scaled_component(data_path, "^GSPC", "none")
        daily_tga = None
        daily_rrp = None
        h41_assets = None
        if fetch_live:
            try:
                daily_tga = fetch_dts_tga(fetch_start, fetch_end, timeout)
            except Exception as exc:
                logger.warning("netliq_tga_failed error=%s", type(exc).__name__)
            try:
                daily_rrp = fetch_nyfed_rrp(fetch_start, fetch_end, timeout)
            except Exception as exc:
                logger.warning("netliq_rrp_failed error=%s", type(exc).__name__)
            try:
                h41_assets = fetch_h41_series("WALCL", timeout) / 1000.0
            except Exception as exc:
                logger.warning("netliq_h41_failed error=%s", type(exc).__name__)
        assets_source = "FRED"
        tga_source = "FRED"
        rrp_source = "FRED"
        if walcl is None and h41_assets is not None:
            walcl = h41_assets
            assets_source = H41_SOURCE
        elif walcl is not None and h41_assets is not None and h41_assets.index[-1] > walcl.dropna().index[-1]:
            walcl = merge_levels(walcl, h41_assets)
            assets_source = H41_SOURCE
        if daily_tga is not None:
            tga = _prefer_daily(tga_file, daily_tga)
            tga_source = TREASURY_SOURCE
        else:
            tga = tga_file
        if daily_rrp is not None:
            rrp = _prefer_daily(rrp_file, daily_rrp)
            rrp_source = NYFED_SOURCE
        else:
            rrp = rrp_file
        if walcl is None or tga is None or rrp is None:
            raise DataSourceError("Net liquidity needs Fed assets, the Treasury general account, and reverse repo.")
        net = net_liquidity_series(walcl, tga, rrp)
        observations = observations_from_series(net)
        if not observations:
            raise DataSourceError("Net liquidity had no overlapping dates.")
        sources = {assets_source, tga_source, rrp_source}
        if sources == {"FRED"}:
            meta = {**meta, "source": "Computed"}
        else:
            meta = {
                **meta,
                "source": f"{assets_source} assets · {tga_source} TGA · {rrp_source} RRP",
                "description": (
                    "Fed assets minus the Treasury general account minus overnight reverse repo, in billions. "
                    "Assets are the weekly H.4.1 level carried forward. TGA and reverse repo are daily when those sources respond."
                ),
            }
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        if spx is not None:
            aligned = spx.reindex(net.index).ffill().dropna()
            document["overlay_name"] = "S&P 500"
            document["overlay_unit"] = "points"
            document["overlay"] = observations_from_series(aligned)
        document["rrp_name"] = "Overnight reverse repo"
        document["rrp_unit"] = "$ billions"
        document["rrp"] = observations_from_series(rrp.dropna().sort_index())
        if write_json(path, document):
            pass
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        stale_keys = []
        extra_notes = []
        if assets_source == "FRED":
            stale_keys.append("WALCL")
        elif _direct_late(walcl, "Weekly", as_of):
            extra_notes.append("Fed assets are past their release window.")
        if tga_source == "FRED":
            stale_keys.append("WTREGEN")
        elif _direct_late(tga, "Daily", as_of):
            extra_notes.append("The Treasury general account is past its release window.")
        if rrp_source == "FRED":
            stale_keys.append("RRPONTSYD")
        elif _direct_late(rrp, "Daily", as_of):
            extra_notes.append("Reverse repo is past its release window.")
        _note_stale(entry, by_key, stale_keys, as_of, " ".join(extra_notes) or None)
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


def funds_spread(two_year: pd.Series, funds: pd.Series) -> pd.Series:
    """2-year Treasury yield minus the effective federal funds rate, in percentage points."""
    frame = pd.concat({"two_year": two_year, "funds": funds}, axis=1).dropna(how="any")
    spread = (frame["two_year"] - frame["funds"]).dropna()
    spread.name = "DGS2DFF"
    return spread


def _write_funds_spread(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    as_of: date,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        two_year = _scaled_component(data_path, "DGS2", "none")
        funds = _scaled_component(data_path, "DFF", "none")
        if two_year is None or funds is None:
            raise DataSourceError("2-year minus fed funds needs DGS2 and DFF.")
        spread = funds_spread(two_year, funds)
        observations = observations_from_series(spread)
        if not observations:
            raise DataSourceError("2-year minus fed funds had no overlapping dates.")
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        _note_stale(entry, by_key, ["DGS2", "DFF"], as_of)
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


def _write_funding_spread(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    as_of: date,
) -> None:
    group, indicator = grouped_indicator
    meta = metadata_for(indicator, group)
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        sofr = _scaled_component(data_path, "SOFR", "none")
        iorb = _scaled_component(data_path, "IORB", "none")
        if sofr is None or iorb is None:
            raise DataSourceError("SOFR minus IORB needs both SOFR and IORB.")
        spread = funds_spread(sofr, iorb)
        spread.name = "SOFRIORB"
        observations = observations_from_series(spread)
        if not observations:
            raise DataSourceError("SOFR minus IORB had no overlapping dates.")
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        _note_stale(entry, by_key, ["SOFR", "IORB"], as_of)
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


def _add_semiconductors(
    data_path: Path,
    entries: list[dict],
    counts: dict[str, int],
    moment: str,
    timeout: float,
) -> None:
    """Rank the semiconductor universe and write the top 10. A failure keeps the previous file."""
    from semis import SEMI_UNIVERSE, build_semi_document, catalog_semi, quote_market_cap

    previous_by_key = {
        entry["key"]: entry for entry in entries if entry.get("group") == "semis"
    }
    # Drop any semis already on the list; this run rebuilds that group.
    entries[:] = [entry for entry in entries if entry.get("group") != "semis"]
    measured: list[tuple[dict, float | None]] = []
    for candidate in SEMI_UNIVERSE:
        try:
            measured.append((candidate, quote_market_cap(candidate, timeout=timeout)))
        except Exception as exc:
            logger.warning("semi_cap_failed ticker=%s error=%s", candidate["ticker"], type(exc).__name__)
            previous = load_json(data_path / "semis" / f"{candidate['ticker']}.json")
            stored = previous.get("market_cap") if isinstance(previous, dict) else None
            measured.append((candidate, float(stored) if stored else None))
    ranked = sorted(
        [(candidate, cap) for candidate, cap in measured if cap],
        key=lambda item: item[1],
        reverse=True,
    )[:10]
    if not ranked:
        for entry in previous_by_key.values():
            entries.append(entry)
        return
    for candidate, _cap in ranked:
        path = data_path / "semis" / f"{candidate['ticker']}.json"
        previous = load_json(path)
        try:
            document = build_semi_document(candidate, timeout=timeout)
            document["status"] = "ok"
            document["fetched_at"] = moment
            write_json(path, document)
            entry = catalog_semi(document, status="ok", last_error=None, fetched_at=moment)
            counts["ok"] += 1
        except Exception as exc:
            message = _public_error(exc)
            logger.error("semi_fetch_failed ticker=%s error=%s", candidate["ticker"], type(exc).__name__)
            if previous_is_usable(previous):
                entry = catalog_semi(previous, status="preserved", last_error=message, fetched_at=previous.get("fetched_at"))
                entry["last_attempt_at"] = moment
                counts["preserved"] += 1
            else:
                entry = catalog_semi(
                    {
                        "key": candidate["ticker"],
                        "name": candidate["name"],
                        "description": message,
                        "observation_count": 0,
                        "latest_date": None,
                    },
                    status="error",
                    last_error=message,
                    fetched_at=None,
                )
                entry["last_attempt_at"] = moment
                counts["error"] += 1
        entries.append(entry)


def _write_fetched_series(
    data_path: Path,
    grouped_indicator: tuple[str, Indicator],
    entries: list[dict],
    by_key: dict[str, dict],
    counts: dict[str, int],
    moment: str,
    loader,
    source_name: str,
) -> None:
    """Fetch one non-FRED series. A failure keeps the previous file."""
    group, indicator = grouped_indicator
    meta = {**metadata_for(indicator, group), "source": source_name}
    path = data_path / meta["file"]
    previous = load_json(path)
    try:
        observations = observations_from_series(loader())
        if not observations:
            raise DataSourceError(f"No data returned for {indicator.key}.")
        document = series_document(meta, observations, status="ok", fetched_at=moment)
        write_json(path, document)
        entry = catalog_entry(document, status="ok", last_attempt_at=moment, last_error=None)
        counts["ok"] += 1
    except Exception as exc:
        message = _public_error(exc)
        logger.warning("external_fetch_failed key=%s error=%s", indicator.key, type(exc).__name__)
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


def _week_movers(entries: list[dict], data_path: Path, as_of: date) -> list[dict]:
    """Largest weekly moves already stored in this snapshot, skipping stale series."""
    items: list[tuple[dict, pd.Series]] = []
    for entry in entries:
        if entry.get("group") not in {"macro", "markets"} or entry.get("key") == "CFTC":
            continue
        if entry.get("visible") is False:
            continue
        series = _series_from_document(load_json(data_path / entry["file"]))
        if series is None:
            continue
        items.append((entry, series))
    return week_move_leaders(items, as_of)


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
