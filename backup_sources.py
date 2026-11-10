"""Free backups and public series used when FRED is down, or that FRED does not publish.

FRED stays the primary source. These parsers are pure so tests do not need the network.
"""

from __future__ import annotations

from datetime import date
from html import unescape
from io import StringIO
import re

import pandas as pd
import requests


TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
# The Treasury site rejects the short client name and serves the CSV to a browser agent.
TREASURY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
}
TREASURY_COLUMNS = {
    "DGS3MO": "3 Mo",
    "DGS2": "2 Yr",
    "DGS10": "10 Yr",
}

NYFED_URLS = {
    "SOFR": "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json",
    "DFF": "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json",
}
NYFED_SOURCE = "New York Fed"
TREASURY_SOURCE = "US Treasury"

CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
CFTC_MARKETS = (
    ("043602", "10Y T-note"),
    ("13874A", "E-mini S&P 500"),
    ("098662", "US Dollar Index"),
)

MULTPL_EARNINGS_YIELD_URL = "https://www.multpl.com/s-p-500-earnings-yield/table/by-month"
MULTPL_ROW = re.compile(
    r"<td>\s*([A-Z][a-z]{2,8} \d{1,2}, \d{4})\s*</td>\s*<td>(.*?)</td>",
    re.S,
)

JSON_HEADERS = {"User-Agent": "macro_indicators/1.0", "Accept": "application/json"}


def _series(values: list[float], dates: list[str], name: str) -> pd.Series:
    series = pd.Series(values, index=pd.to_datetime(dates), name=name)
    series = pd.to_numeric(series, errors="coerce").dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def parse_treasury_yield_csv(text: str) -> dict[str, pd.Series]:
    """Map a Treasury par-yield CSV into DGS3MO, DGS2, and DGS10."""
    frame = pd.read_csv(StringIO(text))
    if "Date" not in frame.columns:
        raise ValueError("Treasury CSV has no Date column.")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    parsed: dict[str, pd.Series] = {}
    for key, column in TREASURY_COLUMNS.items():
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        series = pd.Series(values.to_numpy(), index=frame["Date"], name=key).dropna()
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if not series.empty:
            parsed[key] = series
    if not parsed:
        raise ValueError("Treasury CSV did not include 3-month, 2-year, or 10-year yields.")
    return parsed


def parse_nyfed_rates(payload: dict, series_id: str) -> pd.Series:
    """Read percentRate rows from a New York Fed reference-rate payload."""
    rows = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"New York Fed returned no rates for {series_id}.")
    dates: list[str] = []
    values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = row.get("effectiveDate")
        raw_rate = row.get("percentRate")
        if raw_date is None or raw_rate is None:
            continue
        try:
            values.append(float(raw_rate))
        except (TypeError, ValueError):
            continue
        dates.append(str(raw_date)[:10])
    series = _series(values, dates, series_id)
    if series.empty:
        raise ValueError(f"New York Fed returned no numeric rates for {series_id}.")
    return series


def speculative_net(row: dict) -> float:
    """Leveraged-money long minus short, the TFF report's speculative net."""
    return float(row["lev_money_positions_long"]) - float(row["lev_money_positions_short"])


def cftc_history(rows: list[dict]) -> pd.Series:
    points: dict[pd.Timestamp, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not raw_date:
            continue
        try:
            net = speculative_net(row)
        except (KeyError, TypeError, ValueError):
            continue
        points[pd.Timestamp(raw_date)] = net
    if not points:
        raise ValueError("CFTC rows had no speculative positions.")
    series = pd.Series(points).sort_index()
    return series[~series.index.duplicated(keep="last")]


def position_percentile(history: pd.Series, years: int = 3) -> float | None:
    clean = history.dropna().sort_index()
    if clean.empty:
        return None
    start = clean.index[-1] - pd.DateOffset(years=years)
    window = clean[clean.index >= start]
    if window.empty:
        window = clean
    latest = float(window.iloc[-1])
    return round(float((window <= latest).mean() * 100), 1)


def parse_multpl_earnings_yield(html: str) -> pd.Series:
    """Monthly S&P 500 earnings yield from Multpl. Estimate rows are left out."""
    dates: list[str] = []
    values: list[float] = []
    for match in MULTPL_ROW.finditer(html):
        cell = match.group(2)
        if "Estimate" in cell:
            continue
        number = unescape(re.sub(r"<[^>]+>", "", cell))
        number = number.replace("%", "").replace(",", "").strip()
        try:
            value = float(number)
            stamp = pd.Timestamp(match.group(1))
        except (TypeError, ValueError):
            continue
        dates.append(stamp.strftime("%Y-%m-%d"))
        values.append(value)
    series = _series(values, dates, "SPEARN")
    if series.empty:
        raise ValueError("Multpl did not include a reported earnings yield.")
    return series


def price_ratio(numerator: pd.Series, denominator: pd.Series, name: str) -> pd.Series:
    frame = pd.concat({"num": numerator, "den": denominator}, axis=1).dropna(how="any")
    frame = frame[frame["den"] != 0]
    ratio = (frame["num"] / frame["den"]).dropna()
    ratio.name = name
    return ratio


def earnings_yield_minus_real(earnings_yield: pd.Series, real_yield: pd.Series) -> pd.Series:
    """Trailing earnings yield minus the 10-year real yield on or before that month."""
    real = real_yield.dropna().sort_index()
    aligned = real.reindex(earnings_yield.dropna().sort_index().index, method="ffill")
    spread = (earnings_yield.dropna().sort_index() - aligned).dropna()
    spread.name = "SPEARN10"
    return spread


def fetch_treasury_yields(start: date, end: date, timeout: float) -> dict[str, pd.Series]:
    frames: list[pd.Series] = []
    parsed: dict[str, list[pd.Series]] = {key: [] for key in TREASURY_COLUMNS}
    for year in range(start.year, end.year + 1):
        response = requests.get(
            TREASURY_URL.format(year=year),
            headers=TREASURY_HEADERS,
            timeout=(10, timeout),
        )
        response.raise_for_status()
        year_book = parse_treasury_yield_csv(response.text)
        for key, series in year_book.items():
            parsed[key].append(series)
        frames.append(pd.Series(dtype=float))
    del frames
    combined: dict[str, pd.Series] = {}
    for key, parts in parsed.items():
        if not parts:
            continue
        series = pd.concat(parts).sort_index()
        series = series[~series.index.duplicated(keep="last")]
        series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
        if not series.empty:
            combined[key] = series
    if not combined:
        raise ValueError("Treasury published no yields in the requested window.")
    return combined


def fetch_nyfed_rate(series_id: str, start: date, end: date, timeout: float) -> pd.Series:
    response = requests.get(
        NYFED_URLS[series_id],
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        headers=JSON_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    series = parse_nyfed_rates(response.json(), series_id)
    return series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]


def fetch_cftc_positions(start: date, timeout: float) -> list[dict]:
    """Three years of leveraged-money nets for the 10-year, E-mini S&P, and dollar index."""
    markets = []
    for code, name in CFTC_MARKETS:
        response = requests.get(
            CFTC_URL,
            params={
                "$limit": 200,
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$where": f"cftc_contract_market_code='{code}'",
            },
            headers=JSON_HEADERS,
            timeout=(10, timeout),
        )
        response.raise_for_status()
        history = cftc_history(response.json())
        history = history[history.index >= pd.Timestamp(start)]
        if history.empty:
            raise ValueError(f"CFTC returned no positions for {name}.")
        markets.append(
            {
                "code": code,
                "name": name,
                "latest": round(float(history.iloc[-1]), 1),
                "as_of": history.index[-1].strftime("%Y-%m-%d"),
                "percentile": position_percentile(history),
                "observations": [
                    [stamp.strftime("%Y-%m-%d"), round(float(value), 1)]
                    for stamp, value in history.sort_index().items()
                ],
            }
        )
    return markets


def fetch_earnings_yield(timeout: float) -> pd.Series:
    response = requests.get(
        MULTPL_EARNINGS_YIELD_URL,
        headers=TREASURY_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    return parse_multpl_earnings_yield(response.text)
