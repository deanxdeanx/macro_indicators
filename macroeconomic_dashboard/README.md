# Macroeconomic Dashboard

An interactive Streamlit dashboard for monitoring US macroeconomic indicators,
major equity indexes, Bitcoin, and a representative basket of 20 large-cap
companies. It loads at least five years of history and supports custom date
ranges, latest-value metrics, previous-period changes, YTD changes, caching, and
manual refreshes.

## Data sources

- **FRED public CSV endpoint**: unemployment, CPI, real GDP growth, and M2.
- **Yahoo Finance via `yfinance`**: S&P 500, Nasdaq Composite, Bitcoin, and
  company stock prices.

No API key is required. The FRED CSV endpoint and Yahoo Finance data are free,
but both services may impose rate limits or occasionally be unavailable.

### Indicator definitions

- US Unemployment Rate: `UNRATE`
- US Inflation: year-over-year percentage change calculated from `CPIAUCSL`
- US Real GDP Growth Rate: `A191RL1Q225SBEA`, annualized quarterly percent change
- US M2 Money Supply: `M2SL`, billions of dollars
- S&P 500: `^GSPC`
- Nasdaq Composite: `^IXIC`
- Bitcoin: `BTC-USD`

The top-company list is a representative basket defined in
`data_sources.py`. Market-cap rankings change frequently, so review the list
periodically if exact current rankings are required.

## Setup

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r macroeconomic_dashboard/requirements.txt
```

## Run

```bash
streamlit run macroeconomic_dashboard/app.py
```

Use the sidebar to choose a custom date range. The **Refresh data** button clears
Streamlit's data cache and downloads fresh observations. FRED responses are
cached for 12 hours and Yahoo Finance responses for 6 hours.

## Notes

- Previous-period change compares the latest observation with the immediately
  preceding observation. The period therefore follows each series' frequency.
- YTD change compares the latest observation with the first available
  observation in its current calendar year.
- The dashboard catches failures per indicator where possible, so a temporary
  issue with one source does not prevent other charts from loading.
