/* Claude Usage Dashboard — frontend */

const MODEL_COLORS = {
  "claude-opus-4-8": "#d97757",
  "claude-opus-4-7": "#e0916f",
  "claude-opus-4-6": "#c76b4e",
  "claude-sonnet-5": "#6ea8fe",
  "claude-sonnet-4-6": "#8bb8fe",
  "claude-haiku-4-5": "#4ec9a5",
};
const FALLBACK_COLORS = ["#b48ead", "#a3be8c", "#ebcb8b", "#88c0d0", "#bf616a"];

let STATE = { summary: null };
const charts = {};

/* ---------- formatting ---------- */
const usd = (x) => {
  x = x || 0;
  if (x >= 100) return "$" + x.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (x >= 1) return "$" + x.toFixed(2);
  return "$" + x.toFixed(3);
};
const tok = (n) => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
};
const num = (n) => Math.round(n || 0).toLocaleString();
const bytes = (n) => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const dateLabel = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
const shortProj = (p) => (p || "").replace(/^\/Users\/[^/]+\//, "~/") || "(unknown)";
const modelColor = (m, i) => MODEL_COLORS[m] || FALLBACK_COLORS[i % FALLBACK_COLORS.length];

/* ---------- boot ---------- */
async function boot() {
  wireTabs();
  document.getElementById("drawer-close").onclick = closeDrawer;
  document.getElementById("drawer").onclick = (e) => { if (e.target.id === "drawer") closeDrawer(); };
  try {
    const res = await fetch("/api/summary");
    if (!res.ok) throw new Error("HTTP " + res.status);
    STATE.summary = await res.json();
  } catch (e) {
    document.getElementById("loading").innerHTML = `<p style="color:var(--high)">Failed to load: ${esc(e.message)}</p>`;
    return;
  }
  document.getElementById("loading").classList.add("hidden");
  document.getElementById("refresh-btn").onclick = refresh;
  renderAll(STATE.summary);
}

function renderAll(s) {
  document.getElementById("generated").textContent = "updated " + dateLabel(s.generated_at);
  renderWrapped(s);
  renderOverview(s);
  renderUsage();
  renderActivity();
  renderProjects(s);
  renderInsights(s);
}

async function refresh() {
  const btn = document.getElementById("refresh-btn");
  if (btn.disabled) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "↻ Refreshing…";
  btn.classList.add("spinning");
  try {
    const res = await fetch("/api/summary?refresh=1");
    if (!res.ok) throw new Error("HTTP " + res.status);
    STATE.summary = await res.json();
    ACTIVITY.data = null;  // force the Activity tab to re-fetch
    renderAll(STATE.summary);
  } catch (e) {
    btn.textContent = "↻ Failed";
    setTimeout(() => { btn.textContent = old; }, 1500);
    btn.disabled = false;
    btn.classList.remove("spinning");
    return;
  }
  btn.textContent = old;
  btn.disabled = false;
  btn.classList.remove("spinning");
}

function wireTabs() {
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
      document.getElementById(b.dataset.tab).classList.remove("hidden");
    };
  });
}

/* ---------- OVERVIEW ---------- */
function renderOverview(s) {
  const t = s.totals;
  const el = document.getElementById("overview");
  el.innerHTML = `
    <div class="cards">
      ${card("Total cost", usd(t.cost), "estimated")}
      ${card("Sessions", num(t.sessions), num(t.projects) + " projects")}
      ${card("Messages", num(t.messages), "assistant turns")}
      ${card("Total tokens", tok(t.total_tokens), "all buckets")}
      ${card("Cache reads", tok(t.cache_read_tokens), "billed at 0.1×")}
      ${card("Output tokens", tok(t.output_tokens), "billed at 5×")}
    </div>
    <div class="panel">
      <h2>Daily cost <span class="hint">local time</span></h2>
      <div class="chart-wrap"><canvas id="ov-daily"></canvas></div>
    </div>
    <div class="grid-2">
      <div class="panel">
        <h2>Token breakdown</h2>
        <div class="chart-wrap"><canvas id="ov-tokens"></canvas></div>
      </div>
      <div class="panel">
        <h2>Activity by hour <span class="hint">local time</span></h2>
        <div class="chart-wrap"><canvas id="ov-hour"></canvas></div>
      </div>
    </div>
    <div class="panel">
      <h2>Activity heatmap <span class="hint">messages · weekday × hour, local</span></h2>
      <div id="heatmap"></div>
      <div class="legend"><span>less <span class="swatch" style="background:#1e232c"></span><span class="swatch" style="background:#3a4a5a"></span><span class="swatch" style="background:#4d7ba0"></span><span class="swatch" style="background:#6ea8fe"></span> more</span></div>
    </div>`;

  const days = Object.keys(s.by_day);
  lineChart("ov-daily", days, [{
    label: "Cost", data: days.map((d) => s.by_day[d].cost),
    color: "#d97757", fill: true,
  }], { yFmt: usd });

  const tb = [
    ["Input", t.input_tokens, "#d97757"],
    ["Output", t.output_tokens, "#e5716a"],
    ["Cache read", t.cache_read_tokens, "#4ec9a5"],
    ["Cache write", t.cache_write_tokens, "#6ea8fe"],
  ];
  doughnut("ov-tokens", tb.map((r) => r[0]), tb.map((r) => r[1]), tb.map((r) => r[2]), tok);

  const hours = [...Array(24).keys()];
  barChart("ov-hour", hours.map((h) => h + ":00"),
    [{ label: "Messages", data: hours.map((h) => s.by_hour[h].messages), color: "#6ea8fe" }]);

  renderHeatmap(s);
}

function renderHeatmap(s) {
  // Build weekday(0-6 Mon..Sun) × hour matrix from by_weekday+by_hour is not
  // enough (they're marginal). We rebuild from sessions? Not available. Use
  // hour marginal duplicated across weekday marginal proportionally is wrong —
  // instead show weekday totals as rows scaled by hour marginal shape.
  // Simpler + honest: show the weekday×hour matrix from server if present,
  // else fall back to hour-only single row.
  const wk = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const m = (s.weekhour && s.weekhour.messages) || [];
  let cellMax = 0;
  for (const row of m) for (const v of row) cellMax = Math.max(cellMax, v);
  const shade = (v) => {
    const r = cellMax ? v / cellMax : 0;
    if (r === 0) return "#161b22";
    if (r < 0.25) return "#2f4155";
    if (r < 0.5) return "#3f6a95";
    if (r < 0.75) return "#5591d6";
    return "#6ea8fe";
  };
  let html = '<div class="heat"><div class="rowlabel"></div>';
  for (let h = 0; h < 24; h++) html += `<div class="collabel">${h % 3 === 0 ? h : ""}</div>`;
  for (let d = 0; d < 7; d++) {
    html += `<div class="rowlabel">${wk[d]}</div>`;
    for (let h = 0; h < 24; h++) {
      const v = (m[d] && m[d][h]) || 0;
      html += `<div class="cell" style="background:${shade(v)}" title="${wk[d]} ${h}:00 · ${v} msgs"></div>`;
    }
  }
  html += "</div>";
  document.getElementById("heatmap").innerHTML = html;
}

/* ---------- USAGE (Cost + Sessions, filterable) ---------- */
const USAGE = { project: "", preset: "all", from: "", to: "", search: "", data: null };
let sessionSort = { key: "last_ts", dir: -1 };
const sTh = (k, label, n) => `<th data-key="${k}" class="${n ? "num" : ""}">${label}</th>`;

const ymdLocal = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const todayLocal = () => ymdLocal(new Date());
function daysAgoLocal(n) { const d = new Date(); d.setDate(d.getDate() - n); return ymdLocal(d); }

function renderUsage() {
  const el = document.getElementById("usage");
  el.innerHTML = `
    <div class="filters">
      <label class="fl">Project
        <select id="u-project"><option value="">All projects</option></select>
      </label>
      <div class="presets" id="u-presets">
        ${["all", "7d", "30d", "90d"].map((p) => `<button data-preset="${p}">${p === "all" ? "All time" : "Last " + p}</button>`).join("")}
      </div>
      <label class="fl">From <input type="date" id="u-from"></label>
      <label class="fl">To <input type="date" id="u-to"></label>
      <button id="u-clear" class="u-clear">Clear</button>
    </div>
    <div id="u-cards" class="cards"></div>
    <div class="panel">
      <h2>Cost over time <span class="hint">daily, local</span></h2>
      <div class="chart-wrap tall"><canvas id="u-daily"></canvas></div>
    </div>
    <div class="grid-2">
      <div class="panel"><h2>Cost by model</h2><div class="chart-wrap"><canvas id="u-model"></canvas></div></div>
      <div class="panel"><h2>Model detail</h2><div class="scroll" style="max-height:300px" id="u-model-table"></div></div>
    </div>
    <div class="panel">
      <h2>Sessions <span class="hint" id="u-sess-count"></span></h2>
      <div class="controls"><input type="search" id="u-search" placeholder="Filter shown sessions by project or id…"></div>
      <div class="scroll"><table>
        <thead><tr>
          ${sTh("project", "Project")}${sTh("last_ts", "Last active", true)}${sTh("assistant_messages", "Msgs", true)}${sTh("cost", "Cost", true)}${sTh("total_tokens", "Tokens", true)}
        </tr></thead>
        <tbody id="u-sess-body"></tbody>
      </table></div>
    </div>`;

  document.getElementById("u-presets").querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      USAGE.preset = b.dataset.preset;
      if (USAGE.preset === "all") { USAGE.from = ""; USAGE.to = ""; }
      else { USAGE.to = todayLocal(); USAGE.from = daysAgoLocal(USAGE.preset === "7d" ? 6 : USAGE.preset === "30d" ? 29 : 89); }
      loadUsage();
    };
  });
  const fromEl = document.getElementById("u-from"), toEl = document.getElementById("u-to");
  fromEl.onchange = () => { USAGE.from = fromEl.value; USAGE.preset = "custom"; loadUsage(); };
  toEl.onchange = () => { USAGE.to = toEl.value; USAGE.preset = "custom"; loadUsage(); };
  document.getElementById("u-clear").onclick = () => { Object.assign(USAGE, { project: "", preset: "all", from: "", to: "", search: "" }); loadUsage(); };
  document.getElementById("u-search").oninput = (e) => { USAGE.search = e.target.value; drawUsageSessions(); };
  document.getElementById("u-project").onchange = (e) => { USAGE.project = e.target.value; loadUsage(); };
  el.querySelectorAll("th[data-key]").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.key;
      sessionSort.dir = sessionSort.key === k ? -sessionSort.dir : -1;
      sessionSort.key = k;
      drawUsageSessions();
    };
  });
  loadUsage();
}

async function loadUsage() {
  document.getElementById("u-from").value = USAGE.from;
  document.getElementById("u-to").value = USAGE.to;
  document.getElementById("u-search").value = USAGE.search;
  document.getElementById("u-presets").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.preset === USAGE.preset));
  document.getElementById("u-cards").innerHTML = `<p class="muted" style="padding:8px 2px">Loading usage…</p>`;
  const params = new URLSearchParams();
  if (USAGE.project) params.set("project", USAGE.project);
  if (USAGE.from) params.set("from", USAGE.from);
  if (USAGE.to) params.set("to", USAGE.to);
  let d;
  try {
    const res = await fetch("/api/usage?" + params.toString());
    d = await res.json();
    if (!res.ok) throw new Error(d.error || "load failed");
  } catch (e) {
    document.getElementById("u-cards").innerHTML = `<p style="color:var(--high)">${esc(e.message)}</p>`;
    return;
  }
  USAGE.data = d;
  const projSel = document.getElementById("u-project");
  projSel.innerHTML = `<option value="">All projects</option>` +
    d.projects.map((p) => `<option value="${esc(p.name)}">${esc(shortProj(p.name))} — ${usd(p.cost)}</option>`).join("");
  projSel.value = USAGE.project;
  renderUsageData();
}

function filterLabel() {
  const p = USAGE.project ? shortProj(USAGE.project) : "all projects";
  const r = (USAGE.from || USAGE.to) ? `${USAGE.from || "start"} → ${USAGE.to || "today"}` : "all time";
  return `${p} · ${r}`;
}

function renderUsageData() {
  const d = USAGE.data, t = d.totals;
  const cacheBase = t.cache_read_tokens + t.cache_write_tokens + t.input_tokens;
  const hit = cacheBase ? t.cache_read_tokens / cacheBase : 0;
  document.getElementById("u-cards").innerHTML = `
    ${card("Total cost", usd(t.cost), filterLabel())}
    ${card("Sessions", num(t.sessions), num(t.messages) + " messages")}
    ${card("Tokens", tok(t.total_tokens), "all buckets")}
    ${card("Cache hit rate", (hit * 100).toFixed(0) + "%", "reads ÷ input+cache")}
    ${card("Avg $/session", usd(t.cost / (t.sessions || 1)), "")}`;
  const days = Object.keys(d.by_day);
  lineChart("u-daily", days, [{ label: "Cost", data: days.map((x) => d.by_day[x].cost), color: "#d97757", fill: true }], { yFmt: usd });
  const models = Object.keys(d.by_model);
  doughnut("u-model", models, models.map((m) => d.by_model[m].cost), models.map((m, i) => modelColor(m, i)), usd);
  document.getElementById("u-model-table").innerHTML = modelTableFrom(d.by_model);
  drawUsageSessions();
}

function modelTableFrom(byModel) {
  const rows = Object.entries(byModel).map(([m, v]) => `
    <tr><td><span class="swatch" style="background:${modelColor(m, 0)}"></span> ${esc(m)}</td>
    <td class="num">${usd(v.cost)}</td><td class="num">${num(v.messages)}</td>
    <td class="num">${tok(v.input_tokens)}</td><td class="num">${tok(v.output_tokens)}</td>
    <td class="num">${tok(v.cache_read_tokens)}</td></tr>`).join("");
  return `<table><thead><tr><th>Model</th><th class="num">Cost</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num">Cache rd</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function drawUsageSessions() {
  if (!USAGE.data) return;
  const q = (USAGE.search || "").toLowerCase();
  const rows = USAGE.data.sessions.filter((r) =>
    !q || (r.project || "").toLowerCase().includes(q) || r.session_id.toLowerCase().includes(q));
  const { key, dir } = sessionSort;
  rows.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "string") return dir * (av < bv ? -1 : av > bv ? 1 : 0);
    return dir * ((av || 0) - (bv || 0));
  });
  document.getElementById("u-sess-count").textContent = `${rows.length} shown`;
  document.getElementById("u-sess-body").innerHTML = rows.map((r) => `
    <tr class="clickable" data-id="${esc(r.session_id)}">
      <td><div class="proj-name">${esc(shortProj(r.project))}</div><span class="mono muted">${esc(r.session_id.slice(0, 8))}</span></td>
      <td class="num">${dateLabel(r.last_ts)}</td>
      <td class="num">${num(r.assistant_messages)}</td>
      <td class="num">${usd(r.cost)}</td>
      <td class="num">${tok(r.total_tokens)}</td>
    </tr>`).join("");
  document.querySelectorAll("#u-sess-body tr").forEach((tr) => { tr.onclick = () => openSession(tr.dataset.id); });
}

/* ---------- ACTIVITY (tool / file / skill / subagent attribution) ---------- */
const ACTIVITY = { data: null };

async function renderActivity() {
  const el = document.getElementById("activity");
  if (!ACTIVITY.data) {
    el.innerHTML = `<p class="muted" style="padding:12px 2px">Loading activity…</p>`;
    try {
      const res = await fetch("/api/tools");
      const d = await res.json();
      if (!res.ok) throw new Error(d.error || "load failed");
      ACTIVITY.data = d;
    } catch (e) {
      el.innerHTML = `<p style="color:var(--high)">${esc(e.message)}</p>`;
      return;
    }
  }
  drawActivity(ACTIVITY.data);
}

function drawActivity(d) {
  const el = document.getElementById("activity");
  const tools = d.tool || [], files = d.file || [], skills = d.skill || [], subs = d.subagent || [];
  const totalCalls = tools.reduce((a, t) => a + t.calls, 0);
  const totalBytes = tools.reduce((a, t) => a + t.result_bytes, 0);

  el.innerHTML = `
    <div class="cards">
      ${card("Tool calls", num(totalCalls), tools.length + " distinct tools")}
      ${card("Files touched", num(files.length), "read / edit / write")}
      ${card("Context injected", bytes(totalBytes), "≈ tool output fed back")}
      ${card("Skills / subagents", num(skills.length) + " / " + num(subs.length), "invocations tracked")}
    </div>

    <div class="grid-2">
      <div class="panel">
        <h2>Most-used tools <span class="hint">by call count</span></h2>
        <div class="chart-wrap tall"><canvas id="ac-tools"></canvas></div>
      </div>
      <div class="panel">
        <h2>Context injected back <span class="hint">approx · tool_result bytes</span></h2>
        <div class="chart-wrap tall"><canvas id="ac-bytes"></canvas></div>
        <p class="muted small">A proxy for the tokens each tool pushed into context, not an isolated cost — token usage is billed per message, not per tool.</p>
      </div>
    </div>

    <div class="panel">
      <h2>File hotspots <span class="hint">files touched most, with output fed back</span></h2>
      <div class="scroll" style="max-height:340px"><table>
        <thead><tr><th>File</th><th class="num">Touches</th><th class="num">≈ Context</th></tr></thead>
        <tbody>${files.slice(0, 40).map((f) => `
          <tr><td><div class="proj-name" title="${esc(f.key)}">${esc(shortProj(f.key))}</div></td>
          <td class="num">${num(f.calls)}</td><td class="num">${bytes(f.result_bytes)}</td></tr>`).join("")
        || `<tr><td colspan="3" class="muted">No file activity recorded.</td></tr>`}</tbody>
      </table></div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h2>Skills <span class="hint">invocations</span></h2>
        <div class="scroll" style="max-height:260px"><table>
          <thead><tr><th>Skill</th><th class="num">Uses</th></tr></thead>
          <tbody>${skills.map((s) => `<tr><td>${esc(s.key)}</td><td class="num">${num(s.calls)}</td></tr>`).join("")
          || `<tr><td colspan="2" class="muted">No skills invoked.</td></tr>`}</tbody>
        </table></div>
      </div>
      <div class="panel">
        <h2>Subagents <span class="hint">Agent / Task by type</span></h2>
        <div class="scroll" style="max-height:260px"><table>
          <thead><tr><th>Subagent</th><th class="num">Spawns</th></tr></thead>
          <tbody>${subs.map((s) => `<tr><td>${esc(s.key)}</td><td class="num">${num(s.calls)}</td></tr>`).join("")
          || `<tr><td colspan="2" class="muted">No subagents spawned.</td></tr>`}</tbody>
        </table></div>
      </div>
    </div>`;

  const topTools = tools.slice(0, 15);
  barChart("ac-tools", topTools.map((t) => t.key), [{ label: "Calls", data: topTools.map((t) => t.calls), color: "#6ea8fe" }], { horizontal: true });

  const topBytes = [...tools].sort((a, b) => b.result_bytes - a.result_bytes).slice(0, 15);
  barChart("ac-bytes", topBytes.map((t) => t.key), [{ label: "Bytes", data: topBytes.map((t) => t.result_bytes), color: "#d97757" }], { horizontal: true, yFmt: bytes });
}

/* ---------- PROJECTS ---------- */
function renderProjects(s) {
  const el = document.getElementById("projects");
  const entries = Object.entries(s.by_project);
  el.innerHTML = `
    <div class="panel">
      <h2>Cost by project</h2>
      <div class="chart-wrap tall"><canvas id="pr-bar"></canvas></div>
    </div>
    <div class="panel">
      <h2>All projects</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Project</th><th class="num">Cost</th><th class="num">Msgs</th><th class="num">In</th><th class="num">Out</th><th class="num">Cache rd</th><th class="num">Tokens</th></tr></thead>
          <tbody>${entries.map(([p, v]) => `
            <tr><td><div class="proj-name" title="${esc(p)}">${esc(shortProj(p))}</div></td>
            <td class="num">${usd(v.cost)}</td>
            <td class="num">${num(v.messages)}</td>
            <td class="num">${tok(v.input_tokens)}</td>
            <td class="num">${tok(v.output_tokens)}</td>
            <td class="num">${tok(v.cache_read_tokens)}</td>
            <td class="num">${tok(v.total_tokens)}</td></tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
  const top = entries.slice(0, 12);
  barChart("pr-bar", top.map(([p]) => shortProj(p)),
    [{ label: "Cost", data: top.map(([, v]) => v.cost), color: "#d97757" }],
    { horizontal: true, yFmt: usd });
}

/* ---------- INSIGHTS ---------- */
function renderInsights(s) {
  const el = document.getElementById("insights");
  const d = s.insights || {};
  const items = d.items || [];
  const gradeColor = { A: "#4ec9a5", B: "#6ea8fe", C: "#e6b450", D: "#e5716a" }[d.grade] || "#8b96a5";
  const groups = [
    ["savings", "💡 Opportunities to cut cost", "money you could actually save"],
    ["signal", "🎯 Where to focus", "concentration & where-to-look — not additive savings"],
    ["positive", "✓ Working well", ""],
  ];
  el.innerHTML = `
    <div class="ins-hero">
      <div class="ins-grade" style="--gc:${gradeColor}">
        <div class="g">${d.grade || "–"}</div>
        <div class="gl"><span class="s">${d.score ?? "–"}</span><span class="o">/100</span></div>
      </div>
      <div class="ins-hero-main">
        <div class="ins-eyebrow">Efficiency score <span class="hint">heuristic · cache reuse + model mix + output discipline</span></div>
        ${d.top_saving
          ? `<div class="ins-top">Top opportunity: <b>${esc(d.top_saving.amount)}</b> — ${esc(d.top_saving.title)}</div>`
          : `<div class="ins-top">No high-impact savings detected — nicely optimized.</div>`}
        <div class="ins-count">${gradeWord(d.grade_label)} · ${d.opportunity_count || 0} savings ${d.opportunity_count === 1 ? "opportunity" : "opportunities"} · ${Math.round((d.hit_rate || 0) * 100)}% cache hit rate</div>
      </div>
    </div>
    ${groups.map(([kind, label, sub]) => {
      const g = items.filter((i) => i.kind === kind);
      if (!g.length) return "";
      return `<div class="ins-group">
        <div class="ins-group-h">${label}${sub ? ` <span class="hint">${sub}</span>` : ""}</div>
        ${g.map(insCard).join("")}
      </div>`;
    }).join("")}`;
}

const gradeWord = (l) => esc(l || "");

function insCard(i) {
  const big = i.kind === "savings" && i.impact;
  return `<div class="insight ${i.severity}">
    <div class="ins-icon">${i.icon || "•"}</div>
    <div class="ins-body">
      <div class="ihead"><span class="sev ${i.severity}">${i.severity === "good" ? "✓ good" : i.severity}</span><h3>${esc(i.title)}</h3></div>
      <p>${esc(i.detail)}</p>
    </div>
    ${i.impact ? `<div class="impact ${big ? "big" : ""}">${big ? "↓ " : ""}${esc(i.impact)}</div>` : ""}
  </div>`;
}

/* ---------- WRAPPED (hero / shareable) ---------- */
const fmtHour = (h) => { const am = h < 12 ? "am" : "pm"; let hh = h % 12; if (hh === 0) hh = 12; return hh + am; };

function renderWrapped(s) {
  const w = s.wrapped;
  const el = document.getElementById("wrapped");
  const outWords = (w.output_tokens || 0) * 0.75;
  const novels = outWords / 90000;                 // ~90k words/novel
  const wp = ((w.total_tokens || 0) * 0.75) / 587000; // War & Peace ~587k words
  const readHrs = outWords / 238 / 60;             // 238 wpm

  el.innerHTML = `
    <div class="wrap-actions"><button class="share-btn" id="share-btn">⬇  Download card</button></div>
    <div class="wrap-hero" id="wrap-card">
      <div class="eyebrow">✦ Claude Wrapped</div>
      <h1>Your Claude Code, by the numbers</h1>
      <div class="range">${esc(w.date_from || "")} → ${esc(w.date_to || "")} · ${w.days_active} active days across ${w.span_days} days</div>
      <div class="wrap-big">
        <div class="b"><div class="n accent" data-count="${w.total_cost}" data-fmt="usd">$0</div><div class="l">Total spend</div></div>
        <div class="b"><div class="n" data-count="${w.total_tokens}" data-fmt="tok">0</div><div class="l">Tokens</div></div>
        <div class="b"><div class="n" data-count="${w.total_messages}" data-fmt="num">0</div><div class="l">Messages</div></div>
        <div class="b"><div class="n blue" data-count="${w.sessions}" data-fmt="num">0</div><div class="l">Sessions · ${w.projects} projects</div></div>
      </div>
      <div class="analogies">
        <div class="analogy">✍️ Claude wrote <b>~${novels < 1 ? novels.toFixed(1) : Math.round(novels)} novels</b> of text</div>
        <div class="analogy">📖 that's <b>~${Math.round(readHrs)} hrs</b> of reading</div>
        <div class="analogy">📚 processed <b>~${Math.round(wp)}×</b> War &amp; Peace</div>
        <div class="analogy">🕐 peak <b>${fmtHour(w.peak_hour)}</b> · ${esc(w.peak_weekday)}s</div>
        <div class="analogy">🦉 <b>${Math.round(w.night_owl_pct * 100)}%</b> after 10pm</div>
        <div class="analogy">🔥 <b>${w.longest_streak}-day</b> streak</div>
      </div>
    </div>

    <div class="panel">
      <h2>Your days in Claude <span class="hint">daily message volume, local time · through today</span></h2>
      <div class="calendar-wrap">${calendarHTML(s)}</div>
      <div class="cal-legend">less <span class="c" style="background:#1b212b"></span><span class="c" style="background:#2f6f57"></span><span class="c" style="background:#3f9a72"></span><span class="c" style="background:#57c99a"></span><span class="c" style="background:#8fe9c4"></span> more</div>
    </div>

    <div class="panel">
      <h2>🏆 Records &amp; superlatives</h2>
      <div class="records">${recordsHTML(w)}</div>
    </div>`;

  el.querySelectorAll("[data-count]").forEach(countUp);
  document.getElementById("share-btn").onclick = exportCard;
}

function calendarHTML(s) {
  const by = s.by_day;
  const w = s.wrapped;
  if (!w.date_from) return "";
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const ymd = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const start = new Date(w.date_from + "T00:00:00");
  // Range always runs through TODAY (local), padding trailing days so the gap
  // since the last active day is visible.
  const end = new Date(); end.setHours(0, 0, 0, 0);
  const dto = new Date(w.date_to + "T00:00:00");
  if (dto > end) end.setTime(dto.getTime());
  const d0 = new Date(start);
  d0.setDate(d0.getDate() - ((d0.getDay() + 6) % 7)); // back up to Monday

  let max = 0;
  for (const k in by) max = Math.max(max, by[k].messages);
  const shade = (v) => {
    if (!v) return "#1b212b";                 // in-range, no activity — muted but visible
    const r = v / (max || 1);
    if (r < 0.25) return "#2f6f57";
    if (r < 0.5) return "#3f9a72";
    if (r < 0.75) return "#57c99a";
    return "#8fe9c4";
  };

  let cells = "", months = "", prevMonth = -1;
  const cur = new Date(d0);
  while (cur <= end) {
    const colMonth = cur.getMonth();
    months += colMonth !== prevMonth ? `<span>${MON[colMonth]}</span>` : "<span></span>";
    prevMonth = colMonth;
    for (let i = 0; i < 7; i++) {
      const inRange = cur >= start && cur <= end;
      const rec = by[ymd(cur)];
      const msgs = rec ? rec.messages : 0;
      const bg = inRange ? shade(msgs) : "transparent";
      const title = inRange ? `${ymd(cur)} · ${msgs} msgs · ${rec ? usd(rec.cost) : "$0"}` : "";
      cells += `<div class="c" style="background:${bg}" title="${title}"></div>`;
      cur.setDate(cur.getDate() + 1);
    }
  }
  const weekdays = ["Mon", "", "Wed", "", "Fri", "", "Sun"].map((d) => `<span>${d}</span>`).join("");
  return `<div class="cal">
    <div class="cal-corner"></div>
    <div class="cal-months">${months}</div>
    <div class="cal-weekdays">${weekdays}</div>
    <div class="calendar">${cells}</div>
  </div>`;
}

function recordsHTML(w) {
  const R = [];
  if (w.priciest_day) R.push(["💸", "Priciest day", usd(w.priciest_day.cost), w.priciest_day.date]);
  if (w.busiest_day) R.push(["⚡", "Busiest day", num(w.busiest_day.messages) + " msgs", w.busiest_day.date]);
  if (w.marathon_session) R.push(["🏃", "Marathon session", num(w.marathon_session.messages) + " msgs", shortProj(w.marathon_session.project)]);
  if (w.priciest_session) R.push(["👑", "Priciest session", usd(w.priciest_session.cost), shortProj(w.priciest_session.project)]);
  if (w.biggest_message) R.push(["🧠", "Biggest single message", tok(w.biggest_message.total_tokens) + " tok", usd(w.biggest_message.cost) + " · " + shortProj(w.biggest_message.project)]);
  R.push(["🔥", "Longest streak", w.longest_streak + " days", "current: " + w.current_streak + " days"]);
  if (w.top_model) R.push(["🤖", "Model of choice", w.top_model.name.replace("claude-", ""), Math.round(w.top_model.share * 100) + "% of messages"]);
  R.push(["📅", "Avg per active day", usd(w.avg_cost_per_day), w.days_active + " active days"]);
  return R.map(([ico, l, v, sub]) =>
    `<div class="record"><div class="ico">${ico}</div><div class="rl">${esc(l)}</div><div class="rv">${esc(v)}</div><div class="rs" title="${esc(sub)}">${esc(sub)}</div></div>`
  ).join("");
}

function countUp(elem) {
  const target = parseFloat(elem.dataset.count) || 0;
  const f = { usd, tok, num }[elem.dataset.fmt] || num;
  const dur = 900, t0 = performance.now();
  function step(t) {
    const p = Math.min(1, (t - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    elem.textContent = f(target * e);
    if (p < 1) requestAnimationFrame(step);
    else elem.textContent = f(target);
  }
  requestAnimationFrame(step);
}

async function exportCard() {
  const node = document.getElementById("wrap-card");
  const btn = document.getElementById("share-btn");
  const old = btn.textContent;
  btn.textContent = "Rendering…";
  try {
    const canvas = await html2canvas(node, {
      backgroundColor: "#10141b",
      scale: 2,
      // html2canvas can't render `background-clip: text` (the gradient fills the
      // whole box instead of the glyphs), so force the gradient numbers to solid
      // colors in the cloned DOM only — the live page keeps its gradient.
      onclone: (doc) => {
        doc.querySelectorAll("#wrap-card .wrap-big .n").forEach((el) => {
          const c = el.classList.contains("accent") ? "#f0a887"
            : el.classList.contains("blue") ? "#6ea8fe" : "#ffffff";
          el.style.background = "none";
          el.style.webkitBackgroundClip = "border-box";
          el.style.backgroundClip = "border-box";
          el.style.webkitTextFillColor = c;
          el.style.color = c;
        });
      },
    });
    const a = document.createElement("a");
    a.download = "claude-wrapped.png";
    a.href = canvas.toDataURL("image/png");
    a.click();
  } catch (e) {
    alert("Export failed: " + e.message);
  }
  btn.textContent = old;
}

/* ---------- SESSION DRAWER ---------- */
async function openSession(id) {
  const drawer = document.getElementById("drawer");
  const content = document.getElementById("drawer-content");
  drawer.classList.remove("hidden");
  content.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  let d;
  try {
    const res = await fetch("/api/session/" + encodeURIComponent(id));
    d = await res.json();
    if (!res.ok) throw new Error(d.error || "load failed");
  } catch (e) {
    content.innerHTML = `<p style="color:var(--high)">${esc(e.message)}</p>`;
    return;
  }
  content.innerHTML = `
    <h2 style="margin-top:0">${esc(shortProj(d.cwd))}</h2>
    <p class="mono muted">${esc(d.session_id)}${d.git_branch ? " · " + esc(d.git_branch) : ""}</p>
    <div class="cards" style="margin:16px 0">
      ${card("Cost", usd(d.cost), "")}
      ${card("Tokens", tok(d.total_tokens), "")}
      ${card("Cache read", tok(d.cache_read_tokens), "")}
      ${card("Compactions", num(d.compactions), "")}
    </div>
    <div>${d.models.map((m) => `<span class="pill">${esc(m)}</span>`).join("")}</div>
    <h2 style="margin-top:20px">Replay <span class="hint">${d.events.length} turns</span></h2>
    ${d.events.map(renderEvent).join("")}`;
}

function renderEvent(e) {
  if (e.role === "compaction")
    return `<div class="event compaction">${esc(e.text)}</div>`;
  const meta = e.role === "assistant" && e.cost != null
    ? `${esc(e.model || "")} · ${usd(e.cost)} · ${tok(e.total_tokens)}` : "";
  return `<div class="event ${e.role}">
    <div class="erole"><span>${e.role}</span><span>${meta}</span></div>
    <pre>${esc(e.text)}</pre></div>`;
}

function closeDrawer() { document.getElementById("drawer").classList.add("hidden"); }

/* ---------- chart + card helpers ---------- */
function card(label, value, sub) {
  return `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub || "")}</div></div>`;
}

function baseOpts(extra = {}) {
  return Object.assign({
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { enabled: true } },
    scales: {
      x: { grid: { color: "#232935" }, ticks: { color: "#8b96a5", maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
      y: { grid: { color: "#232935" }, ticks: { color: "#8b96a5" }, beginAtZero: true },
    },
  }, extra);
}

function lineChart(id, labels, series, opts = {}) {
  destroy(id);
  const o = baseOpts();
  if (opts.yFmt) o.scales.y.ticks.callback = (v) => opts.yFmt(v);
  charts[id] = new Chart(document.getElementById(id), {
    type: "line",
    data: { labels, datasets: series.map((s) => ({
      label: s.label, data: s.data, borderColor: s.color,
      backgroundColor: s.fill ? s.color + "22" : "transparent",
      fill: !!s.fill, tension: 0.25, pointRadius: 0, borderWidth: 2,
    })) },
    options: o,
  });
}

function barChart(id, labels, series, opts = {}) {
  destroy(id);
  const o = baseOpts();
  if (opts.horizontal) o.indexAxis = "y";
  const valAxis = opts.horizontal ? o.scales.x : o.scales.y;
  if (opts.yFmt) valAxis.ticks.callback = (v) => opts.yFmt(v);
  charts[id] = new Chart(document.getElementById(id), {
    type: "bar",
    data: { labels, datasets: series.map((s) => ({ label: s.label, data: s.data, backgroundColor: s.color, borderRadius: 4 })) },
    options: o,
  });
}

function doughnut(id, labels, data, colors, fmt) {
  destroy(id);
  charts[id] = new Chart(document.getElementById(id), {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: "#171b22", borderWidth: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "62%",
      plugins: {
        legend: { position: "right", labels: { color: "#e6e9ee", boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: (c) => `${c.label}: ${fmt ? fmt(c.raw) : c.raw}` } },
      },
    },
  });
}

function destroy(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

boot();
