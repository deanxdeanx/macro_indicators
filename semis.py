"""Semiconductor prices, market cap, and trailing P/E from free sources.

Market cap is the latest SEC share count times the Yahoo price. TSM's ADR
represents five ordinary shares, so its cap is shares × price / 5 and its EPS
is scaled by the same ratio. ASML's ADR is one ordinary share. EPS in EUR is
converted with FRED DEXUSEU (dollars per euro). Quarterly diluted EPS is
summed into a trailing twelve months when four consecutive quarters exist.
Filers that only publish annual EPS (TSM's 20-F) use that full-year figure
until the next annual report. Negative or missing EPS leaves a gap in P/E.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
import math
import time

import pandas as pd
import requests

from data_sources import fetch_fred_series, fetch_yahoo_chart_series


logger = logging.getLogger("semis")

SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# SEC fair-access returns 403 unless the User-Agent is a contact-style name
# and email. A noreply address is rejected; this is the form their policy accepts.
SEC_HEADERS = {
    "User-Agent": "Macro Indicators AdminContact@example.com",
    "Accept": "application/json",
}

# Ordinary shares represented by one US-listed share. Verified against the
# latest SEC share count × Yahoo price (TSM without the /5 ratio is about 5×
# too large).
SEMI_UNIVERSE = (
    {"ticker": "NVDA", "name": "NVIDIA", "cik": 1045810, "ordinary_per_share": 1},
    {"ticker": "TSM", "name": "TSMC", "cik": 1046179, "ordinary_per_share": 5},
    {"ticker": "AVGO", "name": "Broadcom", "cik": 1730168, "ordinary_per_share": 1},
    {"ticker": "ASML", "name": "ASML", "cik": 937966, "ordinary_per_share": 1},
    {"ticker": "AMD", "name": "AMD", "cik": 2488, "ordinary_per_share": 1},
    {"ticker": "MU", "name": "Micron", "cik": 723125, "ordinary_per_share": 1},
    {"ticker": "AMAT", "name": "Applied Materials", "cik": 6951, "ordinary_per_share": 1},
    {"ticker": "ARM", "name": "Arm", "cik": 1973239, "ordinary_per_share": 1},
    {"ticker": "LRCX", "name": "Lam Research", "cik": 707549, "ordinary_per_share": 1},
    {"ticker": "INTC", "name": "Intel", "cik": 50863, "ordinary_per_share": 1},
    {"ticker": "TXN", "name": "Texas Instruments", "cik": 97476, "ordinary_per_share": 1},
    {"ticker": "QCOM", "name": "Qualcomm", "cik": 804328, "ordinary_per_share": 1},
    {"ticker": "ADI", "name": "Analog Devices", "cik": 6281, "ordinary_per_share": 1},
    {"ticker": "KLAC", "name": "KLA", "cik": 319201, "ordinary_per_share": 1},
    {"ticker": "MRVL", "name": "Marvell", "cik": 1835632, "ordinary_per_share": 1},
    {"ticker": "NXPI", "name": "NXP", "cik": 1413447, "ordinary_per_share": 1},
)

EPS_CONCEPTS = (
    ("us-gaap", "EarningsPerShareDiluted"),
    ("ifrs-full", "DilutedEarningsLossPerShare"),
    ("ifrs-full", "BasicEarningsLossPerShare"),
)
SHARE_CONCEPTS = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("ifrs-full", "NumberOfSharesOutstanding"),
)


def _span_days(start: str, end: str) -> int | None:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _period_frame(rows: list[dict], min_days: int, max_days: int) -> pd.DataFrame:
    """Latest filing of each period, with the filing date kept for split adjustments."""
    best: dict[str, tuple[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        start, end, value = row.get("start"), row.get("end"), row.get("val")
        if not start or not end or value is None:
            continue
        span = _span_days(str(start), str(end))
        if span is None or not (min_days <= span <= max_days):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        filed = str(row.get("filed") or end)
        current = best.get(str(end))
        if current is None or filed >= current[0]:
            best[str(end)] = (filed, number)
    if not best:
        return pd.DataFrame(columns=["value", "filed"])
    frame = pd.DataFrame(
        {
            "value": [item[1] for item in best.values()],
            "filed": [pd.Timestamp(item[0]) for item in best.values()],
        },
        index=pd.to_datetime(list(best.keys())),
    )
    return frame.sort_index()


def period_series(rows: list[dict], min_days: int, max_days: int) -> pd.Series:
    """One value per period end, keeping the latest filing of that period."""
    frame = _period_frame(rows, min_days, max_days)
    if frame.empty:
        return pd.Series(dtype=float)
    return frame["value"].astype(float)


def trailing_twelve_months(quarterly: pd.Series) -> pd.Series:
    """Sum four quarters only when each step is about one quarter apart."""
    clean = quarterly.dropna().sort_index()
    if len(clean) < 4:
        return pd.Series(dtype=float)
    dates = list(clean.index)
    values = [float(value) for value in clean.tolist()]
    points: dict[pd.Timestamp, float] = {}
    for index in range(3, len(dates)):
        gaps = [(dates[index - offset] - dates[index - offset - 1]).days for offset in range(0, 3)]
        if all(70 <= gap <= 120 for gap in gaps):
            points[dates[index]] = sum(values[index - 3:index + 1])
    return pd.Series(points).sort_index()


def choose_eps_rows(facts: dict) -> tuple[str, str, list[dict]]:
    """Prefer diluted EPS, and USD when the filer reports more than one currency."""
    for taxonomy, concept in EPS_CONCEPTS:
        node = (facts.get(taxonomy) or {}).get(concept) or {}
        units = node.get("units") or {}
        if not isinstance(units, dict) or not units:
            continue
        for unit in ("USD/shares", "EUR/shares", "TWD/shares"):
            rows = units.get(unit)
            if isinstance(rows, list) and rows:
                return concept, unit, rows
        for unit, rows in units.items():
            if str(unit).endswith("/shares") and isinstance(rows, list) and rows:
                return concept, str(unit), rows
    return "", "", []


def latest_shares(facts: dict) -> float | None:
    best_end = ""
    best_value: float | None = None
    for taxonomy, concept in SHARE_CONCEPTS:
        node = (facts.get(taxonomy) or {}).get(concept) or {}
        units = node.get("units") or {}
        rows = units.get("shares") if isinstance(units, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            end = str(row.get("end") or "")
            try:
                value = float(row.get("val"))
            except (TypeError, ValueError):
                continue
            if end >= best_end and value > 0:
                best_end = end
                best_value = value
    return best_value


def earnings_series(rows: list[dict]) -> tuple[pd.Series, str]:
    """Trailing earnings. Quarterly TTM when four consecutive quarters exist."""
    quarterly = trailing_twelve_months(period_series(rows, 60, 120))
    if not quarterly.empty:
        return quarterly, "quarterly TTM diluted EPS"
    annual = period_series(rows, 300, 380)
    if not annual.empty:
        return annual, "annual diluted EPS carried forward; this filer does not report a quarterly EPS series"
    return pd.Series(dtype=float), "no diluted EPS in EDGAR"


def listed_share_earnings(
    rows: list[dict],
    unit: str,
    ordinary_per_share: int,
    fx: pd.Series | None,
    ticker: str,
    timeout: float,
) -> tuple[pd.Series, str]:
    """EPS in dollars per listed share, split-adjusted to today's price and filled to four quarters."""
    quarters = _period_frame(rows, 60, 120)
    annuals = _period_frame(rows, 300, 380)
    def _convert(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        converted = to_usd_per_listed_share(frame["value"], unit, ordinary_per_share, fx)
        filed = frame["filed"].reindex(converted.index)
        return pd.DataFrame({"value": converted.to_numpy(), "filed": filed.to_numpy()}, index=converted.index).dropna(subset=["value"])

    quarters = _convert(quarters)
    annuals = _convert(annuals)
    try:
        factors = fetch_splits(ticker, date.today() - timedelta(days=365 * 12), timeout)
    except requests.RequestException as exc:
        logger.warning("semi_splits_failed ticker=%s error=%s", ticker, type(exc).__name__)
        factors = []
    quarters = split_adjust_frame(quarters, factors)
    annuals = split_adjust_frame(annuals, factors)
    combined = implied_fourth_quarters(quarters, annuals)
    trailing = trailing_twelve_months(combined["value"] if not combined.empty else pd.Series(dtype=float))
    if not trailing.empty:
        return trailing, "quarterly TTM diluted EPS, split-adjusted to the current share count"
    if not annuals.empty:
        return annuals["value"].astype(float), "annual diluted EPS carried forward; this filer does not report a quarterly EPS series"
    return pd.Series(dtype=float), "no diluted EPS in EDGAR"


def splits_from_chart(payload: dict) -> list[tuple[pd.Timestamp, float]]:
    """Split ratios after which historical EPS must be divided to match today's price."""
    result = ((payload or {}).get("chart") or {}).get("result") or []
    events = (result[0].get("events") if result else None) or {}
    raw = events.get("splits") or {}
    factors: list[tuple[pd.Timestamp, float]] = []
    if not isinstance(raw, dict):
        return factors
    for item in raw.values():
        if not isinstance(item, dict):
            continue
        try:
            ratio = float(item["numerator"]) / float(item["denominator"])
            when = pd.to_datetime(int(item["date"]), unit="s", utc=True).tz_convert(None).normalize()
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio > 0 and math.isfinite(ratio):
            factors.append((when, ratio))
    return factors


def _split_divisor(filed: pd.Timestamp, factors: list[tuple[pd.Timestamp, float]]) -> float:
    divisor = 1.0
    for when, ratio in factors:
        if when > filed:
            divisor *= ratio
    return divisor


def split_adjust_frame(frame: pd.DataFrame, factors: list[tuple[pd.Timestamp, float]]) -> pd.DataFrame:
    """Scale EPS by splits that happened after the filing, which is when the number was stated."""
    if frame.empty:
        return frame
    adjusted = frame.copy()
    adjusted["value"] = [
        float(value) / _split_divisor(pd.Timestamp(filed), factors)
        for value, filed in zip(adjusted["value"], adjusted["filed"], strict=True)
    ]
    return adjusted


def split_adjust_earnings(earnings: pd.Series, factors: list[tuple[pd.Timestamp, float]]) -> pd.Series:
    """Divide as-reported EPS by splits that happened after that period."""
    if earnings.empty or not factors:
        return earnings
    frame = pd.DataFrame({"value": earnings.astype(float), "filed": earnings.index})
    return split_adjust_frame(frame, factors)["value"]


def implied_fourth_quarters(quarters: pd.DataFrame, annuals: pd.DataFrame) -> pd.DataFrame:
    """Fill a missing fiscal fourth quarter as full-year EPS minus the other three."""
    if quarters.empty or annuals.empty:
        return quarters
    rows = []
    for end, annual in annuals.iterrows():
        if end in quarters.index:
            continue
        window = quarters[(quarters.index > end - pd.Timedelta(days=370)) & (quarters.index < end)]
        if len(window) < 3:
            continue
        last_three = window.tail(3)
        if (end - last_three.index[0]).days > 320:
            continue
        value = float(annual["value"]) - float(last_three["value"].sum())
        if not math.isfinite(value):
            continue
        rows.append({"end": end, "value": value, "filed": annual["filed"]})
    if not rows:
        return quarters
    extra = pd.DataFrame(rows).set_index("end").sort_index()
    return pd.concat([quarters, extra]).sort_index()


def fetch_splits(ticker: str, start: date, timeout: float) -> list[tuple[pd.Timestamp, float]]:
    period1 = int(pd.Timestamp(start).timestamp())
    period2 = int(pd.Timestamp(date.today() + timedelta(days=1)).timestamp())
    response = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"period1": period1, "period2": period2, "interval": "1mo", "events": "split"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=(10, timeout),
    )
    response.raise_for_status()
    return splits_from_chart(response.json())


def to_usd_per_listed_share(earnings: pd.Series, unit: str, ordinary_per_share: int, fx: pd.Series | None) -> pd.Series:
    """Convert local EPS into dollars per US-listed share or ADR."""
    if earnings.empty:
        return earnings
    converted = earnings
    if unit.startswith("EUR"):
        if fx is None or fx.dropna().empty:
            return pd.Series(dtype=float)
        rate = fx.reindex(converted.index, method="ffill")
        converted = converted * rate
    elif unit.startswith("TWD"):
        if fx is None or fx.dropna().empty:
            return pd.Series(dtype=float)
        rate = fx.reindex(converted.index, method="ffill")
        converted = converted / rate
    elif not unit.startswith("USD"):
        return pd.Series(dtype=float)
    return (converted * float(ordinary_per_share)).dropna()


def market_cap(shares: float, price: float, ordinary_per_share: int) -> float:
    """Listed price is per ADR. Ordinary-share counts are divided by the ADR ratio."""
    return float(shares) * float(price) / float(ordinary_per_share)


def rank_by_market_cap(candidates: list[dict], limit: int = 10) -> list[dict]:
    ranked = [item for item in candidates if item.get("market_cap")]
    ranked.sort(key=lambda item: float(item["market_cap"]), reverse=True)
    return ranked[:limit]


def monthly_price_and_pe(prices: pd.Series, earnings: pd.Series, years: int = 10) -> tuple[list[list], list[list]]:
    """Month-end price and P/E. Non-positive EPS is omitted from the P/E list."""
    prices = prices.dropna().sort_index()
    if prices.empty:
        return [], []
    start = prices.index[-1] - pd.DateOffset(years=years)
    prices = prices[prices.index >= start]
    earnings = earnings.dropna().sort_index()
    price_rows: list[list] = []
    pe_rows: list[list] = []
    for _, month in prices.groupby(prices.index.to_period("M")):
        timestamp = month.index[-1]
        price = float(month.iloc[-1])
        day = pd.Timestamp(timestamp).strftime("%Y-%m-%d")
        price_rows.append([day, round(price, 4)])
        prior = earnings[earnings.index <= timestamp]
        if prior.empty:
            continue
        trailing = float(prior.iloc[-1])
        if trailing <= 0:
            continue
        pe_rows.append([day, round(float(price) / trailing, 4)])
    return price_rows, pe_rows


def pe_summary(price_rows: list[list], pe_rows: list[list]) -> dict[str, float | None]:
    """Current price, current P/E, 10-year median P/E, and the current percentile."""
    price = float(price_rows[-1][1]) if price_rows else None
    current = float(pe_rows[-1][1]) if pe_rows and price_rows and pe_rows[-1][0] == price_rows[-1][0] else None
    history = [float(row[1]) for row in pe_rows]
    if current is None or not history:
        median = None
        percentile = None
    else:
        ordered = sorted(history)
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            median = ordered[midpoint]
        else:
            median = (ordered[midpoint - 1] + ordered[midpoint]) / 2
        percentile = sum(1 for value in history if value <= current) / len(history) * 100
    return {
        "price": None if price is None else round(price, 4),
        "pe": None if current is None else round(current, 4),
        "median_pe": None if median is None else round(median, 4),
        "pe_percentile": None if percentile is None else round(percentile, 1),
    }


def quote_market_cap(candidate: dict, *, timeout: float = 30) -> float:
    """Share count from EDGAR times the latest Yahoo price, adjusted for ADR ratio."""
    facts = fetch_company_facts(candidate["cik"], timeout=timeout)
    shares = latest_shares(facts)
    if not shares:
        raise ValueError(f"EDGAR has no share count for {candidate['ticker']}.")
    start = date.today() - timedelta(days=14)
    prices = fetch_yahoo_chart_series(candidate["ticker"], start, timeout=timeout, attempts=2)
    price = float(prices.dropna().iloc[-1])
    return market_cap(shares, price, candidate["ordinary_per_share"])


def fetch_company_facts(cik: int, timeout: float = 60) -> dict:
    time.sleep(0.15)
    response = requests.get(SEC_FACTS_URL.format(cik=cik), headers=SEC_HEADERS, timeout=(10, timeout))
    response.raise_for_status()
    payload = response.json()
    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, dict):
        raise ValueError(f"EDGAR returned no facts for CIK {cik}.")
    return facts


def _fx_series(unit: str, timeout: float) -> pd.Series | None:
    if unit.startswith("EUR"):
        start = date.today() - timedelta(days=365 * 12)
        return fetch_fred_series("DEXUSEU", start, timeout=timeout, attempts=2)
    if unit.startswith("TWD"):
        start = date.today() - timedelta(days=365 * 12)
        return fetch_fred_series("DEXTAUS", start, timeout=timeout, attempts=2)
    return None


def catalog_semi(document: dict, *, status: str, last_error: str | None, fetched_at: str | None) -> dict:
    return {
        "key": document["key"],
        "ticker": document.get("ticker") or str(document["key"]).removeprefix("semi-"),
        "name": document["name"],
        "source": document.get("source") or "Yahoo Finance + SEC EDGAR",
        "unit": "$",
        "frequency": "Monthly",
        "change_mode": "percent",
        "description": document.get("description") or "",
        "transform": "none",
        "max_age_days": 45,
        "section": "Semiconductors",
        "higher_is_bad": False,
        "visible": True,
        "group": "semis",
        "file": document.get("file") or f"semis/{document['key']}.json",
        "status": status,
        "observation_count": document.get("observation_count") or 0,
        "latest_date": document.get("latest_date"),
        "fetched_at": fetched_at,
        "last_attempt_at": fetched_at,
        "last_error": last_error,
        "market_cap": document.get("market_cap"),
        "price": document.get("price"),
        "pe": document.get("pe"),
        "median_pe": document.get("median_pe"),
        "pe_percentile": document.get("pe_percentile"),
        "eps_basis": document.get("eps_basis") or "",
        "week_change": None,
        "month_change": None,
        "range_percentile": document.get("pe_percentile"),
    }


def build_semi_document(candidate: dict, *, timeout: float = 30) -> dict:
    """One company: monthly price, positive P/E points, and the summary stats."""
    facts = fetch_company_facts(candidate["cik"], timeout=timeout)
    shares = latest_shares(facts)
    concept, unit, rows = choose_eps_rows(facts)
    fx = _fx_series(unit, timeout)
    per_share, basis = listed_share_earnings(
        rows,
        unit,
        candidate["ordinary_per_share"],
        fx,
        candidate["ticker"],
        timeout,
    )
    start = date(date.today().year - 10, 1, 1)
    prices = fetch_yahoo_chart_series(candidate["ticker"], start, timeout=timeout, attempts=2, adjusted=False)
    price_rows, pe_rows = monthly_price_and_pe(prices, per_share)
    if not price_rows:
        raise ValueError(f"No monthly prices for {candidate['ticker']}.")
    summary = pe_summary(price_rows, pe_rows)
    latest_price = float(prices.dropna().iloc[-1])
    cap = None
    if shares and latest_price:
        cap = market_cap(shares, latest_price, candidate["ordinary_per_share"])
    notes = [basis]
    if candidate["ordinary_per_share"] != 1:
        notes.append(
            f"One US share is {candidate['ordinary_per_share']} ordinary shares; EPS and the share count use that ratio."
        )
    if unit.startswith("EUR"):
        notes.append("EPS converted from euros with FRED DEXUSEU.")
    if unit.startswith("TWD"):
        notes.append("EPS converted from Taiwan dollars with FRED DEXTAUS.")
    if summary["pe"] is None:
        notes.append("Current P/E is blank because trailing EPS is negative or missing.")
    return {
        "key": f"semi-{candidate['ticker']}",
        "ticker": candidate["ticker"],
        "name": candidate["name"],
        "cik": candidate["cik"],
        "source": "Yahoo Finance + SEC EDGAR",
        "unit": "$",
        "pe_unit": "x",
        "frequency": "Monthly",
        "group": "semis",
        "file": f"semis/{candidate['ticker']}.json",
        "visible": True,
        "market_cap": None if cap is None else round(cap, 2),
        "shares": shares,
        "ordinary_per_share": candidate["ordinary_per_share"],
        "eps_concept": concept,
        "eps_unit": unit,
        "eps_basis": basis,
        "description": " ".join(notes),
        "price": summary["price"],
        "pe": summary["pe"],
        "median_pe": summary["median_pe"],
        "pe_percentile": summary["pe_percentile"],
        "latest_date": price_rows[-1][0],
        "observations": price_rows,
        "pe_observations": pe_rows,
        "observation_count": len(price_rows),
    }
