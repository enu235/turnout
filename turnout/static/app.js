"use strict";
/* ==========================================================================
   Turnout — UI logic. Vanilla JS, no build step, no deps.

   Everything the model ever produces is escaped before it touches innerHTML
   (see escapeHtml/safeInline/renderMarkdownLite). Trusted, hand-authored
   template strings are the only other thing assigned to innerHTML.
   ========================================================================== */

const JSON_HEADERS = { "Content-Type": "application/json" };

const ROUTER_INFO = {
  manual: "Fixed default target; the control condition.",
  heuristic: "Transparent scored rules — the everyday router.",
  random: "Uniform random. Pure exploration; produces unbiased training data.",
  explore: "Heuristic 90% of the time, random 10% — everyday policy that still explores.",
  switchyard: "NVIDIA NeMo Switchyard's LLM-classifier route. Judges the task first, so the first turn of a session costs ~20s.",
  "switchyard-random": "Switchyard's random route. Same integration, no judgment call, ~14ms.",
  "switchyard-random": "Switchyard configured for a randomized decision route — exploration through the same backend.",
};

const ADAPTER_LABELS = {
  claude_cli: "Claude",
  copilot_cli: "GitHub Copilot",
  codex_cli: "Codex",
  xai: "xAI",
  ollama: "Ollama (local)",
};

const FEATURE_LABELS = {
  n_messages: "Messages", n_chars: "Prompt chars", n_chars_last: "Last turn chars",
  est_tokens: "Est. tokens", has_code_fence: "Code fence", n_lines: "Lines",
  n_question_marks: "Question marks", is_multi_turn: "Multi-turn", priority: "Priority",
};
const FEATURE_ORDER = Object.keys(FEATURE_LABELS);

// -- tiny inline icon set -----------------------------------------------

const SUN_SVG = '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.6"/><path d="M12 2.5v2.4M12 19.1v2.4M4.9 4.9l1.7 1.7M17.4 17.4l1.7 1.7M2.5 12h2.4M19.1 12h2.4M4.9 19.1l1.7-1.7M17.4 6.6l1.7-1.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
const MOON_SVG = '<svg viewBox="0 0 24 24" fill="none"><path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
const THUMB_UP_SVG = '<svg viewBox="0 0 24 24" fill="none"><path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3Zm0 0 4.5-8a2 2 0 0 1 2 2.2L12.7 9H18a2 2 0 0 1 2 2.4l-1.4 7A2 2 0 0 1 16.6 20H10a3 3 0 0 1-3-3v-6Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const THUMB_DOWN_SVG = '<svg viewBox="0 0 24 24" fill="none"><path d="M17 13V4h3a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3Zm0 0-4.5 8a2 2 0 0 1-2-2.2L11.3 15H6a2 2 0 0 1-2-2.4l1.4-7A2 2 0 0 1 7.4 4H14a3 3 0 0 1 3 3v6Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
const TRACK_ICON = '<svg viewBox="0 0 24 24" fill="none"><path d="M3 17h5l3-4 3 8 3-8 3 4h1" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const HISTORY_ICON = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 4v6h6M4.5 14a8 8 0 1 0 1.5-8.5L4 10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ANALYTICS_ICON = '<svg viewBox="0 0 24 24" fill="none"><path d="M5 19V9M12 19V5M19 19v-7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';
const INSPECTOR_ICON = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M15 4v16" stroke="currentColor" stroke-width="1.5"/></svg>';
const WARN_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none"><path d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// -- global state ---------------------------------------------------------

const S = {
  theme: localStorage.getItem("sy-theme") || "system",
  state: null,
  sessions: [],
  currentSessionId: null,
  messages: [],
  turns: [],
  priority: "balanced",
  pinTarget: "",
  compareMode: false,
  compareTargets: [],
  activeView: "chat",
  historyRows: [],
  selectedHistoryRequestId: null,
  streaming: false,
  inspectorCtx: null,
  inspectorOpen: true,
};

const turnsById = new Map();
let turnCounter = 0;
let toastSeq = 0;
let codeBlockCounter = 0;

// ==========================================================================
// generic helpers
// ==========================================================================

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
const escapeAttr = escapeHtml;

function safeInline(raw) {
  return escapeHtml(raw).replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

function truncate(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function capitalize(s) {
  s = String(s ?? "");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function safeParseJSON(v, fallback) {
  if (v == null) return fallback;
  if (typeof v !== "string") return v;
  try { return JSON.parse(v); } catch { return fallback; }
}

function fmtLatency(ms) {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + "s";
}

function fmtCost(usd, credits) {
  if (usd != null) {
    const n = Number(usd);
    if (Number.isNaN(n)) return "—";
    if (n === 0) return "$0.00";
    return "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
  }
  if (credits != null) return Number(credits).toFixed(2) + " cr";
  return "—";
}

function timeAgo(ms) {
  if (!ms) return "—";
  const diff = Date.now() - ms;
  const s = Math.floor(diff / 1000);
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  const d = Math.floor(h / 24);
  if (d < 7) return d + "d ago";
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.statusText || `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || j.message || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(message, type = "error", ms = 5000) {
  const region = document.getElementById("toast-region");
  const div = document.createElement("div");
  div.className = "toast" + (type === "info" ? " info" : "");
  div.id = "toast_" + toastSeq++;
  div.textContent = message;
  region.appendChild(div);
  setTimeout(() => div.remove(), ms);
}

function errorBannerHtml(message, retryAction) {
  return `<div class="error-banner">${escapeHtml(message)}${retryAction ? `<button type="button" data-action="${retryAction}">Retry</button>` : ""}</div>`;
}

// ==========================================================================
// target / router lookups
// ==========================================================================

function targetById(id) { return (S.state?.targets || []).find((t) => t.id === id); }
function targetLabel(id) { const t = targetById(id); return t ? t.label : id; }
function targetIndex(id) { return (S.state?.targets || []).findIndex((t) => t.id === id); }
function catColorVar(id) {
  const i = targetIndex(id);
  const n = i < 0 ? 6 : i % 10;
  return `var(--cat-${n + 1})`;
}
function routerLabel(name) {
  if (!name) return "—";
  if (name === "pinned") return "manual pin";
  if (name === "compare") return "comparison";
  return capitalize(name);
}
function adapterLabel(a) { return ADAPTER_LABELS[a] || capitalize(String(a || "").replace(/_/g, " ")); }
function isBuffered(t) { return !!(t && Array.isArray(t.tags) && t.tags.includes("buffered")); }
function groupTargetsByAdapter(targets) {
  const map = new Map();
  (targets || []).forEach((t) => {
    if (!map.has(t.adapter)) map.set(t.adapter, []);
    map.get(t.adapter).push(t);
  });
  return [...map.entries()];
}

// ==========================================================================
// SSE streaming (fetch + ReadableStream; EventSource can't POST)
// ==========================================================================

function emitFrame(raw, onEvent) {
  if (!raw.trim()) return;
  let event = "message";
  const dataLines = [];
  for (let line of raw.split("\n")) {
    line = line.replace(/\r$/, "");
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return;
  const dataStr = dataLines.join("\n");
  let data;
  try { data = JSON.parse(dataStr); } catch { data = dataStr; }
  onEvent(event, data);
}

async function streamSSE(url, body, onEvent) {
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });
  } catch (err) {
    onEvent("fetch_error", { message: err.message || "network error" });
    return;
  }
  if (!res.ok || !res.body) {
    let msg = res.statusText || `HTTP ${res.status}`;
    try { const j = await res.json(); msg = j.detail || j.message || msg; } catch { /* ignore */ }
    onEvent("fetch_error", { message: msg });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    let step;
    try { step = await reader.read(); }
    catch (err) { onEvent("fetch_error", { message: err.message }); return; }
    const { done, value } = step;
    if (value) buf += decoder.decode(value, { stream: true });
    if (done) { buf += decoder.decode(); break; }
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      emitFrame(raw, onEvent);
    }
  }
  if (buf.trim()) emitFrame(buf, onEvent);
}

// ==========================================================================
// lite markdown: fenced code, inline code, bold, paragraphs. Escapes first.
// ==========================================================================

function renderMarkdownLite(raw) {
  if (!raw) return "";
  const parts = [];
  const re = /```(\w+)?\n?([\s\S]*?)```/g;
  let last = 0, m;
  while ((m = re.exec(raw))) {
    if (m.index > last) parts.push({ type: "text", content: raw.slice(last, m.index) });
    parts.push({ type: "code", lang: m[1] || "", content: m[2] });
    last = re.lastIndex;
  }
  if (last < raw.length) parts.push({ type: "text", content: raw.slice(last) });
  return parts.map((p) => (p.type === "code" ? codeBlockHtml(p) : textBlockHtml(p.content))).join("");
}

function inlineFmt(s) {
  return s.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^\n*]+?)\*\*/g, "<strong>$1</strong>");
}

function textBlockHtml(raw) {
  const lines = escapeHtml(raw).split("\n");
  const out = [];
  let para = [];
  const flushPara = () => {
    if (para.length) { out.push(`<p>${inlineFmt(para.join("<br>"))}</p>`); para = []; }
  };
  for (const line of lines) {
    const h = line.match(/^(#{1,6})\s+(.+)$/);
    if (h) {
      flushPara();
      const level = h[1].length;
      out.push(`<h${level}>${inlineFmt(h[2])}</h${level}>`);
    } else if (line.trim() === "") {
      flushPara();
    } else {
      para.push(line);
    }
  }
  flushPara();
  return out.join("");
}

function codeBlockHtml({ lang, content }) {
  const id = "cb" + codeBlockCounter++;
  const escaped = escapeHtml(content.replace(/\n$/, ""));
  return `<div class="codeblock"><div class="codeblock-bar"><span class="codeblock-lang">${escapeHtml(lang || "text")}</span><button type="button" class="copy-btn" data-action="copy" data-copy-target="${id}" aria-label="Copy code">Copy</button></div><pre><code id="${id}">${escaped}</code></pre></div>`;
}

function featureGridHtml(features) {
  if (!features) return `<div class="feature-item"><span class="k">No feature data recorded</span></div>`;
  const keys = FEATURE_ORDER.filter((k) => k in features)
    .concat(Object.keys(features).filter((k) => !FEATURE_ORDER.includes(k)));
  if (!keys.length) return `<div class="feature-item"><span class="k">No feature data recorded</span></div>`;
  return keys.map((k) => {
    let v = features[k];
    if (typeof v === "boolean") v = v ? "Yes" : "No";
    else if (typeof v === "number") v = Number.isInteger(v) ? v.toLocaleString() : v;
    return `<div class="feature-item"><span class="k">${escapeHtml(FEATURE_LABELS[k] || k)}</span><span class="v">${escapeHtml(String(v))}</span></div>`;
  }).join("");
}

// ==========================================================================
// theme
// ==========================================================================

function effectiveDark() {
  return S.theme === "dark" || (S.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
}
function applyTheme() {
  const root = document.documentElement;
  if (S.theme === "light") root.setAttribute("data-theme", "light");
  else if (S.theme === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  updateThemeButton();
}
function updateThemeButton() {
  const btn = document.getElementById("theme-toggle-btn");
  btn.innerHTML = effectiveDark() ? SUN_SVG : MOON_SVG;
  const label = S.theme === "system"
    ? `Theme: system (currently ${effectiveDark() ? "dark" : "light"}) — click to override`
    : `Theme: ${S.theme} — click to change`;
  btn.setAttribute("aria-label", label);
  btn.title = label;
}
function cycleTheme() {
  const order = ["system", "light", "dark"];
  S.theme = order[(order.indexOf(S.theme) + 1) % order.length];
  localStorage.setItem("sy-theme", S.theme);
  applyTheme();
}

// ==========================================================================
// popovers (router / health)
// ==========================================================================

function closeAllPopovers() {
  document.querySelectorAll(".health-popover.open").forEach((p) => p.classList.remove("open"));
  document.getElementById("health-btn").setAttribute("aria-expanded", "false");
  document.getElementById("router-btn").setAttribute("aria-expanded", "false");
}
function togglePopover(btnId, popId) {
  const pop = document.getElementById(popId);
  const willOpen = !pop.classList.contains("open");
  closeAllPopovers();
  if (willOpen) {
    pop.classList.add("open");
    document.getElementById(btnId).setAttribute("aria-expanded", "true");
  }
}

// ==========================================================================
// topbar: router, priority, pin, health
// ==========================================================================

function renderRouterPopover() {
  const routers = S.state?.routers || [];
  const active = routers.find((r) => r.active) || routers[0];
  const labelEl = document.getElementById("router-btn-label");
  if (labelEl) labelEl.textContent = active ? capitalize(active.name) : "—";
  document.getElementById("router-popover").innerHTML = routers.map((r) => `
    <button type="button" class="router-option ${r.active ? "active" : ""}" data-action="select-router" data-router="${escapeAttr(r.name)}" role="menuitemradio" aria-checked="${r.active}">
      <div class="router-option-head"><span class="dot"></span><span class="router-option-name">${escapeHtml(r.name)}</span><span class="router-option-ver">v${escapeHtml(String(r.version))}</span></div>
      <div class="router-option-desc">${escapeHtml(ROUTER_INFO[r.name] || "")}</div>
    </button>`).join("");
}

async function postRouter(name) {
  try {
    await fetchJSON("/api/router", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ name }) });
    S.state.active_router = name;
    S.state.routers = (S.state.routers || []).map((r) => ({ ...r, active: r.name === name }));
    renderRouterPopover();
    updateModeLabel();
  } catch (err) {
    toast("Could not switch router: " + err.message);
  }
}

function renderPinSelect() {
  const sel = document.getElementById("pin-select");
  const groups = groupTargetsByAdapter(S.state?.targets || []);
  let html = `<option value="">Auto (router decides)</option>`;
  html += groups.map(([adapter, targets]) => `<optgroup label="${escapeAttr(adapterLabel(adapter))}">${
    targets.map((t) => {
      const disabled = !t.enabled || t.available === false;
      const suffix = (disabled ? " — unavailable" : "") + (isBuffered(t) ? " · buffered" : "");
      return `<option value="${escapeAttr(t.id)}" ${disabled ? "disabled" : ""}>${escapeHtml(t.label + suffix)}</option>`;
    }).join("")
  }</optgroup>`).join("");
  sel.innerHTML = html;
  sel.value = S.pinTarget || "";
}

function computeHealth() {
  const entries = Object.entries(S.state?.adapters || {});
  if (!entries.length) return { level: "", text: "checking…" };
  const ok = entries.filter(([, v]) => v.ok).length;
  if (ok === entries.length) return { level: "ok", text: "All adapters healthy" };
  if (ok === 0) return { level: "bad", text: "No adapters reachable" };
  return { level: "warn", text: `${ok}/${entries.length} adapters up` };
}

function renderHealth() {
  const h = computeHealth();
  document.getElementById("health-dot").className = "health-dot " + h.level;
  document.getElementById("health-label").textContent = h.text;
  const entries = Object.entries(S.state?.adapters || {});
  document.getElementById("health-popover").innerHTML =
    entries.map(([name, v]) => `
      <div class="health-row">
        <div class="health-row-name"><span class="health-dot ${v.ok ? "ok" : "bad"}"></span>${escapeHtml(adapterLabel(name))}</div>
        <div class="health-row-detail">${escapeHtml(v.detail || "")}</div>
      </div>`).join("") +
    `<div style="padding-top:8px;"><button type="button" class="btn" style="width:100%;justify-content:center;" data-action="recheck-adapters">Recheck adapters</button></div>`;
}

function updateTargetCountFooter() {
  const targets = S.state?.targets || [];
  const avail = targets.filter((t) => t.enabled && t.available !== false).length;
  document.getElementById("target-count").textContent = `${avail} of ${targets.length} targets available`;
}

function updateModeLabel() {
  const label = document.getElementById("composer-mode-label");
  if (S.compareMode) {
    label.textContent = `Comparing ${S.compareTargets.length} model${S.compareTargets.length === 1 ? "" : "s"} — pick 2 to 4`;
    return;
  }
  if (S.pinTarget) {
    label.textContent = `Pinned to ${targetLabel(S.pinTarget)}`;
    return;
  }
  const active = S.state?.active_router || "—";
  label.textContent = `Routed by ${routerLabel(active)} · ${S.priority} priority`;
}

// ==========================================================================
// inspector
// ==========================================================================

function emptyInspectorHtml() {
  return `<div class="inspector-empty">${INSPECTOR_ICON}<p>Send a message, or click a row in History, to see exactly which model answered and why.</p></div>`;
}
function inspectorLoadingHtml() {
  return `<div class="loading-row"><span class="spinner"></span><span>Loading…</span></div>`;
}

function section(label, body) {
  return `<div><div class="section-label"><span>${label}</span></div>${body}</div>`;
}

function candidateRowHtml(c, chosenId, idx, minS, maxS) {
  const pct = maxS === minS ? 100 : Math.round(((c.score - minS) / (maxS - minS)) * 100);
  const chosen = c.target_id === chosenId;
  return `<div class="candidate ${chosen ? "chosen" : ""}">
    <div class="candidate-row">
      <span class="candidate-rank">#${idx + 1}</span>
      <span class="candidate-name" title="${escapeAttr(targetLabel(c.target_id))}">${escapeHtml(targetLabel(c.target_id))}</span>
      ${chosen ? '<span class="candidate-badge">Chosen</span>' : ""}
      <span class="candidate-score">${Number(c.score).toFixed(2)}</span>
    </div>
    <div class="candidate-bar"><div class="candidate-bar-fill" style="width:${pct}%"></div></div>
    <div class="candidate-reasons">${(c.reasons || []).map(reasonRowHtml).join("")}</div>
  </div>`;
}
function reasonRowHtml(r) {
  const s = String(r);
  const cls = s.trim().startsWith("+") ? "pos" : s.trim().startsWith("-") ? "neg" : "";
  return `<div class="candidate-reason"><span class="mk ${cls}"></span><span>${safeInline(s)}</span></div>`;
}

function buildInspectorHTML(ctx) {
  const { decision, features, prompt, executions } = ctx;
  if (!decision) {
    return `<div class="inspector-empty">${INSPECTOR_ICON}<p>No routing decision was recorded for this request.</p></div>`;
  }
  const t = targetById(decision.target_id);
  const color = catColorVar(decision.target_id);
  const sorted = [...(decision.candidates || [])].sort((a, b) => b.score - a.score);
  const scores = sorted.map((c) => c.score);
  const minS = scores.length ? Math.min(...scores, 0) : 0;
  const maxS = scores.length ? Math.max(...scores, 0) : 0;
  const confPct = decision.confidence != null ? Math.round(decision.confidence * 100) + "%" : "—";
  const propPct = decision.propensity != null ? Math.round(decision.propensity * 100) + "%" : "—";
  const buffered = isBuffered(t);

  let html = "";
  html += `<div class="decision-summary">
    <div class="decision-target">
      <span class="dot" style="background:${color}"></span>
      <div>
        <div class="decision-target-name">${escapeHtml(t ? t.label : decision.target_id)}</div>
        <div class="decision-target-model">${escapeHtml(t ? `${adapterLabel(t.adapter)} · ${t.model}` : decision.target_id)}</div>
      </div>
    </div>
    ${(decision.overridden || decision.explored || buffered) ? `<div class="tag-list">
      ${decision.overridden ? '<span class="tag" style="color:var(--accent);border-color:var(--accent);">pinned by user</span>' : ""}
      ${decision.explored ? '<span class="badge-explore">exploration step</span>' : ""}
      ${buffered ? '<span class="badge-buffered">buffered response</span>' : ""}
    </div>` : ""}
    <div class="decision-meta-row">
      <div class="meta-pill"><span class="k">Router</span><span class="v">${escapeHtml(routerLabel(decision.router_name))} v${escapeHtml(String(decision.router_version || ""))}</span></div>
      <div class="meta-pill"><span class="k">Confidence</span><span class="v">${confPct}</span></div>
      <div class="meta-pill"><span class="k">Propensity</span><span class="v">${propPct}</span></div>
      <div class="meta-pill"><span class="k">Decide time</span><span class="v">${fmtLatency(decision.latency_ms)}</span></div>
    </div>
  </div>`;

  if (prompt) html += section("Prompt", `<div class="inspector-prompt">${escapeHtml(prompt)}</div>`);

  html += section("Rationale", `<div class="rationale-box">${safeInline(decision.rationale || "—")}</div>`);

  const eligibleN = (decision.eligible_ids || []).length;
  html += section(
    `Candidates${eligibleN ? ` <span style="font-weight:500;color:var(--text-faint);text-transform:none;letter-spacing:0;">of ${eligibleN} eligible</span>` : ""}`,
    `<div class="candidate-list">${sorted.map((c, i) => candidateRowHtml(c, decision.target_id, i, minS, maxS)).join("") ||
      '<p style="color:var(--text-faint);font-size:var(--text-sm);">No candidates recorded.</p>'}</div>`
  );

  if (decision.fallback_order && decision.fallback_order.length) {
    html += section("Fallback chain", `<div class="chain-row">${[decision.target_id, ...decision.fallback_order]
      .map((id, i) => `${i > 0 ? '<span class="sep">→</span>' : ""}<span>${escapeHtml(targetLabel(id))}</span>`).join("")}</div>`);
  }

  if (decision.constraints_applied && decision.constraints_applied.length) {
    html += section("Constraints applied", `<div class="tag-list">${decision.constraints_applied
      .map((c) => `<span class="tag">${escapeHtml(c)}</span>`).join("")}</div>`);
  }

  html += section("Request features", `<div class="feature-grid">${featureGridHtml(features)}</div>`);

  if (executions && executions.length) {
    const e = executions[0];
    const statusCls = e.status === "ok" ? "ok" : (e.status === "error" || e.status === "timeout") ? "bad" : "neutral";
    html += section("Response", `<div class="decision-meta-row">
      <div class="meta-pill"><span class="k">Status</span><span class="v"><span class="pill ${statusCls}">${escapeHtml(e.status || "—")}</span></span></div>
      <div class="meta-pill"><span class="k">Latency</span><span class="v">${fmtLatency(e.latency_ms)}</span></div>
      <div class="meta-pill"><span class="k">TTFT</span><span class="v">${e.ttft_ms != null ? fmtLatency(e.ttft_ms) : "—"}</span></div>
      <div class="meta-pill"><span class="k">Cost</span><span class="v">${fmtCost(e.cost_usd, e.credits)}</span></div>
    </div>`);
  }

  return html;
}

function renderInspector() {
  const body = document.getElementById("inspector-body");
  const subtitle = document.getElementById("inspector-subtitle");
  const ctx = S.inspectorCtx;
  if (!ctx) {
    subtitle.textContent = "Nothing selected yet";
    body.innerHTML = emptyInspectorHtml();
    return;
  }
  subtitle.textContent = ctx.subtitle || "";
  body.innerHTML = buildInspectorHTML(ctx);
}

function setInspectorFromTurn(turn) {
  S.inspectorCtx = { decision: turn.decision, features: turn.features, prompt: turn.userText, executions: null, subtitle: "This turn · live" };
  renderInspector();
}

async function loadRequestIntoInspector(requestId, subtitle) {
  document.getElementById("inspector-subtitle").textContent = subtitle || "";
  document.getElementById("inspector-body").innerHTML = inspectorLoadingHtml();
  try {
    const data = await fetchJSON(`/api/request/${encodeURIComponent(requestId)}`);
    const d = data.decisions && data.decisions[0];
    const decision = d ? {
      target_id: d.target_id, router_name: d.router_name, router_version: d.router_version,
      rationale: d.rationale, candidates: safeParseJSON(d.candidates, []),
      fallback_order: safeParseJSON(d.fallback_order, []), confidence: d.confidence,
      latency_ms: d.latency_ms, eligible_ids: safeParseJSON(d.eligible_ids, []),
      constraints_applied: safeParseJSON(d.constraints_applied, []),
      overridden: !!d.overridden, propensity: d.propensity ?? null, explored: !!d.explored,
    } : null;
    const features = safeParseJSON(data.request?.features, null);
    S.inspectorCtx = { decision, features, prompt: data.request?.prompt, executions: data.executions, subtitle: subtitle || "From history" };
    renderInspector();
  } catch (err) {
    document.getElementById("inspector-body").innerHTML = errorBannerHtml("Could not load that request: " + err.message);
  }
}

function isNarrow() { return window.innerWidth <= 1100; }
function setInspectorOpen(open) {
  S.inspectorOpen = open;
  const narrow = isNarrow();
  document.getElementById("inspector").classList.toggle("open", narrow && open);
  document.getElementById("inspector-backdrop").classList.toggle("open", narrow && open);
  document.getElementById("body-grid").classList.toggle("inspector-collapsed", !narrow && !open);
  document.getElementById("inspector-toggle-btn").setAttribute("aria-pressed", String(open));
}
function openInspectorIfNarrow() { if (isNarrow()) setInspectorOpen(true); }
function setRailOpen(open) {
  document.getElementById("rail").classList.toggle("open", open);
  document.getElementById("rail-backdrop").classList.toggle("open", open);
  document.getElementById("rail-toggle-btn").setAttribute("aria-pressed", String(open));
}
function closeRailIfNarrow() { if (window.innerWidth <= 780) setRailOpen(false); }

// ==========================================================================
// chat transcript primitives
// ==========================================================================

function emptyStateHtml() {
  return `<div class="empty-state">${TRACK_ICON}<h3>Nothing routed yet</h3><p>Ask anything. Every reply here shows exactly which model answered, which router chose it, and why — the whole decision, not just the output.</p></div>`;
}
function hideEmptyState() {
  document.querySelector("#chat-transcript > .empty-state")?.remove();
}
function withAutoScroll(fn) {
  const el = document.getElementById("chat-transcript");
  const wasNear = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  fn();
  if (wasNear) el.scrollTop = el.scrollHeight;
}
function appendTurnToTranscript(turn) {
  const el = document.getElementById("chat-transcript");
  el.appendChild(turn.dom);
  el.scrollTop = el.scrollHeight;
}

function buildTurnSkeleton(userText) {
  const id = "turn_" + turnCounter++;
  const wrap = document.createElement("div");
  wrap.className = "turn";
  wrap.dataset.turnId = id;
  wrap.innerHTML = `
    <div class="turn-user"></div>
    <div class="turn-answer-wrap">
      <div class="answer-head"></div>
      <div class="answer-card">
        <div class="answer-body"></div>
        <div class="metric-strip" hidden></div>
      </div>
    </div>`;
  wrap.querySelector(".turn-user").textContent = userText;
  const turn = {
    id, dom: wrap, kind: "chat", userText,
    headEl: wrap.querySelector(".answer-head"),
    cardEl: wrap.querySelector(".answer-card"),
    bodyEl: wrap.querySelector(".answer-body"),
    metricsEl: wrap.querySelector(".metric-strip"),
    answerRaw: "", reasoningRaw: "",
    thinkingBlock: null, thinkingBodyEl: null, thinkingOpenAuto: false,
    decision: null, features: null, usage: null, requestId: null,
    feedbackRating: 0, buffered: false, errored: false,
  };
  turnsById.set(id, turn);
  S.turns.push(turn);
  return turn;
}

function decidingHeadHtml() {
  return `<span class="target-chip"><span class="dot" style="background:var(--text-faint)"></span><span>Deciding…</span></span>`;
}
function renderAnswerHead(turn) {
  const d = turn.decision;
  const t = targetById(d.target_id);
  const buffered = isBuffered(t);
  turn.buffered = buffered;
  return `<span class="target-chip"><span class="dot" style="background:${catColorVar(d.target_id)}"></span><span>${escapeHtml(t ? t.label : d.target_id)}</span></span>
    ${buffered ? '<span class="badge-buffered">buffered</span>' : ""}
    ${d.explored ? '<span class="badge-explore">exploration</span>' : ""}
    <span class="via">via ${escapeHtml(routerLabel(d.router_name))}</span>
    <button type="button" class="inspect-link" data-action="inspect-turn" data-turn="${turn.id}">Inspect</button>`;
}
function restoredHeadHtml(row, turnId) {
  const t = targetById(row.target_id);
  return `<span class="target-chip"><span class="dot" style="background:${catColorVar(row.target_id)}"></span><span>${escapeHtml(t ? t.label : row.target_id)}</span></span>
    ${isBuffered(t) ? '<span class="badge-buffered">buffered</span>' : ""}
    <span class="via">via ${escapeHtml(routerLabel(row.router_name))}</span>
    <button type="button" class="inspect-link" data-action="inspect-turn" data-turn="${turnId}">Inspect</button>`;
}

function ensurePlaceholder(turn) {
  if (turn.answerRaw) return;
  turn.bodyEl.innerHTML = turn.buffered
    ? `<div class="loading-row"><span class="spinner"></span><span>This model replies in one block — it may take a moment before anything appears.</span></div>`
    : `<span class="typing-cursor"></span>`;
}

function ensureThinkingBlock(turn) {
  if (turn.thinkingBlock) return turn.thinkingBlock;
  const wrap = document.createElement("div");
  wrap.className = "thinking-block open";
  wrap.innerHTML = `
    <button type="button" class="thinking-toggle" data-action="toggle-thinking">
      <span class="pulse"></span><span class="label">Reasoning</span><svg class="chev" viewBox="0 0 24 24" fill="none"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="thinking-body"></div>`;
  turn.cardEl.insertBefore(wrap, turn.bodyEl);
  turn.thinkingBlock = wrap;
  turn.thinkingBodyEl = wrap.querySelector(".thinking-body");
  turn.thinkingOpenAuto = true;
  return wrap;
}
function collapseThinking(turn) {
  if (!turn.thinkingBlock || !turn.thinkingOpenAuto) return;
  turn.thinkingBlock.classList.remove("open");
  turn.thinkingBlock.querySelector(".pulse")?.classList.add("done");
  const label = turn.thinkingBlock.querySelector(".label");
  if (label) label.textContent = `Reasoning (${turn.reasoningRaw.length.toLocaleString()} chars)`;
  turn.thinkingOpenAuto = false;
}
function appendReasoning(turn, text) {
  withAutoScroll(() => {
    ensureThinkingBlock(turn);
    turn.reasoningRaw += text;
    turn.thinkingBodyEl.textContent = turn.reasoningRaw;
    turn.thinkingBodyEl.scrollTop = turn.thinkingBodyEl.scrollHeight;
  });
}
function appendAnswerText(turn, text) {
  withAutoScroll(() => {
    collapseThinking(turn);
    turn.answerRaw += text;
    turn.bodyEl.innerHTML = renderMarkdownLite(turn.answerRaw) + '<span class="typing-cursor"></span>';
  });
}
function showStatusNote(turn, text) {
  const note = document.createElement("div");
  note.className = "status-note";
  note.innerHTML = `${WARN_ICON}<span>${safeInline(text)}</span>`;
  turn.cardEl.insertBefore(note, turn.thinkingBlock || turn.bodyEl);
}
function showTurnError(turn, message) {
  turn.errored = true;
  turn.cardEl.classList.add("errored");
  const note = document.createElement("div");
  note.className = "status-note is-error";
  note.textContent = message;
  turn.cardEl.appendChild(note);
  turn.bodyEl.querySelector(".typing-cursor")?.remove();
}

function updateVoteButtons(turn) {
  if (!turn.metricsEl) return;
  const up = turn.metricsEl.querySelector(".vote-btn.up");
  const down = turn.metricsEl.querySelector(".vote-btn.down");
  if (up) { up.classList.toggle("active", turn.feedbackRating === 1); up.setAttribute("aria-pressed", String(turn.feedbackRating === 1)); }
  if (down) { down.classList.toggle("active", turn.feedbackRating === -1); down.setAttribute("aria-pressed", String(turn.feedbackRating === -1)); }
}

function renderMetricStrip(turn) {
  const u = turn.usage;
  if (!u) return;
  const usage = u.usage || {};
  turn.metricsEl.hidden = false;
  turn.metricsEl.innerHTML = `
    <span class="metric">Latency <strong>${fmtLatency(u.latency_ms)}</strong></span>
    <span class="metric">TTFT <strong>${u.ttft_ms != null ? fmtLatency(u.ttft_ms) : "—"}</strong></span>
    <span class="metric">Tokens <strong>${usage.input_tokens ?? "—"}→${usage.output_tokens ?? "—"}</strong></span>
    <span class="metric">Cost <strong>${fmtCost(usage.cost_usd, usage.credits)}</strong></span>
    ${u.attempt > 1 ? `<span class="metric" style="color:var(--warn)">Fallback (attempt ${u.attempt})</span>` : ""}
    <span class="spacer"></span>
    <div class="turn-actions">
      <button type="button" class="vote-btn up" data-action="vote-up" data-turn="${turn.id}" aria-label="Good response" aria-pressed="false">${THUMB_UP_SVG}</button>
      <button type="button" class="vote-btn down" data-action="vote-down" data-turn="${turn.id}" aria-label="Bad response" aria-pressed="false">${THUMB_DOWN_SVG}</button>
    </div>`;
  updateVoteButtons(turn);
}

// ==========================================================================
// sending a chat turn
// ==========================================================================

function setStreaming(v) {
  S.streaming = v;
  const btn = document.getElementById("send-btn");
  btn.disabled = v;
  btn.setAttribute("aria-busy", String(v));
}

async function sendChat(text) {
  clearChatError();
  hideEmptyState();
  S.messages.push({ role: "user", content: text });
  const turn = buildTurnSkeleton(text);
  turn.headEl.innerHTML = decidingHeadHtml();
  ensurePlaceholder(turn);
  appendTurnToTranscript(turn);
  setStreaming(true);
  const body = { messages: S.messages, constraints: { priority: S.priority, pin_target: S.pinTarget || undefined } };
  if (S.currentSessionId) body.session_id = S.currentSessionId;
  try {
    await streamSSE("/api/chat", body, (event, data) => handleChatEvent(turn, event, data));
  } finally {
    setStreaming(false);
    loadSessions();
  }
}

function handleChatEvent(turn, event, data) {
  switch (event) {
    case "meta":
      turn.requestId = data.request_id;
      turn.features = data.features;
      if (!S.currentSessionId) { S.currentSessionId = data.session_id; onNewSessionStarted(turn.userText); }
      break;
    case "decision":
      turn.decision = data;
      turn.headEl.innerHTML = renderAnswerHead(turn);
      ensurePlaceholder(turn);
      setInspectorFromTurn(turn);
      break;
    case "reasoning":
      appendReasoning(turn, data.text);
      break;
    case "text":
      appendAnswerText(turn, data.text);
      break;
    case "status":
      showStatusNote(turn, data.text);
      break;
    case "usage":
      turn.usage = data;
      renderMetricStrip(turn);
      break;
    case "error":
      showTurnError(turn, data.message);
      break;
    case "fetch_error":
      showTurnError(turn, "Connection lost: " + data.message);
      break;
    case "done":
      finalizeTurn(turn);
      break;
  }
}

function finalizeTurn(turn) {
  turn.bodyEl.querySelector(".typing-cursor")?.remove();
  if (turn.answerRaw) {
    S.messages.push({ role: "assistant", content: turn.answerRaw });
  } else if (!turn.errored) {
    turn.bodyEl.innerHTML = `<p style="color:var(--text-faint)">No response text.</p>`;
  }
  if (turn.decision) { turn.decision.explored = !!turn.decision.explored; setInspectorFromTurn(turn); }
}

function onNewSessionStarted(promptText) {
  if (!S.sessions.some((s) => s.session_id === S.currentSessionId)) {
    S.sessions.unshift({ session_id: S.currentSessionId, title: (promptText || "New session").slice(0, 80), updated_ms: Date.now(), n: 1 });
    renderSessionList();
  }
}

// ==========================================================================
// compare mode
// ==========================================================================

function toggleCompareMode() {
  S.compareMode = !S.compareMode;
  document.getElementById("compare-picker").classList.toggle("open", S.compareMode);
  document.getElementById("compare-note").style.display = S.compareMode ? "block" : "none";
  document.getElementById("compare-toggle-btn").textContent = S.compareMode ? "Cancel compare" : "Compare models";
  if (S.compareMode) renderComparePicker();
  updateModeLabel();
}

function targetToggleHtml(t) {
  const disabled = !t.enabled || t.available === false;
  const selected = S.compareTargets.includes(t.id);
  return `<button type="button" class="target-toggle ${selected ? "selected" : ""}" data-action="toggle-compare-target" data-target="${escapeAttr(t.id)}" ${disabled ? "disabled" : ""} title="${escapeAttr(disabled ? (t.notes || "Unavailable right now") : t.label)}">
    <span class="dot" style="background:${catColorVar(t.id)}"></span>${escapeHtml(t.label)}${isBuffered(t) ? '<span class="badge-buffered">buffered</span>' : ""}
  </button>`;
}
function renderComparePicker() {
  const el = document.getElementById("compare-picker");
  const groups = groupTargetsByAdapter(S.state?.targets || []);
  el.innerHTML = groups.map(([adapter, targets]) => `
    <div class="compare-group">
      <div class="compare-group-label">${escapeHtml(adapterLabel(adapter))}</div>
      <div class="compare-group-chips">${targets.map(targetToggleHtml).join("")}</div>
    </div>`).join("");
}

function buildCompareTurn(text, targetIds) {
  const id = "turn_" + turnCounter++;
  const wrap = document.createElement("div");
  wrap.className = "turn";
  const cols = targetIds.map((tid) => {
    const t = targetById(tid);
    return `<div class="compare-col" data-target="${escapeAttr(tid)}">
      <div class="compare-col-head"><span class="dot" style="width:8px;height:8px;border-radius:50%;background:${catColorVar(tid)};flex-shrink:0;"></span><span>${escapeHtml(t ? t.label : tid)}</span>${isBuffered(t) ? '<span class="badge-buffered" style="margin-left:auto;">buffered</span>' : ""}</div>
      <div class="compare-col-body"><span class="typing-cursor"></span></div>
      <div class="compare-col-foot" hidden></div>
    </div>`;
  }).join("");
  wrap.innerHTML = `<div class="turn-user"></div><div class="compare-grid" style="--cols:${targetIds.length}">${cols}</div>`;
  wrap.querySelector(".turn-user").textContent = text;

  const colState = {};
  targetIds.forEach((tid) => {
    colState[tid] = {
      raw: "",
      bodyEl: wrap.querySelector(`.compare-col[data-target="${CSS.escape(tid)}"] .compare-col-body`),
      footEl: wrap.querySelector(`.compare-col[data-target="${CSS.escape(tid)}"] .compare-col-foot`),
      usage: null, done: false, errored: false,
    };
  });
  const turn = { id, dom: wrap, kind: "compare", userText: text, requestId: null, targetIds, colState };
  turnsById.set(id, turn);
  S.turns.push(turn);
  return turn;
}

function renderCompareFoot(turn, targetId) {
  const c = turn.colState[targetId];
  if (!c || !c.usage) return;
  const usage = c.usage.usage || {};
  c.footEl.hidden = false;
  c.footEl.innerHTML = `<span class="mini-metrics"><span>${fmtLatency(c.usage.latency_ms)}</span><span>${fmtCost(usage.cost_usd, usage.credits)}</span></span><button type="button" class="win-btn" data-action="compare-win" data-turn="${turn.id}" data-target="${escapeAttr(targetId)}">This one won</button>`;
}

function handleCompareEvent(turn, event, data) {
  switch (event) {
    case "meta":
      turn.requestId = data.request_id;
      if (!S.currentSessionId) { S.currentSessionId = data.session_id; onNewSessionStarted(turn.userText); }
      break;
    case "text": {
      const c = turn.colState[data.target_id];
      if (!c) break;
      withAutoScroll(() => {
        c.raw += data.text;
        c.bodyEl.innerHTML = renderMarkdownLite(c.raw) + (c.done ? "" : '<span class="typing-cursor"></span>');
      });
      break;
    }
    case "usage": {
      const c = turn.colState[data.target_id];
      if (!c) break;
      c.usage = data;
      renderCompareFoot(turn, data.target_id);
      break;
    }
    case "error": {
      const c = turn.colState[data.target_id];
      if (!c) break;
      c.errored = true;
      c.bodyEl.querySelector(".typing-cursor")?.remove();
      const note = document.createElement("div");
      note.className = "status-note is-error";
      note.textContent = data.message;
      c.bodyEl.appendChild(note);
      break;
    }
    case "target_done": {
      const c = turn.colState[data.target_id];
      if (!c) break;
      c.done = true;
      c.bodyEl.querySelector(".typing-cursor")?.remove();
      if (!c.raw && !c.errored) c.bodyEl.innerHTML = `<p style="color:var(--text-faint)">No response text.</p>`;
      break;
    }
    case "fetch_error":
      toast("Compare stream failed: " + data.message);
      break;
  }
}

async function sendCompare(text) {
  clearChatError();
  hideEmptyState();
  S.messages.push({ role: "user", content: text });
  const targetIds = [...S.compareTargets];
  const turn = buildCompareTurn(text, targetIds);
  appendTurnToTranscript(turn);
  setStreaming(true);
  const body = { messages: S.messages, targets: targetIds };
  if (S.currentSessionId) body.session_id = S.currentSessionId;
  try {
    await streamSSE("/api/compare", body, (event, data) => handleCompareEvent(turn, event, data));
  } finally {
    setStreaming(false);
    loadSessions();
  }
}

// ==========================================================================
// composer
// ==========================================================================

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

function handleComposerSubmit(e) {
  e.preventDefault();
  const input = document.getElementById("composer-input");
  const text = input.value.trim();
  if (!text) return;
  if (S.streaming) { toast("Still waiting on the last response.", "info"); return; }
  if (S.compareMode && S.compareTargets.length < 2) { toast("Pick at least two models to compare.", "info"); return; }
  input.value = "";
  autoGrow(input);
  if (S.compareMode) sendCompare(text);
  else sendChat(text);
}

function resetChat() {
  if (S.streaming) { toast("Wait for the current response to finish first.", "info"); return; }
  S.currentSessionId = null;
  S.messages = [];
  S.turns = [];
  turnsById.clear();
  S.compareMode = false;
  S.compareTargets = [];
  document.getElementById("compare-picker").classList.remove("open");
  document.getElementById("compare-note").style.display = "none";
  document.getElementById("compare-toggle-btn").textContent = "Compare models";
  document.getElementById("chat-transcript").innerHTML = emptyStateHtml();
  S.inspectorCtx = null;
  renderInspector();
  highlightActiveSession();
  updateModeLabel();
  switchView("chat");
  document.getElementById("composer-input").focus();
}

function showChatError(msg) {
  clearChatError();
  const div = document.createElement("div");
  div.id = "chat-error-banner";
  div.innerHTML = errorBannerHtml(msg, "retry-state");
  document.getElementById("view-chat").prepend(div);
}
function clearChatError() { document.getElementById("chat-error-banner")?.remove(); }

// ==========================================================================
// sessions / history-as-chat
// ==========================================================================

function renderSessionList() {
  const el = document.getElementById("session-list");
  if (!S.sessions.length) { el.innerHTML = `<li class="rail-empty">No conversations yet. Start one above.</li>`; return; }
  el.innerHTML = S.sessions.map((s) => `
    <li>
      <button type="button" class="session-item ${s.session_id === S.currentSessionId ? "active" : ""}" data-action="load-session" data-session="${escapeAttr(s.session_id)}">
        <div class="session-item-title">${escapeHtml(s.title || "Untitled")}</div>
        <div class="session-item-meta">${timeAgo(s.updated_ms)} · ${s.n} msg${s.n === 1 ? "" : "s"}</div>
      </button>
    </li>`).join("");
}
function highlightActiveSession() {
  document.querySelectorAll(".session-item").forEach((b) => b.classList.toggle("active", b.dataset.session === S.currentSessionId));
}

async function loadSessions() {
  try {
    const data = await fetchJSON("/api/sessions");
    S.sessions = data.rows || [];
    renderSessionList();
  } catch {
    document.getElementById("session-list").innerHTML = `<li class="rail-empty">Couldn't load sessions.</li>`;
  }
}

async function loadSession(sessionId) {
  if (S.streaming) { toast("Wait for the current response to finish first.", "info"); return; }
  S.currentSessionId = sessionId;
  S.messages = [];
  S.turns = [];
  turnsById.clear();
  S.inspectorCtx = null;
  renderInspector();
  S.compareMode = false;
  S.compareTargets = [];
  document.getElementById("compare-picker").classList.remove("open");
  document.getElementById("compare-note").style.display = "none";
  document.getElementById("compare-toggle-btn").textContent = "Compare models";
  const el = document.getElementById("chat-transcript");
  el.innerHTML = `<div class="loading-row"><span class="spinner"></span><span>Loading conversation…</span></div>`;
  try {
    const data = await fetchJSON(`/api/history?session_id=${encodeURIComponent(sessionId)}&limit=200`);
    const rows = (data.rows || []).slice().reverse();
    el.innerHTML = "";
    if (!rows.length) el.innerHTML = emptyStateHtml();
    rows.forEach((row) => {
      const turn = buildTurnSkeleton(row.prompt || "");
      turn.requestId = row.request_id;
      turn.headEl.innerHTML = row.target_id ? restoredHeadHtml(row, turn.id) : decidingHeadHtml();
      turn.answerRaw = row.response || "";
      if (row.error && !turn.answerRaw) {
        turn.cardEl.classList.add("errored");
        turn.bodyEl.innerHTML = `<p style="color:var(--bad)">${safeInline(row.error)}</p>`;
      } else {
        turn.bodyEl.innerHTML = renderMarkdownLite(turn.answerRaw) || `<p style="color:var(--text-faint)">No response text.</p>`;
      }
      if (row.execution_id) {
        turn.usage = {
          execution_id: row.execution_id, target_id: row.target_id, attempt: 1,
          latency_ms: row.latency_ms, ttft_ms: row.ttft_ms,
          usage: { output_tokens: row.output_tokens, cost_usd: row.cost_usd, credits: row.credits },
        };
        renderMetricStrip(turn);
      }
      appendTurnToTranscript(turn);
      if (row.prompt) S.messages.push({ role: "user", content: row.prompt });
      if (turn.answerRaw) S.messages.push({ role: "assistant", content: turn.answerRaw });
    });
  } catch (err) {
    el.innerHTML = errorBannerHtml("Could not load this conversation: " + err.message);
  }
  highlightActiveSession();
  switchView("chat");
}

// ==========================================================================
// history view
// ==========================================================================

function historySkeletonHtml() {
  return `<div style="padding:16px;">${[1, 2, 3, 4, 5].map(() => '<div class="skeleton skeleton-line" style="width:100%"></div>').join("")}</div>`;
}

function renderHistoryFilters() {
  const routerSel = document.getElementById("history-router-filter");
  const targetSel = document.getElementById("history-target-filter");
  const curR = routerSel.value, curT = targetSel.value;
  const routers = (S.state?.routers || []).map((r) => r.name);
  routerSel.innerHTML = `<option value="">All routers</option>` + routers.map((r) => `<option value="${escapeAttr(r)}">${escapeHtml(routerLabel(r))}</option>`).join("");
  routerSel.value = routers.includes(curR) ? curR : "";
  const groups = groupTargetsByAdapter(S.state?.targets || []);
  targetSel.innerHTML = `<option value="">All targets</option>` + groups.map(([a, ts]) => `<optgroup label="${escapeAttr(adapterLabel(a))}">${ts.map((t) => `<option value="${escapeAttr(t.id)}">${escapeHtml(t.label)}</option>`).join("")}</optgroup>`).join("");
  targetSel.value = (S.state?.targets || []).some((t) => t.id === curT) ? curT : "";
}

function historyRowHtml(r) {
  const selected = r.request_id === S.selectedHistoryRequestId;
  const statusCls = r.status === "ok" ? "ok" : (r.status === "error" || r.status === "timeout") ? "bad" : r.status === "cancelled" ? "warn" : "neutral";
  return `<tr class="clickable ${selected ? "selected" : ""}" data-action="select-history-row" data-id="${escapeAttr(r.request_id)}">
    <td class="cell-mono" title="${escapeAttr(new Date(r.created_ms).toLocaleString())}">${timeAgo(r.created_ms)}</td>
    <td class="wrap"><span class="cell-truncate" title="${escapeAttr(r.prompt || "")}">${escapeHtml(truncate(r.prompt || "", 90))}</span></td>
    <td><span class="target-chip"><span class="dot" style="background:${catColorVar(r.target_id)}"></span>${escapeHtml(targetLabel(r.target_id))}</span></td>
    <td>${escapeHtml(routerLabel(r.router_name))}</td>
    <td><span class="pill ${statusCls}">${escapeHtml(r.status || "—")}</span></td>
    <td class="cell-mono">${fmtLatency(r.latency_ms)}</td>
    <td class="cell-mono">${fmtCost(r.cost_usd, r.credits)}</td>
  </tr>`;
}

function renderHistoryTable() {
  const wrap = document.getElementById("history-table-wrap");
  const routerF = document.getElementById("history-router-filter").value;
  const targetF = document.getElementById("history-target-filter").value;
  let rows = S.historyRows;
  if (routerF) rows = rows.filter((r) => r.router_name === routerF);
  if (targetF) rows = rows.filter((r) => r.target_id === targetF);
  if (!S.historyRows.length) {
    wrap.innerHTML = `<div class="empty-state">${HISTORY_ICON}<h3>No requests yet</h3><p>Send a message from Chat and it will show up here with full routing detail.</p></div>`;
    return;
  }
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty-state"><h3>No matches</h3><p>Nothing matches these filters. Try clearing one.</p></div>`;
    return;
  }
  wrap.innerHTML = `<table class="data-table"><thead><tr>
    <th>Time</th><th>Prompt</th><th>Target</th><th>Router</th><th>Status</th><th>Latency</th><th>Cost</th>
  </tr></thead><tbody>${rows.map(historyRowHtml).join("")}</tbody></table>`;
}

async function loadHistory() {
  const wrap = document.getElementById("history-table-wrap");
  wrap.innerHTML = historySkeletonHtml();
  try {
    const data = await fetchJSON("/api/history?limit=200");
    S.historyRows = data.rows || [];
    renderHistoryFilters();
    renderHistoryTable();
  } catch (err) {
    wrap.innerHTML = errorBannerHtml("Could not load history: " + err.message, "retry-history");
  }
}

// ==========================================================================
// analytics view
// ==========================================================================

function analyticsSkeletonHtml() {
  return `<div class="stat-grid">${[1, 2, 3, 4].map(() => '<div class="skeleton skeleton-block"></div>').join("")}</div>`;
}
function statTile(label, value) {
  return `<div class="stat-tile"><span class="label">${escapeHtml(label)}</span><span class="value">${value}</span></div>`;
}
function targetStatsTableHtml(rows) {
  if (!rows.length) return `<div class="loading-row">No executions recorded yet.</div>`;
  return `<table class="data-table"><thead><tr>
    <th>Target</th><th>Calls</th><th>Success</th><th>Avg latency</th><th>Avg TTFT</th><th>Cost</th><th>Credits</th>
  </tr></thead><tbody>${rows.map((r) => {
    const rate = r.success_rate;
    const barCls = rate == null ? "" : rate >= 0.9 ? "good" : rate >= 0.7 ? "warn" : "bad";
    return `<tr>
      <td><span class="target-chip"><span class="dot" style="background:${catColorVar(r.target_id)}"></span>${escapeHtml(targetLabel(r.target_id))}</span></td>
      <td class="cell-mono">${r.n}</td>
      <td><div class="bar-cell"><div class="bar-track"><div class="bar-fill ${barCls}" style="width:${rate != null ? Math.round(rate * 100) : 0}%"></div></div><span class="cell-mono">${rate != null ? Math.round(rate * 100) + "%" : "—"}</span></div></td>
      <td class="cell-mono">${fmtLatency(r.avg_latency_ms)}</td>
      <td class="cell-mono">${fmtLatency(r.avg_ttft_ms)}</td>
      <td class="cell-mono">${fmtCost(r.total_cost_usd, null)}</td>
      <td class="cell-mono">${r.total_credits != null ? Number(r.total_credits).toFixed(2) : "—"}</td>
    </tr>`;
  }).join("")}</tbody></table>`;
}
function routerStatsTableHtml(rows) {
  if (!rows.length) return `<div class="loading-row">No decisions recorded yet.</div>`;
  return `<table class="data-table"><thead><tr>
    <th>Router</th><th>Decisions</th><th>Avg decide</th><th>Avg response</th><th>Success</th><th>Cost</th><th>Credits</th>
  </tr></thead><tbody>${rows.map((r) => `
    <tr>
      <td>${escapeHtml(routerLabel(r.router_name))}</td>
      <td class="cell-mono">${r.n}</td>
      <td class="cell-mono">${fmtLatency(r.avg_decision_ms)}</td>
      <td class="cell-mono">${fmtLatency(r.avg_exec_ms)}</td>
      <td class="cell-mono">${r.success_rate != null ? Math.round(r.success_rate * 100) + "%" : "—"}</td>
      <td class="cell-mono">${fmtCost(r.cost_usd, null)}</td>
      <td class="cell-mono">${r.credits != null ? Number(r.credits).toFixed(2) : "—"}</td>
    </tr>`).join("")}</tbody></table>`;
}
function routingMixHtml(mix) {
  if (!mix.length) return `<p style="color:var(--text-faint);font-size:var(--text-sm);">No routing decisions recorded yet.</p>`;
  const byRouter = new Map();
  mix.forEach((m) => { if (!byRouter.has(m.router_name)) byRouter.set(m.router_name, []); byRouter.get(m.router_name).push(m); });
  const seenTargets = new Map();
  let bars = "";
  for (const [router, segs] of byRouter) {
    const total = segs.reduce((a, s) => a + s.n, 0);
    segs.sort((a, b) => b.n - a.n);
    bars += `<div class="mix-router">
      <div class="mix-router-name">${escapeHtml(routerLabel(router))}<span class="count">${total} decision${total === 1 ? "" : "s"}</span></div>
      <div class="mix-bar">${segs.map((s) => {
        seenTargets.set(s.target_id, targetLabel(s.target_id));
        const pct = total ? (s.n / total) * 100 : 0;
        return `<div class="mix-seg" style="width:${pct}%;background:${catColorVar(s.target_id)}" title="${escapeAttr(targetLabel(s.target_id))}: ${s.n} (${pct.toFixed(0)}%)"></div>`;
      }).join("")}</div>
    </div>`;
  }
  const legend = [...seenTargets.entries()].map(([id, label]) => `<div class="legend-item"><span class="legend-swatch" style="background:${catColorVar(id)}"></span>${escapeHtml(label)}</div>`).join("");
  return bars + `<div class="mix-legend">${legend}</div>`;
}

function renderAnalytics(data) {
  const el = document.getElementById("analytics-body");
  const totals = data.totals || {};
  if (!totals.requests) {
    el.innerHTML = `<div class="empty-state">${ANALYTICS_ICON}<h3>Not enough data yet</h3><p>Send a few chat messages and this view fills in with real routing performance.</p></div>`;
    return;
  }
  el.innerHTML = `
    <div class="stat-grid">
      ${statTile("Requests", (totals.requests ?? 0).toLocaleString())}
      ${statTile("Executions", (totals.executions ?? 0).toLocaleString())}
      ${statTile("Total cost", fmtCost(totals.cost_usd, null))}
      ${statTile("Total credits", totals.credits != null ? Number(totals.credits).toFixed(2) + " cr" : "—")}
    </div>
    <div class="panel">
      <div class="panel-head"><h3>Per-target performance</h3><span class="hint">${(data.targets || []).length} target${(data.targets || []).length === 1 ? "" : "s"} with traffic</span></div>
      <div class="table-wrap">${targetStatsTableHtml(data.targets || [])}</div>
    </div>
    <div class="panel">
      <div class="panel-head"><h3>Per-router comparison</h3></div>
      <div class="table-wrap">${routerStatsTableHtml(data.routers || [])}</div>
    </div>
    <div class="panel">
      <div class="panel-head"><h3>Routing mix</h3><span class="hint">which targets each router reaches for</span></div>
      <div class="panel-body">${routingMixHtml(data.routing_mix || [])}</div>
    </div>`;
}

async function loadStats() {
  const el = document.getElementById("analytics-body");
  el.innerHTML = analyticsSkeletonHtml();
  try {
    renderAnalytics(await fetchJSON("/api/stats"));
  } catch (err) {
    el.innerHTML = errorBannerHtml("Could not load analytics: " + err.message, "retry-stats");
  }
}

// ==========================================================================
// view switching
// ==========================================================================

function switchView(view) {
  S.activeView = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === view));
  closeRailIfNarrow();
  if (view === "history") loadHistory();
  if (view === "analytics") loadStats();
}

// ==========================================================================
// state bootstrap
// ==========================================================================

async function loadState() {
  try {
    const data = await fetchJSON("/api/state");
    S.state = data;
    renderRouterPopover();
    renderPinSelect();
    renderHealth();
    updateTargetCountFooter();
    if (S.compareMode) renderComparePicker();
    updateModeLabel();
    clearChatError();
  } catch (err) {
    showChatError("Could not reach the Turnout API: " + err.message);
  }
}

// ==========================================================================
// global click delegation
// ==========================================================================

async function onGlobalClick(e) {
  const t = e.target.closest("[data-action]");
  if (!t) return;
  const action = t.dataset.action;
  switch (action) {
    case "toggle-thinking":
      t.closest(".thinking-block")?.classList.toggle("open");
      break;
    case "copy": {
      const codeEl = document.getElementById(t.dataset.copyTarget);
      if (!codeEl) return;
      try {
        await navigator.clipboard.writeText(codeEl.textContent);
        t.textContent = "Copied";
        t.classList.add("copied");
        setTimeout(() => { t.textContent = "Copy"; t.classList.remove("copied"); }, 1400);
      } catch {
        toast("Could not copy to clipboard.", "info");
      }
      break;
    }
    case "vote-up":
    case "vote-down": {
      const turn = turnsById.get(t.dataset.turn);
      if (!turn || !turn.requestId) return;
      const rating = action === "vote-up" ? 1 : -1;
      turn.feedbackRating = turn.feedbackRating === rating ? 0 : rating;
      updateVoteButtons(turn);
      fetchJSON("/api/feedback", {
        method: "POST", headers: JSON_HEADERS,
        body: JSON.stringify({ request_id: turn.requestId, execution_id: turn.usage?.execution_id, rating: turn.feedbackRating }),
      }).catch((err) => toast("Could not save feedback: " + err.message));
      break;
    }
    case "inspect-turn": {
      const turn = turnsById.get(t.dataset.turn);
      if (!turn) return;
      if (turn.decision) setInspectorFromTurn(turn);
      else if (turn.requestId) await loadRequestIntoInspector(turn.requestId, "From history");
      openInspectorIfNarrow();
      break;
    }
    case "toggle-compare-target": {
      const tid = t.dataset.target;
      const idx = S.compareTargets.indexOf(tid);
      if (idx >= 0) S.compareTargets.splice(idx, 1);
      else {
        if (S.compareTargets.length >= 4) { toast("Compare supports up to 4 models at once.", "info"); return; }
        S.compareTargets.push(tid);
      }
      renderComparePicker();
      updateModeLabel();
      break;
    }
    case "compare-win": {
      const turn = turnsById.get(t.dataset.turn);
      const targetId = t.dataset.target;
      if (!turn) return;
      const winner = turn.colState[targetId]?.usage?.execution_id;
      if (!winner) { toast("Wait for this response to finish before picking a winner.", "info"); return; }
      const losers = Object.entries(turn.colState)
        .filter(([tid, c]) => tid !== targetId && c.usage?.execution_id)
        .map(([, c]) => c.usage.execution_id);
      if (!losers.length) { toast("Need at least one other finished response to compare against.", "info"); return; }
      Object.keys(turn.colState).forEach((tid) => {
        turn.dom.querySelector(`.compare-col[data-target="${CSS.escape(tid)}"] .win-btn`)?.classList.toggle("winner", tid === targetId);
      });
      Promise.all(losers.map((loser) => fetchJSON("/api/preference", {
        method: "POST", headers: JSON_HEADERS,
        body: JSON.stringify({ request_id: turn.requestId, winner, loser }),
      }))).then(() => toast("Preference recorded — thanks.", "info")).catch((err) => toast("Could not record preference: " + err.message));
      break;
    }
    case "select-history-row": {
      const id = t.dataset.id;
      if (!id) return;
      document.querySelectorAll("#history-table-wrap tr.selected").forEach((r) => r.classList.remove("selected"));
      t.classList.add("selected");
      S.selectedHistoryRequestId = id;
      await loadRequestIntoInspector(id, "From history");
      openInspectorIfNarrow();
      break;
    }
    case "load-session": {
      const sid = t.dataset.session;
      if (sid) await loadSession(sid);
      break;
    }
    case "select-router":
      closeAllPopovers();
      await postRouter(t.dataset.router);
      break;
    case "recheck-adapters": {
      t.disabled = true;
      t.textContent = "Checking…";
      try { await fetchJSON("/api/probe", { method: "POST" }); await loadState(); }
      catch (err) { toast("Recheck failed: " + err.message); }
      break;
    }
    case "retry-state": loadState(); break;
    case "retry-history": loadHistory(); break;
    case "retry-stats": loadStats(); break;
  }
}

// ==========================================================================
// wiring + boot
// ==========================================================================

function wireEvents() {
  document.getElementById("composer-form").addEventListener("submit", handleComposerSubmit);
  const input = document.getElementById("composer-input");
  input.addEventListener("input", () => autoGrow(input));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); document.getElementById("composer-form").requestSubmit(); }
  });
  document.getElementById("new-chat-btn").addEventListener("click", resetChat);
  document.getElementById("compare-toggle-btn").addEventListener("click", toggleCompareMode);
  document.getElementById("priority-select").addEventListener("change", (e) => { S.priority = e.target.value; updateModeLabel(); });
  document.getElementById("pin-select").addEventListener("change", (e) => { S.pinTarget = e.target.value; updateModeLabel(); });
  document.getElementById("theme-toggle-btn").addEventListener("click", cycleTheme);
  document.getElementById("rail-toggle-btn").addEventListener("click", () => setRailOpen(!document.getElementById("rail").classList.contains("open")));
  document.getElementById("rail-backdrop").addEventListener("click", () => setRailOpen(false));
  document.getElementById("inspector-toggle-btn").addEventListener("click", () => setInspectorOpen(!S.inspectorOpen));
  document.getElementById("inspector-close-btn").addEventListener("click", () => setInspectorOpen(false));
  document.getElementById("inspector-backdrop").addEventListener("click", () => setInspectorOpen(false));
  document.getElementById("history-refresh-btn").addEventListener("click", loadHistory);
  document.getElementById("history-router-filter").addEventListener("change", renderHistoryTable);
  document.getElementById("history-target-filter").addEventListener("change", renderHistoryTable);
  document.getElementById("analytics-refresh-btn").addEventListener("click", loadStats);
  document.getElementById("nav-chat").addEventListener("click", () => switchView("chat"));
  document.getElementById("nav-history").addEventListener("click", () => switchView("history"));
  document.getElementById("nav-analytics").addEventListener("click", () => switchView("analytics"));
  document.getElementById("health-btn").addEventListener("click", (e) => { e.stopPropagation(); togglePopover("health-btn", "health-popover"); });
  document.getElementById("router-btn").addEventListener("click", (e) => { e.stopPropagation(); togglePopover("router-btn", "router-popover"); });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".health-popover") && !e.target.closest("#health-btn") && !e.target.closest("#router-btn")) closeAllPopovers();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAllPopovers(); });
  window.addEventListener("resize", () => setInspectorOpen(S.inspectorOpen));
  document.body.addEventListener("click", onGlobalClick);
}

async function boot() {
  applyTheme();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (S.theme === "system") updateThemeButton(); });
  wireEvents();
  document.getElementById("chat-transcript").innerHTML = emptyStateHtml();
  renderInspector();
  setInspectorOpen(!isNarrow());
  updateModeLabel();
  await Promise.all([loadState(), loadSessions()]);
}

document.addEventListener("DOMContentLoaded", boot);
