"""Data fetching and transformation helpers for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO
import logging
import time
from typing import Iterable
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st
import yfinance as yf


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_CACHE_TTL = 60 * 60 * 12
YAHOO_CACHE_TTL = 60 * 60 * 6
MAX_FETCH_ATTEMPTS = 3
FRESHNESS_THRESHOLDS_DAYS = {
    "Daily": 7,
    "Weekly": 16,
    "Monthly": 75,
    "Quarterly": 200,
}

logger = logging.getLogger(__name__)


class DataSourceError(RuntimeError):
    """A source failure with a concise message suitable for the dashboard."""


@dataclass(frozen=True)
class Indicator:
    key: str
    name: str
    source: str
    unit: str
    frequency: str
    transform: str = "none"
    change_mode: str = "percent"
    description: str = ""
    max_age_days: int | None = None


MACRO_INDICATORS = (
    Indicator("UNRATE", "US Unemployment Rate", "FRED", "%", "Monthly", change_mode="points"),
    Indicator(
        "CPIAUCSL",
        "US Inflation (CPI, YoY)",
        "FRED",
        "%",
        "Monthly",
        "yoy",
        "points",
    ),
    Indicator(
        "A191RL1Q225SBEA",
        "US Real GDP Growth Rate",
        "FRED",
        "%",
        "Quarterly",
        change_mode="points",
    ),
    Indicator(
        "M2SL",
        "US M2 Money Supply",
        "FRED",
        "$ billions",
        "Monthly",
        description="Seasonally adjusted monthly M2 level.",
        max_age_days=120,
    ),
    Indicator(
        "T10Y2Y",
        "10Y-2Y Treasury Spread",
        "FRED",
        "%",
        "Daily",
        change_mode="points",
        description="Negative values indicate an inverted yield curve.",
    ),
    Indicator(
        "DGS10",
        "10-Year Treasury Yield",
        "FRED",
        "%",
        "Daily",
        change_mode="points",
        description="Benchmark 10-year constant-maturity Treasury yield.",
    ),
    Indicator(
        "ICSA",
        "Initial Jobless Claims",
        "FRED",
        "claims",
        "Weekly",
        change_mode="points",
    ),
    Indicator(
        "FEDFUNDS",
        "Effective Federal Funds Rate",
        "FRED",
        "%",
        "Monthly",
        change_mode="points",
    ),
    Indicator(
        "UMCSENT",
        "Consumer Sentiment",
        "FRED",
        "index",
        "Monthly",
        description="University of Michigan index; not seasonally adjusted and delayed one month.",
        max_age_days=110,
    ),
)

MARKET_INDICATORS = (
    Indicator("^GSPC", "S&P 500 Index", "Yahoo Finance", "points", "Daily"),
    Indicator("^IXIC", "Nasdaq Composite Index", "Yahoo Finance", "points", "Daily"),
    Indicator("BTC-USD", "Bitcoin Price (USD)", "Yahoo Finance", "$", "Daily"),
    Indicator("^VIX", "CBOE Volatility Index", "Yahoo Finance", "points", "Daily"),
    Indicator("GC=F", "Gold Futures", "Yahoo Finance", "$", "Daily"),
    Indicator("CL=F", "WTI Crude Oil Futures", "Yahoo Finance", "$", "Daily"),
)

# A representative, maintainable basket of 20 of the largest US-listed companies.
# Market-cap rankings move frequently, so this list should be reviewed periodically.
TOP_COMPANY_TICKERS = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "Nvidia",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta Platforms",
    "AVGO": "Broadcom",
    "TSLA": "Tesla",
    "BRK-B": "Berkshire Hathaway",
    "LLY": "Eli Lilly",
    "JPM": "JPMorgan Chase",
    "WMT": "Walmart",
    "V": "Visa",
    "ORCL": "Oracle",
    "MA": "Mastercard",
    "XOM": "Exxon Mobil",
    "COST": "Costco",
    "NFLX": "Netflix",
    "JNJ": "Johnson & Johnson",
    "PG": "Procter & Gamble",
}


def _normalize_series(series: pd.Series, name: str) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).tz_localize(None)
    series = series[~series.index.duplicated(keep="last")].sort_index()
    series.name = name
    return series


def _validate_series(series: pd.Series, name: str) -> pd.Series:
    if series.empty:
        raise ValueError(f"No data returned for {name}.")
    if not isinstance(series.index, pd.DatetimeIndex):
        raise ValueError(f"{name} did not return a valid date index.")
    return series


def validate_fred_response(frame: pd.DataFrame, series_id: str) -> pd.DataFrame:
    """Validate the schema and usable content of a FRED CSV response."""
    required_columns = {"observation_date", series_id}
    missing_columns = required_columns.difference(frame.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise DataSourceError(
            f"FRED returned an invalid response for {series_id} (missing {missing})."
        )
    if frame.empty:
        raise DataSourceError(f"FRED returned no observations for {series_id}.")
    valid_dates = pd.to_datetime(frame["observation_date"], errors="coerce").notna()
    if not valid_dates.all():
        raise DataSourceError(f"FRED returned malformed observation dates for {series_id}.")
    if pd.to_numeric(frame[series_id], errors="coerce").notna().sum() == 0:
        raise DataSourceError(f"FRED returned no numeric observations for {series_id}.")
    return frame


def series_freshness(
    series: pd.Series,
    frequency: str,
    max_age_days: int | None = None,
    as_of: date | pd.Timestamp | None = None,
) -> dict[str, int | bool | pd.Timestamp | None]:
    """Return observation age and a frequency-aware potential-staleness flag."""
    threshold = max_age_days or FRESHNESS_THRESHOLDS_DAYS.get(frequency, 30)
    clean = series.dropna().sort_index()
    if clean.empty:
        return {
            "latest_date": None,
            "age_days": 0,
            "max_age_days": threshold,
            "is_stale": True,
        }

    latest_date = pd.Timestamp(clean.index[-1]).tz_localize(None).normalize()
    reference_date = pd.Timestamp(as_of or date.today()).tz_localize(None).normalize()
    age_days = max(0, int((reference_date - latest_date).days))
    return {
        "latest_date": latest_date,
        "age_days": age_days,
        "max_age_days": threshold,
        "is_stale": age_days > threshold,
    }


def _request_error_message(source: str, item: str, exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return f"{source} timed out while fetching {item}. Try refreshing the data."
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status == 429:
            return f"{source} rate-limited the request for {item}. Try again later."
        if status >= 500:
            return f"{source} is temporarily unavailable for {item} (HTTP {status})."
        return f"{source} rejected the request for {item} (HTTP {status})."
    return f"{source} could not fetch {item}. Check the connection and try refreshing the data."


@st.cache_data(ttl=FRED_CACHE_TTL, show_spinner=False)
def fetch_fred_series(series_id: str, start_date: date, transform: str = "none", end_date: date | None = None) -> pd.Series:
    """Fetch one FRED series through the public CSV endpoint."""
    if end_date is None:
        end_date = date.today()
    params = {
        "id": series_id,
        "cosd": start_date.isoformat(),
        "coed": end_date.isoformat(),
    }
    url = f"{FRED_CSV_URL}?{urlencode(params)}"
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            response = requests.get(
                url,
                timeout=60,
                headers={"User-Agent": "macro_indicators/1.0"},
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            logger.warning(
                "fred_request_failed series_id=%s attempt=%s error=%s",
                series_id,
                attempt + 1,
                type(exc).__name__,
            )
            if attempt == MAX_FETCH_ATTEMPTS - 1:
                raise DataSourceError(_request_error_message("FRED", series_id, exc)) from exc
            time.sleep(2**attempt)

    try:
        frame = pd.read_csv(StringIO(response.text))
    except pd.errors.ParserError as exc:
        raise DataSourceError(f"FRED returned unreadable CSV data for {series_id}.") from exc
    frame = validate_fred_response(frame, series_id)
    series = _normalize_series(frame.set_index("observation_date")[series_id], series_id)
    if transform == "yoy":
        series = series.pct_change(periods=12, fill_method=None).mul(100).dropna()
    return _validate_series(series, series_id)


def _extract_close_prices(download: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if download.empty:
        return pd.DataFrame()

    if isinstance(download.columns, pd.MultiIndex):
        if "Close" in download.columns.get_level_values(0):
            close = download["Close"]
        else:
            close = download.xs("Close", axis=1, level=-1)
    else:
        close = download[["Close"]].rename(columns={"Close": tickers[0]})

    if isinstance(close, pd.Series):
        close = close.to_frame(name=tickers[0])
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close.sort_index().dropna(how="all")


@st.cache_data(ttl=YAHOO_CACHE_TTL, show_spinner=False)
def fetch_yahoo_prices(tickers: tuple[str, ...], start_date: date, end_date: date | None = None) -> pd.DataFrame:
    """Fetch adjusted historical close prices from Yahoo Finance in one batch."""
    if end_date is None:
        end_date = date.today()
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            download = yf.download(
                list(tickers),
                start=start_date.isoformat(),
                end=(end_date + pd.Timedelta(days=1)).isoformat(),
                auto_adjust=True,
                progress=False,
                group_by="column",
                threads=True,
                timeout=30,
            )
            close = _extract_close_prices(download, list(tickers))
            if not close.empty:
                return close
            raise ValueError("Yahoo Finance returned no price data.")
        except Exception as exc:
            logger.warning(
                "yahoo_request_failed ticker_count=%s attempt=%s error=%s",
                len(tickers),
                attempt + 1,
                type(exc).__name__,
            )
            if attempt == MAX_FETCH_ATTEMPTS - 1:
                detail = str(exc).lower()
                if "rate limit" in detail or "429" in detail:
                    message = "Yahoo Finance rate-limited the request. Try again later."
                elif isinstance(exc, TimeoutError) or "timed out" in detail:
                    message = "Yahoo Finance timed out. Try refreshing the data."
                else:
                    message = (
                        "Yahoo Finance returned no usable price data. Try refreshing the data."
                    )
                raise DataSourceError(message) from exc
            time.sleep(2**attempt)
    raise DataSourceError("Yahoo Finance fetch failed.")


def latest_metrics(series: pd.Series, change_mode: str = "percent") -> dict[str, float | pd.Timestamp]:
    """Calculate latest value, previous-period change, and YTD change."""
    series = series.dropna().sort_index()
    if series.empty:
        return {"latest": float("nan"), "previous_change": float("nan"), "ytd_change": float("nan")}

    latest = float(series.iloc[-1])
    current_year = series[series.index.year == series.index[-1].year]
    if change_mode == "points":
        previous_change = float(latest - series.iloc[-2]) if len(series) > 1 else float("nan")
        ytd_change = float(latest - current_year.iloc[0]) if len(current_year) > 1 else float("nan")
    else:
        previous_change = (
            float(series.pct_change(fill_method=None).iloc[-1] * 100)
            if len(series) > 1
            else float("nan")
        )
        ytd_change = (
            float((latest / current_year.iloc[0] - 1) * 100)
            if len(current_year) > 1 and current_year.iloc[0] != 0
            else float("nan")
        )
    return {
        "latest": latest,
        "previous_change": previous_change,
        "ytd_change": ytd_change,
        "latest_date": series.index[-1],
    }


def fetch_macro_data(start_date: date, end_date: date | None = None) -> tuple[dict[str, pd.Series], dict[str, str]]:
    """Fetch independent FRED indicators with per-series failure isolation."""
    if end_date is None:
        end_date = date.today()
    data: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}

    for indicator in MACRO_INDICATORS:
        try:
            data[indicator.key] = fetch_fred_series(
                indicator.key,
                start_date,
                indicator.transform,
                end_date,
            )
        except Exception as exc:  # Individual failures should not take down the dashboard.
            logger.error(
                "macro_indicator_fetch_failed series_id=%s error=%s",
                indicator.key,
                type(exc).__name__,
            )
            errors[indicator.key] = str(exc)

    ordered_data = {
        indicator.key: data[indicator.key] for indicator in MACRO_INDICATORS if indicator.key in data
    }
    return ordered_data, errors


def frame_to_series(
    frame: pd.DataFrame, indicators: Iterable[Indicator]
) -> tuple[dict[str, pd.Series], dict[str, str]]:
    data: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for indicator in indicators:
        if indicator.key not in frame or frame[indicator.key].dropna().empty:
            errors[indicator.key] = "No data returned."
            continue
        try:
            data[indicator.key] = _validate_series(
                _normalize_series(frame[indicator.key], indicator.key), indicator.key
            )
        except (TypeError, ValueError) as exc:
            logger.error(
                "price_series_validation_failed ticker=%s error=%s",
                indicator.key,
                type(exc).__name__,
            )
            errors[indicator.key] = "The source returned malformed price data."
    return data, errors
