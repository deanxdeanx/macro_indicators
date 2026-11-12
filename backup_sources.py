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
TREASURY_REAL_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_real_yield_curve"
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
H41_SOURCE = "Federal Reserve H.4.1"
ECB_SOURCE = "ECB"
MOF_SOURCE = "Japan MOF"
# Wednesday total assets, Wednesday reserve balances, and the week-average
# Treasury general account. Values are millions of dollars.
H41_SERIES = {
    "WALCL": "RESPPA_N.WW",
    "WRESBAL": "RESH4R_N.WW",
    "WTREGEN": "RESPPLLDT_XAW_N.WW",
}
H41_PREVIEW_URL = (
    "https://www.federalreserve.gov/datadownload/Preview.aspx"
    "?pi=400&preview=H41/H41/{code}&rel=H41"
)
DTS_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
    "v1/accounting/dts/operating_cash_balance"
)
NYFED_RRP_URL = "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json"
ECB_AAA_10Y_URL = (
    "https://data-api.ecb.europa.eu/service/data/"
    "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y?format=csvdata"
)
JGB_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
MULTPL_EARNINGS_URL = "https://www.multpl.com/s-p-500-earnings/table/by-month"
# A quarterly print plus the time it takes to reach the index aggregate.
EPS_MAX_AGE_DAYS = 120

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


def yield_spread(longer: pd.Series, shorter: pd.Series, name: str) -> pd.Series:
    """Subtract two yield series on their common dates, in percentage points."""
    frame = pd.concat({"long": longer, "short": shorter}, axis=1).dropna(how="any")
    spread = (frame["long"] - frame["short"]).dropna()
    spread.name = name
    return spread


def parse_treasury_real_yield_csv(text: str) -> pd.Series:
    """10-year real par yield from the Treasury real-yield CSV. Column is ``10 YR``."""
    frame = pd.read_csv(StringIO(text))
    if "Date" not in frame.columns or "10 YR" not in frame.columns:
        raise ValueError("Treasury real-yield CSV has no 10-year column.")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    values = pd.to_numeric(frame["10 YR"], errors="coerce")
    series = pd.Series(values.to_numpy(), index=frame["Date"], name="DFII10").dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    if series.empty:
        raise ValueError("Treasury real-yield CSV had no 10-year observations.")
    return series


def parse_h41_preview(html: str, name: str) -> pd.Series:
    """Read the date/value rows from an H.4.1 data-download preview page.

    The preview publishes the latest levels in millions of dollars. A full
    history stays in the previous file; these rows refresh the newest prints.
    """
    cells = re.findall(r"<td>\s*([^<]+?)\s*</td>", html, flags=re.S)
    dates: list[str] = []
    values: list[float] = []
    for index in range(0, len(cells) - 2, 3):
        stamp = cells[index + 1].strip()
        raw = cells[index + 2].strip().replace(",", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
        dates.append(stamp)
    series = _series(values, dates, name)
    if series.empty:
        raise ValueError(f"H.4.1 preview had no observations for {name}.")
    return series


def _dts_balance(row: dict) -> float | None:
    """Closing TGA balance in millions. The API currently leaves close_today_bal as the text null."""
    for field in ("close_today_bal", "open_today_bal"):
        raw = row.get(field)
        if raw is None or str(raw).strip().lower() in {"", "null", "none"}:
            continue
        try:
            return float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def parse_dts_tga(payload: dict) -> pd.Series:
    """Daily Treasury general account closing balance, in billions of dollars."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Treasury DTS returned no cash-balance rows.")
    dates: list[str] = []
    values: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        millions = _dts_balance(row)
        raw_date = str(row.get("record_date") or "")[:10]
        if millions is None or not raw_date:
            continue
        dates.append(raw_date)
        values.append(millions / 1000.0)
    series = _series(values, dates, "WTREGEN")
    if series.empty:
        raise ValueError("Treasury DTS returned no numeric TGA balance.")
    return series


def parse_nyfed_rrp(payload: dict) -> pd.Series:
    """Overnight reverse repo accepted, in billions of dollars."""
    operations = None
    if isinstance(payload, dict):
        operations = (payload.get("repo") or {}).get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("New York Fed returned no reverse-repo operations.")
    dates: list[str] = []
    values: list[float] = []
    for row in operations:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("operationDate") or "")[:10]
        raw_amount = row.get("totalAmtAccepted")
        if not raw_date or raw_amount is None:
            continue
        try:
            values.append(float(raw_amount) / 1_000_000_000.0)
        except (TypeError, ValueError):
            continue
        dates.append(raw_date)
    series = _series(values, dates, "RRPONTSYD")
    if series.empty:
        raise ValueError("New York Fed returned no reverse-repo amounts.")
    return repair_rrp_outliers(series)


def repair_rrp_outliers(series: pd.Series) -> pd.Series:
    """Fill a one-day print that collapses toward zero between two large operations.

    The New York Fed feed occasionally records a near-zero amount on a day when
    the surrounding operations are still in the trillions. Leaving it in draws
    a spike through net liquidity.
    """
    clean = series.dropna().sort_index().astype(float).copy()
    if len(clean) < 3:
        return clean
    values = clean.to_numpy(dtype=float)
    for index in range(1, len(values) - 1):
        left = values[index - 1]
        right = values[index + 1]
        if min(left, right) > 100 and values[index] < 0.05 * min(left, right):
            values[index] = float("nan")
    clean.iloc[:] = values
    return clean.interpolate(method="time").ffill().bfill()


def parse_ecb_csv(text: str, name: str) -> pd.Series:
    """One ECB SDMX CSV. Uses TIME_PERIOD and OBS_VALUE."""
    frame = pd.read_csv(StringIO(text))
    if "TIME_PERIOD" not in frame.columns or "OBS_VALUE" not in frame.columns:
        raise ValueError("ECB CSV has no time period or observation.")
    frame["TIME_PERIOD"] = pd.to_datetime(frame["TIME_PERIOD"], errors="coerce")
    values = pd.to_numeric(frame["OBS_VALUE"], errors="coerce")
    series = pd.Series(values.to_numpy(), index=frame["TIME_PERIOD"], name=name).dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    if series.empty:
        raise ValueError("ECB CSV had no numeric observations.")
    return series


def parse_jgb_csv(text: str) -> pd.Series:
    """10-year Japanese government bond yield from the Ministry of Finance CSV."""
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.startswith("Date,"):
            start = index
            break
    frame = pd.read_csv(StringIO("\n".join(lines[start:])))
    if "Date" not in frame.columns or "10Y" not in frame.columns:
        raise ValueError("JGB CSV has no 10-year column.")
    frame["Date"] = pd.to_datetime(frame["Date"], format="%Y/%m/%d", errors="coerce")
    raw = frame["10Y"].replace({"-": None, "—": None})
    values = pd.to_numeric(raw, errors="coerce")
    series = pd.Series(values.to_numpy(), index=frame["Date"], name="JGB10Y").dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    if series.empty:
        raise ValueError("JGB CSV had no 10-year observations.")
    return series


def parse_multpl_earnings(html: str) -> tuple[pd.Series, pd.Series]:
    """Trailing and forward S&P 500 earnings per share, in index points.

    Estimate rows are kept apart so the daily yield can say which one it used.
    """
    trailing_dates: list[str] = []
    trailing_values: list[float] = []
    forward_dates: list[str] = []
    forward_values: list[float] = []
    for match in MULTPL_ROW.finditer(html):
        cell = match.group(2)
        number = unescape(re.sub(r"<[^>]+>", "", cell)).replace(",", "").replace("%", "")
        found = re.search(r"-?\d+(?:\.\d+)?", number)
        if not found:
            continue
        try:
            value = float(found.group(0))
            stamp = pd.Timestamp(match.group(1))
        except (TypeError, ValueError):
            continue
        iso = stamp.strftime("%Y-%m-%d")
        if "Estimate" in cell:
            forward_dates.append(iso)
            forward_values.append(value)
        else:
            trailing_dates.append(iso)
            trailing_values.append(value)
    trailing = _series(trailing_values, trailing_dates, "SPEARN_EPS")
    forward = _series(forward_values, forward_dates, "SPEARN_EPS_FWD")
    if trailing.empty and forward.empty:
        raise ValueError("Multpl did not include S&P 500 earnings.")
    return trailing, forward


def choose_eps(
    trailing: pd.Series,
    forward: pd.Series,
    as_of: date,
    max_age_days: int = EPS_MAX_AGE_DAYS,
) -> tuple[pd.Series, str, pd.Timestamp]:
    """Prefer trailing EPS. Use a newer forward print only when trailing is old."""
    trailing_clean = trailing.dropna().sort_index() if trailing is not None else pd.Series(dtype=float)
    forward_clean = forward.dropna().sort_index() if forward is not None else pd.Series(dtype=float)
    if trailing_clean.empty and forward_clean.empty:
        raise ValueError("No S&P 500 earnings are available.")
    if trailing_clean.empty:
        return forward_clean, "forward", forward_clean.index[-1]
    age_days = (pd.Timestamp(as_of).normalize() - trailing_clean.index[-1].normalize()).days
    newer_forward = (
        not forward_clean.empty
        and forward_clean.index[-1] > trailing_clean.index[-1]
        and age_days > max_age_days
    )
    if newer_forward:
        return forward_clean, "forward", forward_clean.index[-1]
    return trailing_clean, "trailing", trailing_clean.index[-1]


def daily_earnings_yield(price: pd.Series, eps: pd.Series) -> pd.Series:
    """Carry the latest known EPS onto each price date. Yield is a percent of price."""
    prices = price.dropna().sort_index()
    carried = eps.dropna().sort_index().reindex(prices.index, method="ffill")
    yield_pct = (carried / prices.replace(0, pd.NA) * 100).dropna()
    yield_pct.name = "SPEARN"
    return yield_pct


def eps_is_old(eps_date: pd.Timestamp | date, as_of: date, max_age_days: int = EPS_MAX_AGE_DAYS) -> bool:
    latest = pd.Timestamp(eps_date).date()
    return (as_of - latest).days > max_age_days


def _window(series: pd.Series, start: date, end: date) -> pd.Series:
    kept = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
    return kept.sort_index()


def fetch_treasury_real_yield(start: date, end: date, timeout: float) -> pd.Series:
    parts: list[pd.Series] = []
    for year in range(start.year, end.year + 1):
        response = requests.get(
            TREASURY_REAL_URL.format(year=year),
            headers=TREASURY_HEADERS,
            timeout=(10, timeout),
        )
        response.raise_for_status()
        parts.append(parse_treasury_real_yield_csv(response.text))
    series = pd.concat(parts).sort_index()
    series = series[~series.index.duplicated(keep="last")]
    series = _window(series, start, end)
    if series.empty:
        raise ValueError("Treasury published no real yields in the requested window.")
    series.name = "DFII10"
    return series


def fetch_h41_series(key: str, timeout: float) -> pd.Series:
    """Latest H.4.1 levels for one balance-sheet line, in millions of dollars."""
    code = H41_SERIES[key]
    response = requests.get(
        H41_PREVIEW_URL.format(code=code),
        headers=TREASURY_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    series = parse_h41_preview(response.text, key)
    return series


def fetch_dts_tga(start: date, end: date, timeout: float) -> pd.Series:
    response = requests.get(
        DTS_URL,
        params={
            "filter": "account_type:eq:Treasury General Account (TGA) Closing Balance",
            "fields": "record_date,close_today_bal,open_today_bal",
            "sort": "record_date",
            "page[size]": 10000,
        },
        headers=JSON_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    series = _window(parse_dts_tga(response.json()), start, end)
    if series.empty:
        raise ValueError("Treasury DTS had no TGA balance in the requested window.")
    return series


def fetch_nyfed_rrp(start: date, end: date, timeout: float) -> pd.Series:
    response = requests.get(
        NYFED_RRP_URL,
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        headers=JSON_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    series = _window(parse_nyfed_rrp(response.json()), start, end)
    if series.empty:
        raise ValueError("New York Fed had no reverse repo in the requested window.")
    return series


def fetch_ecb_aaa_yield(start: date, end: date, timeout: float) -> pd.Series:
    response = requests.get(
        ECB_AAA_10Y_URL,
        params={"startPeriod": start.isoformat()},
        headers={**JSON_HEADERS, "Accept": "text/csv"},
        timeout=(10, timeout),
    )
    response.raise_for_status()
    series = _window(parse_ecb_csv(response.text, "EUR10Y"), start, end)
    if series.empty:
        raise ValueError("ECB returned no AAA 10-year yield in the requested window.")
    return series


def fetch_jgb_yield(start: date, end: date, timeout: float) -> pd.Series:
    response = requests.get(JGB_URL, headers=TREASURY_HEADERS, timeout=(10, max(timeout, 30)))
    response.raise_for_status()
    series = _window(parse_jgb_csv(response.text), start, end)
    if series.empty:
        raise ValueError("Japan MOF returned no 10-year yield in the requested window.")
    return series


def fetch_spx_earnings(timeout: float) -> tuple[pd.Series, pd.Series]:
    response = requests.get(
        MULTPL_EARNINGS_URL,
        headers=TREASURY_HEADERS,
        timeout=(10, timeout),
    )
    response.raise_for_status()
    return parse_multpl_earnings(response.text)
