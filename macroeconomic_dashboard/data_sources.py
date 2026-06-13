"""Data fetching and transformation helpers for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO
from typing import Iterable
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st
import yfinance as yf


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


@dataclass(frozen=True)
class Indicator:
    key: str
    name: str
    source: str
    unit: str
    frequency: str
    transform: str = "none"


MACRO_INDICATORS = (
    Indicator("UNRATE", "US Unemployment Rate", "FRED", "%", "Monthly"),
    Indicator("CPIAUCSL", "US Inflation (CPI, YoY)", "FRED", "%", "Monthly", "yoy"),
    Indicator("A191RL1Q225SBEA", "US Real GDP Growth Rate", "FRED", "%", "Quarterly"),
    Indicator("M2SL", "US M2 Money Supply", "FRED", "$ billions", "Monthly"),
)

MARKET_INDICATORS = (
    Indicator("^GSPC", "S&P 500 Index", "Yahoo Finance", "points", "Daily"),
    Indicator("^IXIC", "Nasdaq Composite Index", "Yahoo Finance", "points", "Daily"),
    Indicator("BTC-USD", "Bitcoin Price (USD)", "Yahoo Finance", "$", "Daily"),
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


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def fetch_fred_series(series_id: str, start_date: date, transform: str = "none") -> pd.Series:
    """Fetch one FRED series through the public CSV endpoint."""
    params = {
        "id": series_id,
        "cosd": start_date.isoformat(),
        "coed": date.today().isoformat(),
    }
    response = requests.get(f"{FRED_CSV_URL}?{urlencode(params)}", timeout=30)
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text), index_col="observation_date")
    series = _normalize_series(frame[series_id], series_id)
    if transform == "yoy":
        series = series.pct_change(periods=12, fill_method=None).mul(100).dropna()
    return series


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


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def fetch_yahoo_prices(tickers: tuple[str, ...], start_date: date) -> pd.DataFrame:
    """Fetch adjusted historical close prices from Yahoo Finance in one batch."""
    download = yf.download(
        list(tickers),
        start=start_date.isoformat(),
        end=(date.today() + pd.Timedelta(days=1)).isoformat(),
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=True,
    )
    return _extract_close_prices(download, list(tickers))


def latest_metrics(series: pd.Series) -> dict[str, float | pd.Timestamp]:
    """Calculate latest value, previous-period change, and YTD change."""
    series = series.dropna().sort_index()
    if series.empty:
        return {"latest": float("nan"), "previous_change": float("nan"), "ytd_change": float("nan")}

    latest = float(series.iloc[-1])
    previous_change = float(series.pct_change(fill_method=None).iloc[-1] * 100) if len(series) > 1 else float("nan")

    current_year = series[series.index.year == series.index[-1].year]
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


def fetch_macro_data(start_date: date) -> tuple[dict[str, pd.Series], dict[str, str]]:
    data: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for indicator in MACRO_INDICATORS:
        try:
            data[indicator.key] = fetch_fred_series(indicator.key, start_date, indicator.transform)
        except Exception as exc:  # Individual failures should not take down the dashboard.
            errors[indicator.key] = str(exc)
    return data, errors


def frame_to_series(
    frame: pd.DataFrame, indicators: Iterable[Indicator]
) -> tuple[dict[str, pd.Series], dict[str, str]]:
    data: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for indicator in indicators:
        if indicator.key not in frame or frame[indicator.key].dropna().empty:
            errors[indicator.key] = "No data returned."
            continue
        data[indicator.key] = _normalize_series(frame[indicator.key], indicator.key)
    return data, errors
