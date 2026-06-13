# Macroeconomic Dashboard

An interactive Streamlit dashboard for monitoring US macroeconomic indicators,
major markets, and a representative basket of 20 large-cap companies. It
supports custom date ranges, normalized stock comparisons, CSV exports,
latest-value metrics, previous-period changes, YTD changes, caching, retries,
and manual refreshes.

## Data sources

- **FRED public CSV endpoint**: unemployment, CPI, real GDP growth, M2, Treasury
  spread, initial jobless claims, and the effective federal funds rate.
- **Yahoo Finance via `yfinance`**: S&P 500, Nasdaq Composite, Bitcoin, VIX,
  gold futures, WTI crude oil futures, and company stock prices.

No API key is required. The FRED CSV endpoint and Yahoo Finance data are free,
but both services may impose rate limits or occasionally be unavailable.

### Indicator definitions

- US Unemployment Rate: `UNRATE`
- US Inflation: year-over-year percentage change calculated from `CPIAUCSL`
- US Real GDP Growth Rate: `A191RL1Q225SBEA`, annualized quarterly percent change
- US M2 Money Supply: `M2SL`, billions of dollars
- 10Y-2Y Treasury Spread: `T10Y2Y`, percentage points
- Initial Jobless Claims: `ICSA`, claims
- Effective Federal Funds Rate: `FEDFUNDS`, percent
- S&P 500: `^GSPC`
- Nasdaq Composite: `^IXIC`
- Bitcoin: `BTC-USD`
- CBOE Volatility Index: `^VIX`
- Gold Futures: `GC=F`
- WTI Crude Oil Futures: `CL=F`

The GDP series is already an annualized real-GDP growth rate. The M2 series is
the active monthly, seasonally adjusted series. Replacing either with a level
series without an explicit growth transformation would mislabel the chart.

The top-company list is a representative basket defined in
`data_sources.py`. Market-cap rankings change frequently, so review the list
periodically if exact current rankings are required.

## Setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Use the sidebar to choose a custom date range. The **Refresh data** button clears
Streamlit's data cache and downloads fresh observations. FRED responses are
cached for 12 hours and Yahoo Finance responses for 6 hours.

## Notes

- Previous-period change compares the latest observation with the immediately
  preceding observation. Rates and growth rates show percentage-point changes;
  levels and prices show percentage changes.
- YTD change compares the latest observation with the first available
  observation in its current calendar year.
- The dashboard catches failures per indicator where possible, so a temporary
  issue with one source does not prevent other charts from loading.
- FRED and Yahoo requests retry transient failures up to three times. Yahoo
  tickers are fetched in a single batch per dashboard section to minimize
  requests and reduce rate-limit pressure.

## Tests

```bash
python -m unittest -v
```
