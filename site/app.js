const POSITIVE = "#3ec1ad";
const NEGATIVE = "#e36b66";
const NEUTRAL = "#8aa8a4";
const LINE_PRIMARY = "#3ec1ad";
const LINE_SECONDARY = "#e8c07a";
const SERIES_COLORS = ["#3ec1ad", "#8fd4c8", "#e8c07a", "#9bb7ff", "#e07a9a", "#c4b5fd", "#f4f7f6", "#6ea8a4"];
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
  if (unit === "ratio") return value.toLocaleString("en-US", { maximumSignificantDigits: 4 });
  if (unit === "contracts") return Math.round(value).toLocaleString("en-US");
  if (unit === "points" || unit === "index") return fixed(value, 2);
  return compact ? compactNumber(value) : fixed(value, 2);
}

function formatChange(value, changeMode) {
  if (!Number.isFinite(value)) return "N/A";
  if (changeMode === "count") return formatClaimsChange(value);
  if (changeMode === "level") {
    const sign = value >= 0 ? "+" : "-";
    return `${sign}${Math.abs(value).toFixed(2)}`;
  }
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
  if (changeMode === "points" || changeMode === "count" || changeMode === "level") {
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

function addDays(iso, days) {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);
}

function addMonths(iso, months) {
  const [year, month, day] = iso.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1 + months, 1));
  const last = new Date(Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, 0)).getUTCDate();
  const result = new Date(Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth(), Math.min(day, last)));
  return result.toISOString().slice(0, 10);
}

function addBusinessDays(iso, count) {
  let current = iso;
  let added = 0;
  while (added < count) {
    current = addDays(current, 1);
    const weekday = new Date(`${current}T00:00:00Z`).getUTCDay();
    if (weekday !== 0 && weekday !== 6) added += 1;
  }
  return current;
}

function releaseDeadline(latest, frequency) {
  if (frequency === "Daily") return addBusinessDays(latest, 2);
  if (frequency === "Weekly") return addDays(latest, 11);
  if (frequency === "Monthly") return addDays(addMonths(latest, 2), 22);
  if (frequency === "Quarterly") return addDays(addMonths(latest, 7), 14);
  return addDays(latest, 30);
}

function releaseIsStale(latest, frequency, asOf, maxAgeDays) {
  if (!latest) return true;
  if (asOf <= releaseDeadline(latest, frequency)) return false;
  const age = civilDays(latest, asOf);
  if (Number.isFinite(maxAgeDays) && age <= maxAgeDays) return false;
  return true;
}

function freshness(latestDate, frequency, maxAgeDays, asOf) {
  const threshold = Number.isFinite(maxAgeDays) ? maxAgeDays : (FRESHNESS_DAYS[frequency] || 30);
  if (!latestDate) return { ageDays: 0, isStale: true, threshold };
  const ageDays = civilDays(latestDate, asOf);
  return { ageDays, isStale: releaseIsStale(latestDate, frequency, asOf, maxAgeDays), threshold };
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
  const through = state.catalog.data_through;
  if (/^\d{4}-\d{2}-\d{2}$/.test(through || "")) return through;
  const dates = [];
  for (const entry of state.catalog.series || []) {
    if (entry.latest_date) dates.push(entry.latest_date);
  }
  if (dates.length) return dates.sort().at(-1);
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
    const names = preserved.map((entry) => entry.key).join(", ");
    pieces.push(`<details class="held-details"><summary>${preserved.length} held over (view)</summary><p>${escapeHtml(names)}</p></details>`);
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

const FONT_STACK = '"Instrument Sans", ui-sans-serif, system-ui, sans-serif';
const AXIS_FONT = { family: FONT_STACK, color: "rgba(255,255,255,0.42)", size: 11 };
const LABEL_FONT = { family: FONT_STACK, color: "rgba(255,255,255,0.72)", size: 12 };

function fillFor(color) {
  if (color === POSITIVE) return "rgba(62, 193, 173, 0.12)";
  if (color === NEGATIVE) return "rgba(227, 107, 102, 0.12)";
  return "rgba(138, 168, 164, 0.10)";
}

function chartLegend(y) {
  return {
    orientation: "h",
    yanchor: "bottom",
    y,
    x: 0,
    bgcolor: "rgba(0,0,0,0)",
    font: LABEL_FONT,
  };
}

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
  if (unit === "ratio") {
    const abs = Math.abs(value);
    const digits = abs >= 1 ? 2 : abs >= 0.01 ? 4 : 5;
    return value.toFixed(digits);
  }
  if (unit === "index") {
    if (Math.abs(value) >= 10_000) return abbreviate(value, 1);
    return groupedNumber(value, step >= 1 ? 0 : 1);
  }
  if (unit === "x") return `${groupedNumber(value, value >= 100 ? 0 : 1)}x`;
  return Math.abs(value) >= 1_000_000 ? abbreviate(value, 1) : groupedNumber(value, 0);
}

function formatHoverValue(value, unit) {
  if (!Number.isFinite(value)) return "N/A";
  if (unit === "%") return `${groupedNumber(value, 2)}%`;
  if (unit === "$") return `$${groupedNumber(value, 2)}`;
  if (unit === "$ billions") return `$${groupedNumber(value, 1)}B`;
  if (unit === "claims") return formatClaimsLevel(value);
  if (unit === "x") return formatPe(value);
  if (unit === "ratio") return value.toLocaleString("en-US", { maximumSignificantDigits: 4 });
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
    margin: { l: 8, r: 8, t: topMargin, b: 36 },
    hovermode: "x unified",
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: AXIS_FONT,
    hoverlabel: {
      bgcolor: "#0c1e1e",
      bordercolor: "rgba(255,255,255,0.12)",
      font: { family: FONT_STACK, color: "#ffffff", size: 12 },
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
    showgrid: false,
    gridcolor: "rgba(0,0,0,0)",
    zeroline: false,
    showline: false,
    showspikes: false,
    automargin: true,
    ticklabelstandoff: 8,
    ticklabeloverflow: "allow",
    hoverformat: "%b %d, %Y",
  };
}

function recentWindow(rows, days) {
  if (!rows.length) return [];
  const end = dayMs(rows[rows.length - 1][0]);
  const start = end - days * 86400000;
  const recent = rows.filter((row) => dayMs(row[0]) >= start);
  return recent.length >= 8 ? recent : rows;
}

function axisRows(entry, visible) {
  if (entry.unit !== "claims") return visible;
  const span = dayMs(visible[visible.length - 1][0]) - dayMs(visible[0][0]);
  if (span <= 730 * 86400000) return visible;
  return recentWindow(visible, 730);
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
    showgrid: false,
    gridcolor: "rgba(0,0,0,0)",
    zeroline: false,
    showline: false,
    showspikes: false,
    automargin: true,
    ticklabelstandoff: 10,
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
    line: { color: "rgba(255,255,255,0.16)", width: 1 },
  }];
}

function drawInset(elementId, rows, unit, name, focusDays = 365) {
  const node = document.getElementById(elementId);
  if (!node || !rows.length || typeof Plotly === "undefined") return;
  const focus = recentWindow(rows, focusDays);
  const y = yAxis(focus.map((row) => row[1]), unit);
  const layout = {
    ...baseLayout(180, 28),
    showlegend: false,
    annotations: [{
      xref: "paper",
      yref: "paper",
      x: 0,
      y: 1.08,
      xanchor: "left",
      yanchor: "bottom",
      text: name,
      showarrow: false,
      font: LABEL_FONT,
    }],
    xaxis: xAxis(rows.map((row) => row[0])),
    yaxis: { ...y, side: "right" },
  };
  Plotly.react(node, [{
    x: rows.map((row) => row[0]),
    y: rows.map((row) => row[1]),
    type: "scatter",
    mode: "lines",
    line: { color: LINE_SECONDARY, width: 1.25 },
    customdata: rows.map((row) => formatHoverValue(row[1], unit)),
    hovertemplate: `%{customdata}<extra>${escapeHtml(name)}</extra>`,
  }], layout, PLOT_CONFIG);
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
    line: { color, width: 1.25 },
    hovertemplate: `%{customdata}<extra>${escapeHtml(entry.name)}</extra>`,
  };
  if (useFill) {
    trace.fill = "tozeroy";
    trace.fillcolor = fillFor(color);
  }
  const yValues = axisRows(entry, visible).map((row) => row[1]);
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
    legend: chartLegend(1.02),
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
      line: { color: LINE_PRIMARY, width: 1.35 },
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
      line: { color: LINE_SECONDARY, width: 1.25 },
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
    line: { width: 1.35, color: SERIES_COLORS[index % SERIES_COLORS.length] },
    hovertemplate: "%{customdata}<extra>%{fullData.name}</extra>",
  }));
  const layout = {
    ...baseLayout(chartHeight("comparison"), chartNarrow() ? 78 : 56),
    showlegend: true,
    legend: { ...chartLegend(1.12), xanchor: "left" },
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
      font: LABEL_FONT,
    }],
  };
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

const REGIME = [
  ["DFII10", "10Y real"],
  ["COPPERGOLD", "Copper/gold"],
  ["T10Y2Y", "2s10s"],
  ["BAMLH0A0HYM2", "HY spread"],
  ["DTWEXBGS", "Broad dollar"],
  ["NETLIQ", "Net liquidity"],
  ["NFCI", "NFCI"],
  ["^VIX", "VIX"],
];
const SECTION_ORDER = [
  "Liquidity",
  "Real rates and credit",
  "Rates and the curve",
  "Dollar",
  "Cross-asset",
  "Growth and labor",
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

function regimeInputsStale() {
  const today = todayISO();
  const present = REGIME.map(([key]) => byKey().get(key)).filter(Boolean);
  if (!present.length) return false;
  let stale = 0;
  for (const entry of present) {
    const held = entry.status === "preserved" || entry.status === "error";
    if (held || releaseIsStale(entry.latest_date, entry.frequency, today, entry.max_age_days)) stale += 1;
  }
  return stale * 2 > present.length;
}

function regimeVerdict(text) {
  if (!text) return "";
  if (!regimeInputsStale()) return text;
  if (text.includes("→")) return text.replace(/→\s*.*$/, "→ stale — no call");
  return "stale — no call";
}

function regimeMarkup() {
  const chip = state.catalog.fred_chip
    ? `<p class="fred-chip${String(state.catalog.fred_chip).startsWith("FRED refresh failed") ? " severe" : ""}">${escapeHtml(state.catalog.fred_chip)}</p>`
    : "";
  const verdict = regimeVerdict(state.catalog.regime_readout || "");
  const staleClass = verdict.includes("stale — no call") ? " stale" : "";
  const readout = verdict
    ? `<p class="regime-readout${staleClass}">${escapeHtml(verdict)}</p>`
    : "";
  const legend = `<p class="regime-legend">Red means the latest week was bad for risk assets: a rise when higher is bad, or a fall when higher is good. Green means the move was supportive.</p>`;
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
    const backup = entry.source && !["FRED", "Computed", "Yahoo Finance"].includes(entry.source)
      ? `<p class="regime-src">${escapeHtml(entry.source)}</p>`
      : "";
    return `
      <article class="regime-tile ${tone}">
        <p class="regime-kicker">${escapeHtml(label)}</p>
        ${backup}
        <p class="regime-value">${escapeHtml(latest)}</p>
        <p class="regime-moves">
          <span class="delta ${deltaClass(week, entry.higher_is_bad)}">1W ${escapeHtml(formatChange(week, entry.change_mode))}</span>
          <span class="delta ${deltaClass(month, entry.higher_is_bad)}">1M ${escapeHtml(formatChange(month, entry.change_mode))}</span>
        </p>
        <div class="range" role="img" aria-label="${escapeHtml(pct)}"><span class="range-fill" style="width:${width}%"></span></div>
        <p class="regime-pct">${escapeHtml(pct)}</p>
      </article>`;
  }).join("");
  return `<section class="regime-block"><div class="regime-head">${readout}${chip}</div>${legend}<div class="regime" aria-label="Risk regime">${tiles}</div></section>`;
}

function positionsCard(entry) {
  const doc = state.series.get(entry.key);
  const markets = doc && Array.isArray(doc.markets) ? doc.markets : null;
  if (!markets) {
    const reason = entry.last_error || "No positioning data returned.";
    return `<article class="card"><h3>${escapeHtml(entry.name)}</h3><p class="alert bad">${escapeHtml(reason)}</p></article>`;
  }
  const rows = markets.map((market) => `
    <tr>
      <td>${escapeHtml(market.name)}</td>
      <td>${escapeHtml(formatValue(market.latest, "contracts"))}</td>
      <td>${Number.isFinite(market.percentile) ? `${ordinal(market.percentile)} of 3Y` : "n/a"}</td>
      <td>${escapeHtml(formatLong(market.as_of))}</td>
    </tr>`).join("");
  return `
    <article class="card">
      <h3>${escapeHtml(entry.name)}</h3>
      <p class="meta">CFTC · weekly · leveraged money, long minus short</p>
      <p class="desc">${escapeHtml(entry.description || "")}</p>
      <div class="table-scroll">
        <table class="semi-table">
          <thead><tr><th>Market</th><th>Net</th><th>Percentile</th><th>As of</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </article>`;
}

function cardMarkup(entry) {
  if (entry.key === "CFTC") return positionsCard(entry);
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
  const hasRrp = doc && Array.isArray(doc.rrp) && doc.rrp.length > 0;
  const warnings = [];
  const firstDate = observationsOf(entry)[0] && observationsOf(entry)[0][0];
  if (firstDate && firstDate > state.start) {
    warnings.push(`<p class="history-note">History starts ${escapeHtml(formatLong(firstDate))}, after the selected start. Earlier dates in this range are blank.</p>`);
  }
  const scaledClaims = entry.unit === "claims" && axisRows(entry, visible).length < visible.length;
  if (scaledClaims) {
    warnings.push(`<p class="history-note">The axis uses the latest two years so recent claims are visible.</p>`);
  }
  if (age.isStale) {
    warnings.push(`<p class="alert warn">Latest observation is ${age.ageDays} days old; this may be stale for ${escapeHtml(entry.frequency.toLowerCase())} data.</p>`);
  }
  if (hasRrp) {
    warnings.push(`<p class="history-note">Reverse repo is on this card. Its axis uses the latest four months so the recent level is visible.</p>`);
  }
  const rrpChart = hasRrp ? `<div class="chart inset" id="rrp-${chartElementId(entry)}"></div>` : "";
  return `
    <article class="card${hasOverlay ? " wide" : ""}">
      <div class="card-top">
        <div>
          <h3>${escapeHtml(entry.name)}</h3>
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
      ${rrpChart}
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
    if (entry.key === "CFTC") continue;
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
    if (doc && Array.isArray(doc.rrp) && doc.rrp.length) {
      drawInset(
        `rrp-${chartElementId(entry)}`,
        rowsBetween(doc.rrp, state.start, state.end),
        doc.rrp_unit || "$ billions",
        doc.rrp_name || "Overnight reverse repo",
        120,
      );
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

function formatCap(value) {
  if (!Number.isFinite(value)) return "n/a";
  if (Math.abs(value) >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  return `$${(value / 1e9).toFixed(2)}B`;
}

function formatPe(value) {
  return Number.isFinite(value) ? `${Number(value).toFixed(1)}x` : "n/a";
}

function semiCard(entry) {
  const doc = state.series.get(entry.key);
  if (!doc || doc.loadError || !Array.isArray(doc.observations) || !doc.observations.length) {
    const reason = (doc && doc.loadError) || entry.last_error || "No data returned.";
    return `<article class="card"><h3>${escapeHtml(entry.name)}</h3><p class="alert bad">${escapeHtml(reason)}</p></article>`;
  }
  const latest = doc.observations[doc.observations.length - 1];
  return `
    <article class="card wide">
      <div class="card-top">
        <div>
          <h3>${escapeHtml(entry.name)}</h3>
          <p class="meta">${escapeHtml(entry.ticker || entry.key)} · monthly · ${escapeHtml(formatCap(entry.market_cap))}</p>
          <p class="as-of-line">As of ${escapeHtml(formatLong(latest[0]))}</p>
          <p class="desc">${escapeHtml(entry.description || doc.description || "")}</p>
        </div>
        <div class="latest">
          <span class="kicker">P/E</span>
          <span class="value">${escapeHtml(formatPe(entry.pe))}</span>
          <span class="delta">10Y median ${escapeHtml(formatPe(entry.median_pe))}</span>
        </div>
      </div>
      <div class="chart dual" id="${chartElementId(entry)}"></div>
    </article>`;
}

function drawSemi(entry) {
  const node = document.getElementById(chartElementId(entry));
  const doc = state.series.get(entry.key);
  if (!node || !doc || typeof Plotly === "undefined") return;
  const prices = doc.observations || [];
  const pe = doc.pe_observations || [];
  if (!prices.length) return;
  const layout = {
    ...baseLayout(chartNarrow() ? 320 : 360, 48),
    showlegend: true,
    legend: chartLegend(1.02),
    xaxis: xAxis(prices.map((row) => row[0])),
    yaxis: { ...yAxis(prices.map((row) => row[1]), "$"), side: "left" },
    yaxis2: {
      ...(pe.length ? yAxis(pe.map((row) => row[1]), "x") : yAxis([0, 1], "x")),
      overlaying: "y",
      side: "right",
    },
  };
  const traces = [{
    x: prices.map((row) => row[0]),
    y: prices.map((row) => row[1]),
    type: "scatter",
    mode: "lines",
    name: "Price",
    line: { color: LINE_PRIMARY, width: 1.35 },
    customdata: prices.map((row) => formatHoverValue(row[1], "$")),
    hovertemplate: "%{customdata}<extra>Price</extra>",
  }];
  if (pe.length) {
    traces.push({
      x: pe.map((row) => row[0]),
      y: pe.map((row) => row[1]),
      type: "scatter",
      mode: "lines",
      name: "P/E",
      connectgaps: false,
      yaxis: "y2",
      line: { color: LINE_SECONDARY, width: 1.25 },
      customdata: pe.map((row) => formatPe(row[1])),
      hovertemplate: "%{customdata}<extra>P/E</extra>",
    });
  }
  Plotly.react(node, traces, layout, PLOT_CONFIG);
}

function renderSemis(libraryNote) {
  const entries = groupEntries("semis");
  const body = entries.map((entry) => `
    <tr>
      <td>${escapeHtml(entry.name)}</td>
      <td>${escapeHtml(entry.ticker || entry.key)}</td>
      <td>${escapeHtml(formatCap(entry.market_cap))}</td>
      <td>${escapeHtml(Number.isFinite(entry.price) ? formatValue(entry.price, "$", false) : "n/a")}</td>
      <td>${escapeHtml(formatPe(entry.pe))}</td>
      <td>${escapeHtml(formatPe(entry.median_pe))}</td>
      <td>${Number.isFinite(entry.pe_percentile) ? `${ordinal(entry.pe_percentile)}` : "n/a"}</td>
    </tr>`).join("");
  panelEl().innerHTML = `
    ${libraryNote}
    <section class="macro-section">
      <h2 class="section-title">Semiconductor multiples</h2>
      <p class="regime-legend">Top 10 US-listed names by market cap at build time (SEC shares × Yahoo price). TSMC's ADR is five ordinary shares. Monthly price against trailing P/E for ten years. P/E is blank where trailing EPS is negative or missing. These charts use that ten-year file.</p>
      <div class="table-scroll">
        <table class="semi-table">
          <thead><tr><th>Company</th><th>Ticker</th><th>Market cap</th><th>Price</th><th>P/E</th><th>10Y median</th><th>10Y pct</th></tr></thead>
          <tbody>${body || `<tr><td colspan="7">No semiconductor snapshot is stored yet.</td></tr>`}</tbody>
        </table>
      </div>
    </section>
    <div class="card-grid">${entries.map(semiCard).join("")}</div>`;
  entries.forEach(drawSemi);
}

function renderPanel() {
  const libraryNote = typeof Plotly === "undefined"
    ? `<p class="alert warn">The chart library did not load. Values are still shown, but the lines cannot be drawn.</p>`
    : "";
  purgeCharts(panelEl());
  if (state.tab === "semis") {
    renderSemis(libraryNote);
    return;
  }
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

  const entries = groupEntries(state.tab).filter((entry) => entry.visible !== false);
  if (state.tab !== "macro" && state.tab !== "markets") {
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
  const order = state.tab === "markets"
    ? ["Markets", "Cross-asset"]
    : (state.catalog.section_order || SECTION_ORDER);
  sections.sort((a, b) => {
    const left = order.indexOf(a.name);
    const right = order.indexOf(b.name);
    return (left < 0 ? 99 : left) - (right < 0 ? 99 : right);
  });
  const regime = state.tab === "macro" ? regimeMarkup() : "";
  panelEl().innerHTML = `${regime}${libraryNote}${sections.map((section) => `
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
  asOfEl().textContent = state.catalog.as_of_label || `Data through ${formatLong(state.end)}`;
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
  if (hash === "markets" || hash === "companies" || hash === "macro" || hash === "semis") {
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
  asOfEl().textContent = state.catalog.as_of_label || `Data through ${formatLong(state.end)}`;
  await ensureTab(state.tab);
  draw();
  const rest = state.catalog.series.filter((entry) => !state.series.has(entry.key));
  ensure(rest);
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => showFatal(error));
});
