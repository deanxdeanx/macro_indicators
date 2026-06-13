"""Interactive macroeconomic and market dashboard."""

from __future__ import annotations

from datetime import date
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
)


st.set_page_config(page_title="Macroeconomic Dashboard", page_icon="📈", layout="wide")

HISTORY_START = date(date.today().year - 6, 1, 1)
DEFAULT_START = date(date.today().year - 5, 1, 1)
CHART_HEIGHT = 310
CHART_MARGIN = {"l": 10, "r": 10, "t": 10, "b": 10}
CHART_COLOR = "#3b82f6"


def format_value(value: float, unit: str) -> str:
    if math.isnan(value):
        return "N/A"
    if unit == "$":
        return f"${value:,.2f}"
    if unit == "$ billions":
        return f"${value:,.1f}B"
    if unit == "%":
        return f"{value:,.2f}%"
    if unit == "claims":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def format_change(value: float, change_mode: str) -> str:
    if math.isnan(value):
        return "N/A"
    suffix = " pp" if change_mode == "points" else "%"
    return f"{value:+.2f}{suffix}"


def filter_series(series: pd.Series, start: date, end: date) -> pd.Series:
    return series.loc[pd.Timestamp(start) : pd.Timestamp(end)]


def render_indicator(indicator: Indicator, series: pd.Series, start: date, end: date) -> None:
    visible = filter_series(series, start, end)
    if visible.empty:
        st.warning(f"No {indicator.name} data is available in the selected date range.")
        return

    metrics = latest_metrics(series, indicator.change_mode)
    st.markdown(f"#### {indicator.name}")
    metric_columns = st.columns(3)
    metric_columns[0].metric("Latest", format_value(metrics["latest"], indicator.unit))
    metric_columns[1].metric(
        "Previous period",
        format_change(metrics["previous_change"], indicator.change_mode),
    )
    metric_columns[2].metric("YTD", format_change(metrics["ytd_change"], indicator.change_mode))

    figure = go.Figure(
        go.Scatter(
            x=visible.index,
            y=visible.values,
            mode="lines",
            line={"color": CHART_COLOR, "width": 2},
            hovertemplate=f"%{{x|%b %d, %Y}}<br>%{{y:,.2f}} {indicator.unit}<extra></extra>",
        )
    )
    figure.update_layout(
        height=CHART_HEIGHT,
        margin=CHART_MARGIN,
        hovermode="x unified",
        xaxis_title=None,
        yaxis_title=indicator.unit,
        template="plotly_white",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    figure.update_xaxes(rangeslider_visible=False, gridcolor="#e5e7eb", zeroline=False)
    figure.update_yaxes(gridcolor="#e5e7eb", zeroline=False)
    st.plotly_chart(figure, width="stretch", config={"displaylogo": False})
    st.download_button(
        "Download CSV",
        data=visible.rename(indicator.name).to_csv(index_label="date"),
        file_name=f"{indicator.key}_data.csv",
        mime="text/csv",
        key=f"export_{indicator.key}",
    )
    latest_date = metrics["latest_date"].strftime("%b %d, %Y")
    st.caption(f"Source: {indicator.source} · {indicator.frequency} · Latest observation: {latest_date}")


def render_section(
    title: str,
    indicators: tuple[Indicator, ...] | list[Indicator],
    data: dict[str, pd.Series],
    errors: dict[str, str],
    start: date,
    end: date,
) -> None:
    if title:
        st.header(title)
    for indicator in indicators:
        if indicator.key in errors:
            st.error(f"{indicator.name}: {errors[indicator.key]}")
        elif indicator.key in data:
            render_indicator(indicator, data[indicator.key], start, end)


st.title("Macroeconomic Dashboard")
st.caption("US economic conditions, major markets, crypto, and large-cap equities in one view.")

with st.sidebar:
    st.header("Controls")
    selected_range = st.date_input(
        "Date range",
        value=(DEFAULT_START, date.today()),
        min_value=HISTORY_START,
        max_value=date.today(),
    )
    if st.button("Refresh data", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.caption("FRED data is cached for 12 hours. Yahoo Finance data is cached for 6 hours.")

if not isinstance(selected_range, tuple) or len(selected_range) != 2:
    st.info("Select both a start and end date.")
    st.stop()

range_start, range_end = selected_range
if range_start > range_end:
    st.error("The start date must be before the end date.")
    st.stop()

with st.spinner("Loading economic and market data..."):
    macro_data, macro_errors = fetch_macro_data(HISTORY_START)

    market_tickers = tuple(indicator.key for indicator in MARKET_INDICATORS)
    try:
        market_frame = fetch_yahoo_prices(market_tickers, HISTORY_START)
        market_data, market_errors = frame_to_series(market_frame, MARKET_INDICATORS)
    except Exception as exc:
        market_data, market_errors = {}, {ticker: str(exc) for ticker in market_tickers}

    company_tickers = tuple(TOP_COMPANY_TICKERS)
    try:
        company_frame = fetch_yahoo_prices(company_tickers, HISTORY_START)
        company_indicators = [
            Indicator(ticker, f"{name} ({ticker})", "Yahoo Finance", "$", "Daily")
            for ticker, name in TOP_COMPANY_TICKERS.items()
        ]
        company_data, company_errors = frame_to_series(company_frame, company_indicators)
    except Exception as exc:
        company_indicators = [
            Indicator(ticker, f"{name} ({ticker})", "Yahoo Finance", "$", "Daily")
            for ticker, name in TOP_COMPANY_TICKERS.items()
        ]
        company_data, company_errors = {}, {ticker: str(exc) for ticker in company_tickers}

macro_tab, markets_tab, companies_tab = st.tabs(["Macro Indicators", "Markets", "Top Companies"])

with macro_tab:
    render_section("Macro Indicators", MACRO_INDICATORS, macro_data, macro_errors, range_start, range_end)

with markets_tab:
    render_section("Markets", MARKET_INDICATORS, market_data, market_errors, range_start, range_end)

with companies_tab:
    st.header("Top Companies")
    st.caption(
        "Representative basket of 20 of the largest US-listed companies. "
        "Market-cap rankings change over time."
    )
    selected_companies = st.multiselect(
        "Companies to display",
        options=list(TOP_COMPANY_TICKERS),
        default=list(TOP_COMPANY_TICKERS)[:5],
        format_func=lambda ticker: f"{TOP_COMPANY_TICKERS[ticker]} ({ticker})",
    )
    show_normalized = st.checkbox(
        "Show normalized comparison (start = 100)",
        value=True,
        help="Compares relative performance over the selected date range.",
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
                comparison = pd.DataFrame(normalized)
                comparison_figure = go.Figure()
                for column in comparison:
                    comparison_figure.add_trace(
                        go.Scatter(
                            x=comparison.index,
                            y=comparison[column],
                            mode="lines",
                            name=column,
                            hovertemplate="%{x|%b %d, %Y}<br>%{y:,.2f}<extra>%{fullData.name}</extra>",
                        )
                    )
                comparison_figure.update_layout(
                    height=430,
                    margin=CHART_MARGIN,
                    hovermode="x unified",
                    yaxis_title="Normalized value",
                    template="plotly_white",
                    plot_bgcolor="rgba(0,0,0,0)",
                )
                comparison_figure.update_xaxes(gridcolor="#e5e7eb", zeroline=False)
                comparison_figure.update_yaxes(gridcolor="#e5e7eb", zeroline=False)
                st.plotly_chart(comparison_figure, width="stretch", config={"displaylogo": False})
        render_section("", selected_indicators, company_data, company_errors, range_start, range_end)
    else:
        st.info("Select at least one company to display its chart.")
