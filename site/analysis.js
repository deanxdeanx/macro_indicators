const ANALYSIS_WINDOWS = { "1Y": 365, "5Y": 365 * 5, "10Y": 365 * 10, Max: null };
const ANALYSIS_DISCLAIMER = "This is not investment advice. It is an automated research draft and it can be wrong, incomplete, or out of date. Do not use it as the basis for buying or selling a security.";
const FILE_LIMIT = 20;
const TEXT_LIMIT = 80000;

const analysisState = {
  doc: null,
  windowKey: "1Y",
  prompt: "",
  files: [],
  config: null,
  session: null,
  account: null,
  client: null,
  busy: false,
  result: "",
  costs: null,
  status: "",
};

function analysisDataUrl(relativePath) {
  return new URL(relativePath, new URL("data/", document.baseURI));
}

function analysisEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function shownField(field) {
  if (!field || field.value === null || field.value === undefined || field.value === "") return "n/a";
  const text = field.display || String(field.value);
  if (field.confirmed === false) return `${text} (Expected)`;
  return text;
}

function clampPrices(prices, key, historyStart, asOf) {
  const days = ANALYSIS_WINDOWS[key];
  let start = historyStart;
  if (days) {
    const end = new Date(`${asOf}T00:00:00Z`);
    end.setUTCDate(end.getUTCDate() - days);
    const iso = end.toISOString().slice(0, 10);
    if (iso > start) start = iso;
  }
  return prices.filter((row) => row[0] >= start && row[0] <= asOf);
}

function renderMarkdown(markdown) {
  const escaped = analysisEscape(markdown || "");
  const lines = escaped.split("\n");
  const html = [];
  let list = false;
  const flush = () => {
    if (list) {
      html.push("</ul>");
      list = false;
    }
  };
  for (const line of lines) {
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const level = heading[1].length + 2;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (line.startsWith("- ")) {
      if (!list) {
        html.push("<ul>");
        list = true;
      }
      html.push(`<li>${inline(line.slice(2))}</li>`);
      continue;
    }
    flush();
    if (!line.trim()) {
      html.push("");
      continue;
    }
    html.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return html.join("\n");
}

function inline(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function money(value) {
  if (value === null || value === undefined) return "n/a";
  const field = analysisState.doc.fundamentals.market_cap;
  return field.display || "n/a";
}

async function loadAnalysis() {
  if (analysisState.doc) return analysisState.doc;
  const response = await fetch(analysisDataUrl("analysis/SPCX.json"), { cache: "no-cache" });
  if (!response.ok) throw new Error(`Could not load SPCX data (HTTP ${response.status}).`);
  analysisState.doc = await response.json();
  analysisState.prompt = analysisState.doc.prompt || "";
  return analysisState.doc;
}

async function loadPublicConfig() {
  if (analysisState.config) return analysisState.config;
  try {
    const response = await fetch("/api/public-config", { cache: "no-store" });
    if (!response.ok) throw new Error("config unavailable");
    analysisState.config = await response.json();
  } catch (error) {
    analysisState.config = { configured: false, supabaseUrl: null, supabaseAnonKey: null };
  }
  try {
    const status = await fetch("/api/analyze", { cache: "no-store" });
    if (status.ok) {
      const payload = await status.json();
      analysisState.config.analyzeConfigured = Boolean(payload.configured);
    } else {
      analysisState.config.analyzeConfigured = false;
    }
  } catch (error) {
    analysisState.config.analyzeConfigured = false;
  }
  return analysisState.config;
}

function drawAnalysisChart() {
  const node = document.getElementById("analysis-chart");
  const doc = analysisState.doc;
  if (!node || !doc || typeof Plotly === "undefined") return;
  const asOf = doc.prices[doc.prices.length - 1][0];
  const visible = clampPrices(doc.prices, analysisState.windowKey, doc.history_start, asOf);
  const releases = (doc.earnings_releases || []).filter((event) => (
    visible.some((row) => row[0] === event.date) || (visible.length && event.date >= visible[0][0] && event.date <= visible[visible.length - 1][0])
  ));
  const traces = [{
    x: visible.map((row) => row[0]),
    y: visible.map((row) => row[1]),
    type: "scatter",
    mode: "lines",
    name: "Close",
    line: { color: "#d08c69", width: 1.7 },
    hovertemplate: "%{x|%b %d, %Y}<br>$%{y:,.2f}<extra>SPCX</extra>",
  }];
  if (releases.length) {
    const anchor = visible.length ? visible[Math.floor(visible.length / 2)][1] : doc.ipo_price.value;
    traces.push({
      x: releases.map((event) => event.date),
      y: releases.map(() => anchor),
      type: "scatter",
      mode: "markers",
      name: "Earnings",
      marker: { color: "#f4f7f7", size: 9, symbol: "diamond" },
      customdata: releases.map((event) => `${event.label} · ${event.date}`),
      hovertemplate: "%{customdata}<extra></extra>",
    });
  }
  Plotly.react(node, traces, {
    height: window.innerWidth <= 800 ? 260 : 300,
    margin: { l: 16, r: 56, t: 12, b: 48 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    showlegend: false,
    hovermode: "x unified",
    font: { family: "Inter, sans-serif", color: "#d5dddd", size: 13 },
    xaxis: { gridcolor: "#2a3434", tickfont: { color: "#d5dddd", size: 12 }, automargin: true },
    yaxis: {
      side: "right",
      gridcolor: "#2a3434",
      tickprefix: "$",
      tickformat: ",.0f",
      tickfont: { color: "#d5dddd", size: 13 },
      automargin: true,
    },
    shapes: releases.map((event) => ({
      type: "line",
      x0: event.date,
      x1: event.date,
      yref: "paper",
      y0: 0,
      y1: 1,
      line: { color: "rgba(255,255,255,0.9)", width: 1.25, dash: "dot" },
    })),
  }, { displaylogo: false, displayModeBar: false, responsive: true });
  const note = document.getElementById("analysis-range-note");
  if (note && visible.length) {
    note.textContent = `Showing ${visible[0][0]} to ${visible[visible.length - 1][0]}. Longer ranges stop at the 12 Jun 2026 IPO.`;
  }
}

function fundamentalsHtml(doc) {
  const rows = [
    ["Description", "description"],
    ["Market cap (Class A)", "market_cap"],
    ["Dividend yield", "dividend_yield"],
    ["PE (TTM)", "pe_ttm"],
    ["PE (NTM)", "pe_ntm"],
    ["EPS (TTM)", "eps_ttm"],
    ["EPS (NTM)", "eps_ntm"],
    ["Last earnings", "last_earnings_date"],
    ["Next earnings", "next_earnings_date"],
    ["CEO", "ceo"],
  ];
  const fields = doc.fundamentals;
  return rows.map(([label, key]) => {
    const field = fields[key] || {};
    const value = key === "market_cap" ? (field.display || shownField(field)) : shownField(field);
    const extra = key === "last_earnings_date" && field.period_end ? ` · period ended ${field.period_end}` : "";
    return `<div class="fact"><span>${label}</span><strong>${analysisEscape(value)}${analysisEscape(extra)}</strong><small>${analysisEscape(field.source || "")}</small></div>`;
  }).join("");
}

function accountHtml() {
  const config = analysisState.config || {};
  if (!config.supabaseUrl) {
    return `<p class="as-of">Sign-in is not configured. The chart and fundamentals still load.</p>`;
  }
  if (!analysisState.session) {
    return `
      <form id="magic-link" class="account-form">
        <input id="magic-email" type="email" placeholder="Email for a magic link" required>
        <button class="btn" type="submit">Email link</button>
        <button class="btn" id="github-signin" type="button">GitHub</button>
      </form>`;
  }
  const credits = analysisState.account ? analysisState.account.credits : "…";
  const tier = analysisState.account ? analysisState.account.tier : "";
  return `<p class="as-of">${analysisEscape(analysisState.session.user.email || "Signed in")} · ${analysisEscape(tier)} · ${analysisEscape(credits)} credits <button class="btn" id="sign-out" type="button">Sign out</button></p>`;
}

function aiHtml() {
  const configured = analysisState.config && analysisState.config.analyzeConfigured;
  const notice = configured
    ? ""
    : `<p class="alert warn">Analysis requests are not configured. The chart and fundamentals above still work. On Vercel, set SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, and OPENROUTER_API_KEY.</p>`;
  const result = analysisState.result
    ? `<div class="markdown">${renderMarkdown(analysisState.result)}</div>`
    : "";
  const costs = analysisState.costs
    ? `<p class="as-of">Estimated cost $${analysisState.costs.estimated.toFixed(4)} · actual ${analysisState.costs.actual === null ? "n/a" : `$${analysisState.costs.actual.toFixed(4)}`}</p>`
    : "";
  return `
    ${accountHtml()}
    ${notice}
    <label class="prompt-label" for="analysis-prompt">Request</label>
    <textarea id="analysis-prompt" ${configured ? "" : "disabled"}>${analysisEscape(analysisState.prompt)}</textarea>
    <div class="analysis-actions">
      <button class="btn primary" id="analysis-submit" type="button" ${configured && analysisState.session && !analysisState.busy ? "" : "disabled"}>Submit request</button>
      <span class="as-of">${analysisEscape(analysisState.status)}</span>
    </div>
    <p class="disclaimer">${analysisEscape(ANALYSIS_DISCLAIMER)}</p>
    ${costs}
    ${result}`;
}

function renderAnalysisPanel(root) {
  const existing = root.querySelector("#analysis-prompt");
  if (existing) analysisState.prompt = existing.value;
  root.innerHTML = `
    <section class="analysis-grid" aria-label="SPCX analysis">
      <article class="card analysis-price">
        <div class="analysis-card-head">
          <h2>SPCX</h2>
          <div class="window-toggles" role="group" aria-label="Price history">
            ${Object.keys(ANALYSIS_WINDOWS).map((key) => `<button type="button" class="chip" data-window="${key}" aria-pressed="${key === analysisState.windowKey}">${key}</button>`).join("")}
          </div>
        </div>
        <p id="analysis-range-note" class="as-of">Loading price history…</p>
        <div id="analysis-chart" class="chart"></div>
      </article>
      <article class="card analysis-facts"><h2>Fundamentals</h2><div id="analysis-facts">Loading…</div></article>
      <article class="card analysis-ai"><h2>Analysis request</h2><div id="analysis-ai">Loading…</div></article>
      <article class="card analysis-files">
        <h2>Documents</h2>
        <p class="as-of">Optional. Up to 20 PDF, XLSX, CSV, or TXT files. Uploads are weighted above the snapshot.</p>
        <div id="analysis-drop" class="dropzone" tabindex="0">Drop files here or click to choose</div>
        <input id="analysis-file-input" type="file" accept=".pdf,.xlsx,.csv,.txt,application/pdf,text/plain,text/csv" multiple hidden>
        <ul id="analysis-file-list" class="file-list"></ul>
      </article>
    </section>`;
  bindAnalysis(root);
  loadAnalysis().then(() => {
    const facts = root.querySelector("#analysis-facts");
    if (facts) facts.innerHTML = fundamentalsHtml(analysisState.doc);
    drawAnalysisChart();
  }).catch((error) => {
    const facts = root.querySelector("#analysis-facts");
    if (facts) facts.innerHTML = `<p class="alert bad">${analysisEscape(error.message)}</p>`;
  });
  loadPublicConfig().then(async () => {
    if (analysisState.config.supabaseUrl && window.supabase) {
      analysisState.client = window.supabase.createClient(analysisState.config.supabaseUrl, analysisState.config.supabaseAnonKey);
      const { data } = await analysisState.client.auth.getSession();
      analysisState.session = data.session;
      await refreshAccount();
    }
    const slot = root.querySelector("#analysis-ai");
    if (slot) slot.innerHTML = aiHtml();
    bindAi(root);
  });
  renderFileList();
}

function bindAnalysis(root) {
  root.querySelectorAll("[data-window]").forEach((button) => {
    button.addEventListener("click", () => {
      analysisState.windowKey = button.dataset.window;
      root.querySelectorAll("[data-window]").forEach((item) => {
        item.setAttribute("aria-pressed", item === button ? "true" : "false");
      });
      drawAnalysisChart();
    });
  });
  const drop = root.querySelector("#analysis-drop");
  const input = root.querySelector("#analysis-file-input");
  if (!drop || !input) return;
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("drag");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("drag");
    addFiles(event.dataTransfer.files);
  });
  input.addEventListener("change", () => addFiles(input.files));
}

function bindAi(root) {
  const prompt = root.querySelector("#analysis-prompt");
  if (prompt) {
    prompt.addEventListener("input", () => {
      analysisState.prompt = prompt.value;
    });
  }
  const submit = root.querySelector("#analysis-submit");
  if (submit) submit.addEventListener("click", submitAnalysis);
  const github = root.querySelector("#github-signin");
  if (github) github.addEventListener("click", () => signIn("github"));
  const magic = root.querySelector("#magic-link");
  if (magic) {
    magic.addEventListener("submit", (event) => {
      event.preventDefault();
      signIn("email", root.querySelector("#magic-email").value);
    });
  }
  const signOut = root.querySelector("#sign-out");
  if (signOut) {
    signOut.addEventListener("click", async () => {
      await analysisState.client.auth.signOut();
      analysisState.session = null;
      analysisState.account = null;
      root.querySelector("#analysis-ai").innerHTML = aiHtml();
      bindAi(root);
    });
  }
}

async function refreshAccount() {
  if (!analysisState.client || !analysisState.session) return;
  const { data, error } = await analysisState.client
    .from("accounts")
    .select("credits,tier,trial_ends_at")
    .eq("user_id", analysisState.session.user.id)
    .maybeSingle();
  if (!error) analysisState.account = data;
}

async function signIn(kind, email) {
  const redirect = `${location.origin}${location.pathname}`;
  if (kind === "github") {
    await analysisState.client.auth.signInWithOAuth({ provider: "github", options: { redirectTo: redirect } });
    return;
  }
  analysisState.status = "Sending magic link…";
  const { error } = await analysisState.client.auth.signInWithOtp({ email, options: { emailRedirectTo: redirect } });
  analysisState.status = error ? error.message : "Check your email for the sign-in link.";
  const slot = document.querySelector("#analysis-ai");
  if (slot) {
    slot.innerHTML = aiHtml();
    bindAi(document);
  }
}

function addFiles(fileList) {
  const incoming = [...fileList];
  for (const file of incoming) {
    if (analysisState.files.length >= FILE_LIMIT) break;
    const allowed = /\.(pdf|xlsx|csv|txt)$/i.test(file.name);
    if (!allowed) continue;
    analysisState.files.push({ file, text: "", status: "Reading…" });
  }
  renderFileList();
  extractAll();
}

function renderFileList() {
  const list = document.getElementById("analysis-file-list");
  if (!list) return;
  list.innerHTML = analysisState.files.map((item, index) => (
    `<li><span>${analysisEscape(item.file.name)}</span><small>${analysisEscape(item.status)}</small><button type="button" data-remove="${index}">Remove</button></li>`
  )).join("");
  list.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => {
      analysisState.files.splice(Number(button.dataset.remove), 1);
      renderFileList();
    });
  });
}

async function extractAll() {
  let used = 0;
  for (const item of analysisState.files) {
    if (item.text) {
      used += item.text.length;
      continue;
    }
    try {
      const text = await extractFile(item.file);
      const room = TEXT_LIMIT - used;
      item.text = text.slice(0, Math.max(0, room));
      used += item.text.length;
      item.status = item.text ? `${item.text.length.toLocaleString()} characters` : "No text extracted";
    } catch (error) {
      item.status = error.message || "Could not read this file";
    }
  }
  renderFileList();
}

async function extractFile(file) {
  const name = file.name.toLowerCase();
  if (name.endsWith(".txt") || name.endsWith(".csv")) return file.text();
  if (name.endsWith(".xlsx")) {
    await loadScript("https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js");
    const buffer = await file.arrayBuffer();
    const book = window.XLSX.read(buffer, { type: "array" });
    return book.SheetNames.map((sheet) => window.XLSX.utils.sheet_to_csv(book.Sheets[sheet])).join("\n");
  }
  if (name.endsWith(".pdf")) {
    await loadScript("https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js");
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    const buffer = await file.arrayBuffer();
    const pdf = await window.pdfjsLib.getDocument({ data: buffer }).promise;
    const pages = [];
    const limit = Math.min(pdf.numPages, 30);
    for (let pageNumber = 1; pageNumber <= limit; pageNumber += 1) {
      const page = await pdf.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(content.items.map((item) => item.str).join(" "));
    }
    return pages.join("\n");
  }
  throw new Error("Unsupported file");
}

function loadScript(src) {
  if (document.querySelector(`script[src="${src}"]`)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("A file reader failed to load."));
    document.head.appendChild(script);
  });
}

async function submitAnalysis() {
  if (analysisState.busy) return;
  analysisState.busy = true;
  analysisState.status = "Running…";
  const slot = document.querySelector("#analysis-ai");
  slot.innerHTML = aiHtml();
  bindAi(document);
  const token = analysisState.session && analysisState.session.access_token;
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        ticker: "SPCX",
        prompt: analysisState.prompt,
        fundamentals: analysisState.doc ? analysisState.doc.fundamentals : {},
        documents: analysisState.files.filter((item) => item.text).map((item) => ({ name: item.file.name, text: item.text })),
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The request failed.");
    analysisState.result = payload.markdown || "";
    analysisState.costs = { estimated: payload.estimated_cost_usd, actual: payload.actual_cost_usd };
    analysisState.status = payload.spend_error || `Charged ${payload.credits_charged} credit. ${payload.credits_remaining} remaining.`;
    if (analysisState.account && payload.credits_remaining !== undefined) {
      analysisState.account.credits = payload.credits_remaining;
    }
  } catch (error) {
    analysisState.status = error.message;
  } finally {
    analysisState.busy = false;
    slot.innerHTML = aiHtml();
    bindAi(document);
  }
}
