"use strict";
// TRACE dashboard. Every number on the page comes from /api/snapshot (the trace_results views); the only fixed
// text is descriptive (what a fault is, what a component does) and anything not built is labelled as such.

const REFRESH_MS = 60000;
const PLANNED = "designed / in progress";
const CATEGORY_ORDER = [
  "authorization_failure",
  "warehouse_schema_mismatch",
  "resource_limit_exceeded",
  "dns_or_endpoint_unreachable",
];
const FAULT_CATALOG = [
  { fault: "iam_revoked", how: "Remove the ETL service account's write access on the target dataset", category: "authorization_failure" },
  { fault: "bq_extra_column", how: "Add a column to the target table so the insert's column count no longer matches", category: "warehouse_schema_mismatch" },
  { fault: "task_timeout_kill", how: "Lower the job's task timeout to a few seconds so the run is killed", category: "resource_limit_exceeded" },
  { fault: "bad_endpoint_url", how: "Point the job's source URL at a host that does not resolve", category: "dns_or_endpoint_unreachable" },
];
const PLANNED_FAULTS = ["upstream_slow", "upstream_429", "upstream_schema_drift", "partial_upstream", "dataset_missing", "control runs (no fault)"];

const state = {
  snapshot: null,
  fingerprint: null,
  campaign: null,
  tab: "overview",
  filters: { q: "", fault: "", outcome: "", model: "" },
  lastOkAt: null,
  error: null,
  opener: null,
};

// ---- small helpers --------------------------------------------------------------------------------------------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);
const clear = (el) => { el.replaceChildren(); return el; };

// An identifier such as authorization_failure, allowed to wrap after each underscore.
function ident(value) {
  const span = h("span", { class: "mono" });
  String(value).split("_").forEach((part, i, all) => {
    span.append(part + (i < all.length - 1 ? "_" : ""));
    if (i < all.length - 1) span.append(document.createElement("wbr"));
  });
  return span;
}
function fmtTime(iso, withDate) {
  if (!iso) return "n/a";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "n/a";
  const s = d.toISOString();
  return (withDate ? s.slice(0, 10) + " " : "") + s.slice(11, 16) + " UTC";
}
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "n/a";
  const s = Math.round(Number(seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}
function fmtPercent(ratio) {
  if (ratio === null || ratio === undefined) return "n/a";
  const pct = Number(ratio) * 100;
  return (Number.isInteger(pct) ? pct : pct.toFixed(1)) + "%";
}
function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}
function countBy(rows, key) {
  const counts = new Map();
  for (const row of rows) {
    const value = typeof key === "function" ? key(row) : row[key];
    if (value === null || value === undefined) continue;
    counts.set(value, (counts.get(value) || 0) + 1);
  }
  return counts;
}
function statusMark(outcome) {
  const map = {
    correct: ["good", "✓", "Correct"],
    wrong: ["bad", "✕", "Wrong"],
    "not scored": ["pending", "○", "Not scored"],
    "not run": ["pending", "○", "Not run"],
  };
  const [cls, icon, label] = map[outcome] || ["pending", "○", outcome || "n/a"];
  return h("span", { class: `status ${cls}` }, h("span", { class: "icon", "aria-hidden": "true" }, icon), label);
}
// Colour follows the model, not its rank: slots are assigned by model name across the whole campaign.
function modelSlots(incidents) {
  const names = [...new Set(incidents.map((i) => i.postmortem_model).filter(Boolean))].sort();
  return new Map(names.map((name, i) => [name, i < 4 ? `s${i + 1}` : "s0"]));
}
function summaryFor(snapshot) {
  return snapshot.campaigns.find((c) => c.campaign_id === snapshot.campaign_id) || null;
}

// ---- tooltip ----------------------------------------------------------------------------------------------------

function attachTip(el, textFn) {
  const tip = $("tooltip");
  const show = (x, y) => {
    tip.textContent = textFn();
    tip.hidden = false;
    const pad = 12;
    const rect = tip.getBoundingClientRect();
    tip.style.left = Math.max(4, Math.min(x + pad, window.innerWidth - rect.width - 4)) + "px";
    tip.style.top = Math.max(4, Math.min(y + pad, window.innerHeight - rect.height - 4)) + "px";
  };
  el.addEventListener("mousemove", (e) => show(e.clientX, e.clientY));
  el.addEventListener("mouseleave", () => { tip.hidden = true; });
  el.addEventListener("focus", () => { const r = el.getBoundingClientRect(); show(r.left, r.bottom); });
  el.addEventListener("blur", () => { tip.hidden = true; });
}

// ---- data ---------------------------------------------------------------------------------------------------------

async function refresh() {
  const query = state.campaign ? "?campaign=" + encodeURIComponent(state.campaign) : "";
  try {
    const response = await fetch("api/snapshot" + query, { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = await response.json();
    // Re-draw only when the data changed, so an unchanged refresh does not disturb typing or scrolling.
    const fingerprint = JSON.stringify([next.campaign_id, next.campaigns, next.incidents, next.matrix, next.stale]);
    const changed = fingerprint !== state.fingerprint;
    state.snapshot = next;
    state.fingerprint = fingerprint;
    state.campaign = next.campaign_id;
    state.lastOkAt = Date.now();
    state.error = null;
    if (changed) renderAll();
    else renderBanner();
  } catch (err) {
    state.error = String(err && err.message ? err.message : err);
    renderBanner();
  }
  renderLive();
}

function renderLive() {
  const live = $("live");
  const text = $("live-text");
  const age = state.lastOkAt === null ? null : Math.round((Date.now() - state.lastOkAt) / 1000);
  live.className = "live";
  if (state.error && age === null) {
    live.classList.add("error");
    text.textContent = "Not loaded";
  } else if (state.error) {
    live.classList.add("error");
    text.textContent = `Refresh failed · last data ${age} s ago`;
  } else if (state.snapshot && state.snapshot.stale) {
    live.classList.add("warn");
    text.textContent = `Stale · query failed, data from ${fmtTime(state.snapshot.fetched_at)}`;
  } else if (age !== null) {
    live.classList.add("ok");
    text.textContent = `Live · refreshed ${age} s ago`;
  } else {
    text.textContent = "Loading…";
  }
  if (state.snapshot) {
    live.title = `Queried from BigQuery at ${fmtTime(state.snapshot.fetched_at)}; results are cached for ${state.snapshot.cache_ttl_s} s.`;
  }
}

function renderBanner() {
  const banner = $("banner");
  if (state.error) {
    banner.hidden = false;
    banner.className = "banner error";
    banner.textContent = state.snapshot
      ? `Could not refresh (${state.error}). Showing the last data that loaded.`
      : `Could not load results (${state.error}). Nothing is shown rather than placeholder numbers.`;
  } else if (state.snapshot && state.snapshot.stale) {
    banner.hidden = false;
    banner.className = "banner";
    banner.textContent = "The last BigQuery query failed on the server. Showing the previous result.";
  } else {
    banner.hidden = true;
  }
}

// ---- rendering ----------------------------------------------------------------------------------------------------

function renderAll() {
  const snap = state.snapshot;
  if (!snap) return;
  $("dataset-name").textContent = snap.dataset;
  const select = clear($("campaign"));
  for (const c of snap.campaigns) {
    select.append(h("option", { value: c.campaign_id, selected: c.campaign_id === snap.campaign_id }, c.campaign_id));
  }
  if (!snap.campaigns.length) select.append(h("option", { value: "" }, "no campaigns"));
  renderBanner();
  renderOverview();
  renderIncidentsTab();
  renderFaults();
  renderArchitecture();
}

function card(title, ...children) {
  return h("div", { class: "card" }, title ? h("h2", {}, title) : null, ...children);
}

function renderOverview() {
  const snap = state.snapshot;
  const root = clear($("tab-overview"));
  const summary = summaryFor(snap);
  const incidents = snap.incidents;
  if (!summary) {
    root.append(card(null, h("div", { class: "empty" }, "No campaign has been recorded in this dataset yet.")));
    return;
  }

  // KPI row
  const times = incidents.map((i) => i.time_to_diagnosis_sec).filter((v) => v !== null && v !== undefined).map(Number);
  const written = incidents.filter((i) => i.postmortem_text);
  const slots = modelSlots(incidents);
  const byModel = [...countBy(written, "postmortem_model")].sort((a, b) => a[0].localeCompare(b[0]));
  const split = h("div", { class: "split", role: "img", "aria-label": byModel.map(([m, n]) => `${m}: ${n}`).join(", ") });
  for (const [model, n] of byModel) {
    const seg = h("span", { class: slots.get(model) || "s0", tabindex: "0" });
    seg.style.flex = String(n);
    attachTip(seg, () => `${model}: ${n} of ${written.length} postmortems`);
    split.append(seg);
  }
  const legend = h("div", { class: "legend" }, byModel.map(([model, n]) =>
    h("span", {}, h("i", { class: slots.get(model) || "s0" }), `${model} · ${n}`)));
  root.append(h("div", { class: "grid kpis" },
    h("div", { class: "card" },
      h("div", { class: "kpi-label" }, "Accuracy"),
      h("div", { class: "kpi-value" }, summary.scored ? fmtPercent(summary.accuracy) : "n/a"),
      h("div", { class: "kpi-sub" }, `${summary.correct} of ${summary.scored} scored incidents correct`)),
    h("div", { class: "card" },
      h("div", { class: "kpi-label" }, "Incidents scored"),
      h("div", { class: "kpi-value" }, `${summary.scored} / ${summary.planned}`),
      h("div", { class: "kpi-sub" }, `${summary.run} run of ${summary.planned} planned`)),
    h("div", { class: "card" },
      h("div", { class: "kpi-label" }, "Time to diagnosis (median)"),
      h("div", { class: "kpi-value" }, times.length ? fmtDuration(median(times)) : "n/a"),
      h("div", { class: "kpi-sub" }, times.length
        ? `range ${fmtDuration(Math.min(...times))} to ${fmtDuration(Math.max(...times))}, failure to verdict`
        : "no scored incidents yet")),
    h("div", { class: "card" },
      h("div", { class: "kpi-label" }, "Postmortems"),
      h("div", { class: "kpi-value" }, `${written.length} / ${incidents.length}`),
      written.length ? split : null,
      written.length ? legend : h("div", { class: "kpi-sub" }, "none written yet"))));

  // Agent pipeline strip
  const total = incidents.length;
  const rcModels = [...new Set(incidents.map((i) => i.root_cause_model).filter(Boolean))].join(", ");
  const pmModels = byModel.map(([m]) => m).join(", ");
  const done = (pred) => `${incidents.filter(pred).length} / ${total}`;
  const stage = (name, kind, count, what, separate) => h("div", { class: "stage" + (separate ? " separate" : "") },
    h("div", { class: "stage-name" }, name), h("div", { class: "stage-kind" }, kind),
    h("div", { class: "stage-count" }, count), h("div", { class: "stage-what" }, what));
  root.append(h("div", { class: "grid" }, card("Agent pipeline",
    h("div", { class: "pipeline" },
      stage("1 · Triage", "rules, no LLM", done((i) => i.severity), "incidents rated for severity"),
      stage("2 · Root-Cause", rcModels ? `LLM · ${rcModels}` : "LLM", done((i) => i.verdict_category), "verdicts from the closed taxonomy"),
      stage("3 · Remediation", "rules, propose-only", done((i) => Array.isArray(i.remediation_proposals) && i.remediation_proposals.length),
        `fixes proposed, ${incidents.filter((i) => i.remediation_executed === true).length} executed`),
      stage("4 · Postmortem", pmModels ? `LLM · ${pmModels}` : "LLM", done((i) => i.postmortem_text), "reports written"),
      stage("5 · Evaluation", "separate identity, deterministic", done((i) => i.outcome === "correct" || i.outcome === "wrong"),
        "scored against ground truth", true)),
    h("div", { class: "card-note" }, "Stages 1 to 4 run inside trace-api as the agent identity. Evaluation runs afterwards as a different identity; the agents cannot read ground truth."))));

  // Confusion matrix and accuracy by fault
  root.append(h("div", { class: "grid two" }, matrixCard(snap), accuracyByFaultCard(incidents)));

  // Timeline chips
  const chips = h("div", { class: "chips" }, incidents.map((i) => {
    const chip = h("button", { class: "chip", type: "button", onclick: (e) => openDrawer(i, e.currentTarget) },
      h("div", { class: "chip-top" }, h("span", {}, `#${i.position}`), h("span", {}, fmtTime(i.failure_time))),
      h("div", { class: "chip-main" }, i.verdict_category ? ident(i.verdict_category) : h("span", { class: "muted" }, i.outcome)),
      h("div", { class: "chip-top" }, statusMark(i.outcome)));
    return chip;
  }));
  root.append(h("div", { class: "grid" }, card("Campaign timeline",
    incidents.length ? chips : h("div", { class: "empty" }, "No planned injections."),
    h("div", { class: "card-note" }, summary.first_failure
      ? `First failure ${fmtTime(summary.first_failure, true)}, last ${fmtTime(summary.last_failure, true)}. Select a chip for the incident detail.`
      : "No failures recorded yet."))));

  root.append(h("div", { class: "grid" }, incidentsCard("Incidents", false)));
}

function matrixCard(snap) {
  const cells = snap.matrix;
  const trues = [...new Set(cells.map((c) => c.true_category))]
    .sort((a, b) => CATEGORY_ORDER.indexOf(a) - CATEGORY_ORDER.indexOf(b));
  const rank = (c) => (CATEGORY_ORDER.includes(c) ? CATEGORY_ORDER.indexOf(c) : 99);
  const preds = [...new Set(cells.map((c) => c.predicted_category))].sort((a, b) => rank(a) - rank(b));
  const max = Math.max(0, ...cells.map((c) => Number(c.incidents)));
  const level = (n) => (n <= 0 || max <= 0 ? 0 : Math.max(1, Math.ceil((n / max) * 4)));
  const table = h("table", { class: "matrix" },
    h("thead", {}, h("tr", {}, h("th", { class: "row" }, h("span", { class: "axis-title" }, "true ↓ · predicted →")),
      preds.map((p) => h("th", { scope: "col" }, ident(p))))),
    h("tbody", {}, trues.map((t) => h("tr", {}, h("th", { class: "row", scope: "row" }, ident(t)),
      preds.map((p) => {
        const cell = cells.find((c) => c.true_category === t && c.predicted_category === p);
        const n = cell ? Number(cell.incidents) : 0;
        const td = h("td", { class: `seq-${level(n)}` + (t === p ? " diag" : ""), tabindex: "0" }, String(n));
        attachTip(td, () => `True: ${t} · Predicted: ${p} · ${n} incident${n === 1 ? "" : "s"}`);
        return td;
      })))));
  const scale = h("div", { class: "scale" }, "0", [0, 1, 2, 3, 4].map((l) => h("i", { class: `seq-${l}` })), String(max),
    h("span", {}, "incidents per cell · outlined cells are the diagonal (correct)"));
  return card("Confusion matrix",
    cells.length ? h("div", { class: "table-wrap" }, table) : h("div", { class: "empty" }, "No scored incidents yet."),
    cells.length ? scale : null,
    h("div", { class: "card-note" }, "Small sample: shows the pipeline works end to end, not 100% in general."));
}

function accuracyByFaultCard(incidents) {
  const faults = [...new Set(incidents.map((i) => i.true_cause))].sort();
  const bars = h("div", { class: "bars" }, faults.map((fault) => {
    const rows = incidents.filter((i) => i.true_cause === fault);
    const scored = rows.filter((i) => i.outcome === "correct" || i.outcome === "wrong");
    const correct = scored.filter((i) => i.outcome === "correct").length;
    const fill = h("div", { class: "bar-fill" });
    fill.style.width = scored.length ? (correct / scored.length) * 100 + "%" : "0";
    const track = h("div", { class: "bar-track", tabindex: "0" }, fill);
    attachTip(track, () => `${fault}: ${correct} of ${scored.length} scored correct (${rows.length} planned)`);
    return h("div", {},
      h("div", { class: "bar-head" }, ident(fault),
        h("span", { class: "secondary" }, scored.length ? `${correct} / ${scored.length} · ${fmtPercent(correct / scored.length)}` : "not scored")),
      track);
  }));
  return card("Accuracy by fault",
    faults.length ? bars : h("div", { class: "empty" }, "No planned injections."),
    h("div", { class: "card-note" }, "Small sample: shows the pipeline works end to end, not 100% in general."));
}

// ---- incidents table ----------------------------------------------------------------------------------------------

function filteredIncidents() {
  const f = state.filters;
  const q = f.q.trim().toLowerCase();
  return state.snapshot.incidents.filter((i) => {
    if (f.fault && i.true_cause !== f.fault) return false;
    if (f.outcome && i.outcome !== f.outcome) return false;
    if (f.model && i.postmortem_model !== f.model) return false;
    if (!q) return true;
    return [i.failed_execution, i.incident_id, i.verdict_category, i.true_category, i.true_cause, i.verdict_hypothesis,
      i.failed_run_status, i.severity].some((v) => v && String(v).toLowerCase().includes(q));
  });
}

function incidentsCard(title, detailed) {
  const all = state.snapshot.incidents;
  const rows = filteredIncidents();
  const slots = modelSlots(all);
  const options = (values, current, label) => [h("option", { value: "" }, label),
    ...values.map((v) => h("option", { value: v, selected: v === current }, v))];
  const set = (key) => (e) => { state.filters[key] = e.target.value; rerenderTables(); };
  const search = h("input", { type: "search", placeholder: "Search execution, category, hypothesis", "aria-label": "Search incidents", value: state.filters.q, size: "34" });
  search.addEventListener("input", (e) => {
    state.filters.q = e.target.value;
    rerenderTables(e.target);
  });
  const filters = h("div", { class: "filters" },
    search,
    h("select", { "aria-label": "Fault", onchange: set("fault") }, options([...new Set(all.map((i) => i.true_cause))].sort(), state.filters.fault, "All faults")),
    h("select", { "aria-label": "Outcome", onchange: set("outcome") }, options([...new Set(all.map((i) => i.outcome))].sort(), state.filters.outcome, "All outcomes")),
    h("select", { "aria-label": "Postmortem model", onchange: set("model") }, options([...slots.keys()], state.filters.model, "All postmortem models")),
    h("span", { class: "muted" }, `${rows.length} of ${all.length}`),
    h("span", { class: "spacer" }),
    h("button", { class: "ghost", type: "button", onclick: () => exportCsv(rows) }, "Export CSV"));

  const head = ["#", "Failure (UTC)", "Execution", "Run status", "Severity", "Verdict", "Conf.", "True cause", "Outcome", "Postmortem by"];
  if (detailed) head.push("To diagnosis", "Hypothesis");
  const numeric = new Set(["#", "Conf.", "To diagnosis"]);
  const table = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, head.map((t) => h("th", { class: numeric.has(t) ? "num" : null, scope: "col" }, t)))),
    h("tbody", {}, rows.map((i) => {
      const open = (e) => openDrawer(i, e.currentTarget);
      const tr = h("tr", { class: "clickable", tabindex: "0", onclick: open,
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); } } },
        h("td", { class: "num" }, i.position),
        h("td", {}, fmtTime(i.failure_time, true)),
        h("td", { class: "mono" }, i.failed_execution || "n/a"),
        h("td", {}, i.failed_run_status || "n/a"),
        h("td", {}, i.severity || "n/a"),
        h("td", {}, i.verdict_category ? ident(i.verdict_category) : h("span", { class: "muted" }, "no verdict")),
        h("td", { class: "num" }, i.confidence === null || i.confidence === undefined ? "n/a" : Number(i.confidence).toFixed(2)),
        h("td", {}, ident(i.true_cause)),
        h("td", {}, statusMark(i.outcome)),
        h("td", {}, i.postmortem_model
          ? h("span", {}, h("i", { class: "swatch " + (slots.get(i.postmortem_model) || "s0") }), i.postmortem_model)
          : h("span", { class: "muted" }, "none")));
      if (detailed) {
        tr.append(h("td", { class: "num" }, fmtDuration(i.time_to_diagnosis_sec)),
          h("td", {}, h("div", { class: "clip", title: i.verdict_hypothesis || "" }, i.verdict_hypothesis || "n/a")));
      }
      return tr;
    })));
  return h("div", { class: "card incidents-card", "data-detailed": detailed ? "1" : "0" },
    h("h2", {}, title), filters,
    rows.length ? h("div", { class: "table-wrap" }, table) : h("div", { class: "empty" }, all.length ? "No incidents match these filters." : "No incidents in this campaign."));
}

function rerenderTables(focusSource) {
  const caret = focusSource ? focusSource.selectionStart : null;
  const inDetailed = focusSource ? focusSource.closest(".incidents-card").dataset.detailed : null;
  document.querySelectorAll(".incidents-card").forEach((old) => {
    const detailed = old.dataset.detailed === "1";
    const fresh = incidentsCard(old.querySelector("h2").textContent, detailed);
    old.replaceWith(fresh);
    if (focusSource && inDetailed === fresh.dataset.detailed) {
      const input = fresh.querySelector('input[type="search"]');
      input.focus();
      input.setSelectionRange(caret, caret);
    }
  });
}

function renderIncidentsTab() {
  clear($("tab-incidents")).append(h("div", { class: "grid" }, incidentsCard("All incidents", true)));
}

function exportCsv(rows) {
  const columns = ["campaign_id", "position", "incident_id", "failure_time", "failed_execution", "failed_run_status", "severity",
    "verdict_category", "confidence", "root_cause_model", "true_category", "true_cause", "injected_params", "match", "outcome",
    "time_to_diagnosis_sec", "postmortem_model", "postmortem_fallback_reason", "injected_at", "reverted_at", "verdict_hypothesis"];
  const cell = (value) => {
    if (value === null || value === undefined) return "";
    let text = typeof value === "object" ? JSON.stringify(value) : String(value);
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text; // never let a spreadsheet read a cell as a formula
    return '"' + text.replace(/"/g, '""') + '"';
  };
  const lines = [columns.join(","), ...rows.map((row) => columns.map((c) => cell(row[c])).join(","))];
  const url = URL.createObjectURL(new Blob([lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" }));
  const link = h("a", { href: url, download: `trace-${state.snapshot.campaign_id}-incidents.csv` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- faults & ledger ------------------------------------------------------------------------------------------------

function renderFaults() {
  const incidents = state.snapshot.incidents;
  const root = clear($("tab-faults"));
  const catalog = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["Fault", "How it is injected", "Expected category", "Injected", "Correct"].map((t, i) =>
      h("th", { class: i > 2 ? "num" : null, scope: "col" }, t)))),
    h("tbody", {}, FAULT_CATALOG.map((f) => {
      const rows = incidents.filter((i) => i.true_cause === f.fault);
      return h("tr", {}, h("td", {}, ident(f.fault)), h("td", {}, f.how), h("td", {}, ident(f.category)),
        h("td", { class: "num" }, `${rows.filter((i) => i.outcome !== "not run").length} / ${rows.length}`),
        h("td", { class: "num" }, rows.filter((i) => i.outcome === "correct").length));
    })));
  root.append(h("div", { class: "grid" }, card("Fault catalog", h("div", { class: "table-wrap" }, catalog),
    h("div", { class: "card-note" }, "Not injected in this build: ",
      PLANNED_FAULTS.map((name) => [h("span", { class: "pill planned" }, `${name} · ${PLANNED}`), " "])))));

  const seeds = [...new Set(incidents.map((i) => i.seed).filter((s) => s !== null && s !== undefined))];
  const ledger = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["#", "Fault", "Parameters", "Injected (UTC)", "Reverted (UTC)", "Open for", "Execution", "Outcome"].map((t) =>
      h("th", { class: t === "#" || t === "Open for" ? "num" : null, scope: "col" }, t)))),
    h("tbody", {}, incidents.map((i) => {
      const open = i.injected_at && i.reverted_at ? (new Date(i.reverted_at) - new Date(i.injected_at)) / 1000 : null;
      return h("tr", { class: "clickable", tabindex: "0", onclick: (e) => openDrawer(i, e.currentTarget) },
        h("td", { class: "num" }, i.position), h("td", {}, ident(i.true_cause)),
        h("td", { class: "mono" }, i.injected_params ? JSON.stringify(i.injected_params) : "n/a"),
        h("td", {}, fmtTime(i.injected_at, true)),
        h("td", {}, i.injected_at && !i.reverted_at ? h("span", { class: "status bad" }, h("span", { class: "icon" }, "!"), "still open") : fmtTime(i.reverted_at, true)),
        h("td", { class: "num" }, fmtDuration(open)),
        h("td", { class: "mono" }, i.failed_execution || "n/a"),
        h("td", {}, statusMark(i.outcome)));
    })));
  root.append(h("div", { class: "grid" }, card("Fault ledger (ground truth, evaluator only)",
    incidents.length ? h("div", { class: "table-wrap" }, ledger) : h("div", { class: "empty" }, "No planned injections."),
    h("div", { class: "card-note" }, `Plan order is fixed before the first injection${seeds.length ? ` (seed ${seeds.join(", ")})` : ""}; every planned item is listed, run or not. The agents cannot read this table.`))));
}

// ---- architecture -----------------------------------------------------------------------------------------------------

function renderArchitecture() {
  const node = (name, what, planned) => h("div", { class: "node" + (planned ? " planned" : "") },
    h("div", { class: "node-name" }, name, planned ? [" ", h("span", { class: "pill planned" }, PLANNED)] : null),
    h("div", { class: "node-what" }, what));
  const arrow = (label) => h("div", { class: "arrow" }, "↓ " + label);
  const lane = (title, ...items) => h("div", { class: "card" }, h("div", { class: "lane-title" }, title), ...items);
  const root = clear($("tab-architecture"));
  root.append(h("div", { class: "grid arch" },
    lane("Target system",
      node("Cloud Run job trace-target-etl", "Hourly air-quality ETL for 36 states (Open-Meteo), runs as trace-etl."),
      arrow("loads"),
      node("BigQuery trace_target", "aqi_hourly and pipeline_runs."),
      arrow("logs"),
      node("Cloud Logging, view target-only", "The only logs the agents can read."),
      arrow("is changed by"),
      node("Cloud Run job trace-maintenance-runner", "Injects one fault at a time and always reverts it (dead-man's switch), as ops-automation."),
      node("Log sink → Pub/Sub trigger, freshness watchdog", "Automatic incident detection.", true),
      node("Upstream gateway service", "Faults that need a proxy in front of the data source.", true)),
    lane("Agents",
      node("Campaign runner", "One script, a different identity per step: inject, run the job, call the agents, revert, score."),
      arrow("sends the failed execution name"),
      node("Cloud Run service trace-api (private)", "LangGraph orchestrator, runs as trace-agents."),
      arrow("runs in order"),
      node("Triage → Root-Cause → Remediation → Postmortem", "Only Root-Cause and Postmortem call an LLM. Remediation proposes; IAM gives the agents no write access to the target."),
      arrow("writes"),
      node("BigQuery trace_ledger", "Incidents, events, verdicts, postmortems."),
      node("Embedding memory and the memory ablation", "Retrieval of similar past incidents.", true),
      node("MCP server trace-mcp", "Read-only tools for an MCP client.", true)),
    lane("Evaluation and reporting",
      node("Evaluator", "Deterministic category match, runs as trace-evaluator after the agents finish. Never an LLM."),
      arrow("writes"),
      node("BigQuery trace_truth", "Fault ledger, campaign plan, evaluation results. The agents have no access."),
      arrow("joined by"),
      node("Views in trace_results", "incident_results, campaign_summary, confusion_matrix."),
      arrow("read by"),
      node("This dashboard, trace-dashboard (private)", "Read-only. Its identity can read the trace_results views and nothing else."),
      node("Looker Studio report", "Over the same views, with the owner's own account."),
      node("The 'unknown' category and control runs", "Wider taxonomy and no-fault runs.", true))));
  const access = [
    ["trace-etl", "writes trace_target", "nothing else"],
    ["trace-agents", "reads trace_target and the target-only log view; writes trace_ledger", "no trace_truth, no trace_results, no write on the target"],
    ["ops-automation", "changes the target job, dataset and table; writes the fault ledger", "does not run the agents"],
    ["trace-evaluator", "reads trace_ledger; writes evaluation results", "does not call an LLM"],
    ["trace-dashboard", "reads the trace_results views", "no direct access to trace_truth, trace_ledger or trace_target"],
  ];
  root.append(h("div", { class: "grid" }, card("Who can read what",
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Identity", "Can", "Cannot"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, access.map(([who, can, cannot]) => h("tr", {}, h("td", { class: "mono" }, who), h("td", {}, can), h("td", {}, cannot)))))),
    h("div", { class: "card-note" }, "Enforced by IAM, not by prompts. Dashed boxes above are designed but not built."))));
}

// ---- drawer -----------------------------------------------------------------------------------------------------------

function openDrawer(i, opener) {
  state.opener = opener || null;
  const drawer = clear($("drawer"));
  const close = h("button", { class: "ghost", type: "button", "aria-label": "Close", onclick: closeDrawer }, "Close");
  const conf = i.confidence === null || i.confidence === undefined ? null : Number(i.confidence);
  const meterFill = h("i", {});
  if (conf !== null) meterFill.style.width = Math.max(0, Math.min(1, conf)) * 100 + "%";
  const proposals = Array.isArray(i.remediation_proposals) ? i.remediation_proposals : [];
  const reasons = Array.isArray(i.triage_reasons) ? i.triage_reasons : [];
  const preview = i.postmortem_text ? i.postmortem_text.slice(0, 700) + (i.postmortem_text.length > 700 ? " …" : "") : null;
  const fullLink = `postmortem?campaign=${encodeURIComponent(i.campaign_id)}&position=${encodeURIComponent(i.position)}`;

  drawer.append(
    h("div", { class: "drawer-head" },
      h("div", {},
        h("div", { class: "muted" }, `${i.campaign_id} · incident #${i.position}`),
        h("h2", { id: "drawer-title", class: "mono" }, i.failed_execution || "not run")),
      close),
    h("dl", { class: "kv" },
      h("dt", {}, "Failure"), h("dd", {}, fmtTime(i.failure_time, true)),
      h("dt", {}, "Run status"), h("dd", {}, i.failed_run_status || "n/a"),
      h("dt", {}, "Severity"), h("dd", {}, (i.severity || "n/a") + (reasons.length ? ` (${reasons.join("; ")})` : "")),
      h("dt", {}, "Incident ID"), h("dd", { class: "mono" }, i.incident_id || "n/a"),
      h("dt", {}, "To diagnosis"), h("dd", {}, fmtDuration(i.time_to_diagnosis_sec))),

    h("h3", {}, "Verdict and confidence"),
    h("div", { class: "box" },
      h("div", { class: "verdict" }, i.verdict_category ? ident(i.verdict_category) : "No verdict"),
      h("div", { class: "secondary" }, conf === null ? "confidence n/a" : `confidence ${conf.toFixed(2)}`, i.root_cause_model ? ` · ${i.root_cause_model}` : ""),
      conf === null ? null : h("div", { class: "meter", role: "img", "aria-label": `confidence ${conf.toFixed(2)} of 1` }, meterFill)),

    h("h3", {}, "Ground truth (evaluator only)"),
    h("div", { class: "truth" },
      h("dl", { class: "kv" },
        h("dt", {}, "True category"), h("dd", {}, ident(i.true_category)),
        h("dt", {}, "Injected fault"), h("dd", {}, ident(i.true_cause)),
        h("dt", {}, "Parameters"), h("dd", { class: "mono" }, i.injected_params ? JSON.stringify(i.injected_params) : "n/a"),
        h("dt", {}, "Outcome"), h("dd", {}, statusMark(i.outcome))),
      h("div", { class: "card-note" }, "Never visible to the agents. Shown here from the evaluator's results.")),

    h("h3", {}, "Evidence the agent saw"),
    h("div", { class: "box" }, i.log_excerpt_seen_by_agent
      ? h("pre", { class: "evidence", tabindex: "0" }, i.log_excerpt_seen_by_agent)
      : h("span", { class: "muted" }, "No warning or error lines were stored for this incident.")),
    h("div", { class: "card-note" }, "Warning and error lines from the failed run, as stored before the model was called."),

    h("h3", {}, "Hypothesis"),
    h("div", { class: "box" }, i.verdict_hypothesis || h("span", { class: "muted" }, "n/a")),

    h("h3", {}, i.remediation_executed === true ? "Remediation" : "Remediation (not executed)"),
    proposals.length
      ? h("div", {}, proposals.map((p) => h("div", { class: "box proposal" },
          h("div", { class: "proposal-action" }, p.action || "n/a"),
          h("dl", { class: "kv" },
            h("dt", {}, "Risk"), h("dd", {}, p.risk || "n/a"),
            h("dt", {}, "Blast radius"), h("dd", {}, p.blast_radius || "n/a"),
            h("dt", {}, "Rollback"), h("dd", {}, p.rollback || "n/a"),
            h("dt", {}, "Confidence"), h("dd", {}, p.confidence === undefined || p.confidence === null ? "n/a" : Number(p.confidence).toFixed(2))))))
      : h("div", { class: "box muted" }, "No proposal recorded."),
    h("div", { class: "card-note" }, "Proposals only. The agents have no permission to change the target system."),

    h("h3", {}, "Postmortem"),
    preview
      ? h("div", { class: "box" },
          h("div", { class: "secondary" }, `written by ${i.postmortem_model || "n/a"}`,
            i.postmortem_fallback_reason ? ` · fallback after: ${String(i.postmortem_fallback_reason).slice(0, 60)}` : ""),
          h("div", { class: "preview" }, preview),
          h("div", {}, h("a", { href: fullLink, target: "_blank", rel: "noopener" }, "Read full")))
      : h("div", { class: "box muted" }, "No postmortem was written for this incident."));

  $("scrim").hidden = false;
  drawer.hidden = false;
  close.focus();
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("scrim").hidden = true;
  if (state.opener && document.contains(state.opener)) state.opener.focus();
  state.opener = null;
}

// ---- chrome -----------------------------------------------------------------------------------------------------------

function showTab(name) {
  if (!document.getElementById("tab-" + name)) name = "overview";
  state.tab = name;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll("section.tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
}

function initTheme() {
  const button = $("theme-toggle");
  const current = () => document.documentElement.getAttribute("data-theme") || "dark";
  const label = () => { button.textContent = current() === "dark" ? "Light" : "Dark"; button.setAttribute("aria-label", `Switch to ${button.textContent.toLowerCase()} theme`); };
  button.addEventListener("click", () => {
    const next = current() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { window.localStorage.setItem("trace-theme", next); } catch (e) { /* storage unavailable: the choice lasts for this page only */ }
    label();
  });
  label();
}

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));
  showTab(location.hash.slice(1) || "overview");
  $("campaign").addEventListener("change", (e) => { state.campaign = e.target.value || null; closeDrawer(); refresh(); });
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
  setInterval(renderLive, 1000);
  // Refresh once a minute, but only while the page is visible; catch up when it becomes visible again.
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && (state.lastOkAt === null || Date.now() - state.lastOkAt >= REFRESH_MS)) refresh();
  });
  refresh();
});
