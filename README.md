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
- A start date, with the window ending on the snapshot date, plus CSV export and frequency-aware stale warnings.
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

The live site is moving to Vercel, and this repository is becoming private.
Leave this Pages workflow in place until that cutover is finished, then delete
`.github/workflows/pages.yml`. The daily refresh workflow should stay: it
commits new data, and Vercel redeploys that push. The Pages deploy job inside
the refresh workflow can be removed at the same time as `pages.yml`.

### Vercel

1. Go to [vercel.com/new](https://vercel.com/new) and import
   `deanxdeanx/macro_indicators`.
2. Leave the root directory as the repository root. Do not set it to `site`.
3. Framework preset **Other**, install command empty, build command empty,
   output directory `site`. `vercel.json` already sets those. If the import
   screen overrides them, put the values back.
4. Deploy. Static files come from `site/`. Python functions in `api/` are
   deployed with them (`/api/analyze` and `/api/public-config`).
5. Add the environment variables below. The chart and fundamentals work
   without them. The Analysis request panel shows a not-configured message
   until they are set.

Vercel's Git integration deploys `main` on every commit, including the daily
data commit. The data responses are cached for five minutes
(`Cache-Control` on `/data/*`). Reload the browser after a refresh if a chart
still looks old.

### Analysis tab

The Analysis tab is a single-ticker workspace. SPCX (SpaceX Class A) is the
only ticker configured. Prices before the 12 Jun 2026 IPO are dropped because
the symbol was used by another security. The daily `build_data.py` run also
writes `site/data/analysis/SPCX.json`.

What the free sources provided for the committed snapshot:

- Yahoo chart closes from 12 Jun 2026.
- SEC 424B4 for the prospectus overview, the CEO (Elon Musk), and the $135 IPO price.
- SEC 10-Q for Class A and Class B shares outstanding as of 15 Oct 2026. Class A market cap is that share count times the latest close. Class B is not priced, so it is not added in.
- SEC 8-K item 2.02 filings for earnings release dates (4 Aug 2026 and 20 Oct 2026).
- TTM and NTM earnings multiples are **n/a**. Companyfacts has quarterly EPS, but not four back-to-back quarters, and no free source returned a forward estimate.
- Dividend yield is **n/a**. No per-share dividend fact was found, and a zero yield was not assumed.
- Next earnings date is **n/a**. An unconfirmed date from a source would be labeled Expected.
- Yahoo quote pages returned 401, so they were not used.
- Financial Modeling Prep is used only when `FMP_API_KEY` is set. It is optional and was not set for this snapshot.

The request panel does nothing until **Submit request**. `/api/analyze` calls OpenRouter with `OPENROUTER_API_KEY`. Model names and prices live in `analysis/models.json`: a cheap extraction step, then a stronger cheap model for the write-up. The browser never sees that key.

### Supabase

1. Create a Supabase project.
2. In the SQL editor, run `supabase/migrations/001_accounts_and_usage.sql`.
3. Authentication → Providers: enable Email (magic link) and GitHub.
4. For GitHub, create an OAuth app and set the callback to `https://<project-ref>.supabase.co/auth/v1/callback`.
5. Authentication → URL configuration: add the Vercel site URL to the redirect allow list.
6. On Vercel, set:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY` (public; the browser receives it from `/api/public-config`)
   - `SUPABASE_SERVICE_ROLE_KEY` (server only)
   - `OPENROUTER_API_KEY` (server only)
   - `FMP_API_KEY` (optional, server only, used by the data build if you add it to the GitHub Action)

New accounts get a 7-day trial and 3 credits. Each successful analysis spends 1 credit. There is a `pro` tier column and no payment flow. Users can read only their own account and usage rows. Credit changes go through the `spend_credits` function, which only the service role can call.

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
