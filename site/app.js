const POSITIVE = "#5f9e68";
const NEGATIVE = "#b95751";
const NEUTRAL = "#7892a8";
const SERIES_COLORS = ["#7c91c9", "#a779c9", "#c47da5", "#d08c69", "#d8aa69", "#92a96e", "#5f9e68", "#63a0a0"];
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

const asOfEl = () => document.getElementById("as-of");
const bannerEl = () => document.getElementById("banner");
const panelEl = () => document.getElementById("panel");
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

function formatClaimsLevel(value) {
  if (!Number.isFinite(value)) return "N/A";
  const sign = value < 0 ? "-" : "";
  const magnitude = Math.abs(value);
  if (magnitude >= 1000) {
    const thousands = magnitude / 1000;
    const text = thousands >= 100
      ? thousands.toFixed(0)
      : thousands.toFixed(1).replace(/\.0$/, "");
    return `${sign}${text}K`;
  }
  return `${sign}${magnitude.toFixed(0)}`;
}

function formatClaimsChange(value) {
  if (!Number.isFinite(value)) return "N/A";
  const text = formatClaimsLevel(value);
  return value > 0 ? `+${text}` : text;
}

function formatValue(value, unit, compact = false) {
  if (!Number.isFinite(value)) return "N/A";
  if (unit === "$") return `$${compact ? compactNumber(value) : fixed(value, 2)}`;
  if (unit === "$ billions") return `$${fixed(value, 1)}B`;
  if (unit === "%") return `${fixed(value, 2)}%`;
  if (unit === "claims") return formatClaimsLevel(value);
  if (unit === "points" || unit === "index") return fixed(value, 2);
  return compact ? compactNumber(value) : fixed(value, 2);
}

function formatChange(value, changeMode) {
  if (!Number.isFinite(value)) return "N/A";
  if (changeMode === "count") return formatClaimsChange(value);
  const suffix = changeMode === "points" ? " pp" : "%";
  const sign = value >= 0 ? "+" : "-";
  return `${sign}${Math.abs(value).toFixed(2)}${suffix}`;
}

function moveIsBad(value, higherIsBad) {
  if (!Number.isFinite(value) || value === 0) return null;
  const rising = value > 0;
  return higherIsBad ? rising : !rising;
}

function deltaClass(value, higherIsBad = false) {
  const bad = moveIsBad(value, higherIsBad);
  if (bad === null) return "";
  return bad ? "down" : "up";
}

function chartColor(value, higherIsBad = false) {
  const bad = moveIsBad(value, higherIsBad);
  if (bad === null) return NEUTRAL;
  return bad ? NEGATIVE : POSITIVE;
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
  if (changeMode === "points" || changeMode === "count") {
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
  return tab === "companies" ? groupEntries("companies") : groupEntries(tab);
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
  const note = asOfEl();
  if (note) note.textContent = "Snapshot unavailable";
  panelEl().innerHTML = `<p class="alert bad">${escapeHtml(error.message || String(error))}</p>`;
}

function snapshotAsOf() {
  const generated = (state.catalog.generated_at || "").slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(generated)) return generated;
  return state.catalog.history_end;
}

function setRangeFromCatalog() {
  const startInput = document.getElementById("range-start");
  const min = state.catalog.history_start;
  state.end = snapshotAsOf();
  const max = state.end;
  startInput.min = min;
  startInput.max = max;
  state.start = state.catalog.default_start || min;
  if (state.start > state.end) state.start = min;
  startInput.value = state.start;
}

function readRange() {
  const start = document.getElementById("range-start").value;
  const end = snapshotAsOf();
  state.end = end;
  const message = rangeMessageEl();
  if (!start) {
    message.hidden = false;
    message.textContent = "Select a start date.";
    return false;
  }
  if (start > end) {
    message.hidden = false;
    message.textContent = "The start date must be on or before the snapshot date.";
    return false;
  }
  message.hidden = true;
  state.start = start;
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
  if (unit === "claims") return formatClaimsLevel(value);
  if (unit === "index") return `${groupedNumber(value, 1)} (start = 100)`;
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

function yScale(values, count) {
  const finite = values.filter((value) => Number.isFinite(value));
  if (!finite.length) return { ticks: [0], step: 1, low: 0, high: 1 };
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = Math.max(max - min, Math.abs(max) * 0.08, 1);
  let step = niceStep(span, count);
  let ticks = ticksBetween(min, max, step);
  if (ticks.length < 3) {
    step = shrinkStep(step);
    ticks = ticksBetween(min, max, step);
  }
  if (min > 0) ticks = ticks.filter((tick) => tick !== 0);
  if (!ticks.length) ticks = [min, max];
  const first = ticks[0];
  const last = ticks[ticks.length - 1];
  return {
    ticks,
    step,
    low: Math.min(min - span * 0.08, first - step * 0.6),
    high: Math.max(max + span * 0.12, last + step * 0.28),
  };
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
    margin: { l: 16, r: 20, t: topMargin, b: 52 },
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

function yAxis(values, unit) {
  const { ticks, step, low, high } = yScale(values, chartNarrow() ? 4 : 5);
  return {
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
    ticklabelstandoff: 12,
    ticklabeloverflow: "allow",
  };
}

function zeroLine(low, high, unit) {
  if (unit !== "%" || !(low < 0 && high > 0)) return [];
  return [{
    type: "line",
    xref: "paper",
    x0: 0,
    x1: 1,
    yref: "y",
    y0: 0,
    y1: 0,
    layer: "below",
    line: { color: "#8fa3a3", width: 1.25 },
  }];
}

function drawLine(entry, visible, change) {
  const node = document.getElementById(chartElementId(entry));
  if (!node || typeof Plotly === "undefined") return;
  const color = chartColor(change, entry.higher_is_bad);
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
  const yValues = visible.map((row) => row[1]);
  const y = yAxis(yValues, entry.unit);
  const layout = {
    ...baseLayout(chartHeight("line"), 14),
    showlegend: false,
    xaxis: xAxis(visible.map((row) => row[0])),
    yaxis: y,
    shapes: zeroLine(y.range[0], y.range[1], entry.unit),
  };
  Plotly.react(node, [trace], layout, PLOT_CONFIG);
}

function drawDual(entry, visible, overlay, overlayName, overlayUnit) {
  const node = document.getElementById(chartElementId(entry));
  if (!node || typeof Plotly === "undefined") return;
  const layout = {
    ...baseLayout(chartNarrow() ? 320 : 360, 48),
    showlegend: true,
    legend: {
      orientation: "h",
      yanchor: "bottom",
      y: 1.02,
      x: 0,
      font: { family: AXIS_FONT.family, size: 13, color: "#e7eeee" },
    },
    xaxis: xAxis(visible.map((row) => row[0])),
    yaxis: { ...yAxis(visible.map((row) => row[1]), entry.unit), side: "left" },
    yaxis2: {
      ...yAxis(overlay.map((row) => row[1]), overlayUnit),
      overlaying: "y",
      side: "right",
    },
  };
  Plotly.react(node, [
    {
      x: visible.map((row) => row[0]),
      y: visible.map((row) => row[1]),
      type: "scatter",
      mode: "lines",
      name: entry.name,
      line: { color: "#7c91c9", width: 1.7 },
      hovertemplate: `%{customdata}<extra>${escapeHtml(entry.name)}</extra>`,
      customdata: visible.map((row) => formatHoverValue(row[1], entry.unit)),
    },
    {
      x: overlay.map((row) => row[0]),
      y: overlay.map((row) => row[1]),
      type: "scatter",
      mode: "lines",
      name: overlayName,
      yaxis: "y2",
      line: { color: "#d08c69", width: 1.4 },
      hovertemplate: `%{customdata}<extra>${escapeHtml(overlayName)}</extra>`,
      customdata: overlay.map((row) => formatHoverValue(row[1], overlayUnit)),
    },
  ], layout, PLOT_CONFIG);
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
    yaxis: yAxis(values, "index"),
    annotations: [{
      xref: "paper",
      yref: "paper",
      x: 1,
      y: 1.01,
      xanchor: "right",
      yanchor: "bottom",
      text: "Index (start = 100)",
      showarrow: false,
      font: { family: AXIS_FONT.family, size: 14, color: "#f2f6f6" },
    }],
  };
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

const REGIME = [
  ["DFII10", "10Y real"],
  ["T10Y2Y", "2s10s"],
  ["BAMLH0A0HYM2", "HY spread"],
  ["DTWEXBGS", "Broad dollar"],
  ["NETLIQ", "Net liquidity"],
  ["^VIX", "VIX"],
];

function dayMs(iso) {
  return Date.parse(`${iso}T00:00:00Z`);
}

function changeOver(rows, days, changeMode) {
  if (!rows || !rows.length) return NaN;
  const latest = rows[rows.length - 1];
  const cutoff = dayMs(latest[0]) - days * 86400000;
  let base = null;
  for (const row of rows) {
    if (dayMs(row[0]) <= cutoff) base = row[1];
  }
  if (base === null) return NaN;
  if (changeMode === "percent") {
    if (base === 0) return NaN;
    return (latest[1] / base - 1) * 100;
  }
  return latest[1] - base;
}

function storedMove(entry, name, rows) {
  if (entry && Object.prototype.hasOwnProperty.call(entry, name)) {
    return Number.isFinite(entry[name]) ? entry[name] : NaN;
  }
  return changeOver(rows, name === "week_change" ? 7 : 30, entry.change_mode);
}

function headlineMove(entry, history) {
  if (entry.frequency === "Daily") {
    const week = storedMove(entry, "week_change", history);
    if (Number.isFinite(week)) return { value: week, label: "1W" };
    const month = storedMove(entry, "month_change", history);
    if (Number.isFinite(month)) return { value: month, label: "1M" };
  }
  return { value: latestMetrics(history, entry.change_mode).previousChange, label: "" };
}

function ordinal(value) {
  const rounded = Math.round(value);
  const mod100 = rounded % 100;
  const suffix = mod100 >= 11 && mod100 <= 13 ? "th" : ["th", "st", "nd", "rd"][rounded % 10] || "th";
  return `${rounded}${suffix}`;
}

function regimeMarkup() {
  const tiles = REGIME.map(([key, label]) => {
    const entry = byKey().get(key);
    if (!entry) {
      return `<article class="regime-tile"><p class="regime-kicker">${escapeHtml(label)}</p><p class="regime-value">N/A</p></article>`;
    }
    const history = rowsThrough(observationsOf(entry), state.end);
    const metrics = history.length ? latestMetrics(history, entry.change_mode) : null;
    const week = storedMove(entry, "week_change", history);
    const month = storedMove(entry, "month_change", history);
    const direction = Number.isFinite(week) ? week : month;
    const bad = moveIsBad(direction, entry.higher_is_bad);
    const tone = bad === null ? "risk-flat" : bad ? "risk-off" : "risk-on";
    const percentile = Object.prototype.hasOwnProperty.call(entry, "range_percentile") && Number.isFinite(entry.range_percentile)
      ? entry.range_percentile
      : NaN;
    const width = Number.isFinite(percentile) ? Math.min(100, Math.max(0, percentile)) : 0;
    const pct = Number.isFinite(percentile) ? `${ordinal(percentile)} pct of 5Y` : "5Y range n/a";
    const latest = metrics ? formatValue(metrics.latest, entry.unit, true) : "N/A";
    return `
      <article class="regime-tile ${tone}">
        <p class="regime-kicker">${escapeHtml(label)}</p>
        <p class="regime-value">${escapeHtml(latest)}</p>
        <p class="regime-moves">
          <span class="delta ${deltaClass(week, entry.higher_is_bad)}">1W ${escapeHtml(formatChange(week, entry.change_mode))}</span>
          <span class="delta ${deltaClass(month, entry.higher_is_bad)}">1M ${escapeHtml(formatChange(month, entry.change_mode))}</span>
        </p>
        <div class="range" role="img" aria-label="${escapeHtml(pct)}"><span class="range-fill" style="width:${width}%"></span></div>
        <p class="regime-pct">${escapeHtml(pct)}</p>
      </article>`;
  }).join("");
  return `<section class="regime" aria-label="Risk regime">${tiles}</section>`;
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
  const headline = headlineMove(entry, history);
  const age = freshness(metrics.latestDate, entry.frequency, entry.max_age_days, todayISO());
  const description = entry.description
    ? `<p class="desc">${escapeHtml(entry.description)}</p>`
    : "";
  const hasOverlay = doc && Array.isArray(doc.overlay) && doc.overlay.length > 0;
  const held = entry.status === "preserved";
  const warnings = [];
  if (held && entry.last_error) {
    const reason = String(entry.last_error).replace(/\.+$/, "");
    const fetched = entry.fetched_at ? ` Showing the last good fetch from ${formatStamp(entry.fetched_at)}.` : "";
    warnings.push(`<p class="alert warn held">Held over from the previous run. Latest refresh failed (${escapeHtml(reason)}).${fetched}</p>`);
  }
  if (age.isStale) {
    warnings.push(`<p class="alert warn">Latest observation is ${age.ageDays} days old; this may be stale for ${escapeHtml(entry.frequency.toLowerCase())} data.</p>`);
  }
  return `
    <article class="card${hasOverlay ? " wide" : ""}">
      <div class="card-top">
        <div>
          <h3>${escapeHtml(entry.name)}${held ? `<span class="held-badge">Held over</span>` : ""}</h3>
          <p class="meta">${escapeHtml(entry.key)} · ${escapeHtml(entry.frequency)} · ${escapeHtml(entry.source)}</p>
          <p class="as-of-line">As of ${escapeHtml(formatLong(metrics.latestDate))}</p>
          ${description}
        </div>
        <div class="latest">
          <span class="kicker">Latest</span>
          <span class="value">${escapeHtml(formatValue(metrics.latest, entry.unit, true))}</span>
          <span class="delta ${deltaClass(headline.value, entry.higher_is_bad)}">${headline.label ? `${escapeHtml(headline.label)} ` : ""}${escapeHtml(formatChange(headline.value, entry.change_mode))}</span>
        </div>
      </div>
      <div class="chart${hasOverlay ? " dual" : ""}" id="${chartElementId(entry)}"></div>
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
    const doc = state.series.get(entry.key);
    const overlay = doc && Array.isArray(doc.overlay) ? rowsBetween(doc.overlay, state.start, state.end) : [];
    if (overlay.length) {
      drawDual(entry, visible, overlay, doc.overlay_name || "Overlay", doc.overlay_unit || "points");
    } else {
      drawLine(entry, visible, headlineMove(entry, history).value);
    }
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
    panelEl().innerHTML = `${companyToolbar(entries)}${libraryNote}${comparisonMarkup(entries)}${grid}`;
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
  if (state.tab !== "macro") {
    panelEl().innerHTML = `${libraryNote}<div class="card-grid">${entries.map(cardMarkup).join("")}</div>`;
    bindCards(entries);
    return;
  }
  const sections = [];
  for (const entry of entries) {
    const name = entry.section || "Macro";
    let section = sections.find((item) => item.name === name);
    if (!section) {
      section = { name, entries: [] };
      sections.push(section);
    }
    section.entries.push(entry);
  }
  panelEl().innerHTML = `${regimeMarkup()}${libraryNote}${sections.map((section) => `
    <section class="macro-section">
      <h2 class="section-title">${escapeHtml(section.name)}</h2>
      <div class="card-grid">${section.entries.map(cardMarkup).join("")}</div>
    </section>`).join("")}`;
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
  renderPanel();
  window.scrollTo(0, scrollY);
  asOfEl().textContent = `Data as of ${formatLong(state.end)}`;
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

function regimeEntries() {
  return REGIME.map(([key]) => byKey().get(key)).filter(Boolean);
}

async function ensureTab(tab) {
  const entries = entriesFor(tab);
  if (tab !== "macro") return ensure(entries);
  return ensure(entries.concat(regimeEntries()));
}

async function activateTab(tab) {
  selectTab(tab);
  await ensureTab(tab);
  draw();
}

function bindChrome() {
  document.getElementById("range-start").addEventListener("change", draw);
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
  asOfEl().textContent = `Data as of ${formatLong(state.end)}`;
  await ensureTab(state.tab);
  draw();
  const rest = state.catalog.series.filter((entry) => !state.series.has(entry.key));
  ensure(rest);
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => showFatal(error));
});
