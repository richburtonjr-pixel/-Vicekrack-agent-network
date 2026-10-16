/* ViceKrack Mobile Command Center app (shell, pairing, Home, System; Research and Studio live in screens.js).
 * Every screen shows only what the PC reports. All text is inserted as plain text nodes, never parsed as HTML. */
"use strict";

const ICONS = {
  bell: ["M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z", "M10 21h4"],
  sliders: ["M4 7h9M17 7h3M4 17h3M11 17h9", "M15 5a2 2 0 1 1 0 4 2 2 0 0 1 0-4z", "M9 15a2 2 0 1 1 0 4 2 2 0 0 1 0-4z"],
  back: ["M15 5l-7 7 7 7"],
};
const STATE = { csrf: null, status: null, timer: null, route: null };
const screen = () => document.getElementById("screen");

/* ---------- tiny DOM helpers ---------- */
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) { continue; }
    if (key === "class") { node.className = value; }
    else if (key.startsWith("on")) { node.addEventListener(key.slice(2), value); }
    else { node.setAttribute(key, value === true ? "" : value); }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) { continue; }
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name]) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
function bar(percent, extra) {
  const fill = h("i");
  fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
  return h("div", { class: "bar" + (extra ? " " + extra : ""), role: "presentation" }, fill);
}
const money = (v) => "$" + Number(v || 0).toFixed(2);
function ago(iso) {
  if (!iso) { return ""; }
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (seconds < 60) { return "just now"; }
  if (seconds < 3600) { return Math.round(seconds / 60) + "m ago"; }
  if (seconds < 86400) { return Math.round(seconds / 3600) + "h ago"; }
  return Math.round(seconds / 86400) + "d ago";
}
function show(...nodes) {
  const main = screen();
  main.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
}

/* ---------- talking to the PC ---------- */
async function call(path, options = {}) {
  try {
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
    let body = {};
    try { body = await response.json(); } catch (_) { body = {}; }
    return { ok: response.ok, status: response.status, body };
  } catch (_) {
    return { ok: false, status: 0, body: {} };
  }
}
async function csrf() {
  if (!STATE.csrf) {
    const session = await call("/m/api/session");
    STATE.csrf = session.ok ? session.body.csrf : null;
  }
  return STATE.csrf;
}
async function post(path, body) {
  const token = await csrf();
  return call(path, { method: "POST", headers: { "Content-Type": "application/json", "X-VK-Mobile-CSRF": token || "" },
                      body: JSON.stringify(body || {}) });
}

/* ---------- shared pieces ---------- */
function header(subtitle, withButtons = true) {
  return h("header", { class: "top" },
    h("div", { class: "logo", "aria-hidden": "true" }, "VK"),
    h("div", { class: "titles" }, h("h1", {}, "Command Center"), h("p", { class: "muted" }, subtitle)),
    withButtons && h("a", { class: "icon-btn", href: "#alerts", "aria-label": "Alerts" }, icon("bell")),
    withButtons && h("a", { class: "icon-btn", href: "#system", "aria-label": "System" }, icon("sliders")));
}
function backTo(label = "Home", target = "#home") {
  return h("a", { class: "back", href: target }, icon("back"), label);
}
function pcBanner(status) {
  if (!status) { return h("div", { class: "banner bad" }, "PC offline", h("span", { class: "sub" }, "Is it on, with Tailscale?")); }
  if (!status.hq.reachable) { return h("div", { class: "banner warn" }, "PC online · HQ not running"); }
  const mode = { real: "Real mode", demo: "Demo mode", read_only: "View only" }[status.hq.mode] || "";
  return h("div", { class: "banner ok" }, "PC online · " + mode, h("span", { class: "sub" }, "Checked " + ago(status.pc.checked_at)));
}
function offline() {
  show(header("Can't reach your PC", false), pcBanner(null),
    h("div", { class: "card" },
      h("p", { class: "muted" }, "Make sure the PC is on, ViceKrack is running, and Tailscale is connected on this phone."),
      h("button", { class: "primary", type: "button", onclick: () => route(true) }, "Try again")));
}
function tabs(on) {
  const nav = document.getElementById("tabs");
  nav.hidden = !on;
  const current = (location.hash || "#home").slice(1).split("/")[0];
  for (const link of nav.querySelectorAll("a")) { link.classList.toggle("on", link.dataset.tab === current); }
}

/* ---------- pairing ---------- */
function pairScreen() {
  tabs(false);
  const code = h("input", { id: "code", class: "code", inputmode: "numeric", autocomplete: "one-time-code",
                            maxlength: "6", placeholder: "000000" });
  const name = h("input", { id: "name", maxlength: "40", value: "iPhone" });
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const button = h("button", { class: "primary", type: "button" }, "Pair");
  code.addEventListener("input", () => { error.hidden = true; });
  button.addEventListener("click", async () => {
    const value = code.value.trim();
    if (!/^[0-9]{6}$/.test(value)) { error.textContent = "Enter the 6-digit code from your PC."; error.hidden = false; return; }
    button.disabled = true;
    const result = await call("/m/api/pair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: value, name: name.value.trim() || "iPhone" }) });
    button.disabled = false;
    if (!result.ok) {
      error.textContent = (result.body.error && result.body.error.message) || "Pairing failed. Try again.";
      error.hidden = false;
      return;
    }
    STATE.csrf = result.body.csrf;
    location.hash = "#home";
    route(true);
  });
  show(header("Not paired yet", false),
    h("section", { class: "card" },
      h("h2", {}, "Pair this iPhone"),
      h("p", { class: "muted" }, "On your PC, double-click “2 Pair my iPhone” and type the 6-digit code it shows."),
      h("label", { for: "code" }, "Pairing code"), code,
      h("label", { for: "name" }, "Name for this phone"), name, error, button));
}

/* ---------- Home ---------- */
const KIND = { research_approval: "Research", clips_approval: "Clips", video_review: "Review" };
function needsYou(items) {
  if (!items.length) { return null; }
  const target = (item) => item.kind === "research_approval" ? "#research/" + item.job_id : "#studio/w/" + item.workflow_id;
  const rows = items.slice(0, 4).map((item) => h("a", { class: "item", href: target(item) },
    h("span", { class: "what" }, (KIND[item.kind] || "") + " · " + item.title +
      (item.kind === "clips_approval" && item.clips ? " (" + item.clips + " clips)" : "")),
    h("span", { class: "cost" }, item.kind === "video_review" ? "Watch" :
      (item.estimate_usd !== null && item.estimate_usd !== undefined ? "up to " + money(item.estimate_usd) : "Cost on PC"))));
  return h("section", { class: "money" },
    h("div", { class: "head" }, h("h2", {}, "Waiting on you"),
      h("span", { class: "pill orange" }, items.length + (items.length === 1 ? " item" : " items"))),
    h("div", {}, rows),
    h("a", { class: "orange-btn", href: "#studio", role: "button" }, "Review"));
}
function stat(value, label, tone) {
  return h("div", { class: "stat" + (tone ? " " + tone : "") }, h("b", {}, value), h("span", {}, label));
}
function liveCard(item) {
  const p = item.progress;
  const step = p && p.total ? (p.stage ? "Step " + Math.min(p.done + 1, p.total) + " of " + p.total : "Finishing") : "Working";
  return h("section", { class: "card live" },
    h("div", { class: "row1" }, h("span", {}, (item.kind === "research" ? "Research · " : "Video · ") + item.title),
      h("span", { class: "step" }, step)),
    p && p.total ? bar((p.done / p.total) * 100) : null,
    p && p.stage ? h("p", { class: "muted" }, "Now: " + p.stage.replaceAll("_", " ")) : null);
}
async function homeScreen() {
  tabs(true);
  const status = await call("/m/api/status");
  if (!status.ok) { offline(); return; }
  if (!status.body.paired) { pairScreen(); return; }
  const home = await call("/m/api/home");
  STATE.status = status.body;
  const s = status.body;
  const nodes = [header(s.device.name + " · paired"), pcBanner(s)];
  if (!home.ok || !home.body.hq_ok) {
    nodes.push(h("div", { class: "card" }, h("p", { class: "muted" },
      "The HQ isn't answering, so there's nothing to show yet. It restarts by itself if it stopped; check back in a minute.")));
  } else {
    const d = home.body;
    const waiting = needsYou(d.needs_you);
    if (waiting) { nodes.push(waiting); }
    nodes.push(h("div", { class: "grid" },
      stat(String(d.counts.running), "Running now"),
      stat(String(d.counts.finished), "Videos finished"),
      stat(money(d.spending.today_usd), "Phone spend of " + money(d.spending.cap_usd) + " today"),
      stat(String(d.counts.problems), "Problems", d.counts.problems ? "bad" : "good")));
    for (const item of d.live.slice(0, 3)) { nodes.push(liveCard(item)); }
    if (d.recent.length) {
      nodes.push(h("section", { class: "card" }, h("h2", {}, "Recent activity"),
        h("div", { class: "list" }, d.recent.map((a) => h("div", { class: "entry" }, a.text, h("small", {}, ago(a.at)))))));
    } else if (!d.live.length && !d.needs_you.length) {
      nodes.push(h("p", { class: "empty" }, "Nothing running right now. Start with Research."));
    }
  }
  show(...nodes);
}

/* ---------- System ---------- */
function rows(pairs) {
  return h("section", { class: "card rows" }, pairs.map(([label, value, tone]) =>
    h("div", { class: "row" }, h("span", {}, label), h("span", { class: "val" + (tone ? " " + tone : "") }, value))));
}
async function systemScreen() {
  tabs(true);
  const [status, system] = await Promise.all([call("/m/api/status"), call("/m/api/system")]);
  if (!status.ok) { offline(); return; }
  if (!status.body.paired) { pairScreen(); return; }
  const s = status.body, y = system.ok ? system.body : null;
  const modes = { real: "Running · Real mode", demo: "Running · Demo mode", read_only: "Running · view only", offline: "Not running" };
  const nodes = [backTo(), h("h1", { class: "page" }, "System"),
    rows([["Your PC", "Online", "good"],
          ["ViceKrack HQ", modes[s.hq.mode] || "Unknown", s.hq.reachable ? null : "bad"],
          ["xAI key", s.hq.xai_key ? "Saved on PC" : (s.hq.reachable ? "Missing" : "—"), s.hq.reachable && !s.hq.xai_key ? "bad" : null],
          ["Connection", s.connection.via === "tailscale" ? "Tailscale · private" : "This PC"]])];
  const cap = s.spending.cap_usd, spent = s.spending.today_usd;
  nodes.push(h("section", { class: "card" }, h("h2", {}, "Spending safety"),
    h("div", { class: "row" }, h("span", {}, "Phone approvals today"), h("span", { class: "val" }, money(spent) + " of " + money(cap))),
    bar(cap > 0 ? (spent / cap) * 100 : 0, "orange"),
    h("div", { class: "row" }, h("span", {}, "PIN for paid actions"),
      h("span", { class: "val" + (y && y.pin.locked ? " bad" : "") }, !s.pin_set ? "Not set" : (y && y.pin.locked ? "Locked for now" : "On"))),
    h("p", { class: "muted" }, "Amounts are ViceKrack's estimates. Your xAI usage page shows the real charges.")));
  if (y) {
    const me = s.device.device_id;
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Paired phones"),
      h("div", { class: "list" }, y.phones.map((p) => h("div", { class: "entry" },
        p.name + (p.device_id === me ? " (this phone)" : ""),
        h("small", {}, "Last seen " + ago(p.last_seen_at) + " · expires " + new Date(p.expires_at).toLocaleDateString())))),
      h("button", { class: "danger", type: "button", onclick: unpair }, "Unpair this phone")));
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Recent phone actions"),
      y.recent_actions.length ? h("div", { class: "list" }, y.recent_actions.map((a) => h("div", { class: "entry" },
        actionName(a.action) + (a.estimate_usd ? " · " + money(a.estimate_usd) : ""),
        h("small", {}, (a.outcome === "ok" ? "Done" : a.outcome.replaceAll("_", " ")) + " · " + ago(a.at)))))
        : h("p", { class: "empty" }, "No actions from a phone yet.")));
  }
  nodes.push(h("button", { class: "primary", type: "button", onclick: () => route(true) }, "Check status now"));
  show(...nodes);
}
const ACTION_NAMES = { pair: "Phone paired", unpair: "Phone unpaired", "research/prepare": "Research prepared",
  "research/submit": "Research approved", "research/make-video": "Make video approved", "workflow/submit": "Clip approved",
  "workflow/submit-all": "All clips approved", "workflow/export": "Video exported", "review/record": "Review recorded" };
function actionName(action) { return ACTION_NAMES[action] || action.replaceAll("/", " · ").replaceAll("-", " "); }
async function unpair() {
  if (!window.confirm("Unpair this phone? You'll need a new code from the PC to use it again.")) { return; }
  await post("/m/api/unpair", {});
  STATE.csrf = null;
  location.hash = "#home";
  route(true);
}

/* ---------- screens arriving in later updates ---------- */
const SOON = {
  agents: ["Agents", ["All eight agents with real status only", "What each one does and is working on now"]],
  alerts: ["Alerts", ["Only failures and finished projects", "Lock-screen alerts on your iPhone"]],
};
function soonScreen(key) {
  tabs(true);
  const [title, items] = SOON[key];
  show(key === "alerts" ? backTo() : null, h("h1", { class: "page" }, title),
    h("section", { class: "card soon" }, h("h2", {}, "Coming in the next update"),
      h("ul", {}, items.map((t) => h("li", {}, t)))));
}

/* ---------- actions, the PIN sheet and busy screen (used by screens.js) ---------- */
const SCREENS = {};
function newRequestId() {
  if (window.crypto && crypto.randomUUID) { return "m-" + crypto.randomUUID(); }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return "m-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
/* One action. Reuse the same requestId when retrying the same tap: the PC answers it once, never twice. */
async function act(action, body, requestId) {
  return post("/m/api/action/" + action, { ...body, request_id: requestId || newRequestId() });
}
function errorText(result) {
  if (result.status === 0) { return "No answer from your PC. Check the result before trying again."; }
  return (result.body.error && result.body.error.message) || "That didn't work. Nothing else was changed.";
}
function note(text, tone) {
  return h("p", { class: tone === "bad" ? "error" : "notice", role: tone === "bad" ? "alert" : null }, text);
}
let busyLayer = null;
function busy(message) {
  if (busyLayer) { busyLayer.remove(); busyLayer = null; }
  if (!message) { return; }
  busyLayer = h("div", { class: "layer", role: "status" }, h("div", { class: "busy" },
    h("div", { class: "spinner", "aria-hidden": "true" }), h("p", {}, message),
    h("p", { class: "muted" }, "Keep the app open. If you leave, it keeps going on your PC; check back here.")));
  document.body.append(busyLayer);
}
/* The approval sheet for anything paid: what it is, the most it can cost, today's phone spend, your PIN. */
function paidSheet({ title, lines, estimate, run }) {
  const spending = (STATE.status && STATE.status.spending) || { today_usd: 0, cap_usd: 5 };
  const after = spending.today_usd + (estimate || 0);
  const requestId = newRequestId();
  const pin = h("input", { type: "password", inputmode: "numeric", autocomplete: "off", maxlength: "8",
                           class: "code", id: "pin", "aria-label": "Your PIN", placeholder: "PIN" });
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const approve = h("button", { class: "orange-btn", type: "button" },
    "Approve " + (estimate !== null && estimate !== undefined ? "up to " + money(estimate) + " " : "") + "with PIN");
  const cancel = h("button", { class: "ghost", type: "button" }, "Not now");
  const sheet = h("div", { class: "layer", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div", { class: "sheet" }, h("h2", {}, title),
      h("div", { class: "lines" }, lines.map((line) => h("p", {}, line))),
      h("div", { class: "row" }, h("span", {}, "Phone spend today after this"),
        h("span", { class: "val" }, money(after) + " of " + money(spending.cap_usd))),
      bar(spending.cap_usd > 0 ? (after / spending.cap_usd) * 100 : 0, "orange"),
      h("label", { for: "pin" }, "Your PIN"), pin, error, approve, cancel));
  const close = () => sheet.remove();
  cancel.addEventListener("click", close);
  pin.addEventListener("input", () => { error.hidden = true; });
  approve.addEventListener("click", async () => {
    if (!/^[0-9]{4,8}$/.test(pin.value)) { error.textContent = "Enter your 4-8 digit PIN."; error.hidden = false; return; }
    approve.disabled = true;
    const ok = await run(pin.value, requestId, (message) => { error.textContent = message; error.hidden = false; });
    approve.disabled = false;
    if (ok) { close(); }
  });
  document.body.append(sheet);
  pin.focus();
}
async function refreshStatus() {
  const status = await call("/m/api/status");
  if (status.ok && status.body.paired) { STATE.status = status.body; }
  return status;
}
const words = (text) => String(text || "").replaceAll("_", " ");

/* ---------- router ---------- */
async function route(force) {
  const name = (location.hash || "#home").slice(1);
  if (!force && name === STATE.route) { return; }
  STATE.route = name;
  clearInterval(STATE.timer);
  const [tab, ...rest] = name.split("/");
  if (tab === "system") { await systemScreen(); }
  else if (SCREENS[tab]) { tabs(true); await SCREENS[tab](rest); }
  else if (SOON[tab]) { soonScreen(tab); }
  else { await homeScreen(); STATE.timer = setInterval(() => { if (!document.hidden) { homeScreen(); } }, 20000); }
  screen().focus({ preventScroll: true });
}

window.addEventListener("hashchange", () => route(false));
document.addEventListener("visibilitychange", () => { if (!document.hidden && STATE.route === "home") { homeScreen(); } });
document.addEventListener("DOMContentLoaded", () => {
  route(true);
  if ("serviceWorker" in navigator) { navigator.serviceWorker.register("/sw.js").catch(() => {}); }
});
