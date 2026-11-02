const POSITIVE = "#5f9e68";
const NEGATIVE = "#b95751";
const NEUTRAL = "#7892a8";
const SERIES_COLORS = ["#7c91c9", "#a779c9", "#c47da5", "#d08c69", "#d8aa69", "#92a96e", "#5f9e68", "#63a0a0"];
const SNAPSHOT_KEYS = ["^GSPC", "^IXIC", "^VIX", "GC=F"];
const FRESHNESS_DAYS = { Daily: 7, Weekly: 16, Monthly: 75, Quarterly: 200 };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const PLOT_CONFIG = { displaylogo: false, displayModeBar: false, responsive: true };

const state = {
  catalog: null,
  series: new Map(),
  inflight: new Map(),
  start: "",
  end: "",
  tab: "macro",
  selected: new Set(),
  selectionReady: false,
  normalize: true,
};

const statusEl = () => document.getElementById("header-status");
const bannerEl = () => document.getElementById("banner");
const panelEl = () => document.getElementById("panel");
const snapshotEl = () => document.getElementById("snapshot");
const rangeMessageEl = () => document.getElementById("range-message");

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function dataUrl(relativePath) {
  const base = new URL("data/", document.baseURI);
  return new URL(relativePath, base);
}

function todayISO() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function formatLong(iso) {
  if (!iso) return "N/A";
  const [year, month, day] = iso.split("-");
  return `${day} ${MONTHS[Number(month) - 1]} ${year}`;
}

function formatStamp(iso) {
  if (!iso) return "UNKNOWN";
  const [day, time] = iso.split("T");
  return `${formatLong(day).toUpperCase()} · ${(time || "").replace("Z", " UTC")}`;
}

function civilDays(earlier, later) {
  const start = Date.parse(`${earlier}T00:00:00Z`);
  const end = Date.parse(`${later}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return 0;
  return Math.max(0, Math.round((end - start) / 86400000));
}

function compactNumber(value) {
  const digits = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  if (Math.abs(value) >= 1_000_000) {
    return `${(value / 1_000_000).toLocaleString("en-US", digits)}M`;
  }
  if (Math.abs(value) >= 1_000) {
    return `${(value / 1_000).toLocaleString("en-US", digits)}K`;
  }
  return value.toLocaleString("en-US", digits);
}

function fixed(value, digits) {
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatValue(value, unit, compact = false) {
  if (!Number.isFinite(value)) return "N/A";
  if (unit === "$") return `$${compact ? compactNumber(value) : fixed(value, 2)}`;
  if (unit === "$ billions") return `$${fixed(value, 1)}B`;
  if (unit === "%") return `${fixed(value, 2)}%`;
  if (unit === "claims") return Math.round(value).toLocaleString("en-US");
  if (unit === "points" || unit === "index") return fixed(value, 2);
  return compact ? compactNumber(value) : fixed(value, 2);
}

function formatChange(value, changeMode) {
  if (!Number.isFinite(value)) return "N/A";
  const suffix = changeMode === "points" ? " pp" : "%";
  const sign = value >= 0 ? "+" : "-";
  return `${sign}${Math.abs(value).toFixed(2)}${suffix}`;
}

function deltaClass(value) {
  if (!Number.isFinite(value) || value === 0) return "";
  return value > 0 ? "up" : "down";
}

function chartColor(value) {
  if (!Number.isFinite(value) || value === 0) return NEUTRAL;
  return value > 0 ? POSITIVE : NEGATIVE;
}

function rowsThrough(rows, end) {
  const kept = [];
  for (const row of rows || []) {
    if (row[0] <= end) kept.push(row);
  }
  return kept;
}

function rowsBetween(rows, start, end) {
  const kept = [];
  for (const row of rows || []) {
    if (row[0] >= start && row[0] <= end) kept.push(row);
  }
  return kept;
}

function latestMetrics(rows, changeMode) {
  if (!rows.length) {
    return { latest: NaN, previousChange: NaN, ytdChange: NaN, latestDate: null };
  }
  const latest = rows[rows.length - 1][1];
  const latestDate = rows[rows.length - 1][0];
  const year = latestDate.slice(0, 4);
  let firstOfYear = null;
  let yearCount = 0;
  for (const row of rows) {
    if (row[0].slice(0, 4) === year) {
      if (firstOfYear === null) firstOfYear = row[1];
      yearCount += 1;
    }
  }
  let previousChange = NaN;
  let ytdChange = NaN;
  if (changeMode === "points") {
    if (rows.length > 1) previousChange = latest - rows[rows.length - 2][1];
    if (yearCount > 1 && firstOfYear !== null) ytdChange = latest - firstOfYear;
  } else if (rows.length > 1) {
    const previous = rows[rows.length - 2][1];
    if (previous !== 0) previousChange = (latest / previous - 1) * 100;
    if (yearCount > 1 && firstOfYear !== 0 && firstOfYear !== null) {
      ytdChange = (latest / firstOfYear - 1) * 100;
    }
  }
  return { latest, previousChange, ytdChange, latestDate };
}

function freshness(latestDate, frequency, maxAgeDays, asOf) {
  const threshold = Number.isFinite(maxAgeDays) ? maxAgeDays : (FRESHNESS_DAYS[frequency] || 30);
  if (!latestDate) return { ageDays: 0, isStale: true, threshold };
  const ageDays = civilDays(latestDate, asOf);
  return { ageDays, isStale: ageDays > threshold, threshold };
}

function byKey() {
  const map = new Map();
  for (const entry of state.catalog.series) map.set(entry.key, entry);
  return map;
}

function groupEntries(group) {
  return state.catalog.series.filter((entry) => entry.group === group);
}

function entriesFor(tab) {
  const wanted = new Map();
  for (const key of SNAPSHOT_KEYS) {
    const entry = byKey().get(key);
    if (entry) wanted.set(entry.key, entry);
  }
  const grouped = tab === "companies" ? groupEntries("companies") : groupEntries(tab);
  for (const entry of grouped) wanted.set(entry.key, entry);
  return [...wanted.values()];
}

function observationsOf(entry) {
  const doc = state.series.get(entry.key);
  return doc && Array.isArray(doc.observations) ? doc.observations : [];
}

async function loadSeries(entry) {
  if (state.series.has(entry.key)) return state.series.get(entry.key);
  if (!entry.observation_count) {
    state.series.set(entry.key, { observations: [] });
    return state.series.get(entry.key);
  }
  if (state.inflight.has(entry.key)) return state.inflight.get(entry.key);
  const version = encodeURIComponent(state.catalog.generated_at || "");
  const pending = fetch(`${dataUrl(entry.file).href}?v=${version}`, { cache: "no-cache" })
    .then((response) => {
      if (!response.ok) throw new Error(`The saved file for ${entry.key} returned HTTP ${response.status}.`);
      return response.json();
    })
    .then((doc) => {
      state.series.set(entry.key, doc);
      state.inflight.delete(entry.key);
      return doc;
    })
    .catch((error) => {
      state.inflight.delete(entry.key);
      const failed = { observations: [], loadError: error.message || String(error) };
      state.series.set(entry.key, failed);
      return failed;
    });
  state.inflight.set(entry.key, pending);
  return pending;
}

async function ensure(entries) {
  await Promise.all(entries.map((entry) => loadSeries(entry)));
}

function showFatal(error) {
  statusEl().textContent = "Snapshot unavailable";
  panelEl().innerHTML = `<p class="alert bad">${escapeHtml(error.message || String(error))}</p>`;
}

function setRangeFromCatalog() {
  const today = todayISO();
  const startInput = document.getElementById("range-start");
  const endInput = document.getElementById("range-end");
  const min = state.catalog.history_start;
  const max = today > state.catalog.history_end ? today : state.catalog.history_end;
  for (const input of [startInput, endInput]) {
    input.min = min;
    input.max = max;
  }
  state.start = state.catalog.default_start || min;
  state.end = today >= min ? today : state.catalog.history_end;
  if (state.start > state.end) state.start = min;
  startInput.value = state.start;
  endInput.value = state.end;
}

function readRange() {
  const start = document.getElementById("range-start").value;
  const end = document.getElementById("range-end").value;
  const message = rangeMessageEl();
  if (!start || !end) {
    message.hidden = false;
    message.textContent = "Select both a start and end date.";
    return false;
  }
  if (start > end) {
    message.hidden = false;
    message.textContent = "The start date must be before the end date.";
    return false;
  }
  message.hidden = true;
  state.start = start;
  state.end = end;
  return true;
}

function renderBanners() {
  const today = todayISO();
  const pieces = [];
  const preserved = state.catalog.series.filter((entry) => entry.status === "preserved");
  const missing = state.catalog.series.filter((entry) => entry.status === "error" || !entry.observation_count);
  const builtOn = (state.catalog.generated_at || "").slice(0, 10);
  if (builtOn && civilDays(builtOn, today) > 3) {
    pieces.push(`<p class="alert warn">This snapshot was built ${formatStamp(state.catalog.generated_at)}. The scheduled refresh may not have run.</p>`);
  }
  if (preserved.length) {
    pieces.push(`<p class="alert warn">${preserved.length} series kept their previous values because the latest refresh failed.</p>`);
  }
  if (missing.length) {
    pieces.push(`<p class="alert bad">${missing.length} series have no saved observations.</p>`);
  }
  bannerEl().innerHTML = pieces.join("");
}

function snapshotTitle(name) {
  return name.replace(" Index", "").replace(" Futures", "");
}

function renderSnapshot() {
  const entries = SNAPSHOT_KEYS.map((key) => byKey().get(key)).filter(Boolean);
  const ready = entries.filter((entry) => observationsOf(entry).length);
  if (!ready.length) {
    snapshotEl().innerHTML = `
      <div class="eyebrow">Live overview</div>
      <h2 class="section-title">Market snapshot</h2>
      <p class="section-copy">Latest observations and previous-period moves across major risk assets.</p>
      <p class="alert warn">Market snapshot is unavailable because no market price data was returned.</p>`;
    return;
  }
  const cards = entries.map((entry) => {
    const rows = rowsThrough(observationsOf(entry), state.end);
    if (!rows.length) {
      return `<article class="metric"><span class="kicker">${escapeHtml(snapshotTitle(entry.name))}</span><span class="value">N/A</span></article>`;
    }
    const metrics = latestMetrics(rows, entry.change_mode);
    return `
      <article class="metric">
        <span class="kicker">${escapeHtml(snapshotTitle(entry.name))}</span>
        <span class="value">${escapeHtml(formatValue(metrics.latest, entry.unit, true))}</span>
        <span class="delta ${deltaClass(metrics.previousChange)}">${escapeHtml(formatChange(metrics.previousChange, entry.change_mode))}</span>
      </article>`;
  }).join("");
  snapshotEl().innerHTML = `
    <div class="eyebrow">Live overview</div>
    <h2 class="section-title">Market snapshot</h2>
    <p class="section-copy">Latest observations and previous-period moves across major risk assets.</p>
    <div class="snapshot-grid">${cards}</div>`;
}

function sectionCopy(tab) {
  if (tab === "markets") {
    return ["Cross-asset monitor", "Markets", "A compact view of US equities, volatility, crypto, energy, and precious metals."];
  }
  if (tab === "companies") {
    return ["Large-cap monitor", "Top companies", "Representative basket of leading US-listed companies. Rankings change over time."];
  }
  return ["US economic regime", "Macro indicators", "Growth, inflation, labor, liquidity, rates, and the yield curve in one analytical grid."];
}

function chartElementId(entry) {
  const stem = (entry.file || entry.key).replace(/^series\//, "").replace(/\.json$/, "");
  return `chart-${stem.replace(/[^A-Za-z0-9_-]/g, "")}`;
}

const AXIS_FONT = { family: "Inter, ui-sans-serif, system-ui, sans-serif", color: "#d5dddd", size: 13 };
const GRID = "#2a3434";

function chartNarrow() {
  return window.innerWidth <= 800;
}

function chartHeight(kind) {
  const narrow = chartNarrow();
  if (kind === "comparison") return narrow ? 520 : 500;
  return narrow ? 268 : 300;
}

function abbreviate(value, digits) {
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  const tiers = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "k"]];
  for (const [scale, suffix] of tiers) {
    if (abs >= scale) {
      const scaled = abs / scale;
      const places = scaled >= 100 ? 0 : digits;
      const text = scaled.toFixed(places).replace(/\.0+$/, "");
      return `${sign}${text}${suffix}`;
    }
  }
  return `${sign}${Math.round(abs).toLocaleString("en-US")}`;
}

function groupedNumber(value, digits) {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatYTick(value, unit, step) {
  if (!Number.isFinite(value)) return "";
  if (unit === "%") {
    const wholeStep = Math.abs(step - Math.round(step)) < 1e-8;
    const digits = step >= 1 && wholeStep ? 0 : step >= 0.1 ? 1 : 2;
    return `${value.toFixed(digits)}%`;
  }
  if (unit === "$ billions") return `$${abbreviate(value * 1e9, 1)}`;
  if (unit === "$") {
    if (Math.abs(value) >= 1_000_000) return `$${abbreviate(value, 1)}`;
    if (step >= 1 || Math.abs(value) >= 100) return `$${groupedNumber(value, 0)}`;
    return `$${groupedNumber(value, step >= 0.1 ? 1 : 2)}`;
  }
  if (unit === "claims") {
    return Math.abs(value) >= 1_000_000 ? abbreviate(value, 1) : groupedNumber(value, 0);
  }
  if (unit === "index") {
    if (Math.abs(value) >= 10_000) return abbreviate(value, 1);
    return groupedNumber(value, step >= 1 ? 0 : 1);
  }
  return Math.abs(value) >= 1_000_000 ? abbreviate(value, 1) : groupedNumber(value, 0);
}

function formatHoverValue(value, unit) {
  if (!Number.isFinite(value)) return "N/A";
  if (unit === "%") return `${groupedNumber(value, 2)}%`;
  if (unit === "$") return `$${groupedNumber(value, 2)}`;
  if (unit === "$ billions") return `$${groupedNumber(value, 1)}B`;
  if (unit === "claims") return groupedNumber(value, 0);
  if (unit === "index") return `${groupedNumber(value, 1)} index`;
  return groupedNumber(value, 2);
}

function niceStep(span, count) {
  if (!(span > 0)) return 1;
  const rough = span / Math.max(2, count - 1);
  const exp = Math.floor(Math.log10(rough));
  const base = rough / 10 ** exp;
  const nice = base <= 1 ? 1 : base <= 2 ? 2 : base <= 5 ? 5 : 10;
  return nice * 10 ** exp;
}

function yExtent(values) {
  const finite = values.filter((value) => Number.isFinite(value));
  if (!finite.length) return { low: 0, high: 1 };
  let min = Math.min(...finite);
  let max = Math.max(...finite);
  if (min === max) {
    const pad = Math.abs(min) * 0.12 || 1;
    return { low: min - pad, high: max + pad };
  }
  const span = max - min;
  return { low: min - span * 0.08, high: max + span * 0.14 };
}

function ticksBetween(low, high, step) {
  const ticks = [];
  if (!(step > 0)) return ticks;
  const start = Math.ceil((low - step * 1e-8) / step) * step;
  for (let value = start; value <= high + step * 0.001; value += step) {
    ticks.push(Number(value.toFixed(6)));
    if (ticks.length > 8) break;
  }
  return ticks;
}

function shrinkStep(step) {
  const exp = Math.floor(Math.log10(step));
  const base = step / 10 ** exp;
  const smaller = base > 5 ? 5 : base > 2 ? 2 : base > 1 ? 1 : 0.5;
  return smaller * 10 ** exp;
}

function yTicks(values, count) {
  const { low, high } = yExtent(values);
  let step = niceStep(high - low, count);
  let ticks = ticksBetween(low, high, step);
  if (ticks.length < 3) {
    step = shrinkStep(step);
    ticks = ticksBetween(low, high, step);
  }
  return { ticks, step, low, high };
}

function dateTicks(dates, narrow) {
  if (!dates.length) return [];
  const startMs = Date.parse(`${dates[0]}T00:00:00Z`);
  const endMs = Date.parse(`${dates[dates.length - 1]}T00:00:00Z`);
  const days = Math.max(1, Math.round((endMs - startMs) / 86400000));
  const count = narrow ? 4 : days > 1400 ? 6 : 5;
  const style = days <= 100 ? "day" : "month";
  const ticks = [];
  const seen = new Set();
  for (let index = 0; index < count; index += 1) {
    const ms = count === 1 ? startMs : startMs + ((endMs - startMs) * index) / (count - 1);
    const iso = new Date(ms).toISOString().slice(0, 10);
    const month = MONTHS[Number(iso.slice(5, 7)) - 1];
    const label = style === "day" ? `${month} ${Number(iso.slice(8, 10))}` : `${month} ${iso.slice(0, 4)}`;
    if (seen.has(label)) continue;
    seen.add(label);
    ticks.push({ iso, label });
  }
  return ticks;
}

function baseLayout(height, topMargin) {
  return {
    height,
    margin: { l: 12, r: 16, t: topMargin, b: 12 },
    hovermode: "x unified",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: AXIS_FONT,
    hoverlabel: {
      bgcolor: "#161b1b",
      bordercolor: "#3a4646",
      font: { family: AXIS_FONT.family, color: "#f2f6f6", size: 13 },
    },
  };
}

function xAxis(dates) {
  const ticks = dateTicks(dates, chartNarrow());
  return {
    type: "date",
    tickmode: "array",
    tickvals: ticks.map((tick) => tick.iso),
    ticktext: ticks.map((tick) => tick.label),
    tickfont: AXIS_FONT,
    gridcolor: GRID,
    zeroline: false,
    showline: false,
    automargin: true,
    ticklabelstandoff: 8,
    ticklabeloverflow: "allow",
    hoverformat: "%b %d, %Y",
  };
}

function yAxis(values, unit, title) {
  const { ticks, step, low, high } = yTicks(values, chartNarrow() ? 4 : 5);
  const axis = {
    side: "right",
    range: [low, high],
    autorange: false,
    tickmode: "array",
    tickvals: ticks,
    ticktext: ticks.map((value) => formatYTick(value, unit, step)),
    tickfont: AXIS_FONT,
    gridcolor: GRID,
    zeroline: false,
    showline: false,
    automargin: true,
    ticklabelstandoff: 10,
    ticklabeloverflow: "allow",
  };
  if (title) {
    axis.title = { text: title, font: AXIS_FONT, standoff: 12 };
  }
  return axis;
}

function drawLine(entry, visible, metrics) {
  const node = document.getElementById(chartElementId(entry));
  if (!node || typeof Plotly === "undefined") return;
  const color = chartColor(metrics.previousChange);
  const useFill = entry.change_mode === "percent";
  const trace = {
    x: visible.map((row) => row[0]),
    y: visible.map((row) => row[1]),
    customdata: visible.map((row) => formatHoverValue(row[1], entry.unit)),
    type: "scatter",
    mode: "lines",
    line: { color, width: 1.6 },
    hovertemplate: `%{customdata}<extra>${escapeHtml(entry.name)}</extra>`,
  };
  if (useFill) {
    trace.fill = "tozeroy";
    trace.fillcolor = color === POSITIVE
      ? "rgba(95, 158, 104, 0.05)"
      : color === NEGATIVE
        ? "rgba(185, 87, 81, 0.05)"
        : "rgba(120, 146, 168, 0.04)";
  }
  const layout = {
    ...baseLayout(chartHeight("line"), 10),
    showlegend: false,
    xaxis: xAxis(visible.map((row) => row[0])),
    yaxis: yAxis(visible.map((row) => row[1]), entry.unit),
  };
  Plotly.react(node, [trace], layout, PLOT_CONFIG);
}

function drawComparison(seriesList) {
  const node = document.getElementById("comparison-chart");
  if (!node || typeof Plotly === "undefined" || !seriesList.length) return;
  const dates = seriesList[0].rows.map((row) => row[0]);
  const values = seriesList.flatMap((item) => item.rows.map((row) => row[1]));
  const traces = seriesList.map((item, index) => ({
    x: item.rows.map((row) => row[0]),
    y: item.rows.map((row) => row[1]),
    customdata: item.rows.map((row) => formatHoverValue(row[1], "index")),
    type: "scatter",
    mode: "lines",
    name: item.name,
    line: { width: 1.7, color: SERIES_COLORS[index % SERIES_COLORS.length] },
    hovertemplate: "%{customdata}<extra>%{fullData.name}</extra>",
  }));
  const layout = {
    ...baseLayout(chartHeight("comparison"), chartNarrow() ? 78 : 56),
    showlegend: true,
    legend: {
      orientation: "h",
      yanchor: "bottom",
      y: 1.12,
      xanchor: "left",
      x: 0,
      font: { family: AXIS_FONT.family, size: 13, color: "#e7eeee" },
      bgcolor: "rgba(0,0,0,0)",
      itemwidth: 40,
    },
    xaxis: xAxis(dates),
    yaxis: yAxis(values, "index", "Index"),
  };
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

function cardMarkup(entry) {
  const doc = state.series.get(entry.key);
  if (doc && doc.loadError) {
    return `<article class="card"><h3>${escapeHtml(entry.name)}</h3><p class="alert bad">${escapeHtml(doc.loadError)}</p></article>`;
  }
  const hasObservations = observationsOf(entry).length > 0;
  if (!hasObservations) {
    const reason = entry.last_error || "No data returned.";
    return `<article class="card"><h3>${escapeHtml(entry.name)}</h3><p class="alert bad">${escapeHtml(entry.name)}: ${escapeHtml(reason)}</p></article>`;
  }
  const history = rowsThrough(observationsOf(entry), state.end);
  const visible = rowsBetween(observationsOf(entry), state.start, state.end);
  if (!visible.length) {
    return `<article class="card"><p class="alert warn">No ${escapeHtml(entry.name)} data is available in the selected date range.</p></article>`;
  }
  const metrics = latestMetrics(history, entry.change_mode);
  const age = freshness(metrics.latestDate, entry.frequency, entry.max_age_days, todayISO());
  const description = entry.description
    ? `<p class="desc">${escapeHtml(entry.description)}</p>`
    : "";
  const warnings = [];
  if (entry.status === "preserved" && entry.last_error) {
    const reason = String(entry.last_error).replace(/\.+$/, "");
    const fetched = entry.fetched_at ? ` Showing data fetched ${formatStamp(entry.fetched_at)}.` : "";
    warnings.push(`<p class="alert warn">Latest refresh failed (${escapeHtml(reason)}).${fetched}</p>`);
  }
  if (age.isStale) {
    warnings.push(`<p class="alert warn">Latest observation is ${age.ageDays} days old; this may be stale for ${escapeHtml(entry.frequency.toLowerCase())} data.</p>`);
  }
  return `
    <article class="card">
      <div class="card-top">
        <div>
          <h3>${escapeHtml(entry.name)}</h3>
          <p class="meta">${escapeHtml(entry.key)} · ${escapeHtml(entry.frequency)} · ${escapeHtml(entry.source)}</p>
          ${description}
        </div>
        <div class="latest">
          <span class="kicker">Latest</span>
          <span class="value">${escapeHtml(formatValue(metrics.latest, entry.unit, true))}</span>
          <span class="delta ${deltaClass(metrics.previousChange)}">${escapeHtml(formatChange(metrics.previousChange, entry.change_mode))}</span>
        </div>
      </div>
      <div class="chart" id="${chartElementId(entry)}"></div>
      <div class="card-foot">
        <span>YTD ${escapeHtml(formatChange(metrics.ytdChange, entry.change_mode))}</span>
        <span>AS OF ${escapeHtml(formatLong(metrics.latestDate).toUpperCase())} · ${age.ageDays}D OLD</span>
        <button class="btn" type="button" data-export="${escapeHtml(entry.key)}">Export CSV</button>
      </div>
      ${warnings.join("")}
    </article>`;
}

function bindCards(entries) {
  for (const entry of entries) {
    const history = rowsThrough(observationsOf(entry), state.end);
    const visible = rowsBetween(observationsOf(entry), state.start, state.end);
    if (!visible.length) continue;
    drawLine(entry, visible, latestMetrics(history, entry.change_mode));
  }
  panelEl().querySelectorAll("[data-export]").forEach((button) => {
    button.addEventListener("click", () => {
      const entry = byKey().get(button.dataset.export);
      if (!entry) return;
      const visible = rowsBetween(observationsOf(entry), state.start, state.end);
      exportCsv(entry, visible);
    });
  });
}

function csvField(value) {
  const text = String(value);
  if (/[",\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
  return text;
}

function exportCsv(entry, rows) {
  const lines = [`date,${csvField(entry.name)}`];
  for (const [day, value] of rows) lines.push(`${day},${value}`);
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${entry.key}_data.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

function companyToolbar(entries) {
  if (!state.selectionReady) {
    state.selected = new Set(entries.slice(0, 5).map((entry) => entry.key));
    state.selectionReady = true;
  }
  const chips = entries.map((entry) => {
    const pressed = state.selected.has(entry.key) ? "true" : "false";
    const label = entry.name.replace(/\s+\([^)]+\)$/, "");
    return `<button class="chip" type="button" data-ticker="${escapeHtml(entry.key)}" aria-pressed="${pressed}">${escapeHtml(label)}</button>`;
  }).join("");
  return `
    <div class="toolbar">
      <div class="chips" role="group" aria-label="Companies">${chips}</div>
      <label class="check"><input id="normalize" type="checkbox" ${state.normalize ? "checked" : ""}> Normalize to 100</label>
    </div>`;
}

function comparisonMarkup(entries) {
  if (!state.normalize) return "";
  const normalized = [];
  for (const entry of entries) {
    if (!state.selected.has(entry.key)) continue;
    const visible = rowsBetween(observationsOf(entry), state.start, state.end);
    if (!visible.length || visible[0][1] === 0) continue;
    const base = visible[0][1];
    normalized.push({
      name: entry.name,
      rows: visible.map(([day, value]) => [day, (value / base) * 100]),
    });
  }
  if (!normalized.length) return "";
  return `
    <article class="card">
      <h3>Relative performance</h3>
      <p class="meta">Normalized · start of selected window = 100</p>
      <div class="chart comparison" id="comparison-chart"></div>
    </article>`;
}

function renderPanel() {
  const [kicker, title, copy] = sectionCopy(state.tab);
  const heading = `
    <div class="eyebrow">${kicker}</div>
    <h2 class="section-title">${title}</h2>
    <p class="section-copy">${copy}</p>`;
  const libraryNote = typeof Plotly === "undefined"
    ? `<p class="alert warn">The chart library did not load. Values are still shown, but the lines cannot be drawn.</p>`
    : "";
  purgeCharts(panelEl());
  if (state.tab === "companies") {
    const entries = groupEntries("companies");
    if (!state.selectionReady) {
      state.selected = new Set(entries.slice(0, 5).map((entry) => entry.key));
      state.selectionReady = true;
    }
    const selected = entries.filter((entry) => state.selected.has(entry.key));
    const grid = selected.length
      ? `<div class="card-grid">${selected.map(cardMarkup).join("")}</div>`
      : `<p class="message">Select at least one company to display its chart.</p>`;
    panelEl().innerHTML = `${heading}${companyToolbar(entries)}${libraryNote}${comparisonMarkup(entries)}${grid}`;
    bindCards(selected);
    if (state.normalize) {
      const normalized = [];
      for (const entry of selected) {
        const visible = rowsBetween(observationsOf(entry), state.start, state.end);
        if (!visible.length || visible[0][1] === 0) continue;
        const base = visible[0][1];
        normalized.push({
          name: entry.name,
          rows: visible.map(([day, value]) => [day, (value / base) * 100]),
        });
      }
      drawComparison(normalized);
    }
    panelEl().querySelectorAll("[data-ticker]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.ticker;
        if (state.selected.has(key)) state.selected.delete(key);
        else state.selected.add(key);
        draw();
      });
    });
    const normalize = document.getElementById("normalize");
    if (normalize) {
      normalize.addEventListener("change", () => {
        state.normalize = normalize.checked;
        draw();
      });
    }
    return;
  }

  const entries = groupEntries(state.tab);
  panelEl().innerHTML = `${heading}${libraryNote}<div class="card-grid">${entries.map(cardMarkup).join("")}</div>`;
  bindCards(entries);
}

function purgeCharts(root) {
  if (!root || typeof Plotly === "undefined") return;
  root.querySelectorAll(".chart").forEach((node) => {
    try { Plotly.purge(node); } catch (error) { /* detached plot */ }
  });
}

function draw() {
  if (!state.catalog || !readRange()) return;
  const scrollY = window.scrollY;
  renderBanners();
  renderSnapshot();
  renderPanel();
  window.scrollTo(0, scrollY);
  const endLabel = formatLong(state.end);
  statusEl().textContent = `Observation window through · ${endLabel}`;
  document.getElementById("snapshot-note").textContent = `Daily snapshot · updated ${formatStamp(state.catalog.generated_at)}`;
}

function selectTab(tab, options = {}) {
  state.tab = tab;
  if (!options.skipHash) history.replaceState(null, "", `#${tab}`);
  document.querySelectorAll("[role='tab']").forEach((button) => {
    const selected = button.dataset.tab === tab;
    button.setAttribute("aria-selected", selected ? "true" : "false");
    button.tabIndex = selected ? 0 : -1;
  });
}

async function activateTab(tab) {
  selectTab(tab);
  statusEl().textContent = "Loading series…";
  await ensure(entriesFor(tab));
  draw();
}

function bindChrome() {
  document.getElementById("range-start").addEventListener("change", draw);
  document.getElementById("range-end").addEventListener("change", draw);
  document.getElementById("reload").addEventListener("click", () => window.location.reload());
  document.getElementById("controls").addEventListener("submit", (event) => event.preventDefault());
  const tabs = [...document.querySelectorAll("[role='tab']")];
  tabs.forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(button);
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : (index - 1 + tabs.length) % tabs.length;
        tabs[next].focus();
        activateTab(tabs[next].dataset.tab);
      }
    });
  });
}

async function init() {
  bindChrome();
  const hash = location.hash.replace("#", "");
  if (hash === "markets" || hash === "companies" || hash === "macro") {
    selectTab(hash, { skipHash: true });
  }
  try {
    const response = await fetch(dataUrl("catalog.json"), { cache: "no-cache" });
    if (!response.ok) throw new Error(`Could not load data/catalog.json (HTTP ${response.status}).`);
    state.catalog = await response.json();
  } catch (error) {
    showFatal(error);
    return;
  }
  setRangeFromCatalog();
  statusEl().textContent = "Loading series…";
  await ensure(entriesFor(state.tab));
  draw();
  const rest = state.catalog.series.filter((entry) => !state.series.has(entry.key));
  ensure(rest);
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => showFatal(error));
});
