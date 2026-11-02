"""Build the Analysis tab snapshot for configured tickers.

Prices come from Yahoo's chart API. Fundamentals come from SEC EDGAR.
Forward estimates stay null unless FMP_API_KEY is set and that call returns
them. Nothing here fills a missing field with a guess.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
import logging
import os
from pathlib import Path
import re

import pandas as pd
import requests

from data_sources import fetch_yahoo_chart_series


logger = logging.getLogger("build_analysis")

SEC_HEADERS = {
    "User-Agent": "MacroTerminal research dean@example.com",
    "Accept": "application/json,text/html",
}
PROMPT_PATH = Path(__file__).resolve().parent / "analysis" / "default_prompt.md"

ANALYSIS_TICKERS = (
    {
        "symbol": "SPCX",
        "name": "SpaceX",
        "class_name": "Class A",
        "exchange": "Nasdaq",
        "cik": "1181412",
        "history_start": "2026-06-12",
        "ipo_date": "2026-06-12",
        "ipo_price": 135.0,
    },
)

SHARES_RE = re.compile(
    r"As of\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4}),\s+the registrant had\s+([\d,]+)\s+"
    r"shares of Class A common stock and\s+([\d,]+)\s+shares of Class B",
    re.IGNORECASE,
)
CEO_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}),\s+our founder,\s+Chief Executive Officer"
)
IPO_PRICE_RE = re.compile(
    r"initial public offering price of\s+\$([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
OVERVIEW_RE = re.compile(r"Founded in 2002, SpaceX\b.{80,900}?(?:Earth\.|services\.)", re.DOTALL)


def field(value, source: str, **extra):
    payload = {"value": value, "source": source}
    payload.update(extra)
    return payload


def _cik(cik: str) -> str:
    return str(cik).zfill(10)


def _parse_day_count(fact: dict) -> int | None:
    start, end = fact.get("start"), fact.get("end")
    if not start or not end:
        return None
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def quarterly_facts(facts: list[dict]) -> list[dict]:
    """Keep one ~quarter-length fact per period end. Prefer the framed fact."""
    chosen: dict[str, dict] = {}
    for fact in facts:
        days = _parse_day_count(fact)
        if days is None or not 80 <= days <= 100:
            continue
        end = fact.get("end")
        if not end:
            continue
        current = chosen.get(end)
        if current is None or (fact.get("frame") and not current.get("frame")):
            chosen[end] = fact
    return [chosen[key] for key in sorted(chosen)]


def trailing_four_quarters(facts: list[dict]) -> list[dict] | None:
    """Return the latest four quarters only when they sit back to back."""
    quarters = quarterly_facts(facts)
    if len(quarters) < 4:
        return None
    last = quarters[-4:]
    ends = [date.fromisoformat(row["end"]) for row in last]
    for previous, current in zip(ends, ends[1:]):
        gap = (current - previous).days
        if not 80 <= gap <= 100:
            return None
    return last


def earnings_releases(submissions: dict) -> list[dict]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    items = recent.get("items") or []
    filed = recent.get("filingDate") or []
    periods = recent.get("reportDate") or []
    events = []
    for index, form in enumerate(forms):
        if form != "8-K":
            continue
        item_text = items[index] if index < len(items) else ""
        parts = [part.strip() for part in str(item_text).split(",")]
        if "2.02" not in parts:
            continue
        events.append(
            {
                "date": filed[index],
                "period_end": periods[index] or None,
                "label": "Earnings release (8-K item 2.02)",
                "confirmed": True,
            }
        )
    return sorted(events, key=lambda event: event["date"])


def prices_from_series(series: pd.Series, history_start: str) -> list[list]:
    cutoff = pd.Timestamp(history_start)
    rows = []
    clean = series.dropna().sort_index()
    for timestamp, value in clean.items():
        if pd.Timestamp(timestamp) < cutoff:
            continue
        rows.append([pd.Timestamp(timestamp).strftime("%Y-%m-%d"), round(float(value), 6)])
    return rows


def _html_text(raw: str) -> str:
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_prospectus(raw_html: str) -> dict:
    text = _html_text(raw_html)
    description = None
    match = OVERVIEW_RE.search(text)
    if match:
        description = re.sub(r"\s+", " ", match.group(0)).strip()
    ceo = None
    ceo_match = CEO_RE.search(text)
    if ceo_match:
        ceo = ceo_match.group(1)
    ipo_price = None
    price_match = IPO_PRICE_RE.search(text)
    if price_match:
        ipo_price = float(price_match.group(1))
    return {"description": description, "ceo": ceo, "ipo_price": ipo_price}


def parse_share_counts(raw_html: str) -> dict | None:
    text = _html_text(raw_html)
    match = SHARES_RE.search(text)
    if not match:
        return None
    month, day, year, class_a, class_b = match.groups()
    as_of = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date().isoformat()
    return {
        "as_of": as_of,
        "class_a": int(class_a.replace(",", "")),
        "class_b": int(class_b.replace(",", "")),
    }


def _concept_rows(facts: dict, name: str) -> list[dict]:
    units = (((facts.get("facts") or {}).get("us-gaap") or {}).get(name) or {}).get("units") or {}
    rows = []
    for unit_rows in units.values():
        rows.extend(unit_rows)
    return rows


def _money(value: float | None) -> str | None:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if absolute >= scale:
            return f"{sign}${absolute / scale:.2f}{suffix}"
    return f"{sign}${absolute:,.0f}"


def assemble_document(
    spec: dict,
    prices: list[list],
    submissions: dict,
    prospectus_html: str = "",
    tenq_html: str = "",
    facts: dict | None = None,
    fmp_profile: dict | None = None,
    fetched_at: str | None = None,
) -> dict:
    """Combine already-fetched payloads. Missing inputs stay null."""
    facts = facts or {}
    releases = earnings_releases(submissions)
    last_release = releases[-1] if releases else None
    prospectus = parse_prospectus(prospectus_html) if prospectus_html else {}
    shares = parse_share_counts(tenq_html) if tenq_html else None
    last_price = prices[-1][1] if prices else None
    last_price_date = prices[-1][0] if prices else None

    market_cap = None
    market_source = "Class A shares outstanding were not found in the latest 10-Q, and no FMP profile was available."
    if shares and last_price:
        market_cap = round(shares["class_a"] * last_price, 2)
        market_source = (
            f"Class A shares outstanding ({shares['class_a']:,} as of {shares['as_of']}, "
            f"10-Q) times the {last_price_date} close. Class B shares are excluded."
        )
    elif fmp_profile and fmp_profile.get("mktCap"):
        market_cap = float(fmp_profile["mktCap"])
        market_source = "Financial Modeling Prep profile. The 10-Q share count was not parsed."

    eps_rows = _concept_rows(facts, "EarningsPerShareDiluted")
    ttm = trailing_four_quarters(eps_rows)
    ttm_eps = round(sum(float(row["val"]) for row in ttm), 4) if ttm else None
    if ttm:
        eps_source = "Sum of the latest four back-to-back quarterly diluted-EPS facts in SEC companyfacts."
    elif len(quarterly_facts(eps_rows)) >= 4:
        eps_source = (
            "Quarterly diluted-EPS facts exist, but they are not four consecutive quarters, "
            "so TTM EPS is not shown."
        )
    else:
        eps_source = "Fewer than four quarterly diluted-EPS facts are in SEC companyfacts, so TTM EPS is not shown."

    pe_ttm = None
    pe_source = "PE is not shown because TTM diluted EPS is missing or not positive."
    if ttm_eps is not None and ttm_eps <= 0:
        pe_source = "TTM diluted EPS is negative, so a price-to-earnings multiple is not shown."
    elif ttm_eps and last_price:
        pe_ttm = round(last_price / ttm_eps, 2)
        pe_source = f"{last_price_date} close divided by TTM diluted EPS from SEC companyfacts."

    dividend = _concept_rows(facts, "CommonStockDividendsPerShareDeclared")
    if dividend:
        latest_dividend = sorted(dividend, key=lambda row: row.get("end") or "")[-1]
        dividend_value = float(latest_dividend.get("val") or 0)
        dividend_source = "Latest CommonStockDividendsPerShareDeclared fact in SEC companyfacts."
    else:
        dividend_value = None
        dividend_source = "SEC companyfacts has no per-share dividend fact for this filer. A zero yield was not assumed."

    ipo_price = prospectus.get("ipo_price") or spec.get("ipo_price")
    ipo_source = (
        "SEC 424B4 prospectus."
        if prospectus.get("ipo_price")
        else "Ticker registry, matching the stated IPO price."
    )
    description = prospectus.get("description")
    ceo = prospectus.get("ceo")
    if fmp_profile:
        if not description and fmp_profile.get("description"):
            description = str(fmp_profile["description"]).strip()
        if not ceo and fmp_profile.get("ceo"):
            ceo = str(fmp_profile["ceo"]).strip()

    return {
        "symbol": spec["symbol"],
        "name": spec["name"],
        "class_name": spec["class_name"],
        "exchange": spec["exchange"],
        "cik": spec["cik"],
        "history_start": spec["history_start"],
        "ipo_date": spec["ipo_date"],
        "ipo_price": field(ipo_price, ipo_source),
        "fetched_at": fetched_at,
        "prices": prices,
        "earnings_releases": releases,
        "fundamentals": {
            "description": field(
                description,
                "SEC 424B4 prospectus overview." if prospectus.get("description") else "No prospectus overview was parsed.",
            ),
            "market_cap": field(market_cap, market_source, label="Class A", display=_money(market_cap)),
            "dividend_yield": field(dividend_value, dividend_source),
            "pe_ttm": field(pe_ttm, pe_source),
            "pe_ntm": field(None, "No free forward-estimate source returned an NTM P/E. FMP is used only when FMP_API_KEY is set."),
            "eps_ttm": field(ttm_eps, eps_source),
            "eps_ntm": field(None, "No free forward-estimate source returned an NTM EPS."),
            "last_earnings_date": field(
                last_release["date"] if last_release else None,
                "Filing date of the latest 8-K that includes item 2.02." if last_release else "No 8-K item 2.02 was found.",
                period_end=last_release.get("period_end") if last_release else None,
                confirmed=True if last_release else None,
            ),
            "next_earnings_date": field(
                None,
                "No confirmed or estimated next earnings date was found. An unconfirmed date would be labeled Expected.",
                confirmed=None,
            ),
            "ceo": field(ceo, "SEC 424B4 prospectus." if prospectus.get("ceo") else "CEO was not found in the prospectus text."),
        },
        "prompt": PROMPT_PATH.read_text(encoding="utf-8").strip(),
    }


def _get(url: str, timeout: float, headers: dict) -> str:
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def _filing_url(cik: str, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"


def _latest_filing(submissions: dict, form: str) -> tuple[str, str] | None:
    recent = submissions["filings"]["recent"]
    for index, name in enumerate(recent["form"]):
        if name == form:
            return recent["accessionNumber"][index], recent["primaryDocument"][index]
    return None


def fetch_fmp_profile(symbol: str, api_key: str | None, timeout: float) -> dict | None:
    if not api_key:
        return None
    response = requests.get(
        f"https://financialmodelingprep.com/api/v3/profile/{symbol}",
        params={"apikey": api_key},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list) and payload:
        return payload[0]
    return None


def build_one(spec: dict, timeout: float = 20, fmp_key: str | None = None) -> dict:
    start = date.fromisoformat(spec["history_start"]) - timedelta(days=30)
    series = fetch_yahoo_chart_series(spec["symbol"], start, date.today(), timeout=timeout, attempts=2)
    prices = prices_from_series(series, spec["history_start"])
    if not prices:
        raise RuntimeError(f"No {spec['symbol']} prices on or after {spec['history_start']}.")
    submissions = json.loads(
        _get(
            f"https://data.sec.gov/submissions/CIK{_cik(spec['cik'])}.json",
            timeout,
            SEC_HEADERS,
        )
    )
    facts = json.loads(
        _get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik(spec['cik'])}.json",
            timeout,
            SEC_HEADERS,
        )
    )
    prospectus_html = ""
    prospectus = _latest_filing(submissions, "424B4")
    if prospectus:
        prospectus_html = _get(_filing_url(spec["cik"], *prospectus), timeout, SEC_HEADERS)
    tenq_html = ""
    tenq = _latest_filing(submissions, "10-Q")
    if tenq:
        tenq_html = _get(_filing_url(spec["cik"], *tenq), timeout, SEC_HEADERS)
    try:
        fmp_profile = fetch_fmp_profile(spec["symbol"], fmp_key, timeout)
    except requests.RequestException:
        logger.warning("fmp_profile_failed symbol=%s", spec["symbol"])
        fmp_profile = None
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return assemble_document(
        spec,
        prices,
        submissions,
        prospectus_html,
        tenq_html,
        facts,
        fmp_profile,
        fetched_at,
    )


def build_analysis_snapshot(directory: Path | str, timeout: float = 20) -> dict:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    fmp_key = os.environ.get("FMP_API_KEY") or None
    written = []
    errors = []
    for spec in ANALYSIS_TICKERS:
        path = target / f"{spec['symbol']}.json"
        try:
            document = build_one(spec, timeout=timeout, fmp_key=fmp_key)
            path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            written.append(spec["symbol"])
        except Exception as exc:
            logger.error("analysis_build_failed symbol=%s error=%s", spec["symbol"], exc)
            errors.append({"symbol": spec["symbol"], "error": str(exc)[:300]})
            if not path.exists():
                continue
    catalog = {
        "tickers": [
            {
                "symbol": spec["symbol"],
                "name": spec["name"],
                "file": f"{spec['symbol']}.json",
                "history_start": spec["history_start"],
            }
            for spec in ANALYSIS_TICKERS
        ]
    }
    (target / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    return {"written": written, "errors": errors}
