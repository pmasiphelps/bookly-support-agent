// Bookly Agent Studio -- the operator console for the support agent.
// Information architecture follows Decagon's admin portal: Home, Convos,
// Watchtower, Build (AOPs / Tools / Settings / Releases), Tests, Preview.
// Vanilla JS on purpose (same as the customer widget): no build step.

// ===========================================================================
// helpers
// ===========================================================================

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value" || (k in el && typeof v !== "string")) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch("/api/studio" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || res.statusText));
  }
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3500);
}

function guard(fn) {
  return async (...args) => {
    try { return await fn(...args); } catch (err) { toast(err.message, true); }
  };
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("studio." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("studio." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const stamp = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
const pill = (text, kind = "") => h("span", { class: "pill " + kind }, text);
const pct = (v) => (v === null || v === undefined ? "–" : `${v}%`);
const ms = (v) => (v === null || v === undefined ? "–" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
const compact = (n) => (n === null || n === undefined ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n));
const rid = () => Math.random().toString(36).slice(2, 10);
const label = (s) => (s || "").replaceAll("_", " ");
// The demo's signed-in user. Used whenever no name is typed in the top bar,
// and shown in place of placeholder names saved before names were required.
const DEFAULT_AUTHOR = "Patrick Phrayme";
const PLACEHOLDER_AUTHORS = new Set(["", "unknown", "demo@bookly", "claude (returns conversion)"]);
const author = () => $("#author").value.trim() || DEFAULT_AUTHOR;
const who = (name) => (PLACEHOLDER_AUTHORS.has((name || "").trim()) ? DEFAULT_AUTHOR : name);

const ROUTE_LABEL = { agent_loop: "agent loop", returns_flow: "returns flow", declined: "declined", clarify: "clarify", error: "error" };
const routePill = (r) => pill(ROUTE_LABEL[r] || r || "?", r === "declined" ? "warn" : r === "error" ? "bad" : r === "returns_flow" ? "info" : "accent");
const TAG_KIND = { Deflected: "good", Escalated: "bad", Declined: "warn" };
const tagPill = (t) => pill(t, TAG_KIND[t] || "");

function openModal(...content) {
  $("#modalBody").replaceChildren(...content);
  $("#modal").hidden = false;
  const first = $("#modalBody input, #modalBody textarea, #modalBody select");
  if (first) first.focus();
}
function closeModal() { $("#modal").hidden = true; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
function openDrawer(...content) { $("#drawerBody").replaceChildren(...content); $("#drawer").hidden = false; }
function closeDrawer() { $("#drawer").hidden = true; }
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });
$(".drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });

function formModal(title, fields, actionLabel, onSubmit, { danger = false } = {}) {
  const err = h("div", { class: "modal-error" });
  const btn = h("button", { class: "btn " + (danger ? "danger" : "primary"), type: "submit" }, actionLabel);
  const form = h("form", { class: "form-grid", onsubmit: async (e) => {
    e.preventDefault();
    btn.disabled = true;
    err.textContent = "";
    try { await onSubmit(); closeModal(); } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
  } },
    h("h2", {}, title), fields, err,
    h("div", { class: "modal-actions" }, h("button", { class: "btn", type: "button", onclick: closeModal }, "Cancel"), btn));
  openModal(form);
}

// ===========================================================================
// state + chrome
// ===========================================================================

const state = {
  meta: null,
  versions: [],
  deployment: null,
  tools: [],
  aops: [],
  filters: store.get("filters", { env: ["production", "simulation"], window: "7d" }),
  dirtyGuard: null,   // returns true if the current view has unsaved edits
  timers: [],
};

$("#author").value = store.get("author", "") || DEFAULT_AUTHOR;
$("#author").addEventListener("change", () => store.set("author", author()));

async function refreshVersions() {
  [state.versions, state.deployment] = await Promise.all([api("GET", "/versions"), api("GET", "/deployment")]);
  const cur = state.deployment.current;
  $("#prodBadge").textContent = !cur ? "Nothing deployed"
    : cur.canary_version_id ? `Production v${cur.production_version_id} · canary v${cur.canary_version_id} @ ${cur.canary_percent}%`
    : `Production: v${cur.production_version_id}`;
}

function versionOptions(includeDraft) {
  const opts = [];
  if (includeDraft) opts.push(h("option", { value: "draft" }, "Draft (saved)"));
  for (const v of state.versions) {
    const tag = v.labels.length ? ` · ${v.labels.join(", ")}` : "";
    opts.push(h("option", { value: String(v.id) }, `v${v.id}${tag} — ${v.note.slice(0, 40)}`));
  }
  return opts;
}

// Shared traffic filter (Home, Convos, Watchtower).
function envQuery(extra = {}) {
  const q = new URLSearchParams({ env: state.filters.env.join(",") || "production", window: state.filters.window });
  for (const [k, v] of Object.entries(extra)) if (v !== "" && v !== null && v !== undefined) q.set(k, v);
  return q;
}
// Shared traffic filter (Home, Convos, Watchtower): the time window stays
// visible; traffic sources live behind a "Filters" button.
function filterBar(onChange) {
  const envs = [["production", "Production"], ["simulation", "Simulated"], ["playground", "Preview"]];
  const changed = () => { store.set("filters", state.filters); onChange(); };
  const nonDefault = state.filters.env.join(",") !== "production,simulation";
  const pop = h("div", { class: "popover", hidden: true },
    h("div", { class: "side-label", style: "margin:0 0 6px" }, "Traffic"),
    envs.map(([v, l]) => h("label", { class: "inline-check" }, h("input", { type: "checkbox", checked: state.filters.env.includes(v), onchange: (e) => {
      state.filters.env = e.target.checked ? [...state.filters.env, v] : state.filters.env.filter((x) => x !== v);
      changed();
    } }), l)));
  const btn = h("button", { class: "btn", type: "button", "aria-expanded": "false", onclick: (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
    btn.setAttribute("aria-expanded", String(!pop.hidden));
  } }, "Filters", nonDefault ? h("span", { class: "dot-badge", "aria-label": "(changed)" }) : null);
  document.addEventListener("click", (e) => { if (!pop.contains(e.target) && e.target !== btn) pop.hidden = true; });
  return h("div", { class: "filters" },
    h("select", { "aria-label": "Time window", onchange: (e) => { state.filters.window = e.target.value; changed(); } },
      [["1h", "Last hour"], ["24h", "Last 24 hours"], ["7d", "Last 7 days"], ["30d", "Last 30 days"], ["all", "All time"]]
        .map(([v, l]) => h("option", { value: v, selected: state.filters.window === v }, l))),
    h("div", { class: "pop-wrap" }, btn, pop));
}

// Conversations are analyzed (AI summary, QA, Watchtower) automatically when
// a page that shows that analysis opens -- no button. At most once a minute,
// so a conversation the model can't analyze doesn't cause a retry loop.
let lastAutoAnalysis = 0;
async function autoAnalyze(onDone) {
  if (Date.now() - lastAutoAnalysis < 60e3) return;
  lastAutoAnalysis = Date.now();
  let r;
  try { r = await api("POST", "/analysis", { env: state.filters.env.join(","), window: state.filters.window, limit: 20 }); } catch { return; }
  if (!r.running) return;
  const chip = $("#activity");
  chip.textContent = `Analyzing ${r.queued} conversation${r.queued === 1 ? "" : "s"}…`;
  chip.hidden = false;
  const poll = setInterval(async () => {
    let st;
    try { st = await api("GET", "/analysis"); } catch { return; }
    chip.textContent = `Analyzing ${st.done}/${st.queued}…`;
    if (!st.running) { clearInterval(poll); chip.hidden = true; onDone && onDone(); }
  }, 2500);
  state.timers.push(poll);
}

// Collapsed section at the bottom of a page for demo and debugging tools --
// out of sight unless you open it.
function demoSection(...content) {
  return demoBox("Demo & diagnostics", ...content);
}
function demoBox(title, ...content) {
  return h("details", { class: "demo-tools" }, h("summary", {}, title), h("div", { class: "demo-body" }, content));
}

// Scripted content for the live demo (see README › Demo).
const DEMO_AOP = {
  name: "Order cancellation",
  when: "The customer wants to cancel an order.",
  logic: "1. get the sender's order ID and email address\n" +
    "2. check the order's status with @get_order_status \n" +
    "a. If it's still processing, it hasn't left our warehouse: follow #Escalation request so the team cancels it.\n" +
    "b. If it has shipped, it can't be cancelled. Tell them they can refuse the delivery when it arrives, and the refund is issued once it's back with us -- or they can return it within 30 days of delivery.",
};
const DEMO_MESSAGES = [
  { label: "Shipped order (BK-1002)", text: "Can I cancel order BK-1002? bob@bookly-demo.com" },
  { label: "Processing order (BK-1003)", text: "I want to cancel my order BK-1003, my email is carol@bookly-demo.com" },
];

// A "⋯" menu of secondary actions.
function moreMenu(...items) {
  const d = h("details", { class: "menu" }, h("summary", { "aria-label": "More actions", title: "More actions" }, "⋯"),
    h("div", { class: "menu-items" }, items));
  d.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => d.removeAttribute("open"));
  return d;
}

const windowLabel = () => ({ "1h": "the last hour", "24h": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days", all: "all time" })[state.filters.window];

function simulateTrafficDialog(onDone) {
  const count = h("input", { type: "number", min: 1, max: 40, value: 8 });
  formModal("Simulate customer traffic", [
    h("p", { class: "muted small" }, "Simulated customers (order lookups, returns, lost packages, people asking for a human, off-topic requests) chat with the deployed agent, canary split included. They're logged as \"Simulated\" traffic. Uses real model calls."),
    h("label", {}, "Conversations", count),
  ], "Start", async () => {
    const r = await api("POST", "/monitor/simulate", { count: Number(count.value) });
    if (!state.filters.env.includes("simulation")) { state.filters.env.push("simulation"); store.set("filters", state.filters); }
    toast(`Simulating ${r.started} conversations -- this page refreshes as they land`);
    let n = 0;
    const t = setInterval(() => { n++; if (n > 30) clearInterval(t); onDone && onDone(); }, 6000);
    state.timers.push(t);
  });
}

// ===========================================================================
// router
// ===========================================================================

function parseHash() {
  const parts = location.hash.replace(/^#/, "").split("/").map(decodeURIComponent);
  return { route: parts[0] || "home", parts };
}

let currentHash = null;
async function route() {
  if (state.dirtyGuard && state.dirtyGuard() && location.hash !== currentHash) {
    if (!confirm("You have unsaved changes. Leave anyway?")) { history.replaceState(null, "", currentHash); return; }
  }
  state.dirtyGuard = null;
  state.timers.forEach((t) => { clearInterval(t); clearTimeout(t); });
  state.timers = [];
  currentHash = location.hash;
  const { route: r, parts } = parseHash();
  $$(".nav-item").forEach((a) => a.classList.toggle("active", a.dataset.route === r));
  $("#subnav").hidden = r !== "build";
  $$("#subnav a").forEach((a) => a.classList.toggle("active", a.dataset.sub === (parts[1] || "aops")));
  $("#view").replaceChildren(h("div", { class: "muted" }, "Loading…"));
  const build = { aops: () => (parts[2] ? renderAopEditor(parts[2]) : renderAops()), tools: renderTools, settings: renderSettings, releases: renderReleases };
  const pages = {
    home: renderHome,
    convos: () => renderConvos(parts[1]),
    watchtower: () => renderWatchtower(parts[1] ? Number(parts[1]) : null),
    build: () => (build[parts[1] || "aops"] || renderAops)(),
    tests: renderTests,
    preview: renderPreview,
  };
  try {
    await (pages[r] || renderHome)();
  } catch (err) {
    $("#view").replaceChildren(h("div", { class: "empty" }, "Couldn't load this page: " + err.message));
  }
}
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => { if (state.dirtyGuard && state.dirtyGuard()) { e.preventDefault(); e.returnValue = ""; } });

function page(title, subtitle, actions, ...body) {
  // replaceChildren() renders null as the text "null", unlike h().
  $("#view").replaceChildren(...[
    title ? h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
      actions ? h("div", { class: "toolbar-actions" }, actions) : null) : null,
    ...body,
  ].flat(Infinity).filter((x) => x !== null && x !== undefined && x !== false));
}

// ===========================================================================
// audit trail: what shaped one reply
// ===========================================================================

function knowledgeUsed(toolCalls) {
  const titles = [];
  for (const c of toolCalls || []) {
    if (c.name === "search_policies" && c.result && c.result.results) titles.push(...c.result.results.map((r) => r.title));
  }
  return [...new Set(titles)];
}

function aopChip(name) {
  const aop = state.aops.find((x) => x.name === name);
  return aop ? h("a", { class: "chip aop", href: `#build/aops/${aop.id}` }, "#" + name) : h("span", { class: "chip aop" }, "#" + name);
}

function auditView(t) {
  const calls = (t.tool_calls || []).filter((c) => c.name !== "use_aop");
  const llm = t.llm_calls || [];
  const tokens = (t.input_tokens || 0) + (t.output_tokens || 0);
  const knowledge = knowledgeUsed(t.tool_calls);
  return h("div", { class: "audit" },
    h("div", { class: "audit-row" }, h("b", {}, "Route"), routePill(t.route), t.category ? pill(t.category) : null,
      t.escalated ? pill("escalated", "bad") : null,
      h("span", { class: "muted" }, `${ms(t.latency_ms)} · ${llm.length} model call${llm.length === 1 ? "" : "s"} · ${tokens} tokens`)),
    h("div", { class: "audit-row" }, h("b", {}, "AOPs"), (t.aops || []).length ? t.aops.map(aopChip) : h("span", { class: "muted" }, "none -- answered from the base prompt")),
    h("div", { class: "audit-row" }, h("b", {}, "Knowledge"), knowledge.length ? knowledge.map((k) => pill(k, "info")) : h("span", { class: "muted" }, "none")),
    h("div", { class: "audit-row" }, h("b", {}, "Tools"), calls.length ? calls.map((c) => h("span", { class: "chip tool" }, "@" + c.name)) : h("span", { class: "muted" }, "none")),
    t.error ? h("pre", { class: "code" }, "Error: " + t.error) : null,
    (t.tool_calls || []).length || llm.length ? h("details", {}, h("summary", {}, "Raw trace"),
      llm.map((c) => h("div", { class: "audit-row small" }, pill(c.purpose, "info"), h("code", {}, c.model), h("span", { class: "muted" }, `${ms(c.latency_ms)} · in ${c.input_tokens} / out ${c.output_tokens} · ${c.stop_reason || ""}`), c.error ? pill(c.error, "bad") : null)),
      (t.tool_calls || []).map((c) => h("pre", { class: "code" }, `${c.name}(${JSON.stringify(c.input)})\n→ ${JSON.stringify(c.result, null, 2)}`))) : null);
}

// A transcript where clicking an agent message reveals its audit trail.
function transcriptView(turns, { channel = "chat" } = {}) {
  const email = channel === "email";
  return h("div", { class: "transcript" }, turns.map((t) => {
    const audit = auditView(t);
    audit.hidden = true;
    const agentMsg = h("button", {
      class: "msg agent" + (email ? " email" : ""), type: "button", title: "Show what shaped this reply",
      onclick: () => { audit.hidden = !audit.hidden; agentMsg.classList.toggle("open", !audit.hidden); },
    }, email ? h("div", { class: "email-head" }, "From: Bookly Support") : null, t.reply);
    return [
      h("div", { class: "msg user" + (email ? " email" : "") }, email ? h("div", { class: "email-head" }, "From: customer") : null, t.user ?? t.user_text),
      agentMsg, audit,
      (t.checks || []).length ? h("div", {}, t.checks.map(checkView)) : null,
    ];
  }));
}

function checkView(c) {
  return h("div", { class: "check" }, h("span", { class: c.passed ? "ok" : "ko" }, c.passed ? "✓" : "✗"), h("span", {}, c.name), c.passed ? null : h("span", { class: "detail" }, c.detail));
}

// ===========================================================================
// HOME
// ===========================================================================

async function renderHome() {
  autoAnalyze(renderHome);
  if (!state.aops.length) state.aops = (await api("GET", "/aops")).aops;
  const s = await api("GET", "/monitor/summary?" + envQuery());
  const t = s.totals;
  const tile = (lbl, value, sub) => h("div", { class: "tile" }, h("div", { class: "value" }, value), h("div", { class: "label" }, lbl), sub ? h("div", { class: "sub" }, sub) : null);
  page("Overview", `Showing ${windowLabel()}.`, [filterBar(renderHome)],
    h("div", { class: "tiles tiles-3" },
      tile("Conversations", compact(t.conversations)),
      tile("Deflection rate", pct(t.automation_rate)),
      tile("User messages per conversation", t.messages_per_conversation ?? "–")),
    h("div", { class: "card" }, h("h2", { class: "section-title" }, "Daily conversations"), volumeChart(s)),
    h("h2", { class: "section-title" }, "Customer satisfaction"),
    h("div", { class: "tiles tiles-3" },
      tile("CSAT", pct(t.csat), `${t.csat_responses} rating${t.csat_responses === 1 ? "" : "s"}`),
      tile("QA score", t.qa_avg_score ? `${t.qa_avg_score} / 5` : "–", `${t.qa_reviewed} analyzed`),
      tile("Escalation rate", pct(t.escalation_rate))),
    h("div", { class: "card" }, h("h2", { class: "section-title" }, "Top AOPs"),
      Object.keys(s.aop_usage).length ? hbars(s.aop_usage) : h("div", { class: "nodata" }, "No AOPs used in this window.")),
    demoSection(
      h("div", { class: "toolbar-actions" }, h("button", { class: "btn", onclick: () => simulateTrafficDialog(renderHome) }, "Simulate traffic…"),
        h("span", { class: "muted small" }, "Simulated customers chat with the deployed agent, canary split included.")),
      h("h3", {}, "By version"), versionCompare(s.by_version),
      h("div", { class: "grid-charts" },
        h("div", {}, h("h3", {}, "Intent mix"), hbars(s.intents)),
        h("div", {}, h("h3", {}, "Tool health"), toolHealth(s.tools))),
      h("dl", { class: "kv" },
        h("dt", {}, "Latency p50 / p95"), h("dd", {}, `${ms(t.p50_latency_ms)} / ${ms(t.p95_latency_ms)}`),
        h("dt", {}, "Tokens"), h("dd", {}, `${compact(t.input_tokens)} in · ${compact(t.output_tokens)} out`),
        h("dt", {}, "Turn errors"), h("dd", {}, t.turn_errors))));
}

function hbars(counts, total) {
  const entries = Object.entries(counts || {});
  if (!entries.length) return h("div", { class: "nodata" }, "No data in this window.");
  const max = Math.max(...entries.map(([, v]) => v));
  total = total || entries.reduce((a, [, v]) => a + v, 0);
  return h("div", { class: "hbars" }, entries.map(([k, v]) => [
    h("span", {}, label(k)),
    h("div", { class: "hbar-track", title: `${k}: ${v} (${Math.round((100 * v) / total)}%)` }, h("div", { class: "hbar-fill", style: `width:${(100 * v) / max}%` })),
    h("span", { class: "hbar-val" }, `${v} · ${Math.round((100 * v) / total)}%`),
  ]));
}

function toolHealth(tools) {
  if (!tools.length) return h("div", { class: "nodata" }, "No tool calls in this window.");
  return h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Tool"), h("th", { class: "num" }, "Calls"), h("th", { class: "num" }, "Failures"), h("th", { class: "num" }, "Failure rate"))),
    h("tbody", {}, tools.map((x) => h("tr", {}, h("td", {}, h("span", { class: "chip tool" }, "@" + x.name)), h("td", { class: "num" }, x.calls), h("td", { class: "num" }, x.failures), h("td", { class: "num" }, x.failures ? pill(pct(x.failure_rate), "bad") : pct(x.failure_rate))))));
}

function versionCompare(rows) {
  if (!rows.length) return h("div", { class: "nodata" }, "No conversations in this window.");
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Version", "Conversations", "Deflection", "Escalation", "CSAT", "QA score", "Flag rate", "p50 latency", "Errors"].map((x, i) => h("th", { class: i ? "num" : "" }, x)))),
    h("tbody", {}, rows.map((v) => h("tr", {},
      h("td", {}, v.version_id ? `v${v.version_id} ` : "draft ", ((state.versions.find((x) => x.id === v.version_id) || { labels: [] }).labels).map((l) => pill(l, l === "production" ? "good" : "warn"))),
      h("td", { class: "num" }, v.conversations), h("td", { class: "num" }, pct(v.automation_rate)), h("td", { class: "num" }, pct(v.escalation_rate)),
      h("td", { class: "num" }, pct(v.csat)), h("td", { class: "num" }, v.qa_score ?? "–"), h("td", { class: "num" }, pct(v.flag_rate)),
      h("td", { class: "num" }, ms(v.p50_latency_ms)), h("td", { class: "num" }, v.errors))))));
}

function bucketKeys(series, unit, window) {
  const spans = { "24h": 24, "7d": 7, "30d": 30 };
  if (!spans[window]) return series.map((p) => p.bucket);
  const keys = [];
  const now = Date.now();
  for (let i = spans[window] - 1; i >= 0; i--) {
    const iso = new Date(now - i * (unit === "hour" ? 3600e3 : 86400e3)).toISOString();
    keys.push(unit === "hour" ? iso.slice(0, 13) + ":00" : iso.slice(0, 10));
  }
  return keys;
}
const fmtBucket = (k, unit) => (unit === "hour"
  ? new Date(k.replace(" ", "T") + ":00Z").toLocaleTimeString(undefined, { hour: "numeric" })
  : new Date(k + "T00:00:00Z").toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }));

// Bar chart with a per-bar tooltip. `segments(point)` -> [{value, color}]:
// stacked bottom-up, or (overlay) drawn on top of each other -- e.g. a
// deflected bar in front of the total-conversations bar.
function barChart(points, { unit, segments, tipText, overlay = false, empty }) {
  if (!points.length || points.every((p) => !segments(p).some((s) => s.value))) return h("div", { class: "nodata" }, empty);
  const totals = points.map((p) => (overlay ? Math.max(...segments(p).map((s) => s.value)) : segments(p).reduce((a, s) => a + s.value, 0)));
  const max = Math.max(1, ...totals);
  const tip = h("div", { class: "chart-tip", hidden: true });
  const wrap = h("div", { class: "vchart" },
    h("div", { class: "vbars" }, points.map((p, i) => {
      const segs = segments(p);
      const bar = h("div", {
        class: "vbar",
        onmouseenter: (e) => {
          tip.textContent = tipText(p);
          const r = e.currentTarget.getBoundingClientRect();
          const w = wrap.getBoundingClientRect();
          tip.style.left = `${r.left - w.left + r.width / 2}px`;
          tip.style.top = `${r.bottom - w.top - (r.height * totals[i]) / max}px`;
          tip.hidden = false;
        },
        onmouseleave: () => (tip.hidden = true),
      });
      if (overlay) {
        segs.forEach((s) => bar.append(h("div", { class: "overlay", style: `height:${(100 * s.value) / max}%;background:${s.color}` })));
      } else {
        bar.append(h("div", { class: "stack", style: `height:${(100 * totals[i]) / max}%` },
          segs.filter((s) => s.value).map((s) => h("div", { class: "seg", style: `flex:${s.value};background:${s.color}` }))));
      }
      return bar;
    })),
    h("div", { class: "vaxis" }, h("span", {}, fmtBucket(points[0].bucket, unit)), h("span", {}, `peak ${max}/${unit}`), h("span", {}, fmtBucket(points[points.length - 1].bucket, unit))),
    tip);
  return wrap;
}

function volumeChart(s) {
  const by = Object.fromEntries(s.series.map((p) => [p.bucket, p]));
  const pts = bucketKeys(s.series, s.series_unit, s.window).map((k) => by[k] || { bucket: k, conversations: 0, deflected: 0 });
  return h("div", {},
    barChart(pts, {
      unit: s.series_unit, overlay: true,
      segments: (p) => [{ value: p.conversations, color: "var(--bar-muted)" }, { value: p.deflected, color: "var(--bar)" }],
      tipText: (p) => `${fmtBucket(p.bucket, s.series_unit)}: ${p.conversations} conversations, ${p.deflected} deflected`,
      empty: "No conversations yet. Chat in the customer widget, or click Simulate traffic.",
    }),
    h("div", { class: "legend" }, h("span", { style: "--sw:var(--bar)" }, "Deflected"), h("span", { style: "--sw:var(--bar-muted)" }, "Total conversations")));
}

// ===========================================================================
// CONVOS
// ===========================================================================

const convoState = { status: "", tag: "", search: "", sample: null };

async function renderConvos(selectedId) {
  autoAnalyze(() => renderConvos(selectedId));
  if (!state.aops.length) state.aops = (await api("GET", "/aops")).aops;
  const q = envQuery({ status: convoState.status, tag: convoState.tag, search: convoState.search, limit: 300 });
  let [list, detail] = await Promise.all([
    api("GET", "/convos?" + q),
    selectedId ? api("GET", `/convos/${encodeURIComponent(selectedId)}`).catch(() => null) : null,
  ]);
  if (convoState.sample) list = convoState.sample.map((id) => list.find((c) => c.id === id)).filter(Boolean);

  // One filter dropdown: review status and tags together.
  const current = convoState.status ? "s:" + convoState.status : convoState.tag ? "t:" + convoState.tag : "";
  const filter = h("select", { "aria-label": "Filter conversations", onchange: (e) => {
    const v = e.target.value;
    convoState.status = v.startsWith("s:") ? v.slice(2) : "";
    convoState.tag = v.startsWith("t:") ? v.slice(2) : "";
    renderConvos(selectedId);
  } },
    h("option", { value: "" }, "All conversations"),
    h("optgroup", { label: "Review" }, [["needs_review", "Needs review"], ["assigned_open", "Assigned, open"], ["flagged", "QA flagged"], ["negative_csat", "Thumbs down"]]
      .map(([v, l]) => h("option", { value: "s:" + v, selected: current === "s:" + v }, l))),
    h("optgroup", { label: "Tag" }, ["Deflected", "Escalated", "Declined", ...state.meta.topics]
      .map((t) => h("option", { value: "t:" + t, selected: current === "t:" + t }, t))));
  let searchTimer;
  const head = h("div", { class: "convo-list-head" },
    h("input", { type: "search", placeholder: "Search for any conversation", value: convoState.search, oninput: (e) => {
      clearTimeout(searchTimer); searchTimer = setTimeout(() => { convoState.search = e.target.value; renderConvos(selectedId); }, 350);
    } }),
    h("div", { class: "filters" }, filter,
      h("button", { class: "btn small", onclick: () => sampleDialog() }, "Sample a batch"),
      convoState.sample ? h("button", { class: "btn small", onclick: () => { convoState.sample = null; renderConvos(selectedId); } }, "Clear sample") : null),
    h("div", { class: "muted small" }, convoState.sample ? `Sample of ${list.length}` : `${list.length} conversation${list.length === 1 ? "" : "s"}`));

  const items = h("div", { class: "convo-items" }, list.length ? list.map((c) =>
    h("button", { class: "convo-item" + (c.id === selectedId ? " selected" : ""), onclick: () => (location.hash = `#convos/${encodeURIComponent(c.id)}`) },
      h("div", { class: "first" }, c.first_message),
      h("div", { class: "meta" }, c.tags.slice(0, 2).map(tagPill),
        c.qa && c.qa.flags.length ? pill("QA flag", "bad") : null,
        h("span", { class: "grow" }), h("span", {}, ago(c.last_at)))))
    : h("div", { class: "empty", style: "margin:12px" }, "No conversations match."));

  page("Conversations", null, [filterBar(() => renderConvos(selectedId))],
    h("div", { class: "convos" + (detail ? "" : " no-selection") },
      h("div", { class: "convo-list" }, head, items),
      detail ? convoThread(detail) : null,
      detail ? convoSide(detail) : null));
}

function sampleDialog() {
  const size = h("input", { type: "number", min: 1, max: 50, value: 10 });
  const onlyOpen = h("input", { type: "checkbox", checked: true });
  formModal("Sample a batch for review", [
    h("p", { class: "muted small" }, "Pull a random set of conversations from the current filters -- how a support lead spot-checks the AI."),
    h("label", {}, "How many", size),
    h("label", { class: "inline-check" }, onlyOpen, "Only ones that still need a human review"),
  ], "Sample", async () => {
    const r = await api("POST", "/convos/sample", { env: state.filters.env.join(","), window: state.filters.window, size: Number(size.value), status: onlyOpen.checked ? "needs_review" : null });
    if (!r.conversation_ids.length) throw new Error("Nothing to sample with these filters.");
    convoState.sample = r.conversation_ids;
    convoState.status = ""; convoState.tag = ""; convoState.search = "";
    const target = `#convos/${encodeURIComponent(r.conversation_ids[0])}`;
    if (location.hash === target) renderConvos(r.conversation_ids[0]); else location.hash = target;
  });
}

function convoThread(c) {
  const first = c.first_message || "";
  return h("div", { class: "convo-thread" },
    h("div", { class: "card-head" },
      h("div", {}, h("h2", {}, first.length > 70 ? first.slice(0, 70) + "…" : first),
        h("div", { class: "muted small" }, `${c.version_id ? "v" + c.version_id : "draft"} · ${c.channel || "chat"} · ${stamp(c.started_at)}`)),
      moreMenu(
        h("button", { class: "menu-item", onclick: guard(async () => { await navigator.clipboard.writeText(location.href); toast("Link copied"); }) }, "Copy link"),
        h("button", { class: "menu-item", onclick: () => saveConversationAsTest(c.id) }, "Save as test"),
        h("div", { class: "menu-note" }, c.id, " · ", c.env))),
    transcriptView(c.turns.map((t) => ({ ...t, user: t.user_text })), { channel: c.channel }));
}

function convoSide(c) {
  const qa = c.qa;
  const loopTurns = c.turns.filter((t) => t.route === "agent_loop").length;
  const flagged = c.watchtowers.filter((w) => w.flagged);
  const tags = [...c.tags.map(tagPill), c.csat === 1 ? pill("👍 Helpful", "good") : c.csat === -1 ? pill("👎 Not helpful", "bad") : null];
  const overview = h("div", { class: "side-section" },
    h("h3", {}, "Overview"),
    qa ? [
      h("div", { class: "side-label" }, "Summary"), h("p", {}, qa.summary),
      h("div", { class: "side-label" }, "Resolution"), h("p", {}, qa.resolution_summary || label(qa.resolution)),
    ] : h("p", { class: "muted" }, "Analyzing… open this conversation again in a moment."),
    h("div", { class: "side-label" }, "Tags"), h("div", { class: "filters" }, tags));

  const watch = h("div", { class: "side-section" }, h("h3", {}, "Watchtower"),
    flagged.length ? flagged.map((w) => h("div", { class: "wt-verdict" },
      h("div", { class: "filters" }, h("a", { href: `#watchtower/${w.id}` }, w.name), pill(w.label || "flagged", "bad")),
      h("div", { class: "muted small" }, w.reason)))
    : h("p", { class: "muted small" }, c.watchtowers.length ? "No flags." : "Not checked yet."));

  const r = c.review || {};
  const sel = (name, options) => h("select", { name }, h("option", { value: "" }, "—"), options.map((o) => h("option", { value: o, selected: r[name] === o }, label(o))));
  const fields = state.meta.review_fields;
  const form = h("form", { class: "review-form", onsubmit: guard(async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target).entries());
    await api("PUT", `/convos/${encodeURIComponent(c.id)}/review`, { review: data, reviewer: author() });
    toast("Review saved");
    renderConvos(c.id);
  }) },
    h("label", {}, "Assign to teammate", h("input", { name: "assignee", value: r.assignee || "", placeholder: "name" })),
    h("label", {}, "Issue status", sel("issue_status", fields.issue_status)),
    h("label", {}, "Fixed", sel("fixed", fields.fixed)),
    h("label", {}, "Trust & safety related", sel("trust_and_safety", fields.trust_and_safety)),
    h("label", {}, "General feedback", h("textarea", { name: "feedback", rows: 3, value: r.feedback || "" })),
    h("div", { class: "filters" }, h("button", { class: "btn primary small", type: "submit" }, "Save review"),
      r.reviewed_by ? h("span", { class: "muted small" }, `Saved by ${who(r.reviewed_by)}, ${ago(r.reviewed_at)}`) : null));

  // AI QA detail lives under the fold: useful when digging in, noise otherwise.
  const aiQa = qa ? h("details", { class: "side-section" },
    h("summary", {}, h("h3", {}, "AI QA "), pill(`${qa.score}/5`, qa.score >= 4 ? "good" : qa.score >= 3 ? "warn" : "bad")),
    h("div", { class: "filters", style: "margin-top:6px" }, qa.flags.length ? qa.flags.map((f) => pill(label(f), "bad")) : h("span", { class: "muted small" }, "No issues found.")),
    qa.suggested_fix ? [h("div", { class: "side-label" }, "Suggested fix"), h("p", {}, qa.suggested_fix),
      loopTurns ? h("button", { class: "btn small", onclick: () => draftAopFromSuggestion(qa) }, "Draft an AOP from this")
        : h("p", { class: "muted small" }, "This conversation ran through the coded returns flow or the scope gate, which AOPs don't change -- the fix belongs in code.")] : null,
    h("button", { class: "btn small", style: "margin-top:8px", onclick: guard(async (e) => {
      e.target.disabled = true; e.target.textContent = "Analyzing…";
      await api("POST", `/convos/${encodeURIComponent(c.id)}/analyze`);
      renderConvos(c.id);
    }) }, "Re-analyze")) : null;

  return h("aside", { class: "convo-side" }, overview, watch,
    h("div", { class: "side-section" }, h("h3", {}, "Review + QA"), form), aiQa);
}

function draftAopFromSuggestion(qa) {
  store.set("aopPrefill", { name: "From QA review", when: qa.summary, logic: qa.suggested_fix });
  location.hash = "#build/aops/new";
}

function saveConversationAsTest(conversationId) {
  const name = h("input", { placeholder: "e.g. Lost package -- offers reship or refund" });
  formModal("Save conversation as a test", [
    h("p", { class: "muted small" }, "Replays the same customer messages and expects the same scope-gate category and tool calls on every turn. Open it in Tests afterwards to add reply checks or judge criteria."),
    h("label", {}, "Test name", name),
  ], "Save test", async () => {
    const c = await api("POST", "/tests/from-conversation", { conversation_id: conversationId, name: name.value });
    toast(`Saved test "${c.name}"`);
  });
}

// ===========================================================================
// WATCHTOWER
// ===========================================================================

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

async function renderWatchtower(wtId) {
  autoAnalyze(() => renderWatchtower(wtId));
  const wts = await api("GET", "/watchtowers?" + envQuery());
  if (wtId) return renderWatchtowerDetail(wts.find((w) => w.id === wtId));
  page("Watchtower", null,
    [filterBar(() => renderWatchtower(null)), h("button", { class: "btn primary", onclick: () => editWatchtower(null) }, "+ Create Watchtower")],
    wts.length ? h("div", { class: "wt-grid" }, wts.map(wtCard)) : h("div", { class: "empty" }, "No Watchtowers yet."));
}

function wtColors(w) {
  const labels = w.labels.length ? w.labels : ["Flagged"];
  return Object.fromEntries(labels.map((l, i) => [l, SERIES[i]]));
}

function wtCard(w) {
  const colors = wtColors(w);
  const by = Object.fromEntries(w.series.map((p) => [p.bucket, p]));
  const pts = bucketKeys(w.series, w.series_unit, state.filters.window).map((k) => by[k] || { bucket: k, analyzed: 0 });
  return h("div", { class: "wt-card" },
    h("div", { class: "card-head" }, h("h2", { title: w.criteria }, w.name),
      moreMenu(
        h("button", { class: "menu-item", onclick: () => editWatchtower(w) }, "Edit"),
        h("button", { class: "menu-item danger", onclick: guard(async () => {
          if (!confirm(`Delete the "${w.name}" Watchtower and its results?`)) return;
          await api("DELETE", `/watchtowers/${w.id}`); renderWatchtower(null);
        }) }, "Delete"))),
    h("div", { class: "wt-stats" },
      h("div", { class: "wt-stat" }, h("div", { class: "label" }, "Conversations analyzed"), h("div", { class: "value" }, w.analyzed)),
      h("div", { class: "wt-stat" }, h("div", { class: "label" }, "Flagged rate"), h("div", { class: "value" }, pct(w.flagged_rate)))),
    barChart(pts, {
      unit: w.series_unit,
      segments: (p) => Object.entries(colors).map(([l, color]) => ({ value: p[l] || 0, color })),
      tipText: (p) => `${fmtBucket(p.bucket, w.series_unit)}: ${Object.keys(colors).map((l) => `${p[l] || 0} ${l.toLowerCase()}`).join(", ")} of ${p.analyzed} analyzed`,
      empty: w.analyzed ? "Nothing flagged in this window." : "Nothing analyzed yet.",
    }),
    Object.keys(colors).length > 1 ? h("div", { class: "legend" }, Object.entries(colors).map(([l, c]) => h("span", { style: `--sw:${c}` }, `${l} (${w.by_label[l] || 0})`))) : null,
    h("a", { class: "link-more", href: `#watchtower/${w.id}` }, `View ${w.flagged} flagged →`));
}

async function renderWatchtowerDetail(w) {
  if (!w) { page("Watchtower", "That Watchtower doesn't exist anymore.", [h("a", { class: "btn", href: "#watchtower" }, "← All Watchtowers")]); return; }
  const wtLabel = store.get(`wtLabel.${w.id}`, "");
  const rows = await api("GET", `/watchtowers/${w.id}/flagged?` + envQuery({ label: wtLabel }));
  const colors = wtColors(w);
  const again = () => renderWatchtower(w.id);
  page(w.name, w.criteria,
    [h("a", { class: "btn", href: "#watchtower" }, "← All Watchtowers"), filterBar(again)],
    w.labels.length ? h("div", { class: "filters", style: "margin-bottom:12px" },
      h("button", { class: "btn small" + (wtLabel ? "" : " primary"), onclick: () => { store.set(`wtLabel.${w.id}`, ""); again(); } }, `All (${w.flagged})`),
      w.labels.map((l) => h("button", { class: "btn small" + (wtLabel === l ? " primary" : ""), onclick: () => { store.set(`wtLabel.${w.id}`, l); again(); } },
        h("span", { style: `color:${colors[l]}` }, "■ "), `${l} (${w.by_label[l] || 0})`))) : null,
    h("div", { class: "card" }, rows.length ? h("table", { class: "table clickable" },
      h("thead", {}, h("tr", {}, ["When", "First message", "Label", "Why it was flagged", "Version"].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.map((c) => h("tr", { onclick: () => (location.hash = `#convos/${encodeURIComponent(c.id)}`) },
        h("td", { class: "muted" }, ago(c.started_at)), h("td", {}, c.first_message),
        h("td", {}, pill(c.watchtower.label || "flagged", "bad")),
        h("td", { class: "small" }, c.watchtower.reason), h("td", {}, c.version_id ? `v${c.version_id}` : "draft")))))
      : h("div", { class: "nodata" }, "No flagged conversations in this window.")));
}

function editWatchtower(w) {
  const name = h("input", { value: w ? w.name : "", placeholder: "e.g. Upsell opportunity" });
  const criteria = h("textarea", { rows: 4, value: w ? w.criteria : "", placeholder: "Describe, in plain English, what should get a conversation flagged." });
  const labels = h("input", { value: w ? w.labels.join(", ") : "", placeholder: "Optional, up to 3, comma-separated: e.g. Mild, Severe" });
  formModal(w ? "Edit Watchtower" : "Create Watchtower", [
    h("label", {}, "Name", name),
    h("label", {}, "Criteria", criteria),
    h("label", {}, "Labels", labels),
    w ? h("p", { class: "muted small" }, "Saving clears this Watchtower's past results; run Analyze to re-check conversations against the new criteria.") : null,
  ], w ? "Save" : "Create", async () => {
    const body = { name: name.value, criteria: criteria.value, labels: labels.value.split(",").map((s) => s.trim()).filter(Boolean) };
    if (w) await api("PUT", `/watchtowers/${w.id}`, body); else await api("POST", "/watchtowers", body);
    toast("Saved -- run Analyze to check conversations against it");
    renderWatchtower(null);
  });
}

// ===========================================================================
// BUILD > AOPs
// ===========================================================================

const STATUS_TEXT = { live: "Live", update: "Update", draft: "Draft" };
const statusBadge = (s) => h("span", { class: "status " + s }, STATUS_TEXT[s]);

async function loadBuildData() {
  const [aops, tools] = await Promise.all([api("GET", "/aops"), api("GET", "/tools")]);
  state.aops = aops.aops;
  state.tools = tools;
  return aops;
}

async function renderAops() {
  const data = await loadBuildData();
  let filter = store.get("aopSearch", "");
  const tbody = h("tbody");
  const draw = () => {
    const rows = state.aops.filter((a) => !filter || (a.name + " " + a.when).toLowerCase().includes(filter.toLowerCase()));
    tbody.replaceChildren(...rows.map((a) => h("tr", { onclick: () => (location.hash = `#build/aops/${a.id}`), title: a.when },
      h("td", {}, statusBadge(a.status)),
      h("td", {}, h("strong", {}, a.name),
        a.force_category ? h("span", { title: "Always used when the scope gate says " + label(a.force_category) }, " ", pill("forced", "accent")) : null,
        a.problems && a.problems.length ? h("span", { title: a.problems.join("\n") }, " ", pill("needs fixing", "bad")) : null),
      h("td", {}, pill(a.type === "coded" ? "Coded" : "Text", a.type === "coded" ? "info" : "")),
      h("td", { class: "muted" }, a.type === "coded" ? "—" : who(a.updated_by)),
      h("td", { class: "muted" }, a.type === "coded" ? "in code" : ago(a.updated_at)))));
  };
  draw();
  page("Agent Operating Procedures",
    "Business logic for the agent, written in plain English. AOPs can call tools, hand off to other AOPs, and use customer data.",
    [h("a", { class: "btn primary", href: "#build/aops/new" }, "+ Create AOP")],
    h("input", { type: "search", placeholder: "Search AOPs", value: filter, class: "page-search", oninput: (e) => { filter = e.target.value; store.set("aopSearch", filter); draw(); } }),
    h("div", { class: "card table-card" }, h("div", { class: "table-wrap" }, h("table", { class: "table clickable" },
      h("thead", {}, h("tr", {}, ["Status", "Name", "Type", "Updated by", "Updated"].map((x) => h("th", {}, x)))),
      tbody))),
    data.pending_removals.length ? h("p", { class: "muted small" }, "Leaving production with the next release: ", data.pending_removals.join("; "), ".") : null);
}

function renderRefs(text, { aopNames, self }) {
  const out = [];
  const known = [...aopNames].sort((a, b) => b.length - a.length);
  const re = /@([A-Za-z_][A-Za-z0-9_]*)|\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|#(?=[A-Za-z])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    out.push(text.slice(last, m.index));
    if (m[1]) {
      const tool = state.tools.find((t) => t.name === m[1] && t.scope === "agent");
      out.push(h("span", { class: "chip tool" + (tool ? "" : " broken"), title: tool ? (tool.enabled ? "Tool" : "Tool is turned off in Tools") : "Not a tool the agent can call" }, "@" + m[1]));
      last = re.lastIndex;
    } else if (m[2]) {
      const ok = m[2] in state.meta.metadata_fields;
      out.push(h("span", { class: "chip meta" + (ok ? "" : " broken"), title: ok ? state.meta.metadata_fields[m[2]] : "Unknown metadata field" }, `{{ ${m[2]} }}`));
      last = re.lastIndex;
    } else {
      const rest = text.slice(m.index + 1);
      const hit = known.find((n) => rest.toLowerCase().startsWith(n.toLowerCase()));
      const len = hit ? hit.length : (rest.match(/^[\w-]+/) || [""])[0].length;
      out.push(h("span", { class: "chip aop" + (hit && hit !== self ? "" : " broken"), title: hit ? "AOP" : "No AOP with this name" }, "#" + rest.slice(0, len)));
      last = m.index + 1 + len;
      re.lastIndex = last;
    }
  }
  out.push(text.slice(last));
  return out;
}

// @ / # / {{ autocomplete for a textarea.
function attachAutocomplete(textarea, wrap, getOptions) {
  const box = h("div", { class: "ac", hidden: true, role: "listbox" });
  wrap.append(box);
  let items = [], sel = 0, token = null;
  const close = () => { box.hidden = true; token = null; };
  const pick = (opt) => {
    const v = textarea.value;
    const before = v.slice(0, token.start) + opt.insert;
    textarea.value = before + v.slice(textarea.selectionStart);
    textarea.selectionStart = textarea.selectionEnd = before.length;
    close();
    textarea.dispatchEvent(new Event("input"));
    textarea.focus();
  };
  const update = () => {
    const upto = textarea.value.slice(0, textarea.selectionStart);
    const m = upto.match(/(@\w*|#[A-Za-z][^\n#@.,;:]{0,40}|#|\{\{\s*\w*)$/);
    if (!m) return close();
    token = { start: upto.length - m[0].length };
    items = getOptions(m[0]);
    if (!items.length) return close();
    sel = 0;
    box.replaceChildren(...items.map((o, i) => h("button", { type: "button", class: i === sel ? "sel" : "", onmousedown: (e) => { e.preventDefault(); pick(o); } },
      h("span", { class: "chip " + o.kind }, o.label), o.hint ? h("small", {}, o.hint) : null)));
    const lineCount = upto.split("\n").length;
    box.style.left = "12px";
    box.style.top = `${Math.max(30, Math.min(textarea.offsetHeight - 10, 16 + lineCount * 24 - textarea.scrollTop))}px`;
    box.hidden = false;
  };
  textarea.addEventListener("input", update);
  textarea.addEventListener("click", update);
  textarea.addEventListener("blur", () => setTimeout(close, 150));
  textarea.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      sel = (sel + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      [...box.children].forEach((b, i) => b.classList.toggle("sel", i === sel));
    } else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(items[sel]); }
    else if (e.key === "Escape") { e.stopPropagation(); close(); }
  });
}

async function renderAopEditor(id) {
  await loadBuildData();
  const existing = id === "new" ? null : state.aops.find((a) => a.id === id);
  if (id !== "new" && !existing) { page("AOP not found", "It may have been deleted.", [h("a", { class: "btn", href: "#build/aops" }, "← All AOPs")]); return; }
  if (existing && existing.type === "coded") return renderCodedAop(existing);
  const prefill = id === "new" ? store.get("aopPrefill", null) : null;
  if (prefill) store.set("aopPrefill", null);
  const src = existing || prefill || {};
  const model = { name: src.name || "", when: src.when || "", logic: src.logic || "", variants: { ...(src.variants || {}) }, enabled: src.enabled !== false, force_category: src.force_category || "" };
  const saved = JSON.stringify(model);
  let channel = "global";
  const isDirty = () => JSON.stringify(model) !== saved;
  state.dirtyGuard = isDirty;

  const others = () => state.aops.filter((a) => a.type !== "coded" && a.id !== (existing && existing.id)).map((a) => a.name);
  const saveState = h("span", { class: "save-state" });
  const refreshState = () => {
    saveState.textContent = isDirty() ? "Unsaved changes" : !existing ? "New" : { live: "Live", update: "Draft not deployed yet", draft: "Draft not deployed yet" }[existing.status];
  };
  const text = () => (channel === "global" ? model.logic : model.variants[channel] || "");

  // Logic shows as text with reference chips; click it to edit.
  const rendered = h("div", { class: "rendered editable", tabindex: "0", role: "button", "aria-label": "Edit AOP logic", title: "Click to edit" });
  const logic = h("textarea", { class: "logic-input", placeholder: "1. First, gather information on…\n   a. …\n2. Check … with @tool_name\n3. If …, follow #Other AOP" });
  const logicWrap = h("div", { class: "logic-wrap" }, logic);
  const drawRendered = () => rendered.replaceChildren(...(text().trim()
    ? renderRefs(text(), { aopNames: [...others(), model.name].filter(Boolean), self: model.name })
    : [h("span", { class: "muted" }, channel === "global" ? "Click to write the steps…" : `No ${channel} variant -- ${channel} conversations use the global logic. Click to add one.`)]));
  const showEditor = (on, focus = true) => {
    logicWrap.hidden = !on;
    rendered.hidden = on;
    if (on) { logic.value = text(); if (focus) logic.focus(); } else drawRendered();
  };
  rendered.addEventListener("click", () => showEditor(true));
  rendered.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); showEditor(true); } });
  logic.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== logic) showEditor(false); }, 200));
  logic.addEventListener("input", () => {
    if (channel === "global") model.logic = logic.value;
    else if (logic.value.trim()) model.variants[channel] = logic.value; else delete model.variants[channel];
    refreshState();
    drawChannels();
  });
  attachAutocomplete(logic, logicWrap, (tok) => {
    const q = tok.replace(/^(@|#|\{\{\s*)/, "").toLowerCase();
    if (tok.startsWith("@")) return state.tools.filter((t) => t.scope === "agent" && t.name.includes(q)).map((t) => ({ kind: "tool", label: "@" + t.name, insert: "@" + t.name + " ", hint: t.enabled ? t.description.slice(0, 80) + "…" : "Off in Tools -- turn it on before releasing" }));
    if (tok.startsWith("#")) return others().filter((n) => n.toLowerCase().startsWith(q)).map((n) => ({ kind: "aop", label: "#" + n, insert: "#" + n + " " }));
    return Object.entries(state.meta.metadata_fields).filter(([f]) => f.startsWith(q)).map(([f, d]) => ({ kind: "meta", label: `{{ ${f} }}`, insert: `{{ ${f} }} `, hint: d }));
  });

  // Channel picker, like Decagon's "Global" dropdown.
  const chan = h("select", { class: "channel-select", "aria-label": "Channel", onchange: (e) => { channel = e.target.value; showEditor(false); } });
  const drawChannels = () => {
    const v = chan.value || channel;
    chan.replaceChildren(...["global", ...state.meta.channels].map((c) => h("option", { value: c, selected: c === v },
      (c === "global" ? "🌐 Global" : c === "chat" ? "💬 Chat" : "✉️ Email") + (c !== "global" && model.variants[c] ? " ●" : ""))));
  };

  const nameInput = h("input", { value: model.name, placeholder: "Name this AOP", "aria-label": "AOP name", class: "title-input",
    oninput: (e) => { model.name = e.target.value; refreshState(); drawRendered(); } });
  const when = h("textarea", { rows: 2, value: model.when, placeholder: "Use this AOP if the customer…", oninput: (e) => { model.when = e.target.value; refreshState(); } });
  const enabled = h("input", { type: "checkbox", checked: model.enabled, onchange: (e) => { model.enabled = e.target.checked; refreshState(); } });
  const takenBy = (cat) => state.aops.find((a) => a.type !== "coded" && a.force_category === cat && a.enabled && a.id !== (existing && existing.id));
  const force = h("select", { onchange: (e) => { model.force_category = e.target.value; refreshState(); } },
    h("option", { value: "" }, "No -- the agent picks AOPs itself"),
    state.meta.forceable_categories.map((c) => h("option", { value: c, selected: model.force_category === c, disabled: !!takenBy(c) },
      `Always for ${label(c)}` + (takenBy(c) ? ` (taken by ${takenBy(c).name})` : ""))));

  const save = guard(async () => {
    const res = existing ? await api("PUT", `/aops/${existing.id}`, { ...model, author: author() }) : await api("POST", "/aops", { ...model, author: author() });
    state.dirtyGuard = null;
    toast(res.problems.length ? "Saved to draft -- fix the highlighted references before releasing" : "Saved to draft");
    const target = `#build/aops/${res.id}`;
    if (location.hash === target) renderAopEditor(res.id); else location.hash = target;
  });

  page(null, null, null,
    h("div", { class: "aop-editor" },
      h("div", { class: "editor-bar" },
        h("a", { class: "btn", href: "#build/aops", "aria-label": "Back to AOPs" }, "←"),
        h("div", { class: "toolbar-actions" }, saveState, h("button", { class: "btn primary", onclick: save }, "Save draft"))),
      h("h1", { class: "editor-title" }, h("span", { class: "verb" }, existing ? "Editing:" : "New AOP:"), nameInput),
      h("div", { class: "aop-section" }, h("h3", {}, "ⓘ  What does this AOP do and when should it be used?"), when),
      h("div", { class: "aop-section" },
        h("div", { class: "aop-section-head" }, h("h3", {}, "AOP Logic"), chan),
        h("div", { class: "help" }, "Use ", h("span", { class: "chip tool" }, "@"), " for tools, ", h("span", { class: "chip aop" }, "#"),
          " for other AOPs, and ", h("span", { class: "chip meta" }, "{{ }}"), " for customer data."),
        rendered, logicWrap),
      existing && existing.problems && existing.problems.length
        ? h("div", { class: "problems" }, h("strong", {}, "Fix before releasing:"), h("ul", {}, existing.problems.map((p) => h("li", {}, p)))) : null,
      h("details", { class: "aop-section advanced" }, h("summary", {}, "Advanced settings"),
        h("div", { class: "form-grid", style: "margin-top:10px" },
          h("label", { class: "inline-check" }, enabled, "Include in the next release"),
          h("label", {}, "Force AOP selection", force),
          h("p", { class: "muted small" }, "Forcing makes this AOP run whenever the scope gate assigns that category, instead of letting the agent choose."),
          existing ? h("p", { class: "muted small" }, `Created by ${who(existing.created_by)} · updated by ${who(existing.updated_by)} ${ago(existing.updated_at)} · used in ${existing.conversations_7d} conversation${existing.conversations_7d === 1 ? "" : "s"} this week`) : null,
          existing ? h("div", {}, h("button", { class: "btn danger", onclick: guard(async () => {
            if (!confirm(`Delete "${existing.name}" from the draft?${existing.status !== "draft" ? " It stays live until the next release." : ""}`)) return;
            await api("DELETE", `/aops/${existing.id}`); state.dirtyGuard = null; location.hash = "#build/aops";
          }) }, "Delete AOP")) : null))));
  drawChannels(); refreshState();
  showEditor(!model.logic.trim(), false);
  if (!existing) {
    const fill = () => {
      Object.assign(model, { name: DEMO_AOP.name, when: DEMO_AOP.when, logic: DEMO_AOP.logic });
      nameInput.value = model.name; when.value = model.when;
      channel = "global"; chan.value = "global";
      showEditor(false); refreshState(); drawChannels();
    };
    const copy = (t) => navigator.clipboard.writeText(t).then(() => toast("Copied"), () => toast("Couldn't copy", true));
    document.querySelector(".aop-editor").append(demoBox("Demo",
      h("div", { class: "toolbar-actions" },
        h("button", { class: "btn", onclick: fill }, `Fill in "${DEMO_AOP.name}"`),
        h("button", { class: "btn", onclick: () => copy(DEMO_AOP.when) }, "Copy “when”"),
        h("button", { class: "btn", onclick: () => copy(DEMO_AOP.logic) }, "Copy logic")),
      h("pre", { class: "code" }, `${DEMO_AOP.name}\n\n${DEMO_AOP.when}\n\n${DEMO_AOP.logic}`)));
  }
}

function renderCodedAop(a) {
  page(a.name, null, [h("a", { class: "btn", href: "#build/aops" }, "← AOPs")],
    h("div", { class: "aop-editor" },
      h("div", { class: "help" }, "🔒 Built-in coded flow (", h("code", {}, a.source), "). To replace it, create an AOP and set Advanced settings › Force AOP selection to returns refunds."),
      h("div", { class: "aop-section" }, h("h3", {}, "When it runs"), h("p", {}, a.when)),
      h("div", { class: "aop-section" }, h("h3", {}, "Steps"), h("div", { class: "rendered" }, renderRefs(a.logic, { aopNames: [], self: "" }))),
      h("p", { class: "muted small" }, `Used in ${a.conversations_7d} conversation${a.conversations_7d === 1 ? "" : "s"} in the last 7 days.`)));
}

// ===========================================================================
// BUILD > Tools
// ===========================================================================

// A brand logo from logos.js: white mark on the brand color.
function logo(key, size = 22, { bare = false } = {}) {
  const l = LOGOS[key] || LOGOS.api;
  // bare: just the mark, in the surrounding text color (for inline use).
  const el = h("span", { class: "brand-logo", title: l.label, style: bare ? `width:${size}px;height:${size}px` : `background:${l.color};width:${size}px;height:${size}px` });
  const g = bare ? size : Math.round(size * 0.64);
  el.innerHTML = `<svg width="${g}" height="${g}" viewBox="0 0 24 24" aria-hidden="true"><path fill="${bare ? "currentColor" : "#fff"}" d="${l.path}"/></svg>`;
  return el;
}

// "+ New tool" catalog. Connected systems back today's tools; the rest are
// illustrative -- connecting them isn't wired up in this build.
const TOOL_CATALOG = [
  { key: "snowflake", connected: true, blurb: "Read or write a table, or search documents with Cortex Search." },
  { key: "api", connected: true, blurb: "Call any HTTP endpoint in your stack." },
  { key: "freshdesk", connected: true, blurb: "Open and update support tickets." },
  { key: "mcp", blurb: "Point at any Model Context Protocol server by URL. The tools it exposes are listed here for AOPs to use.", actions: [], note: "Tools come from the server" },
  { key: "shopify", blurb: "Look up, edit, and cancel orders.", actions: ["cancel_order", "update_shipping_address"] },
  { key: "stripe", blurb: "Look up payments and issue refunds.", actions: ["issue_refund", "get_payment"] },
  { key: "zendesk", blurb: "Create tickets and add internal notes.", actions: ["create_ticket", "add_internal_note"] },
  { key: "salesforce", blurb: "Read accounts and update cases.", actions: ["get_account", "update_case"] },
  { key: "ups", blurb: "Track shipments and create labels.", actions: ["track_shipment", "create_label"] },
  { key: "slack", blurb: "Notify a team channel.", actions: ["notify_channel"] },
  { key: "intercom", blurb: "Hand conversations off to your inbox.", actions: ["hand_off_conversation"] },
];

function newToolDialog() {
  const card = (c) => {
    const ours = state.tools.filter((t) => t.source === c.key).map((t) => t.name);
    return h("button", { type: "button", class: "integration" + (c.connected ? " connected" : ""),
      onclick: () => (c.connected
        ? toast(`${LOGOS[c.key].label} is connected. New ${LOGOS[c.key].label} tools are added in app/tools.py.`)
        : toast(`Demo: connecting ${LOGOS[c.key].label} isn't wired up in this build.`)) },
      h("div", { class: "integration-head" }, logo(c.key, 28), h("strong", {}, LOGOS[c.key].label),
        c.connected ? pill("Connected", "good") : null),
      h("div", { class: "muted small" }, c.blurb),
      c.note ? h("div", { class: "muted small" }, h("em", {}, c.note)) : null,
      h("div", { class: "integration-actions" }, (c.connected ? ours : c.actions).map((n) => h("span", { class: "chip tool" }, n))));
  };
  openModal(
    h("h2", {}, "Add a tool"),
    h("p", { class: "muted small" }, "Tools are code, so guardrails like identity checks and the return window are enforced whatever the AOP says. Connect a system, and its actions show up here for AOPs to use with @."),
    h("div", { class: "side-label" }, "Connected"),
    h("div", { class: "integration-grid" }, TOOL_CATALOG.filter((c) => c.connected).map(card)),
    h("div", { class: "side-label" }, "Integrations"),
    h("div", { class: "integration-grid" }, TOOL_CATALOG.filter((c) => !c.connected).map(card)),
    h("div", { class: "modal-actions" }, h("button", { class: "btn", type: "button", onclick: closeModal }, "Close")));
}

async function renderTools() {
  await loadBuildData();
  const details = (t) => openDrawer(
    h("h2", {}, t.name), h("p", {}, t.description),
    h("dl", { class: "kv" },
      h("dt", {}, "System"), h("dd", { class: "with-logo" }, logo(t.source, 18), t.source_detail || LOGOS[t.source].label),
      h("dt", {}, "Enforced in code"), h("dd", {}, t.enforces || "—"),
      h("dt", {}, "Used by"), h("dd", {}, t.used_by.join(", ") || "no AOP"),
      h("dt", {}, "Calls (7d)"), h("dd", {}, t.calls_7d),
      h("dt", {}, "Implementation"), h("dd", {}, h("code", {}, "app/tools.py"))),
    h("div", { class: "field-label" }, "Input schema"), h("pre", { class: "code" }, JSON.stringify(t.input_schema, null, 2)));
  page("Tools", "Coded actions the agent can take. AOPs reference them with @.",
    [h("button", { class: "btn primary", onclick: newToolDialog }, "+ New tool")],
    h("div", { class: "card table-card" }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Name", "Status", "Type", "On", "Scope"].map((x) => h("th", {}, x)))),
      h("tbody", {}, state.tools.map((t) => h("tr", {},
        h("td", {}, h("span", { class: "with-logo" }, logo(t.source),
          h("button", { class: "chip tool chip-btn", onclick: () => details(t), title: t.source_detail || "Details" }, t.name))),
        h("td", { title: t.used_by.join(", ") }, t.used_by.length ? pill("Used", "good") : pill("Unused", "bad")),
        h("td", {}, pill("Coded tool", "info")),
        h("td", {}, h("input", { type: "checkbox", checked: t.enabled, "aria-label": `${t.name} on`, onchange: guard(async (e) => {
          state.tools = await api("PUT", `/tools/${t.name}`, { enabled: e.target.checked });
          toast(`${t.name} turned ${e.target.checked ? "on" : "off"} in the draft`);
          renderTools();
        }) })),
        h("td", {}, h("select", { "aria-label": `Scope for ${t.name}`, disabled: !t.enabled, onchange: guard(async (e) => {
          state.tools = await api("PUT", `/tools/${t.name}`, { aop_only: e.target.value === "aop" });
          toast(`${t.name}: ${e.target.value === "aop" ? "only inside AOPs that use it" : "always available"} (draft)`);
          renderTools();
        }) },
          h("option", { value: "always", selected: !t.aop_only }, "Always available"),
          h("option", { value: "aop", selected: t.aop_only }, "Only in AOPs that use it")),
          t.aop_only && !t.used_by.length ? h("div", { class: "small", style: "color:var(--bad)" }, "No AOP uses it") : null))))))),
    h("p", { class: "muted small" }, "\"Only in AOPs that use it\": the agent can call the tool only after loading an AOP that references it. Changes apply to the draft."));
}

// ===========================================================================
// BUILD > Settings (prompts, models, limits)
// ===========================================================================

// Model picker grouped by family. A saved model that isn't in the catalog
// stays selectable so opening Settings never changes the config.
function modelSelect(value, onChange, { needsForcedTools = false } = {}) {
  const models = state.meta.models;
  const families = [...new Set(models.map((m) => m.family))];
  return h("select", { onchange: onChange },
    models.some((m) => m.id === value) ? null : h("option", { value, selected: true }, value),
    families.map((fam) => h("optgroup", { label: fam }, models.filter((m) => m.family === fam).map((m) => {
      const blocked = needsForcedTools && !m.forced_tools;
      return h("option", { value: m.id, selected: m.id === value, disabled: blocked && m.id !== value },
        `${m.name} · ${m.id}` + (blocked ? " (no forced tool use)" : ""));
    }))));
}

async function renderSettings() {
  const d = await api("GET", "/draft");
  const c = d.config;
  let dirty = false;
  state.dirtyGuard = () => dirty;
  const saveBtn = h("button", { class: "btn primary", disabled: true }, "Save draft");
  const mark = () => { dirty = true; saveBtn.disabled = false; };
  const f = {
    agent_model: modelSelect(c.agent_model, mark),
    router_model: modelSelect(c.router_model, mark, { needsForcedTools: true }),
    max_tool_iterations: h("input", { type: "number", min: 1, max: 12, value: c.max_tool_iterations, oninput: mark }),
    system_prompt: h("textarea", { class: "code", rows: 22, value: c.system_prompt, oninput: mark }),
    router_prompt: h("textarea", { class: "code", rows: 22, value: c.router_prompt, oninput: mark }),
    out_of_scope_reply: h("textarea", { rows: 4, value: c.out_of_scope_reply, oninput: mark }),
  };
  saveBtn.addEventListener("click", guard(async () => {
    const config = { ...c };
    for (const [k, el] of Object.entries(f)) config[k] = k === "max_tool_iterations" ? Number(el.value) : el.value;
    await api("PUT", "/draft", { config });
    dirty = false; state.dirtyGuard = null;
    toast("Draft saved");
    renderSettings();
  }));
  page("Settings", "The base prompts and models AOPs build on.", [saveBtn],
    h("div", { class: "card" }, h("h3", {}, "Models & limits"),
      h("div", { class: "grid2" }, h("label", {}, "Agent model", f.agent_model), h("label", {}, "Scope-gate model", f.router_model), h("label", {}, "Max tool round-trips per turn", f.max_tool_iterations)),
      h("p", { class: "muted small" }, "The scope gate uses a forced tool call, so models that don't support one are greyed out there.")),
    h("div", { class: "card" },
      h("details", {}, h("summary", {}, h("h3", {}, "System prompt"), h("span", { class: "muted small" }, "Hard rules for the agent loop")), f.system_prompt),
      h("details", {}, h("summary", {}, h("h3", {}, "Scope gate prompt"), h("span", { class: "muted small" }, "Classifies every message before the agent sees it")), f.router_prompt),
      h("details", {}, h("summary", {}, h("h3", {}, "Decline message"), h("span", { class: "muted small" }, "Fixed reply for out-of-scope requests")), f.out_of_scope_reply),
      h("details", {}, h("summary", {}, h("h3", {}, "Compiled prompt"), h("span", { class: "muted small" }, "What the model receives (chat channel, as saved)")), h("pre", { class: "code" }, d.compiled_system_prompt))));
}

// ===========================================================================
// BUILD > Releases (versions, gate, canary)
// ===========================================================================

async function renderReleases() {
  await refreshVersions();
  const [d, diff, git] = await Promise.all([api("GET", "/draft"), api("GET", "/diff?a=production&b=draft"),
    api("GET", "/git").catch(() => ({ connected: false }))]);
  state.git = git;
  const cur = state.deployment.current;
  const problems = Object.values(d.reference_problems).flat();
  const draftCard = h("div", { class: "card release-card" },
    h("div", {}, h("div", { class: "side-label" }, "Draft"),
      h("div", {}, diff.identical ? "Same as production -- nothing to release." : "Has changes that aren't live yet.")),
    h("div", { class: "toolbar-actions" },
      diff.identical ? null : h("button", { class: "btn", onclick: guard(() => showDiffModal("production", "draft")) }, "View changes"),
      h("button", { class: "btn", onclick: guard(async () => { await api("POST", "/runs", { target: "draft" }); location.hash = "#tests"; }) }, "Test draft"),
      h("button", { class: "btn primary", disabled: diff.identical || problems.length > 0, onclick: publishDialog }, "Publish version…")),
    problems.length ? h("div", { class: "problems", style: "flex-basis:100%" }, h("strong", {}, "Fix before publishing:"), h("ul", {}, problems.map((p) => h("li", {}, p)))) : null);

  const isOlder = (v) => !!cur && v.id < cur.production_version_id;
  const canary = cur && cur.canary_version_id && state.versions.find((v) => v.id === cur.canary_version_id);
  const liveCard = h("div", { class: "card release-card" }, !cur ? h("div", { class: "muted" }, "Nothing deployed yet.") : [
    h("div", {}, h("div", { class: "side-label" }, "Live"),
      h("div", { class: "live-line" }, h("span", { class: "big" }, `v${cur.production_version_id}`),
        canary ? [h("span", { class: "muted" }, ` · v${canary.id} on ${cur.canary_percent}% of new conversations`),
          h("div", { class: "split-bar", title: `${100 - cur.canary_percent}% v${cur.production_version_id} / ${cur.canary_percent}% v${canary.id}` },
            h("span", { class: "s-prod", style: `width:${100 - cur.canary_percent}%` }), h("span", { class: "s-canary", style: `width:${cur.canary_percent}%` }))] : null),
      git.connected ? gitLine(git) : null),
    h("div", { class: "toolbar-actions" },
      gitPullButton(git),
      canary ? h("button", { class: "btn primary", onclick: () => confirmAction(`Promote v${canary.id} to 100%?`, "/deploy/promote", "Promote") }, "Promote canary") : null,
      canary ? h("button", { class: "btn", onclick: () => confirmAction(`Stop sending traffic to v${canary.id}?`, "/deploy/abort-canary", "Abort canary") }, "Abort canary") : null,
      moreMenu(
        git.connected ? h("a", { class: "menu-item", href: git.url + "/pulls", target: "_blank", rel: "noopener" }, "Pull requests on GitHub ↗") : null,
        h("button", { class: "menu-item danger", onclick: () => confirmAction("Roll production back to the previous version? Rollbacks skip the release gate.", "/deploy/rollback", "Roll back") }, "Roll back…"),
        git.connected ? h("button", { class: "menu-item", onclick: guard(async () => {
          if (!confirm(`Disconnect ${git.repo}? The repo is left as it is.`)) return;
          await api("POST", "/git/disconnect"); renderReleases();
        }) }, "Disconnect GitHub") : null)),
  ]);

  page("Releases", "Publish the draft, pass the tests, then deploy.",
    git.connected ? null : [h("button", { class: "btn", onclick: () => connectGitDialog(git) }, logo("github", 18), " Connect GitHub")],
    h("div", { class: "release-pair" }, draftCard, liveCard),
    h("div", { class: "card table-card" }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, ["Version", "Change", "Published", "Tests", "", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, state.versions.map((v) => h("tr", {},
        h("td", {}, h("strong", {}, `v${v.id}`)), h("td", {}, v.note, git.connected ? prLink(git, v) : null), h("td", { class: "muted" }, ago(v.created_at)),
        h("td", { title: v.gate.reason }, v.gate.passed ? pill("passed", "good") : pill("not cleared", "warn")),
        h("td", {}, v.labels.map((l) => pill(l, l === "production" ? "good" : "warn"))),
        h("td", {}, h("div", { class: "row-actions" },
          // Newer than production: the next release. Older: a rollback target, so it's tucked in the menu.
          v.labels.includes("production") || isOlder(v) ? null : h("button", { class: "btn small primary", onclick: guard(() => deployDialog(v)) }, "Deploy…"),
          moreMenu(
            isOlder(v) && !v.labels.includes("production") ? h("button", { class: "menu-item", onclick: guard(() => deployDialog(v)) }, `Redeploy v${v.id}…`) : null,
            git.connected && !isOlder(v) && !v.labels.includes("production") && !((git.prs || {})[v.id] || {}).state
              ? h("button", { class: "menu-item", onclick: guard(() => openPrFor(v)) }, "Open pull request") : null,
            h("button", { class: "menu-item", onclick: guard(() => showDiffModal("production", String(v.id))) }, "Compare with production"),
            h("button", { class: "menu-item", onclick: guard(async () => { await api("POST", "/runs", { target: String(v.id) }); location.hash = "#tests"; }) }, "Run tests"),
            h("button", { class: "menu-item", onclick: guard(async () => {
              if (!confirm(`Replace the current draft with v${v.id}? Unreleased draft changes will be lost.`)) return;
              await api("POST", `/draft/load/${v.id}`); toast(`Loaded v${v.id} into the draft`); renderReleases();
            }) }, "Load into draft"),
            v.labels.length ? null : h("button", { class: "menu-item danger", onclick: guard(async () => {
              const rec = ((state.git || {}).prs || {})[v.id];
              const extra = rec && rec.state === "open" ? ` Its pull request #${rec.number} will be closed.` : "";
              if (!confirm(`Delete v${v.id} ("${v.note}")? This can't be undone.${extra}`)) return;
              const res = await api("DELETE", `/versions/${v.id}?author=${encodeURIComponent(author())}`);
              await renderReleases();
              toast(`Deleted v${v.id}`);
              setTimeout(() => gitNotice(res), 1200);
            }) }, `Delete v${v.id}…`),
            h("div", { class: "menu-note" }, "Config ", v.fingerprint)))))))))),
    h("details", { class: "demo-tools" }, h("summary", {}, "Release history"), h("div", { class: "timeline" }, state.deployment.history.map((dep) => {
      const what = {
        deploy: `v${dep.production_version_id} deployed to 100%`,
        canary: `v${dep.canary_version_id} canaried to ${dep.canary_percent}% (production v${dep.production_version_id})`,
        promote: `Canary v${dep.production_version_id} promoted to 100%`,
        abort_canary: `Canary aborted; v${dep.production_version_id} serves 100%`,
        rollback: `Rolled back to v${dep.production_version_id}`,
      }[dep.action] || dep.action;
      return h("div", { class: "tl-item" },
        h("span", { class: "muted" }, stamp(dep.created_at)),
        h("span", {}, pill(label(dep.action), dep.action === "rollback" ? "bad" : dep.action === "canary" ? "warn" : "good")),
        h("span", {}, what, dep.note ? h("span", { class: "muted" }, ` — ${dep.note}`) : null,
          dep.gate_override_reason ? h("div", { class: "small", style: "color:var(--warn)" }, `Gate overridden: ${dep.gate_override_reason}`) : null));
    }))));
}

function publishDialog() {
  const note = h("input", { placeholder: "e.g. Add lost-package and escalation AOPs" });
  const git = state.git || {};
  const openPr = h("input", { type: "checkbox", checked: !!git.connected });
  formModal("Publish a new version", [
    h("p", { class: "muted small" }, "Freezes the draft as an immutable version. If the full suite already passed on this exact draft, the version clears the release gate immediately."),
    h("label", {}, "What changed?", note),
    git.connected ? h("label", { class: "inline-check" }, openPr, logo("github", 18), `Open a pull request on ${git.repo}`) : null,
  ], "Publish", async () => {
    const v = await api("POST", "/versions", { note: note.value, open_pr: openPr.checked, author: author() });
    await renderReleases();
    if (v.git_error) toast(`Published v${v.id}, but the pull request failed: ${v.git_error}`, true);
    else if (v.git && v.git.url) setTimeout(() => prOpenedModal(v), 0);  // after formModal closes itself
    else if (v.git && v.git.state === "none") toast(`Published v${v.id}. main already matches it, so there's no pull request.`);
    else toast(`Published v${v.id}`);
  });
}

const GATE_ICON = { success: "✅", failure: "❌", pending: "⏳" };

function prOpenedModal(v) {
  const gate = v.gate || {};
  openModal(
    h("div", { class: "pr-opened" }, logo("github", 40),
      h("h2", {}, `Pull request #${v.git.number} is open`),
      h("p", { class: "muted" }, `v${v.id} was committed to `, h("code", {}, v.git.branch), ` and opened for review on ${state.git.repo}.`),
      h("p", { class: "small" }, `${GATE_ICON[v.git.gate] || "⏳"} Studio posted the release gate as a check on the PR`,
        v.git.gate === "success" ? " -- the full suite already passed on this config." : ". It updates when a full test run finishes.")),
    h("div", { class: "modal-actions" },
      h("button", { class: "btn", onclick: closeModal }, "Close"),
      h("a", { class: "btn primary", href: v.git.url, target: "_blank", rel: "noopener", onclick: closeModal }, "View pull request ↗")));
}

function connectGitDialog(git) {
  const repo = h("input", { value: git.suggested_repo || "", placeholder: "owner/name" });
  formModal("Connect GitHub", [
    h("p", { class: "muted small" }, "Keep the agent in a repo as code. Studio commits what's in production to main, then each version you publish opens a pull request, and deploying it to 100% merges it."),
    git.gh_installed === false ? h("div", { class: "problems" }, "Install the GitHub CLI and run ", h("code", {}, "gh auth login"), " first.") : null,
    h("label", {}, "Repository", repo),
  ], "Connect", async () => {
    await api("POST", "/git/connect", { repo: repo.value, author: author() });
    await renderReleases();
    toast(`Connected ${repo.value}. main has the production version.`);
  });
}

// One line in the Live card: the repo, and whether main matches production.
function gitLine(git) {
  const m = git.main;
  const status = !m ? pill("main is empty", "warn")
    : git.in_sync ? pill("main matches production", "good")
    : m.version_id ? pill(`main has v${m.version_id}`, "warn")
    : pill("main has edits made outside Studio", "warn");
  return h("div", { class: "git-line muted small" }, logo("github", 15, { bare: true }),
    h("a", { href: git.url, target: "_blank", rel: "noopener" }, git.repo),
    m ? h("span", {}, "main @ ", h("a", { href: m.url, target: "_blank", rel: "noopener", class: "mono", title: `“${m.subject}” by ${m.author}, ${ago(m.date)}` }, m.short)) : null,
    status,
    git.error && git.error.message ? h("span", { style: "color:var(--bad)", title: git.error.message }, "⚠ GitHub error") : null);
}

function gitPullButton(git) {
  const m = git.main;
  if (!git.connected || !m || git.in_sync || m.version_id || git.draft_matches_main) return null;
  return h("button", { class: "btn", title: `“${m.subject}” by ${m.author}`, onclick: guard(async () => {
    if (!confirm("Replace the draft with what's on main? Unreleased draft changes will be lost.")) return;
    const r = await api("POST", "/git/pull", { author: author() });
    toast(`Pulled ${r.commit.short} into the draft. Test it, then publish.`);
    renderReleases();
  }) }, "Pull main into draft");
}

// A small "#N" link after the change note, for versions that have a PR.
function prLink(git, v) {
  const rec = (git.prs || {})[v.id];
  if (!rec || !rec.number) return null;
  const merged = rec.state === "merged";
  return h("a", { class: "pr-link", href: rec.url, target: "_blank", rel: "noopener",
    title: merged ? "Pull request merged" : `Pull request open · release gate: ${rec.gate || "pending"}` },
    logo("github", 13, { bare: true }), `#${rec.number}`, merged ? " merged" : ` ${GATE_ICON[rec.gate] || ""}`);
}

async function openPrFor(v) {
  const rec = await api("POST", `/git/prs/${v.id}`, { author: author() });
  await renderReleases();
  if (rec.url) prOpenedModal({ ...v, git: rec }); else toast("main already matches this version");
}

function gitNotice(res) {
  if (res && res.git_error) toast(`GitHub: ${res.git_error}`, true);
  else if (res && res.git_notice) toast(`GitHub: ${res.git_notice}`);
}

function diffView(diff) {
  if (!diff.diff.length) return h("div", { class: "muted small" }, "No differences.");
  return h("div", { class: "diff" }, diff.diff.map((l) =>
    h("span", { class: l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : "" }, l + "\n")));
}

async function showDiffModal(a, b) {
  const diff = await api("GET", `/diff?a=${a}&b=${b}`);
  openModal(h("h2", {}, `Changes: ${a} → ${b === "draft" ? "draft" : "v" + b}`), diffView(diff),
    h("div", { class: "modal-actions" }, h("button", { class: "btn", onclick: closeModal }, "Close")));
}

function confirmAction(question, path, lbl) {
  const note = h("input", { placeholder: "Optional note" });
  formModal(question, [h("label", {}, "Note", note)], lbl, async () => {
    const res = await api("POST", path, { note: note.value, author: author() });
    await renderReleases();
    toast(lbl + " done");
    setTimeout(() => gitNotice(res), 1200);
  }, { danger: lbl === "Roll back" });
}

async function deployDialog(v) {
  const [gate, diff] = await Promise.all([api("GET", `/gate/${v.id}`), api("GET", `/diff?a=production&b=${v.id}`)]);
  const cur = state.deployment.current;
  const mode = h("select", {},
    h("option", { value: "canary", disabled: !cur }, "Canary: a slice of new conversations"),
    h("option", { value: "full" }, "Full: 100% of new conversations"));
  const percent = h("input", { type: "number", min: 1, max: 50, value: 10 });
  const percentLabel = h("label", {}, "Canary traffic %", percent);
  mode.addEventListener("change", () => (percentLabel.hidden = mode.value !== "canary"));
  if (!cur) { mode.value = "full"; percentLabel.hidden = true; }
  const note = h("input", { placeholder: "Why now?" });
  const override = h("textarea", { rows: 2, placeholder: "Required to deploy without a passing run, and recorded in the deploy history" });
  formModal(`Deploy v${v.id}`, [
    h("div", { class: "gate " + (gate.passed ? "pass" : "fail") }, h("strong", {}, gate.passed ? "Release gate: passed. " : "Release gate: blocked. "), gate.reason,
      gate.passed ? null : h("div", { class: "small" }, "Run the full suite on this version (or the identical draft) in Tests, or override below.")),
    h("div", { class: "field-label" }, `Changes vs production (v${cur ? cur.production_version_id : "–"})`), diffView(diff),
    h("div", { class: "grid2" }, h("label", {}, "Rollout", mode), percentLabel),
    h("label", {}, "Note", note),
    gate.passed ? null : h("label", {}, "Override reason", override),
  ], "Deploy", async () => {
    const res = await api("POST", "/deploy", { version_id: v.id, mode: mode.value, canary_percent: Number(percent.value), note: note.value, override_reason: gate.passed ? null : override.value, author: author() });
    await renderReleases();
    toast(mode.value === "canary" ? `v${v.id} is live for ${percent.value}% of new conversations` : `v${v.id} is live`);
    setTimeout(() => gitNotice(res), 1200);
  });
}

// ===========================================================================
// TESTS
// ===========================================================================

const testState = { tests: [], runs: [], selectedRun: null, els: null };

async function renderTests() {
  await refreshVersions();
  if (!state.aops.length) await loadBuildData();
  const target = h("select", { id: "runTarget", "aria-label": "Config to test" }, versionOptions(true));
  const caseList = h("div", { class: "case-list" });
  const runList = h("div", { class: "run-list" });
  const runDetail = h("div");
  const suiteCount = h("span", { class: "muted small" });
  page("Tests", "A version can be deployed once the full suite passes on it.",
    [h("details", { class: "menu new-menu" }, h("summary", { class: "btn primary" }, "+ New test"),
      h("div", { class: "menu-items" },
        h("button", { class: "menu-item", onclick: () => editCase(null, "scripted") }, h("strong", {}, "Scripted test"), h("div", { class: "muted small" }, "Fixed customer messages, checked turn by turn")),
        h("button", { class: "menu-item", onclick: () => editCase(null, "simulation") }, h("strong", {}, "Simulation"), h("div", { class: "muted small" }, "An AI customer with a goal; an AI judge grades it"))))],
    h("div", { class: "split" },
      h("div", { class: "col" }, h("div", { class: "card-head" }, h("h2", {}, "Test suite"), suiteCount), caseList),
      h("div", { class: "col" },
        h("div", { class: "card-head" }, h("h2", {}, "Runs"),
          h("div", { class: "toolbar-actions" }, target,
            h("button", { class: "btn", onclick: guard(() => {
              const ids = $$(".case-select:checked").map((i) => i.value);
              if (!ids.length) throw new Error("Select at least one test");
              return startRun(ids);
            }) }, "Run selected"),
            h("button", { class: "btn primary", onclick: guard(() => startRun(null)) }, "Run full suite"))),
        runList, runDetail)));
  testState.els = { caseList, runList, runDetail, suiteCount, target };
  await Promise.all([loadTests(), loadRuns()]);
}

async function loadTests() {
  testState.tests = await api("GET", "/tests");
  const { caseList, suiteCount } = testState.els;
  suiteCount.textContent = `${testState.tests.length} tests · ${testState.tests.filter((t) => t.kind === "simulation").length} simulations`;
  caseList.replaceChildren(...testState.tests.map((c) => {
    const n = c.kind === "simulation" ? `≤${c.simulation.max_turns} turns` : `${c.turns.length} turn${c.turns.length === 1 ? "" : "s"}`;
    return h("div", { class: "case" },
      h("input", { type: "checkbox", class: "case-select", value: c.id, "aria-label": "Select " + c.name }),
      h("div", {}, h("button", { class: "case-title link-like", onclick: () => editCase(c), title: "Edit" }, c.name),
        h("div", { class: "muted small" }, (c.kind === "simulation" ? "Simulation" : "Scripted") + " · " + n + (c.criteria ? " · judged" : ""))),
      h("button", { class: "btn small", onclick: guard(() => startRun([c.id])) }, "Run"));
  }));
}

async function startRun(caseIds) {
  const run = await api("POST", "/runs", { target: testState.els.target.value || "draft", case_ids: caseIds || null });
  toast(`Run #${run.id} started on ${run.target} (${run.total} test${run.total === 1 ? "" : "s"})`);
  testState.selectedRun = run.id;
  await loadRuns();
}

async function loadRuns() {
  if (!testState.els || !document.body.contains(testState.els.runList)) return;
  testState.runs = await api("GET", "/runs");
  const { runList } = testState.els;
  if (!testState.runs.some((r) => r.id === testState.selectedRun)) testState.selectedRun = testState.runs.length ? testState.runs[0].id : null;
  runList.replaceChildren(...(testState.runs.length ? testState.runs.slice(0, 8).map((r) =>
    h("div", { class: "run" + (r.id === testState.selectedRun ? " selected" : ""), onclick: guard(() => { testState.selectedRun = r.id; return loadRuns(); }) },
      h("strong", {}, `#${r.id}`),
      h("div", { class: "grow" }, h("div", {}, `${r.target} `, h("span", { class: "muted small mono" }, r.fingerprint)), h("div", { class: "muted small" }, ago(r.started_at))),
      h("div", { class: "progress", title: `${r.passed} passed, ${r.failed} failed of ${r.total}` },
        h("span", { class: "p-pass", style: `width:${(100 * r.passed) / r.total}%` }), h("span", { class: "p-fail", style: `width:${(100 * r.failed) / r.total}%` })),
      h("span", { class: "small" }, `${r.passed}/${r.total}`), runPills(r)))
    : [h("div", { class: "empty" }, "No runs yet. Run the full suite against the draft to get a baseline.")]));
  if (testState.selectedRun) await showRun(testState.selectedRun);
  if (testState.runs.some((r) => r.status === "running")) state.timers.push(setTimeout(guard(loadRuns), 1500));
}

function runPills(r) {
  const done = r.passed + r.failed;
  return [
    r.status === "running" ? pill(`running ${done}/${r.total}`, "info")
      : r.status !== "completed" ? pill(r.status, "warn")
      : r.failed ? pill(`${r.failed} failed`, "bad") : pill("all passed", "good"),
    r.full_suite ? null : pill("partial", ""),
  ];
}

async function showRun(id) {
  const run = await api("GET", `/runs/${id}`);
  const results = [...run.results].sort((a, b) => a.passed - b.passed);
  testState.els.runDetail.replaceChildren(h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h3", {}, `Run #${run.id} · ${run.target}`), h("div", {}, runPills(run))),
    h("div", { class: "muted small" }, `Config ${run.fingerprint} · started ${stamp(run.started_at)}${run.finished_at ? " · finished " + stamp(run.finished_at) : ""}`),
    results.map((r) => h("details", { class: "result" },
      h("summary", {}, h("span", { class: r.passed ? "ok" : "ko" }, r.passed ? "✓" : "✗"), h("strong", {}, r.case_name), pill(r.kind, r.kind === "simulation" ? "info" : ""), h("span", { class: "muted small" }, ms(r.duration_ms))),
      r.error ? h("pre", { class: "code" }, r.error) : null,
      h("div", {}, r.checks.filter((c) => !c.name.startsWith("turn ")).map(checkView)),
      transcriptView(r.transcript))),
    run.status === "running" ? h("div", { class: "muted small" }, `${run.total - run.results.length} still running…`) : null));
}

function toolChecks(selected, extra = []) {
  const all = [...extra, ...state.meta.all_tools, "use_aop"];
  return h("div", { class: "checks" }, all.map((t) =>
    h("label", {}, h("input", { type: "checkbox", value: t, checked: (selected || []).includes(t) }), t === "*" ? "any tool" : h("code", {}, t))));
}
const checkedValues = (el) => $$("input:checked", el).map((i) => i.value);
function selectOf(values, current, blank = "— any —") {
  return h("select", {}, h("option", { value: "" }, blank), values.map((v) => h("option", { value: v, selected: v === current }, v)));
}

function turnEditor(turn = { user: "", expect: {} }) {
  const e = turn.expect || {};
  const el = h("div", { class: "turn-editor" });
  const user = h("input", { value: turn.user || "", placeholder: "Customer message" });
  const button = selectOf(state.meta.categories.filter((c) => c !== "unclear"), turn.button_category, "typed (classified normally)");
  const category = selectOf(state.meta.categories, e.category);
  const routeSel = selectOf(state.meta.routes, e.route);
  const suggestions = h("select", {}, h("option", { value: "" }, "— any —"),
    h("option", { value: "true", selected: e.suggestions === true }, "shown"), h("option", { value: "false", selected: e.suggestions === false }, "not shown"));
  const called = toolChecks(e.tools_called);
  const notCalled = toolChecks(e.tools_not_called, ["*"]);
  const contains = h("textarea", { rows: 2, value: (e.reply_contains || []).join("\n"), placeholder: "one per line" });
  const omits = h("textarea", { rows: 2, value: (e.reply_not_contains || []).join("\n"), placeholder: "one per line" });
  el.append(
    h("div", { class: "card-head" }, h("strong", {}, "Turn"), h("button", { class: "btn small danger", type: "button", onclick: () => el.remove() }, "Remove")),
    h("div", { class: "grid2" }, h("label", {}, "Customer says", user), h("label", {}, "Sent as", button)),
    h("div", { class: "grid2" }, h("label", {}, "Expected category", category), h("label", {}, "Expected route", routeSel), h("label", {}, "Quick-reply buttons", suggestions)),
    h("div", { class: "field-label" }, "Must call"), called,
    h("div", { class: "field-label" }, "Must not call"), notCalled,
    h("div", { class: "grid2" }, h("label", {}, "Reply contains", contains), h("label", {}, "Reply must not contain", omits)));
  el.collect = () => ({
    user: user.value, button_category: button.value || null,
    expect: {
      category: category.value || null, route: routeSel.value || null,
      suggestions: suggestions.value === "" ? null : suggestions.value === "true",
      tools_called: checkedValues(called), tools_not_called: checkedValues(notCalled),
      reply_contains: contains.value.split("\n").map((s) => s.trim()).filter(Boolean),
      reply_not_contains: omits.value.split("\n").map((s) => s.trim()).filter(Boolean),
    },
  });
  return el;
}

function editCase(existing, kind) {
  const c = existing || { kind: kind || "scripted", name: "", description: "", tags: [], criteria: "", turns: [{ user: "", expect: {} }], simulation: { persona: "", goal: "", details: "", max_turns: 6 }, expect_overall: {} };
  const name = h("input", { value: c.name, placeholder: "What this test proves" });
  const tags = h("input", { value: (c.tags || []).join(", "), placeholder: "returns, guardrail" });
  const criteria = h("textarea", { rows: 3, value: c.criteria || "", placeholder: "Optional for scripted tests. e.g. The agent never promises a refund before the item is received." });
  const overallCalled = toolChecks((c.expect_overall || {}).tools_called);
  const overallNot = toolChecks((c.expect_overall || {}).tools_not_called);
  let body, collectKind;
  if (c.kind === "simulation") {
    const s = c.simulation || {};
    const persona = h("textarea", { rows: 2, value: s.persona || "", placeholder: "Who the simulated customer is and how they behave" });
    const goal = h("textarea", { rows: 2, value: s.goal || "", placeholder: "What they're trying to get done" });
    const details = h("input", { value: s.details || "", placeholder: "Order BK-1001, email alice@bookly-demo.com" });
    const maxTurns = h("input", { type: "number", min: 1, max: 12, value: s.max_turns || 6 });
    body = [h("label", {}, "Persona", persona), h("label", {}, "Goal", goal), h("div", { class: "grid2" }, h("label", {}, "Details they know", details), h("label", {}, "Max turns", maxTurns))];
    collectKind = () => ({ simulation: { persona: persona.value, goal: goal.value, details: details.value, max_turns: Number(maxTurns.value) } });
  } else {
    const turnsBox = h("div", { class: "form-grid" }, (c.turns || []).map((t) => turnEditor(t)));
    body = [turnsBox, h("button", { class: "btn small", type: "button", onclick: () => turnsBox.append(turnEditor()) }, "+ Add turn")];
    collectKind = () => ({ turns: [...turnsBox.children].map((t) => t.collect()) });
  }
  formModal(existing ? "Edit test" : c.kind === "simulation" ? "New simulation" : "New scripted test", [
    h("div", { class: "grid2" }, h("label", {}, "Name", name), h("label", {}, "Tags", tags)),
    c.kind === "simulation" ? h("p", { class: "muted small" }, "An LLM plays this customer against the agent until the goal is met or it gives up; the judge then grades the transcript against your criteria.") : null,
    body,
    h("label", {}, c.kind === "simulation" ? "Pass criteria (graded by an LLM judge)" : "Judge criteria", criteria),
    h("details", {}, h("summary", {}, h("strong", {}, "Whole-conversation tool checks")),
      h("div", { class: "field-label" }, "Must call at some point"), overallCalled,
      h("div", { class: "field-label" }, "Must never call"), overallNot),
    existing ? h("button", { class: "btn small danger", type: "button", onclick: guard(async () => {
      if (!confirm(`Delete "${c.name}"?`)) return;
      await api("DELETE", `/tests/${c.id}`); closeModal(); await loadTests(); toast("Test deleted");
    }) }, "Delete test") : null,
  ], "Save test", async () => {
    await api("POST", "/tests", {
      id: existing ? existing.id : undefined, kind: c.kind, name: name.value, description: c.description,
      tags: tags.value.split(","), criteria: criteria.value,
      expect_overall: { tools_called: checkedValues(overallCalled), tools_not_called: checkedValues(overallNot) },
      ...collectKind(),
    });
    await loadTests();
    toast("Test saved");
  });
}

// ===========================================================================
// PREVIEW
// ===========================================================================

const pv = { sessionId: "pv-" + rid(), turns: 0, channel: store.get("pvChannel", "chat"), target: "draft", metadata: store.get("pvMetadata", {}) };

async function renderPreview() {
  await Promise.all([refreshVersions(), state.aops.length ? null : loadBuildData()]);
  const msgs = h("div", { class: "pv-messages" });
  const sessionBox = h("div", { class: "muted small" }, "No conversation yet.");
  const reset = () => {
    pv.sessionId = "pv-" + rid(); pv.turns = 0;
    msgs.replaceChildren(h("div", { class: "muted small" }, `New ${pv.channel} conversation with ${pv.target === "draft" ? "the draft" : "v" + pv.target}. Click any reply to see what shaped it.`));
    sessionBox.textContent = "No conversation yet.";
  };
  const target = h("select", { "aria-label": "Config to chat with", onchange: (e) => { pv.target = e.target.value; reset(); } }, versionOptions(true));
  target.value = pv.target;
  const chan = h("div", { class: "variant-tabs", role: "group", "aria-label": "Channel" }, state.meta.channels.map((c) =>
    h("button", { type: "button", class: c === pv.channel ? "active" : "", onclick: () => { pv.channel = c; store.set("pvChannel", c); renderPreview(); } }, c === "chat" ? "💬 Chat" : "✉️ Email")));

  const send = async (text, category) => {
    if (pv.turns === 0) msgs.replaceChildren();
    const email = pv.channel === "email";
    const userEl = h("div", { class: "msg user" + (email ? " email" : "") }, text);
    const pending = h("div", { class: "msg agent pending" }, "…");
    msgs.append(userEl, pending);
    msgs.scrollTop = msgs.scrollHeight;
    try {
      const data = await api("POST", "/playground/chat", { session_id: pv.sessionId, message: text, target: pv.target, category: category || null, channel: pv.channel, metadata: pv.metadata });
      pv.turns++;
      userEl.remove(); pending.remove();
      msgs.append(transcriptView([{ ...data.trace, user: text, reply: data.reply, tool_calls: data.tool_calls, aops: data.aops }], { channel: pv.channel }));
      if (data.suggestions && data.suggestions.length) {
        const row = h("div", { class: "suggest-row" }, data.suggestions.map((s) => h("button", { class: "btn small", onclick: () => { row.remove(); send(s.value, s.category); } }, s.label)));
        msgs.append(row);
      }
      const s = data.session;
      const flow = s.return_flow;
      sessionBox.replaceChildren(
        h("div", {}, s.verified_email ? `Verified: ${s.verified_email}${s.verified_order_id ? ", " + s.verified_order_id : ""}` : "Not verified yet"),
        flow ? h("div", {}, `Return in progress: ${flow.filed_return_id ? "filed " + flow.filed_return_id : flow.order_item_id ? "item chosen" : flow.verified_order ? "choosing item" : "collecting details"}`) : null,
        h("div", { class: "side-label" }, "Metadata the agent sees"), h("pre", { class: "code" }, JSON.stringify(s.metadata, null, 2)),
        h("div", {}, `Config ${s.fingerprint}`));
    } catch (err) {
      pending.className = "msg agent"; pending.textContent = "Error: " + err.message;
    }
    msgs.scrollTop = msgs.scrollHeight;
  };

  let composer, chatInput = null;
  if (pv.channel === "email") {
    const subject = h("input", { placeholder: "Subject", value: "Question about my order" });
    const body = h("textarea", { rows: 4, placeholder: "Write the customer's email…" });
    composer = h("form", { class: "form-grid", style: "margin-top:10px", onsubmit: (e) => { e.preventDefault(); if (!body.value.trim()) return; send(`Subject: ${subject.value}\n\n${body.value.trim()}`); body.value = ""; } },
      h("div", { class: "muted small" }, "To: support@bookly-demo.com"), subject, body, h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Send email")));
  } else {
    const input = chatInput = h("input", { placeholder: "Try: My order BK-1001 says delivered but never arrived (alice@bookly-demo.com)", autocomplete: "off" });
    composer = h("form", { class: "pv-composer", onsubmit: (e) => { e.preventDefault(); const t = input.value.trim(); if (!t) return; input.value = ""; send(t); } }, input, h("button", { class: "btn primary", type: "submit" }, "Send"));
  }

  const fields = state.meta.metadata_fields;
  const metaInputs = Object.keys(fields).filter((k) => k !== "channel").map((k) => {
    const input = k === "customer_type"
      ? h("select", {}, ["", "new", "returning"].map((v) => h("option", { value: v, selected: (pv.metadata[k] || "") === v }, v || "— from account —")))
      : h("input", { value: pv.metadata[k] ?? "", placeholder: "from account" });
    input.addEventListener("change", () => { pv.metadata[k] = input.value; store.set("pvMetadata", pv.metadata); });
    return h("label", { title: fields[k] }, `{{ ${k} }}`, input);
  });

  page("Preview", null,
    [target, chan, h("button", { class: "btn", onclick: reset }, "New conversation"),
      h("button", { class: "btn", onclick: () => (pv.turns ? saveConversationAsTest(pv.sessionId) : toast("Chat first -- there's nothing to save yet", true)) }, "Save as test…")],
    h("div", { class: "preview" },
      h("div", {}, msgs, composer),
      h("aside", { class: "pv-side" },
        h("div", { class: "card" }, h("h3", {}, "Test metadata"),
          h("p", { class: "muted small" }, "Pretend to be a customer with these details. Applies to new conversations."),
          h("div", { class: "form-grid" }, metaInputs)),
        h("details", { class: "card" }, h("summary", {}, h("h3", {}, "Session details")), sessionBox),
        chatInput ? demoBox("Demo", h("p", { class: "muted small" }, "Puts the message in the box. Start a new conversation between them."),
          DEMO_MESSAGES.map((m) => h("button", { class: "btn small demo-msg", title: m.text,
            onclick: () => { chatInput.value = m.text; chatInput.focus(); } }, m.label))) : null)));
  reset();
}

// ===========================================================================
// boot
// ===========================================================================

(async function boot() {
  try {
    state.meta = await api("GET", "/meta");
    await refreshVersions();
    if (!location.hash) history.replaceState(null, "", "#home");
    await route();
  } catch (err) {
    toast("Couldn't reach the Studio API: " + err.message, true);
  }
})();
