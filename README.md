# Macro Terminal

A static dashboard for US macroeconomic indicators, major markets, and a
representative basket of 20 large-cap companies. The live site is
[https://deanxdeanx.github.io/macro_indicators/](https://deanxdeanx.github.io/macro_indicators/).

The page is plain HTML, CSS, and JavaScript. It reads JSON files that a Python
script refreshes on a schedule. Nothing in the browser calls FRED or Yahoo
Finance.

## What you see

- US economy, in five groups: growth and labor; the funds rate and the
  Treasury curve (daily effective funds rate, 2-year, 10-year, 10Y–2Y, and
  10Y–3M); real rates and credit spreads; liquidity, including net liquidity
  against the S&P 500; and the dollar index.
- Markets: S&P 500, Nasdaq Composite, Bitcoin, VIX, gold futures, and WTI.
- Equities: the 20-stock basket, with a normalized comparison (window start =
  100) and one chart per selected company.
- Latest value, a 1-week change on daily series, and year-to-date change.
- A regime strip on the Macro tab: 10-year real yield, 2s10s, high-yield
  spread, the broad dollar, net liquidity, and VIX, each with the 1-week and
  1-month change and its place in the 5-year range, plus a one-line readout.
- Semis: the ten largest US-listed semiconductor names, with monthly price
  against trailing P/E for ten years.
- A start date, with the window ending on the snapshot date, plus CSV export and frequency-aware stale warnings.
- The dark Macro Terminal layout, including on a phone.

Daily cards show the change over about seven days, not the previous print.
Other frequencies still compare the latest observation with the one before it.
Rates and spreads use percentage points (`pp`). Claims use a count. Levels and
prices use percent. Series where a rise is risk-off are drawn red when they
rise and green when they fall.
Year-to-date change uses the first observation in that series' current
calendar year. Metrics use every observation through the selected end date,
not only the points inside the chart window.

## How the data gets there

```text
FRED API (or public CSV) ─┐
                          ├─ build_data.py ─ site/data/catalog.json
Yahoo chart API ──────────┘                 site/data/series/*.json
                                                    │
                                                    ▼
                                             site/index.html
```

`build_data.py` fetches each series on its own. FRED uses a 30 second read
timeout and four attempts with exponential backoff. Yahoo uses the requested
timeout (12 seconds by default) and two attempts. One failure does not cancel
the others. If a fetch fails and a previous file has observations, that file
is left untouched and the catalog marks the series `preserved` with the error
and the last successful `fetched_at`. The card then shows a Held over badge
and the date of that last good observation. A failed fetch never replaces
good observations with an empty list. The catalog records `last_attempt_at`
for every series.

FRED uses `https://api.stlouisfed.org/fred/series/observations` when
`FRED_API_KEY` is set, and the public CSV otherwise. An API failure falls
back to the CSV. Yahoo comes from the public chart API. No key is required
for the CSV path. Both services can rate-limit or go down; the last good
snapshot stays on the site.

### Indicator definitions

- US Unemployment Rate: `UNRATE`
- US Inflation: year-over-year percentage change calculated from `CPIAUCSL`
- US Real GDP Growth Rate: `A191RL1Q225SBEA`, annualized quarterly percent change
- Initial Jobless Claims: `ICSA`, a count such as 218K, with the change as a count
- Initial Claims, 4-Week Average: `IC4WSA`, a count
- Consumer Sentiment: `UMCSENT`, University of Michigan index, not seasonally
  adjusted and delayed one month by the source
- Effective Federal Funds Rate: `DFF`, daily percent
- 2-Year Treasury Yield: `DGS2`, percent
- 10-Year Treasury Yield: `DGS10`, percent
- 10Y-2Y Treasury Spread: `T10Y2Y`, percentage points
- 10Y-3M Treasury Spread: `T10Y3M`, percentage points
- 2-Year minus Fed Funds: `DGS2` minus `DFF`
- 10-Year Term Premium: `THREEFYTP10`, Kim-Wright. ACM is not on FRED.
- 10-Year Real Yield: `DFII10`, percent
- 10-Year Breakeven Inflation: `T10YIE`, percent
- US High Yield Spread: `BAMLH0A0HYM2`, option-adjusted spread, percent
- US Investment Grade Spread: `BAMLC0A0CM`, option-adjusted spread, percent
- US M2 Money Supply: `M2SL`, billions of dollars
- Fed Total Assets: `WALCL`, stored in billions (FRED publishes millions)
- Reserve Balances: `WRESBAL`, stored in billions (FRED publishes millions)
- Overnight Reverse Repo: `RRPONTSYD`, billions of dollars
- Treasury General Account: `WTREGEN`, stored in billions (FRED publishes millions)
- Net Liquidity: `WALCL − WTREGEN − RRPONTSYD` in billions, charted on a second
  axis against the S&P 500. Overnight reverse repo is drawn on that card.
- SOFR minus IORB: secured overnight financing rate minus interest on reserve balances
- Chicago Fed NFCI: `NFCI`, weekly financial conditions index
- US Dollar Index: `DTWEXBGS`, or ICE `DX-Y.NYB` from Yahoo when that print is
  more than five days newer
- Dollar vs Advanced Economies: `DTWEXAFEGS`
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

The Semis tab ranks a fixed US-listed universe at build time. Market cap is
the latest SEC share count times the Yahoo price. TSMC's ADR is five ordinary
shares, so that count is divided by five and EPS is multiplied by five. ASML's
ADR is one share; euro EPS is converted with FRED `DEXUSEU`. Quarterly diluted
EPS from EDGAR is summed into a trailing twelve months when four consecutive
quarters exist. A missing fiscal fourth quarter is the full year minus the
other three. Names that only file annual EPS, such as TSMC's 20-F, carry that
full-year figure forward. Splits after the filing date scale older EPS onto
today's share count. Negative or missing EPS leaves a gap instead of a negative
P/E. A failed fetch keeps the previous company file.

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
python build_data.py --timeout 30 --workers 6
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

`.github/workflows/refresh-data.yml` runs every day at 13:17 UTC and whenever
someone starts it by hand. It installs `requirements.txt`, runs
`python build_data.py --timeout 30`, and commits `site/data` only when a file changed.
GitHub often creates that scheduled run hours after 13:17 UTC when Actions is
busy. The 2 Nov 2026 run was created at 20:51 UTC, about nine hours after the
old 11:30 cron, and it started immediately once the event existed. That delay
is GitHub's scheduler, not a timezone setting. **Actions → Refresh data →
Run workflow** on `main` starts a refresh immediately.

FRED is read from the official API when `FRED_API_KEY` is set, and from the
public CSV otherwise. The 2 Nov run used a 12 second CSV timeout and every
FRED series timed out, so the 10-year yield stayed at 29 Oct and the 10Y–2Y
spread at 30 Oct. The job now waits longer and retries with backoff. To use
the API:

1. Request a free key at <https://fredaccount.stlouisfed.org/apikeys>.
2. On GitHub, open **Settings → Secrets and variables → Actions**.
3. Choose **New repository secret**. Name it `FRED_API_KEY` and paste the key.
4. Run **Actions → Refresh data → Run workflow** on `main`.

The key is only needed in that Actions secret. Vercel does not fetch FRED.
A push made with the workflow token does not start another workflow, so the
same run also deploys GitHub Pages.

FRED requests stay at two at a time. The 3 Nov 2026 scheduled run opened six
CSV connections together from the GitHub runner. Every one raised `ReadTimeout`
at 30 seconds, including the last pair, while every Yahoo request on that
runner succeeded. `fred.stlouisfed.org` is served by Akamai and was accepting
the connection without sending a body. A browser User-Agent made the same CSV
endpoint hang, so the client name stays `macro_indicators/1.0`. The official
API is still used first when `FRED_API_KEY` is set, and a failed API call still
falls back to the CSV.

If more than half of the FRED series fail, the script keeps the previous
observations, prints the failed series, and exits with status 2. The workflow
still commits that snapshot and deploys the held-over page, then the job
fails so the run is not reported as a success.

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
(`Cache-Control` on `/data/*`). Reload the browser after a refresh if a chart
still looks old.

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
