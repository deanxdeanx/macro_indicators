"""Data fetching and transformation helpers for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import logging
import math
import time
from typing import Iterable
from urllib.parse import quote, urlencode

import pandas as pd
import requests


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
YAHOO_CHART_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)
YAHOO_CHART_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
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


def observation_window(today: date | None = None) -> tuple[date, date, date]:
    """Return ``(history_start, default_view_start, today)``.

    The fetch keeps six calendar years. The default chart window is the most
    recent five, which leaves the earlier year available for year-over-year
    inflation once the level series is transformed.
    """
    today = today or date.today()
    return date(today.year - 6, 1, 1), date(today.year - 5, 1, 1), today


def company_indicators() -> tuple[Indicator, ...]:
    """Indicators for the representative large-cap basket, in display order."""
    return tuple(
        Indicator(ticker, f"{name} ({ticker})", "Yahoo Finance", "$", "Daily")
        for ticker, name in TOP_COMPANY_TICKERS.items()
    )


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


def fetch_fred_series(
    series_id: str,
    start_date: date,
    transform: str = "none",
    end_date: date | None = None,
    *,
    timeout: float = 60,
    attempts: int = MAX_FETCH_ATTEMPTS,
    retry_backoff: float = 1.0,
) -> pd.Series:
    """Fetch one FRED series through the public CSV endpoint."""
    if end_date is None:
        end_date = date.today()
    params = {
        "id": series_id,
        "cosd": start_date.isoformat(),
        "coed": end_date.isoformat(),
    }
    url = f"{FRED_CSV_URL}?{urlencode(params)}"
    request_timeout = (min(5.0, timeout), timeout)
    total_attempts = max(1, attempts)
    for attempt in range(total_attempts):
        try:
            response = requests.get(
                url,
                timeout=request_timeout,
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
            if attempt == total_attempts - 1:
                raise DataSourceError(_request_error_message("FRED", series_id, exc)) from exc
            time.sleep(retry_backoff * (2**attempt))

    try:
        frame = pd.read_csv(StringIO(response.text))
    except pd.errors.ParserError as exc:
        raise DataSourceError(f"FRED returned unreadable CSV data for {series_id}.") from exc
    frame = validate_fred_response(frame, series_id)
    series = _normalize_series(frame.set_index("observation_date")[series_id], series_id)
    if transform == "yoy":
        series = series.pct_change(periods=12, fill_method=None).mul(100).dropna()
    return _validate_series(series, series_id)


def _has_finite_number(values: Iterable[object] | None) -> bool:
    if not values:
        return False
    for value in values:
        if value is None:
            continue
        try:
            if math.isfinite(float(value)):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _yahoo_close_values(indicators: dict) -> list | None:
    """Prefer adjusted closes, matching the adjusted prices the dashboard charts."""
    adj_groups = indicators.get("adjclose") or []
    quote_groups = indicators.get("quote") or []
    adjusted = adj_groups[0].get("adjclose") if adj_groups and isinstance(adj_groups[0], dict) else None
    closes = quote_groups[0].get("close") if quote_groups and isinstance(quote_groups[0], dict) else None
    if _has_finite_number(adjusted):
        return list(adjusted)
    if closes is not None:
        return list(closes)
    return None


def parse_yahoo_chart(payload: dict, ticker: str) -> pd.Series:
    """Convert one Yahoo v8 chart payload into a daily adjusted-close series."""
    if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
        raise DataSourceError(f"Yahoo Finance returned an invalid response for {ticker}.")
    chart = payload["chart"]
    error = chart.get("error")
    if error:
        if isinstance(error, dict):
            description = error.get("description") or error.get("code") or "request failed"
        else:
            description = str(error)
        lowered = str(description).lower()
        if "rate limit" in lowered or "too many" in lowered:
            raise DataSourceError(
                f"Yahoo Finance rate-limited the request for {ticker}. Try again later."
            )
        raise DataSourceError(f"Yahoo Finance rejected the request for {ticker} ({description}).")

    results = chart.get("result") or []
    if not results or not isinstance(results[0], dict):
        raise DataSourceError(f"Yahoo Finance returned no price data for {ticker}.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") if isinstance(result.get("indicators"), dict) else {}
    closes = _yahoo_close_values(indicators)
    if not timestamps or closes is None or len(closes) != len(timestamps):
        raise DataSourceError(f"Yahoo Finance returned no price data for {ticker}.")

    index = pd.to_datetime(list(timestamps), unit="s", utc=True).tz_convert(None).normalize()
    series = _normalize_series(pd.Series(list(closes), index=index), ticker)
    try:
        return _validate_series(series, ticker)
    except ValueError as exc:
        raise DataSourceError(str(exc)) from exc


def _yahoo_crumb(session: requests.Session, timeout: tuple[float, float]) -> str | None:
    """Best-effort crumb for hosts that refuse anonymous chart requests."""
    try:
        session.get("https://fc.yahoo.com", timeout=timeout)
        response = session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    crumb = response.text.strip()
    if response.ok and crumb and not crumb.startswith("{") and len(crumb) < 64:
        return crumb
    return None


def fetch_yahoo_chart_series(
    ticker: str,
    start_date: date,
    end_date: date | None = None,
    *,
    timeout: float = 12,
    attempts: int = 2,
    retry_backoff: float = 0.4,
) -> pd.Series:
    """Fetch one Yahoo series from the chart API with a hard timeout.

    Each ticker is independent, so one rate limit or timeout cannot stall the
    other symbols the way a single batched client call can.
    """
    if end_date is None:
        end_date = date.today()
    period1 = int(datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period2 = int(
        datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp()
    )
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "includeAdjustedClose": "true",
    }
    request_timeout = (min(5.0, timeout), timeout)
    session = requests.Session()
    session.headers.update(YAHOO_CHART_HEADERS)
    crumb: str | None = None
    last_error: Exception | None = None
    total_attempts = max(1, attempts)
    encoded = quote(ticker, safe="")

    for attempt in range(total_attempts):
        for host in YAHOO_CHART_HOSTS:
            url = f"{host}/v8/finance/chart/{encoded}"
            try:
                response = session.get(url, params=params, timeout=request_timeout)
                if response.status_code in (401, 403) and crumb is None:
                    crumb = _yahoo_crumb(session, request_timeout)
                    if crumb:
                        response = session.get(
                            url,
                            params={**params, "crumb": crumb},
                            timeout=request_timeout,
                        )
                if response.status_code == 429:
                    raise requests.HTTPError(response=response)
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise DataSourceError(
                        f"Yahoo Finance returned unreadable data for {ticker}."
                    ) from exc
                return parse_yahoo_chart(payload, ticker)
            except (DataSourceError, requests.RequestException) as exc:
                last_error = exc
                logger.warning(
                    "yahoo_chart_request_failed ticker=%s attempt=%s host=%s error=%s",
                    ticker,
                    attempt + 1,
                    host,
                    type(exc).__name__,
                )
        if attempt == total_attempts - 1:
            break
        time.sleep(retry_backoff * (2**attempt))

    if isinstance(last_error, DataSourceError):
        raise last_error
    if isinstance(last_error, requests.RequestException):
        raise DataSourceError(_request_error_message("Yahoo Finance", ticker, last_error)) from last_error
    raise DataSourceError(f"Yahoo Finance returned no price data for {ticker}.")


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
