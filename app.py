"""Interactive macroeconomic and market dashboard."""

from __future__ import annotations

from datetime import date
import logging
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data_sources import (
    MACRO_INDICATORS,
    MARKET_INDICATORS,
    TOP_COMPANY_TICKERS,
    Indicator,
    fetch_macro_data,
    fetch_yahoo_prices,
    frame_to_series,
    latest_metrics,
    series_freshness,
)


logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Macro Terminal",
    page_icon="▰",
    layout="wide",
    initial_sidebar_state="collapsed",
)

HISTORY_START = date(date.today().year - 6, 1, 1)
DEFAULT_START = date(date.today().year - 5, 1, 1)
CHART_HEIGHT = 235
CHART_MARGIN = {"l": 4, "r": 8, "t": 8, "b": 4}
POSITIVE = "#5f9e68"
NEGATIVE = "#b95751"
NEUTRAL = "#7892a8"
SERIES_COLORS = (
    "#7c91c9",
    "#a779c9",
    "#c47da5",
    "#d08c69",
    "#d8aa69",
    "#92a96e",
    "#5f9e68",
    "#63a0a0",
)


def inject_terminal_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #070909;
            --panel: #101313;
            --panel-hi: #131717;
            --border: #202626;
            --text: #d8dddd;
            --muted: #7d8888;
            --green: #5f9e68;
            --red: #b95751;
        }

        .stApp {
            background:
                radial-gradient(circle at 72% -20%, rgba(51, 83, 69, 0.13), transparent 34rem),
                var(--bg);
            color: var(--text);
        }

        [data-testid="stAppViewContainer"] > .main {
            background: transparent;
        }

        .block-container {
            max-width: 1800px;
            padding: 1.15rem 1.35rem 3rem;
        }

        header[data-testid="stHeader"] {
            background: transparent;
            height: 2.25rem;
        }

        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] {
            display: none;
        }

        html, body, [class*="css"] {
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }

        h1, h2, h3, h4, p {
            letter-spacing: -0.02em;
        }

        .terminal-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding: 0.15rem 0 1rem;
            border-bottom: 1px solid var(--border);
            margin-bottom: 0.85rem;
        }

        .brand-lockup {
            display: flex;
            align-items: center;
            gap: 0.7rem;
        }

        .brand-mark {
            width: 1.8rem;
            height: 1.8rem;
            display: grid;
            place-items: center;
            color: #0b100d;
            background: #7aa480;
            border-radius: 5px;
            font-size: 0.8rem;
            font-weight: 800;
        }

        .brand-name {
            color: #e5eaea;
            font-size: 0.98rem;
            font-weight: 680;
            line-height: 1.05;
        }

        .brand-sub {
            color: var(--muted);
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.59rem;
            letter-spacing: 0.11em;
            margin-top: 0.25rem;
        }

        .market-status {
            display: flex;
            align-items: center;
            gap: 0.45rem;
            color: #889292;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.65rem;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }

        .status-dot {
            width: 6px;
            height: 6px;
            border-radius: 99px;
            background: var(--green);
            box-shadow: 0 0 10px rgba(95, 158, 104, 0.55);
        }

        .eyebrow {
            color: #798484;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.61rem;
            font-weight: 700;
            letter-spacing: 0.13em;
            text-transform: uppercase;
            margin: 1.15rem 0 0.25rem;
        }

        .section-title {
            color: #e4e8e8;
            font-size: 1.35rem;
            font-weight: 650;
            margin: 0 0 0.15rem;
        }

        .section-copy {
            color: #747f7f;
            font-size: 0.76rem;
            margin: 0 0 0.7rem;
        }

        div[data-testid="stMetric"] {
            min-height: 76px;
            background: linear-gradient(180deg, #121616, #0f1212);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 0.65rem 0.75rem;
        }

        div[data-testid="stMetricLabel"] {
            color: #737e7e;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.62rem;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }

        div[data-testid="stMetricValue"] {
            color: #dce1e1;
            font-size: 1.15rem;
            font-weight: 610;
        }

        div[data-testid="stMetricDelta"] {
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.64rem;
        }

        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: linear-gradient(180deg, rgba(19, 23, 23, 0.98), rgba(14, 17, 17, 0.98));
            border: 1px solid var(--border);
            border-radius: 7px;
            box-shadow: none;
        }

        div[data-testid="stVerticalBlockBorderWrapper"] > div {
            padding: 0.78rem 0.85rem 0.65rem;
        }

        div[data-testid="stPlotlyChart"] {
            margin-top: -0.35rem;
        }

        div[data-testid="stDownloadButton"] button,
        div[data-testid="stButton"] button {
            min-height: 1.85rem;
            border: 1px solid #293030;
            border-radius: 4px;
            background: #151919;
            color: #9ba4a4;
            font-size: 0.66rem;
            font-weight: 600;
        }

        div[data-testid="stDownloadButton"] button:hover,
        div[data-testid="stButton"] button:hover {
            border-color: #526b58;
            color: #dce3de;
            background: #18201b;
        }

        button[kind="primary"] {
            background: #496c50 !important;
            border-color: #5b8163 !important;
            color: #f0f4f1 !important;
        }

        [data-baseweb="tab-list"] {
            gap: 0.15rem;
            border-bottom: 1px solid var(--border);
            margin-bottom: 0.7rem;
        }

        button[data-baseweb="tab"] {
            height: 2.25rem;
            padding: 0 0.85rem;
            border-radius: 4px 4px 0 0;
            color: #788282;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.67rem;
            letter-spacing: 0.055em;
            text-transform: uppercase;
        }

        button[data-baseweb="tab"][aria-selected="true"] {
            color: #d9dfdb;
            background: #111515;
        }

        [data-baseweb="tab-highlight"] {
            background-color: #6f9876;
        }

        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] > div,
        div[data-testid="stDateInput"] div[data-baseweb="input"] > div {
            background: #111515;
            border-color: #252c2c;
            border-radius: 4px;
            color: #b8c0c0;
        }

        [data-baseweb="tag"] {
            background-color: #2b4130 !important;
            border: 1px solid #405c47 !important;
            border-radius: 3px !important;
        }

        [data-baseweb="tag"] span {
            color: #c2cec5 !important;
        }

        [data-testid="stCheckbox"] svg {
            color: #dbe5dd !important;
        }

        label[data-testid="stWidgetLabel"] p {
            color: #788282;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.62rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        [data-testid="stCaptionContainer"] {
            color: #697474;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 0.58rem;
        }

        div[data-testid="stAlert"] {
            border-radius: 5px;
            background: #161b1b;
            border: 1px solid #293030;
            color: #aeb7b7;
        }

        hr {
            border-color: var(--border) !important;
        }

        @media (max-width: 800px) {
            .block-container { padding: 0.8rem 0.7rem 2rem; }
            .market-status { display: none; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _compact_number(value: float) -> str:
    """Format large numbers with K/M suffix for compact display."""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:,.2f}K"
    return f"{value:,.2f}"


def format_value(value: float, unit: str, compact: bool = False) -> str:
    """Format a value with its unit, optionally in compact (K/M) notation."""
    if math.isnan(value):
        return "N/A"
    # Unit-specific formatting first to preserve currency symbols and conventions
    if unit == "$":
        return f"${_compact_number(value) if compact else f'{value:,.2f}'}"
    if unit == "$ billions":
        return f"${value:,.1f}B"  # billions displayed as B, no compaction
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "claims":
        return f"{value:,.0f}"
    if unit == "points":
        return f"{value:,.2f}"  # index levels shown with full precision
    if unit == "index":
        return f"{value:,.2f}"
    # Generic fallback
    return _compact_number(value) if compact else f"{value:,.2f}"


def format_change(value: float, change_mode: str) -> str:
    if math.isnan(value):
        return "N/A"
    suffix = " pp" if change_mode == "points" else "%"
    return f"{value:+.2f}{suffix}"


def filter_series(series: pd.Series, start: date, end: date) -> pd.Series:
    return series.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def chart_color(metrics: dict[str, float | pd.Timestamp]) -> str:
    change = float(metrics["previous_change"])
    if math.isnan(change) or change == 0:
        return NEUTRAL
    return POSITIVE if change > 0 else NEGATIVE


def make_line_figure(
    indicator: Indicator,
    visible: pd.Series,
    metrics: dict[str, float | pd.Timestamp],
) -> go.Figure:
    color = chart_color(metrics)
    change_mode = getattr(indicator, "change_mode", "percent")
    # Only fill for level/price series (percent change mode), not rates/spreads (points mode)
    use_fill = change_mode == "percent"
    figure = go.Figure(
        go.Scatter(
            x=visible.index,
            y=visible.values,
            mode="lines",
            line={"color": color, "width": 1.45},
            fill="tozeroy" if use_fill else None,
            fillcolor=(
                "rgba(95, 158, 104, 0.05)"
                if color == POSITIVE
                else "rgba(185, 87, 81, 0.05)"
                if color == NEGATIVE
                else "rgba(120, 146, 168, 0.04)"
            )
            if use_fill
            else None,
            hovertemplate=(
                f"<b>{indicator.name}</b><br>"
                f"%{{x|%b %d, %Y}}<br>%{{y:,.2f}} {indicator.unit}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        height=CHART_HEIGHT,
        margin=CHART_MARGIN,
        hovermode="x unified",
        showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, sans-serif", "color": "#7c8787", "size": 9},
        hoverlabel={"bgcolor": "#161b1b", "bordercolor": "#303838", "font_color": "#d7dddd"},
    )
    figure.update_xaxes(
        rangeslider_visible=False,
        gridcolor="#1c2222",
        gridwidth=1,
        zeroline=False,
        showline=False,
        tickfont={"color": "#657070", "size": 9},
        nticks=5,
    )
    figure.update_yaxes(
        gridcolor="#1c2222",
        gridwidth=1,
        zeroline=False,
        showline=False,
        side="right",
        tickfont={"color": "#657070", "size": 9},
        nticks=5,
    )
    return figure


def section_heading(kicker: str, title: str, copy: str) -> None:
    st.markdown(
        f"""
        <div class="eyebrow">{kicker}</div>
        <div class="section-title">{title}</div>
        <div class="section-copy">{copy}</div>
        """,
        unsafe_allow_html=True,
    )


def render_indicator(indicator: Indicator, series: pd.Series, start: date, end: date) -> None:
    visible = filter_series(series, start, end)
    if visible.empty:
        st.warning(f"No {indicator.name} data is available in the selected date range.")
        return

    change_mode = getattr(indicator, "change_mode", "percent")
    metrics = latest_metrics(series, change_mode)
    with st.container(border=True):
        title_column, value_column = st.columns([1.5, 1], vertical_alignment="center")
        title_column.markdown(f"**{indicator.name}**")
        title_column.caption(
            f"{indicator.key} · {indicator.frequency.upper()} · {indicator.source}"
        )
        if indicator.description:
            title_column.caption(indicator.description)
        value_column.metric(
            "LATEST",
            format_value(float(metrics["latest"]), indicator.unit, compact=True),
            format_change(float(metrics["previous_change"]), change_mode),
        )
        st.plotly_chart(
            make_line_figure(indicator, visible, metrics),
            width="stretch",
            config={"displaylogo": False, "displayModeBar": False},
        )
        ytd_column, date_column, export_column = st.columns([1, 1.3, 1])
        ytd_column.caption(f"YTD {format_change(float(metrics['ytd_change']), change_mode)}")
        freshness = series_freshness(
            series,
            indicator.frequency,
            max_age_days=indicator.max_age_days,
        )
        latest_date = pd.Timestamp(metrics["latest_date"]).strftime("%d %b %Y")
        age_days = int(freshness["age_days"])
        date_column.caption(f"AS OF {latest_date.upper()} · {age_days}D OLD")
        export_column.download_button(
            "EXPORT CSV",
            data=visible.rename(indicator.name).to_csv(index_label="date"),
            file_name=f"{indicator.key}_data.csv",
            mime="text/csv",
            key=f"export_{indicator.key}",
            width="stretch",
        )
        if freshness["is_stale"]:
            st.warning(
                f"Latest observation is {age_days} days old; this may be stale for "
                f"{indicator.frequency.lower()} data."
            )


def render_grid(
    indicators: tuple[Indicator, ...] | list[Indicator],
    data: dict[str, pd.Series],
    errors: dict[str, str],
    start: date,
    end: date,
) -> None:
    for row_start in range(0, len(indicators), 2):
        columns = st.columns(2)
        for column, indicator in zip(columns, indicators[row_start : row_start + 2]):
            with column:
                if indicator.key in errors:
                    st.error(f"{indicator.name}: {errors[indicator.key]}")
                elif indicator.key in data:
                    render_indicator(indicator, data[indicator.key], start, end)


# Explicitly curated snapshot indicators (order matters)
SNAPSHOT_KEYS = ("^GSPC", "^IXIC", "^VIX", "GC=F")


def render_snapshot(
    indicators: tuple[Indicator, ...],
    data: dict[str, pd.Series],
) -> None:
    snapshot_indicators = [ind for ind in indicators if ind.key in SNAPSHOT_KEYS and ind.key in data]
    columns = st.columns(len(snapshot_indicators))
    for column, indicator in zip(columns, snapshot_indicators):
        metrics = latest_metrics(data[indicator.key], getattr(indicator, "change_mode", "percent"))
        column.metric(
            indicator.name.replace(" Index", "").replace(" Futures", ""),
            format_value(float(metrics["latest"]), indicator.unit, compact=True),
            format_change(float(metrics["previous_change"]), getattr(indicator, "change_mode", "percent")),
        )


def render_comparison_chart(comparison: pd.DataFrame) -> None:
    figure = go.Figure()
    for index, column in enumerate(comparison):
        figure.add_trace(
            go.Scatter(
                x=comparison.index,
                y=comparison[column],
                mode="lines",
                name=column,
                line={"width": 1.35, "color": SERIES_COLORS[index % len(SERIES_COLORS)]},
                hovertemplate="%{x|%b %d, %Y}<br>%{y:,.2f}<extra>%{fullData.name}</extra>",
            )
        )
    figure.update_layout(
        height=390,
        margin={"l": 6, "r": 10, "t": 8, "b": 6},
        hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, sans-serif", "color": "#849090", "size": 9},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 9},
        },
    )
    figure.update_xaxes(gridcolor="#1c2222", zeroline=False, nticks=7)
    figure.update_yaxes(gridcolor="#1c2222", zeroline=False, side="right", nticks=6)
    st.plotly_chart(
        figure,
        width="stretch",
        config={"displaylogo": False, "displayModeBar": False},
    )


inject_terminal_css()

control_columns = st.columns([2.6, 1.2, 0.8], vertical_alignment="bottom")
with control_columns[0]:
    selected_range = st.date_input(
        "Observation window",
        value=(DEFAULT_START, date.today()),
        min_value=HISTORY_START,
        max_value=date.today(),
    )
with control_columns[1]:
    st.caption("FRED CACHE 12H · YAHOO CACHE 6H")
with control_columns[2]:
    refresh = st.button("REFRESH DATA", type="primary", width="stretch")
    if refresh:
        st.cache_data.clear()
        st.rerun()

if not isinstance(selected_range, tuple) or len(selected_range) != 2:
    st.info("Select both a start and end date.")
    st.stop()

range_start, range_end = selected_range
if range_start > range_end:
    st.error("The start date must be before the end date.")
    st.stop()

# Header with dynamic end date
st.markdown(
    f"""
    <div class="terminal-header">
        <div class="brand-lockup">
            <div class="brand-mark">MT</div>
            <div>
                <div class="brand-name">Macro Terminal</div>
                <div class="brand-sub">US ECONOMY / MARKETS / EQUITIES</div>
            </div>
        </div>
        <div class="market-status">
            <span class="status-dot"></span>
            Observation window through · {range_end.strftime("%d %b %Y")}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.spinner("Synchronizing economic and market feeds..."):
    try:
        macro_data, macro_errors = fetch_macro_data(HISTORY_START, range_end)
    except Exception as exc:
        logger.exception("macro_data_fetch_failed")
        macro_data = {}
        macro_errors = {indicator.key: str(exc) for indicator in MACRO_INDICATORS}

    market_tickers = tuple(indicator.key for indicator in MARKET_INDICATORS)
    try:
        market_frame = fetch_yahoo_prices(market_tickers, HISTORY_START, range_end)
        market_data, market_errors = frame_to_series(market_frame, MARKET_INDICATORS)
    except Exception as exc:
        logger.exception("market_data_fetch_failed")
        market_data, market_errors = {}, {ticker: str(exc) for ticker in market_tickers}

    company_tickers = tuple(TOP_COMPANY_TICKERS)
    company_indicators = [
        Indicator(ticker, f"{name} ({ticker})", "Yahoo Finance", "$", "Daily")
        for ticker, name in TOP_COMPANY_TICKERS.items()
    ]
    try:
        company_frame = fetch_yahoo_prices(company_tickers, HISTORY_START, range_end)
        company_data, company_errors = frame_to_series(company_frame, company_indicators)
    except Exception as exc:
        logger.exception("company_data_fetch_failed")
        company_data, company_errors = {}, {ticker: str(exc) for ticker in company_tickers}

section_heading(
    "Live overview",
    "Market snapshot",
    "Latest observations and previous-period moves across major risk assets.",
)
render_snapshot(MARKET_INDICATORS, market_data)

macro_tab, markets_tab, companies_tab = st.tabs(["Macro indicators", "Markets", "Top companies"])

with macro_tab:
    section_heading(
        "US economic regime",
        "Macro indicators",
        "Growth, inflation, labor, liquidity, rates, and the yield curve in one analytical grid.",
    )
    render_grid(MACRO_INDICATORS, macro_data, macro_errors, range_start, range_end)

with markets_tab:
    section_heading(
        "Cross-asset monitor",
        "Markets",
        "A compact view of US equities, volatility, crypto, energy, and precious metals.",
    )
    render_grid(MARKET_INDICATORS, market_data, market_errors, range_start, range_end)

with companies_tab:
    section_heading(
        "Large-cap monitor",
        "Top companies",
        "Representative basket of leading US-listed companies. Rankings change over time.",
    )
    selection_column, mode_column = st.columns([3, 1])
    with selection_column:
        selected_companies = st.multiselect(
            "Companies",
            options=list(TOP_COMPANY_TICKERS),
            default=list(TOP_COMPANY_TICKERS)[:5],
            format_func=lambda ticker: f"{TOP_COMPANY_TICKERS[ticker]} ({ticker})",
        )
    with mode_column:
        show_normalized = st.checkbox(
            "Normalize to 100",
            value=True,
            help="Compare relative performance from the beginning of the selected window.",
        )

    selected_indicators = [
        indicator for indicator in company_indicators if indicator.key in selected_companies
    ]
    if selected_indicators:
        if show_normalized:
            normalized = {}
            for indicator in selected_indicators:
                if indicator.key not in company_data:
                    continue
                visible = filter_series(company_data[indicator.key], range_start, range_end)
                if not visible.empty and visible.iloc[0] != 0:
                    normalized[indicator.name] = visible.div(visible.iloc[0]).mul(100)
            if normalized:
                with st.container(border=True):
                    st.markdown("**Relative performance**")
                    st.caption("NORMALIZED · START OF SELECTED WINDOW = 100")
                    render_comparison_chart(pd.DataFrame(normalized))
        render_grid(selected_indicators, company_data, company_errors, range_start, range_end)
    else:
        st.info("Select at least one company to display its chart.")
