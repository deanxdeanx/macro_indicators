# Macro Terminal

A static dashboard for US macroeconomic indicators, major markets, and a
representative basket of 20 large-cap companies. The live site is
[https://deanxdeanx.github.io/macro_indicators/](https://deanxdeanx.github.io/macro_indicators/).

The page is plain HTML, CSS, and JavaScript. It reads JSON files that a Python
script refreshes on a schedule. Nothing in the browser calls FRED or Yahoo
Finance.

## What you see

- US economy: unemployment, CPI inflation, real GDP growth, M2, the 10Y–2Y
  spread, the 10-year yield, initial claims, the federal funds rate, and
  consumer sentiment.
- Markets: S&P 500, Nasdaq Composite, Bitcoin, VIX, gold futures, and WTI.
- Equities: the 20-stock basket, with a normalized comparison (window start =
  100) and one chart per selected company.
- Latest value, previous-period change, and year-to-date change.
- A custom date range, CSV export, and frequency-aware stale warnings.
- The dark Macro Terminal layout, including on a phone.

Previous-period change compares the latest observation with the one before it.
Rates and spreads use percentage points (`pp`). Levels and prices use percent.
Year-to-date change uses the first observation in that series' current
calendar year. Metrics use every observation through the selected end date,
not only the points inside the chart window.

## How the data gets there

```text
FRED CSV ─┐
          ├─ build_data.py ─ site/data/catalog.json
Yahoo chart API ─┘            site/data/series/*.json
                                      │
                                      ▼
                               site/index.html
```

`build_data.py` fetches each series on its own, with a 12 second timeout and
two attempts. One failure does not cancel the others. If a fetch fails and a
previous file has observations, that file is left untouched and the catalog
marks the series `preserved` with the error and the last successful
`fetched_at`. A failed fetch never replaces good observations with an empty
list. Each successful series records `fetched_at`. The catalog records
`last_attempt_at` for every series.

FRED comes from the public CSV endpoint. Yahoo comes from the public chart
API. Each request uses a short timeout. No API key is required. Both services
can rate-limit or go down; the last good snapshot stays on the site.

### Indicator definitions

- US Unemployment Rate: `UNRATE`
- US Inflation: year-over-year percentage change calculated from `CPIAUCSL`
- US Real GDP Growth Rate: `A191RL1Q225SBEA`, annualized quarterly percent change
- US M2 Money Supply: `M2SL`, billions of dollars
- 10Y-2Y Treasury Spread: `T10Y2Y`, percentage points
- 10-Year Treasury Yield: `DGS10`, percent
- Initial Jobless Claims: `ICSA`, claims
- Effective Federal Funds Rate: `FEDFUNDS`, percent
- Consumer Sentiment: `UMCSENT`, University of Michigan index, not seasonally
  adjusted and delayed one month by the source
- S&P 500: `^GSPC`
- Nasdaq Composite: `^IXIC`
- Bitcoin: `BTC-USD`
- CBOE Volatility Index: `^VIX`
- Gold Futures: `GC=F`
- WTI Crude Oil Futures: `CL=F`

The GDP series is already an annualized real-GDP growth rate. The M2 series is
the active monthly, seasonally adjusted series. The company list is the basket
in `data_sources.py`. Market-cap rankings move, so review that list when the
names should change.

## Run the site locally

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python build_data.py
python -m http.server 8000 -d site
```

Open http://localhost:8000. `build_data.py` writes `site/data/`. The committed
snapshot is enough to preview the page if you skip the fetch. Re-running the
script refreshes whatever sources answer and keeps the previous file for any
source that does not.

Useful flags:

```bash
python build_data.py --timeout 12 --workers 6
python build_data.py --start 2020-01-01 --end 2026-11-02
```

## Tests

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

GitHub Actions runs the same command on every push and pull request
(`.github/workflows/tests.yml`).

## Refresh the data on a schedule

`.github/workflows/refresh-data.yml` runs every day at 11:30 UTC and whenever
someone starts it by hand. It installs `requirements.txt`, runs
`python build_data.py`, and commits `site/data` only when a file changed.
A push made with the workflow token does not start another workflow, so the
same run also deploys GitHub Pages.

### Manual refresh

1. Open the repository on GitHub.
2. Click **Actions**.
3. Click **Refresh data** in the left sidebar.
4. Click **Run workflow**.
5. Choose branch **main**, then click **Run workflow** again.

The run commits to the branch you select. Use `main` so the published site
picks up the new snapshot. If `main` has a ruleset that blocks direct pushes,
allow `github-actions[bot]` to push, or the commit step will fail and the
existing snapshot will stay as it is.

## Deploy

The site is the `site/` directory. Paths are relative, so the same files work
at the GitHub Pages project URL and at a Vercel domain root.

### GitHub Pages

The site is published at
[https://deanxdeanx.github.io/macro_indicators/](https://deanxdeanx.github.io/macro_indicators/).
**Settings → Pages → Source** is **GitHub Actions**.

Pushes to `main` that change `site/` deploy through
`.github/workflows/pages.yml`. The daily data job deploys from the same run
that commits the snapshot, so Pages updates even though that bot push does
not start `pages.yml`. To publish again by hand, open **Actions → Deploy
GitHub Pages** and run the workflow on `main`.

### Vercel

1. Go to [vercel.com/new](https://vercel.com/new) and import
   `deanxdeanx/macro_indicators`.
2. Leave the root directory as the repository root. Do not set it to `site`.
3. Framework preset **Other**, install command empty, build command empty,
   output directory `site`. `vercel.json` already sets those. If the import
   screen overrides them, put the values back.
4. Deploy. There are no environment variables.

Vercel's Git integration deploys `main` on every commit, including the daily
data commit. The data responses are cached for five minutes
(`Cache-Control` on `/data/*`). Reload the page after a refresh if a chart
still looks old; the **Reload** button does that.

You can use Pages, Vercel, or both. They read the same `site/` files.

## Layout

- `build_data.py` fetches every series and writes `site/data/`.
- `data_sources.py` holds indicator metadata, FRED and Yahoo requests,
  validation, transforms, and freshness rules.
- `site/` is the static dashboard (`index.html`, `styles.css`, `app.js`,
  `data/`).
- `.github/workflows/refresh-data.yml` refreshes and deploys.
- `.github/workflows/pages.yml` deploys the site on ordinary pushes.
- `.github/workflows/tests.yml` runs the unit tests.
- `vercel.json` tells Vercel to publish `site/` with an empty build.
- `requirements.txt` is pandas and requests, used by the snapshot job and the tests.

## Troubleshooting

- A preserved or stale banner means the latest observation is older than
  expected for that frequency, or the last refresh failed and the previous
  file was kept. It does not mean the page failed to load.
- If every series fails on a brand-new checkout with an empty `site/data`,
  `build_data.py` exits with status 1 and does not pretend there is data.
  Re-run it when the sources answer.
- If the Pages deploy is red, check **Settings → Pages → Source** is
  **GitHub Actions**, then re-run **Deploy GitHub Pages**.
- If the data workflow cannot push, `main` is blocking the Actions bot.
