/* ViceKrack Trading 2.0 (stage 1: Home dashboard + Market). PRACTICE MONEY ONLY.
 *
 * A standalone trading app page served at /trading by the same Mobile Command Center server, so it uses the same
 * pairing, the same doorman and the same read/order requests as the Trade tab (/m/api/trade/*). Nothing new runs on
 * the PC: every price, balance, agent decision and trade comes from those requests. Orders are priced on the PC from
 * a fresh Kraken request and still pass the $80 guard; the page never sends a price.
 * Text is always inserted as text, never parsed as HTML. */
"use strict";

const VT = { csrf: null, timer: null, route: null, markets: null, account: null, filter: "all", query: "", shown: 60, history: "all",
             range: "1D", homeRange: "1D", flash: null };
const MOVER_MIN_VOLUME = 1000000;        // movers list skips coins with under $1M traded in 24h (thin, jumpy prices)
const NAMES = { market_scout: "Market Scout", trend_agent: "Trend Agent", strategy_agent: "Strategy Agent", verifier: "Verifier",
  risk_review: "Risk Review", executor: "Executor", position_monitor: "Position Monitor", performance_reviewer: "Performance Reviewer" };
const OUTCOME = { executed: ["Trade placed", "green"], vetoed: ["Stopped by an agent", ""], refused: ["$80 guard refused", "red"],
  waiting: ["Waiting for price", "amber"], approved_not_executed: ["Approved, auto-trades off", "blue"] };

/* ---------- DOM ---------- */
const $screen = () => document.getElementById("vt-screen");
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
const SVG = "http://www.w3.org/2000/svg";
function s(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) { node.setAttribute(k, v); }
  return node;
}
function show(...nodes) { $screen().replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false)); }
function tabs() {
  const current = (location.hash || "#home").slice(1).split("/")[0];
  for (const link of document.querySelectorAll("#vt-tabs a")) {
    const on = link.dataset.tab === current;
    link.classList.toggle("on", on);
    if (on) { link.setAttribute("aria-current", "page"); } else { link.removeAttribute("aria-current"); }
  }
}
function back(label, target) {
  const svg = s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
  svg.append(s("path", { d: "M15 5l-7 7 7 7" }));
  return h("a", { class: "vt-back", href: target }, svg, label);
}

/* ---------- formatting ---------- */
function usd(value) {
  const n = Number(value);
  if (value === null || value === undefined || value === "" || !isFinite(n)) { return "—"; }
  if (Math.abs(n) >= 1) { return (n < 0 ? "−$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  if (n === 0) { return "$0.00"; }
  const places = Math.min(12, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(n)))));
  return (n < 0 ? "−$" : "$") + Math.abs(n).toFixed(places).replace(/(\.\d*?[1-9])0+$/, "$1");
}
function cents(value) {
  const n = Number(value);
  return isFinite(n) ? (n < 0 ? "−$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}
function signed(value) { const n = Number(value); return isFinite(n) ? (n >= 0 ? "+" : "−") + cents(Math.abs(n)).replace("−", "") : "—"; }
function compact(value) {
  const n = Number(value);
  if (!isFinite(n)) { return "—"; }
  if (n >= 1e9) { return "$" + (n / 1e9).toFixed(2) + "B"; }
  if (n >= 1e6) { return "$" + (n / 1e6).toFixed(2) + "M"; }
  if (n >= 1e3) { return "$" + (n / 1e3).toFixed(1) + "K"; }
  return "$" + n.toFixed(0);
}
function qty(value) {
  const n = Number(value);
  if (!isFinite(n)) { return "—"; }
  return n >= 1000 ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(Number(n.toPrecision(8)));
}
function pct(value) {
  if (value === null || value === undefined || !isFinite(Number(value))) { return h("span", { class: "vt-chg flat" }, "—"); }
  const n = Number(value);
  return h("span", { class: "vt-chg " + (n > 0 ? "up" : n < 0 ? "down" : "flat") }, (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toFixed(2) + "%");
}
function ago(iso) {
  const t = Date.parse(iso);
  if (!iso || !isFinite(t)) { return ""; }
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) { return "just now"; }
  if (sec < 3600) { return Math.round(sec / 60) + " min ago"; }
  if (sec < 86400) { return Math.round(sec / 3600) + " hr ago"; }
  return Math.round(sec / 86400) + " days ago";
}
const baseOf = (symbol) => String(symbol || "").replace(/USD$/, "");
const words = (text) => String(text || "").replaceAll("_", " ");
function badge(base, small) { return h("span", { class: "vt-badge" + (small ? " sm" : ""), "aria-hidden": "true" }, String(base).slice(0, 4)); }
function note(text, tone) { return h("p", { class: tone === "bad" ? "vt-error" : "vt-note", role: tone === "bad" ? "alert" : null }, text); }

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
async function post(path, body) {
  if (!VT.csrf) {
    const session = await call("/m/api/session");
    VT.csrf = session.ok ? session.body.csrf : null;
  }
  return call(path, { method: "POST", headers: { "Content-Type": "application/json", "X-VK-Mobile-CSRF": VT.csrf || "" },
                      body: JSON.stringify(body || {}) });
}
function problem(result) {
  if (result.status === 0) { return "No answer from your PC. Is it on, with ViceKrack running?"; }
  return (result.body.error && result.body.error.message) || "That didn't work. Nothing was traded.";
}
function notPaired() {
  show(h("h1", { class: "vt-title" }, "Pair first"),
    h("section", { class: "vt-card" },
      h("p", {}, "This phone isn't paired with your PC yet. Pair it once in the Command Center, then come back here."),
      h("a", { class: "vt-primary", href: "/#home", role: "button" }, "Open Command Center to pair")));
}
function offline(text) {
  show(h("section", { class: "vt-banner bad" }, h("span", { class: "dot" }),
    h("div", {}, h("strong", {}, "Can't reach your PC"), h("span", { class: "muted" }, text))),
    h("button", { class: "vt-primary", type: "button", onclick: () => route(true) }, "Try again"));
}

/* ---------- agents' watch: is it fresh? ---------- */
function watchState(account) {
  const w = (account && account.watch) || {};
  if (!w.last_cycle_at) { return { tone: "bad", running: false, title: "AI agents are not running",
    text: "Start ViceKrack on your PC. Prices below are still live from Kraken." }; }
  const age = (Date.now() - Date.parse(w.last_cycle_at)) / 1000;
  const limit = Math.max(600, 3 * (Number(w.cycle_seconds) || 60));
  if (age > limit) { return { tone: "bad", running: false, title: "AI agents stopped · last check " + ago(w.last_cycle_at),
    text: "Their research is old. Prices below are still live from Kraken." }; }
  return { tone: "good", running: true, title: "AI agents watching the market",
    text: "Last check " + ago(w.last_cycle_at) + " · every " + Math.round((Number(w.cycle_seconds) || 60) / 60) + " min" };
}
function watchBanner(account) {
  const st = watchState(account);
  return h("section", { class: "vt-banner " + st.tone, role: "status" }, h("span", { class: "dot" }),
    h("div", {}, h("strong", {}, st.title), h("span", { class: "muted" }, st.text)));
}

/* ---------- interactive chart ---------- */
function chartCard({ title, base, last, change, points, range, onRange, subtitle }) {
  const readout = h("p", { class: "vt-readout" }, usd(last));
  const sub = h("p", { class: "vt-readout-sub muted" }, subtitle || "");
  const restSub = () => { sub.replaceChildren(change === undefined ? (subtitle || "") : h("span", {}, pct(change), " today")); };
  restSub();
  const ranges = h("div", { class: "vt-ranges", role: "group", "aria-label": "Chart range" },
    ["1H", "1D", "1W", "1M", "1Y"].map((key) => h("button", { type: "button", class: "vt-chip" + (range === key ? " on" : ""),
      "aria-pressed": String(range === key), onclick: () => onRange(key) }, key)));
  return h("section", { class: "vt-chart-card" },
    h("div", { class: "vt-chart-top" }, badge(base), h("div", { class: "who" }, h("h2", {}, title), h("p", { class: "muted" }, "Kraken live price"))),
    readout, sub, drawChart(points, readout, sub, last, restSub), ranges);
}
function drawChart(points, readout, sub, last, restSub) {
  if (!points || points.length < 2) { return h("div", { class: "vt-chart-empty" }, "No chart data for this range right now."); }
  const W = 600, H = 200, top = 10, bottom = 190;
  const closes = points.map((p) => Number(p[1]));
  const low = Math.min(...closes), high = Math.max(...closes), span = high - low || Math.abs(high) * 0.001 || 1;
  const x = (i) => (i / (closes.length - 1)) * W;
  const y = (v) => bottom - ((v - low) / span) * (bottom - top);
  const dir = closes[closes.length - 1] >= closes[0] ? "up" : "down";
  const path = closes.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
  const svg = s("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", class: "vt-chart", role: "img",
    "aria-label": "Price chart. Touch or hover to see each price." });
  svg.append(s("line", { x1: 0, x2: W, y1: y(closes[0]), y2: y(closes[0]), class: "base" }),
    s("path", { d: path + " L" + W + " " + H + " L0 " + H + " Z", class: "area " + dir }),
    s("path", { d: path, class: "line " + dir, "vector-effect": "non-scaling-stroke" }));
  const cross = s("line", { x1: -10, x2: -10, y1: 0, y2: H, class: "cross", "vector-effect": "non-scaling-stroke" });
  const dot = s("circle", { r: 5, cx: -20, cy: -20, class: "dot " + dir, "vector-effect": "non-scaling-stroke" });
  svg.append(cross, dot);
  const scrub = (event) => {
    const box = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(closes.length - 1, Math.round(((event.clientX - box.left) / box.width) * (closes.length - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(closes[i]));
    readout.textContent = usd(closes[i]);
    const move = closes[0] ? ((closes[i] - closes[0]) / closes[0]) * 100 : null;
    sub.replaceChildren(pct(move), " · " + new Date(points[i][0] * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
  };
  const reset = () => { cross.setAttribute("x1", -10); cross.setAttribute("x2", -10); dot.setAttribute("cx", -20); readout.textContent = usd(last); restSub(); };
  svg.addEventListener("pointermove", scrub);
  svg.addEventListener("pointerdown", scrub);
  svg.addEventListener("pointerleave", reset);
  svg.addEventListener("pointercancel", reset);
  return svg;
}

/* ---------- ranked opportunities (autonomous step 2: the smarter scanner) ---------- */
const SIGNAL = { streak: "Steady climb", volume_spike: "Volume spike", accelerating: "Speeding up" };
function opportunitiesCard(radar, limit) {
  radar = radar || {};
  const list = (radar.candidates || []).slice(0, limit || 8);
  const st = radar.stats || {};
  return h("section", { class: "vt-card flush" },
    h("div", { class: "pad vt-head" }, h("h2", {}, "Opportunities right now"), h("span", { class: "muted small" }, "ranked by the scanner")),
    list.length ? h("div", {}, list.map((c, i) => h("a", { class: "vt-coin", href: "#market/coin/" + c.symbol },
      h("span", { class: "vt-rank", "aria-label": "Rank " + (i + 1) }, String(i + 1)), badge(c.base),
      h("span", { class: "who" }, h("strong", {}, c.base),
        h("span", { class: "vt-sigs" }, (c.signals || ["streak"]).map((s) => h("span", { class: "vt-tag " + (s === "volume_spike" ? "amber" : s === "accelerating" ? "green" : "blue") },
          SIGNAL[s] || words(s) + (s === "volume_spike" && c.volume_spike_x ? " " + c.volume_spike_x + "×" : ""))))),
      h("span", { class: "num" }, h("strong", { class: "up" }, "+" + c.rise_15m_pct + "%"), h("span", { class: "muted small" }, "15 min · score " + (c.score || "—"))))))
      : h("p", { class: "muted pad" }, radar.ok === false ? "The last market scan failed; it retries every minute."
        : "Nothing qualifies right now. Quiet is normal: most minutes no coin is climbing cleanly."),
    radar.ok && st.coins_priced ? h("p", { class: "muted small pad" }, st.coins_priced + " coins scanned · " + st.liquid + " trade enough"
      + (st.volume_spikes ? " · " + st.volume_spikes + " volume spikes" : "")
      + (st.skipped_already_pumped ? " · " + st.skipped_already_pumped + " skipped (already up big today)" : "")
      + (st.skipped_wide_spread ? " · " + st.skipped_wide_spread + " skipped (wide spread)" : "")) : null);
}

/* ---------- Home dashboard ---------- */
function todayClosed(trades) {
  const today = new Date().toDateString();
  let total = 0, count = 0;
  for (const t of trades || []) {
    if (t.kind !== "fill" || t.realized_pnl === null || t.realized_pnl === undefined) { continue; }
    if (new Date(t.at).toDateString() !== today) { continue; }
    total += Number(t.realized_pnl) || 0;
    count += 1;
  }
  return { total, count };
}
function balanceHero(body) {
  const a = body.account;
  const equity = Number(a.equity), start = Number(a.starting_cash), floor = Number(a.shutdown_floor);
  const pnl = isFinite(equity) ? equity - start : null;
  const halted = a.status === "halted";
  const room = isFinite(equity) ? Math.max(0, Math.min(100, ((equity - floor) / (start - floor)) * 100)) : 0;
  const fill = h("i");
  fill.style.width = (halted ? 0 : room) + "%";
  const today = todayClosed(body.trades);
  return [
    h("section", { class: "vt-hero" },
      h("p", { class: "vt-label" }, "Practice balance"),
      h("p", { class: "vt-big" }, isFinite(equity) ? usd(equity) : "Checking prices…"),
      pnl === null ? null : h("p", { class: "vt-pnl " + (pnl >= 0 ? "up" : "down") },
        signed(pnl) + " (" + (pnl >= 0 ? "+" : "−") + Math.abs((pnl / start) * 100).toFixed(2) + "%) all time")),
    h("div", { class: "vt-stats" },
      h("div", { class: "vt-stat" }, h("b", { class: today.count ? (today.total >= 0 ? "up" : "down") : "" }, today.count ? signed(today.total) : "$0.00"),
        h("span", {}, "Today · " + today.count + " closed trade" + (today.count === 1 ? "" : "s"))),
      h("div", { class: "vt-stat" }, h("b", {}, usd(a.cash)), h("span", {}, "Cash available")),
      h("div", { class: "vt-stat" }, h("b", { class: halted ? "down" : "" }, halted ? "Stopped" : a.dollars_above_floor === null ? "—" : usd(a.dollars_above_floor)),
        h("span", {}, halted ? "$80 shutdown tripped" : "Above the " + cents(floor) + " shutdown"))),
    h("div", { class: "vt-gauge" }, h("div", { class: "vt-fuse" + (halted ? " tripped" : room < 40 ? " low" : ""), role: "img",
      "aria-label": halted ? "Shutdown tripped" : Math.round(room) + "% of the way from the shutdown to the starting balance" }, fill),
    h("div", { class: "vt-fuse-labels muted small" }, h("span", {}, cents(floor) + " shutdown"), h("span", {}, "Safety gauge"), h("span", {}, cents(start) + " start"))),
    halted ? note("The $80 shutdown sold everything and stopped trading " + ago(a.halt && a.halt.at) + ". Start a fresh account from the Command Center's Trade tab.") : null,
    a.guard === "unverified" && !halted ? note("Some prices are over 10 minutes old, so new buys are paused until the agents' watch updates them.") : null,
  ];
}
function startCard(body) {
  const changed = body.problem && body.problem.code === "practice_config_changed";
  const button = h("button", { class: "vt-primary", type: "button" }, "Start my $100 practice account");
  button.addEventListener("click", async () => {
    button.disabled = true;
    const r = await post("/m/api/trade/restart", { confirm: "restart-practice" });
    if (r.ok) { route(true); } else { button.disabled = false; button.after(note(problem(r), "bad")); }
  });
  return h("section", { class: "vt-card" }, h("h2", {}, changed ? "Start fresh" : "Start practicing"),
    h("p", { class: "muted" }, changed ? "Practice settings changed, so the account starts over at $100. The old one stays in history on the PC."
      : "You get $100 of pretend money. If it falls to $80, everything is sold and trading stops."), button);
}
function movers() {
  if (!VT.markets) { return null; }
  const stable = new Set(["USDT", "USDC", "DAI", "PYUSD", "USDG", "TUSD", "FDUSD", "USDE", "RLUSD", "USDQ", "USDR", "EURT", "EUR", "GBP"]);
  const liquid = VT.markets.coins.filter((c) => Number(c.volume_24h_usd) >= MOVER_MIN_VOLUME && c.change_today_pct !== null && !stable.has(c.base));
  const sorted = [...liquid].sort((a, b) => Number(b.change_today_pct) - Number(a.change_today_pct));
  const pick = sorted.slice(0, 4).concat(sorted.slice(-2).reverse()).filter((c, i, list) => list.indexOf(c) === i);
  if (!pick.length) { return null; }
  return h("section", {}, h("div", { class: "vt-head" }, h("h2", {}, "Top movers today"), h("a", { href: "#market/gainers" }, "See all")),
    h("div", { class: "vt-movers" }, pick.map((c) => h("a", { class: "vt-mover", href: "#market/coin/" + c.symbol },
      h("span", { class: "sym" }, badge(c.base, true), c.base), h("span", { class: "px" }, usd(c.last)), pct(c.change_today_pct)))),
    h("p", { class: "muted small" }, "Only coins with over $1 million traded in 24 hours."));
}
function activityCard(body) {
  const w = body.watch || {}, radar = w.radar || {}, stats = radar.stats || null;
  const cases = (body.cases || []).slice(0, 4);
  const items = cases.map((c) => {
    const [label, tone] = OUTCOME[c.outcome] || [words(c.outcome), ""];
    const signed_by = (c.signatures || []).filter((x) => x && x.decision).map((x) => NAMES[x.agent] || words(x.agent));
    return h("div", { class: "vt-feed-item" }, badge(baseOf(c.symbol), true),
      h("div", { class: "what" }, h("strong", {}, (c.kind === "exit" ? "Sell idea · " : "Buy idea · ") + baseOf(c.symbol)),
        h("span", { class: "muted" }, ago(c.opened_at) + (signed_by.length ? " · checked by " + signed_by.length + " agents" : ""))),
      h("span", { class: "vt-tag " + tone }, label));
  });
  return h("section", { class: "vt-card" },
    h("div", { class: "vt-head" }, h("h2", {}, "AI agent activity"), h("a", { href: "#desk" }, "AI Desk")),
    items.length ? h("div", { class: "vt-feed" }, items)
      : h("p", { class: "muted" }, "No trade ideas yet. An idea only opens when research says a coin looks good, so quiet is normal."));
}
async function homeScreen() {
  const [account, markets] = await Promise.all([call("/m/api/trade/account"), call("/m/api/trade/markets")]);
  if (account.status === 401 || markets.status === 401) { notPaired(); return; }
  if (!account.ok) { offline(problem(account)); return; }
  VT.account = account.body;
  if (markets.ok) { VT.markets = markets.body; }
  const body = account.body;
  const holding = ((body.holdings || [])[0] || {}).symbol;
  const featured = holding || "BTCUSD";
  const coin = await call("/m/api/trade/coin?symbol=" + encodeURIComponent(featured) + "&range=" + VT.homeRange);
  const t = coin.ok ? coin.body.ticker || {} : {};
  const chart = coin.ok ? chartCard({ title: (holding ? "Your " : "") + (t.name || baseOf(featured)), base: baseOf(featured), last: t.last,
      change: t.change_today_pct, points: coin.body.chart, range: VT.homeRange,
      onRange: (key) => { VT.homeRange = key; route(true); } })
    : h("section", { class: "vt-chart-card" }, h("div", { class: "vt-chart-empty" }, "Couldn't load the chart from Kraken. Try again in a minute."));
  const left = [];
  const right = [];
  if (body.needs_start || !body.account) { left.push(startCard(body)); } else { left.push(...balanceHero(body)); }
  left.push(chart, movers());
  right.push(opportunitiesCard((body.watch || {}).radar, 5), activityCard(body));
  if (!markets.ok) { right.push(note("Kraken prices couldn't load right now. They refresh by themselves.")); }
  show(flashNote(), emergencyBanner(body.emergency_stop), watchBanner(body), h("div", { class: "vt-cols" }, h("div", {}, left), h("div", {}, right)));
}
function flashNote() { const text = VT.flash; VT.flash = null; return text ? note(text) : null; }

/* ---------- Market ---------- */
async function marketScreen(filter) {
  if (filter && ["all", "gainers", "losers", "radar"].includes(filter)) { VT.filter = filter; }
  const [markets, account] = await Promise.all([call("/m/api/trade/markets"), call("/m/api/trade/account")]);
  if (markets.status === 401) { notPaired(); return; }
  if (markets.ok) { VT.markets = markets.body; }
  if (account.ok) { VT.account = account.body; }
  const search = h("input", { type: "search", class: "vt-search", placeholder: "Search coins", "aria-label": "Search coins",
    value: VT.query, autocomplete: "off", autocapitalize: "characters", spellcheck: "false" });
  const list = h("div", {});
  const draw = () => list.replaceChildren(...marketRows());
  search.addEventListener("input", () => { VT.query = search.value.trim().toUpperCase(); VT.shown = 60; draw(); });
  const chips = [["all", "All"], ["gainers", "Gainers"], ["losers", "Losers"], ["radar", "Agents' radar"]].map(([key, label]) =>
    h("button", { type: "button", class: "vt-chip" + (VT.filter === key ? " on" : ""), "aria-pressed": String(VT.filter === key),
      onclick: () => { VT.filter = key; VT.shown = 60; location.hash = key === "all" ? "#market" : "#market/" + key; } }, label));
  draw();
  show(h("div", { class: "vt-head" }, h("h1", { class: "vt-title" }, "Market"),
      h("span", { class: "muted" }, VT.markets ? VT.markets.coins.length + " coins · updated " + ago(VT.markets.updated_at) : "")),
    VT.account ? watchBanner(VT.account) : null,
    search, h("div", { class: "vt-chips vt-filters" }, chips),
    markets.ok || VT.markets ? null : note(problem(markets), "bad"),
    h("section", { class: "vt-card flush" }, list));
}
function marketRows() {
  if (!VT.markets) { return [h("p", { class: "muted pad" }, "Couldn't load Kraken prices. They refresh by themselves.")]; }
  let coins = VT.markets.coins;
  if (VT.query) { coins = coins.filter((c) => c.base.includes(VT.query) || String(c.name).toUpperCase().includes(VT.query)); }
  if (VT.filter === "gainers") { coins = coins.filter((c) => Number(c.change_today_pct) > 0).sort((a, b) => b.change_today_pct - a.change_today_pct); }
  if (VT.filter === "losers") { coins = coins.filter((c) => Number(c.change_today_pct) < 0).sort((a, b) => a.change_today_pct - b.change_today_pct); }
  if (VT.filter === "radar") {
    const watching = VT.markets.agents_watching || [];
    coins = coins.filter((c) => watching.includes(c.symbol));
    if (!coins.length) { return [h("p", { class: "muted pad" }, "Nothing on the agents' radar right now. Coins show up here when the agents spot a price streak.")]; }
  }
  const rows = coins.slice(0, VT.shown).map((c) => h("a", { class: "vt-coin", href: "#market/coin/" + c.symbol }, badge(c.base),
    h("span", { class: "who" }, h("strong", {}, c.base), h("span", { class: "muted" }, (c.name && c.name !== c.base ? c.name + " · " : "") + compact(c.volume_24h_usd) + " vol")),
    h("span", { class: "num" }, h("strong", {}, usd(c.last)), pct(c.change_today_pct))));
  if (coins.length > VT.shown) {
    rows.push(h("button", { type: "button", class: "vt-more", onclick: (e) => { VT.shown += 100; e.target.parentNode.replaceChildren(...marketRows()); } },
      "Show more (" + (coins.length - VT.shown) + ")"));
  }
  if (!rows.length) { rows.push(h("p", { class: "muted pad" }, "No coins match.")); }
  return rows;
}

/* ---------- coin page ---------- */
async function coinScreen(symbol) {
  const [r, account] = await Promise.all([call("/m/api/trade/coin?symbol=" + encodeURIComponent(symbol) + "&range=" + VT.range),
    VT.account ? Promise.resolve({ ok: true, body: VT.account }) : call("/m/api/trade/account")]);
  if (r.status === 401) { notPaired(); return; }
  if (account.ok) { VT.account = account.body; }
  if (!r.ok) { show(back("Market", "#market"), note(problem(r), "bad")); return; }
  const c = r.body, t = c.ticker || {}, base = baseOf(symbol), holding = c.holding;
  const nodes = [flashNote(), back("Market", "#market"),
    chartCard({ title: t.name || base, base, last: t.last, change: t.change_today_pct, points: c.chart, range: VT.range,
      onRange: (key) => { VT.range = key; route(true); } }),
    h("section", { class: "vt-card" }, h("h2", {}, "Market details"),
      h("div", { class: "vt-row" }, h("span", {}, "Buy price (ask)"), h("span", { class: "val" }, usd(t.ask))),
      h("div", { class: "vt-row" }, h("span", {}, "Sell price (bid)"), h("span", { class: "val" }, usd(t.bid))),
      h("div", { class: "vt-row" }, h("span", {}, "24h high"), h("span", { class: "val" }, usd(t.high_24h))),
      h("div", { class: "vt-row" }, h("span", {}, "24h low"), h("span", { class: "val" }, usd(t.low_24h))),
      h("div", { class: "vt-row" }, h("span", {}, "24h volume"), h("span", { class: "val" }, compact(t.volume_24h_usd))))];
  if (holding) {
    const value = Number(holding.quantity) * Number(t.bid), pnl = value - Number(holding.cost_basis);
    nodes.push(h("section", { class: "vt-card" }, h("h2", {}, "Your " + base),
      h("div", { class: "vt-grid2" },
        h("div", { class: "vt-stat" }, h("b", {}, usd(value)), h("span", {}, "Worth at sell price")),
        h("div", { class: "vt-stat" }, h("b", { class: pnl >= 0 ? "up" : "down" }, signed(pnl)), h("span", {}, "Profit / loss"))),
      h("div", { class: "vt-row" }, h("span", {}, "Amount"), h("span", { class: "val" }, qty(holding.quantity) + " " + base)),
      h("div", { class: "vt-row" }, h("span", {}, "Bought by"), h("span", { class: "val" }, holding.opened_by === "you" ? "You" : "The agents"))));
  }
  if (c.agents) {
    const liked = c.agents.verdict === "sufficient_for_future_paper_evaluation";
    nodes.push(h("section", { class: "vt-card" }, h("div", { class: "vt-head" }, h("h2", {}, "What the agents saw"),
        h("span", { class: "vt-tag " + (liked ? "green" : "") }, liked ? "Looks good" : "Wait")),
      h("p", { class: "muted" }, "Checked " + ago(c.agents.checked_at)),
      c.agents.radar ? h("p", {}, "Streak: +" + c.agents.radar.rise_5m_pct + "% in 5 min, +" + c.agents.radar.rise_15m_pct + "% in 15 min") : null,
      Object.entries(c.agents.summaries || {}).map(([role, text]) => h("p", { class: "muted" }, h("strong", {}, (NAMES[role] || words(role)) + ": "), text))));
  } else {
    nodes.push(h("p", { class: "muted" }, "The agents haven't researched " + base + " recently."));
  }
  const agentsHold = holding && holding.opened_by !== "you";
  if (!t.tradeable) { nodes.push(note("This coin's price is too tiny to practice-trade accurately. You can still watch it.")); }
  else if (agentsHold) { nodes.push(note("The agents are holding " + base + " right now. You can buy after they sell, or sell theirs.")); }
  const buy = h("button", { class: "vt-buy", type: "button", disabled: t.tradeable && !agentsHold ? null : true }, "Buy");
  const sell = h("button", { class: "vt-sell", type: "button", disabled: holding ? null : true }, "Sell");
  buy.addEventListener("click", () => orderSheet(symbol, "buy", t, holding));
  sell.addEventListener("click", () => orderSheet(symbol, "sell", t, holding));
  nodes.push(h("div", { class: "vt-spacer", "aria-hidden": "true" }), h("div", { class: "vt-tradebar" }, buy, sell));
  show(nodes);
}

/* ---------- buy / sell sheet (same order request as the Trade tab) ---------- */
function orderSheet(symbol, side, t, holding) {
  const base = baseOf(symbol);
  const cash = Number(((VT.account || {}).account || {}).cash || 0);
  const worth = holding ? Number(holding.quantity) * Number(t.bid) : 0;
  const price = Number(side === "buy" ? t.ask : t.bid);
  const input = h("input", { type: "text", inputmode: "decimal", "aria-label": "Amount in dollars", placeholder: "0", autocomplete: "off" });
  let all = false;
  const estimate = h("p", { class: "muted", "aria-live": "polite" });
  const error = h("p", { class: "vt-error", role: "alert", hidden: true });
  const update = () => {
    const dollars = all ? (side === "buy" ? cash / 1.0025 : worth) : Number(input.value);
    error.hidden = true;
    estimate.textContent = dollars > 0 && price > 0
      ? (side === "buy" ? "About " : "Sells about ") + qty(dollars / price) + " " + base + " at ~" + usd(price) + " · fee about " + cents(Math.max(0.01, dollars * 0.0025))
      : (side === "buy" ? "Cash available: " + usd(cash) : "You have " + usd(worth) + " of " + base);
  };
  const quick = (side === "buy" ? [["$5", "5"], ["$10", "10"], ["$25", "25"], ["Max", "all"]] : [["25%", 0.25], ["50%", 0.5], ["All", "all"]]).map(([label, value]) =>
    h("button", { type: "button", class: "vt-chip", onclick: () => {
      all = value === "all";
      input.value = all ? "" : side === "buy" ? value : (worth * value).toFixed(2);
      input.placeholder = all ? (side === "buy" ? usd(cash).slice(1) : usd(worth).slice(1)) : "0";
      update();
    } }, label));
  input.addEventListener("input", () => { all = false; input.placeholder = "0"; update(); });
  const confirm = h("button", { class: side === "buy" ? "vt-buy" : "vt-sell", type: "button" }, (side === "buy" ? "Buy " : "Sell ") + base);
  const cancel = h("button", { class: "vt-ghost", type: "button" }, "Cancel");
  const sheet = h("div", { class: "vt-layer", role: "dialog", "aria-modal": "true", "aria-label": (side === "buy" ? "Buy " : "Sell ") + base },
    h("div", { class: "vt-sheet" },
      h("div", { class: "vt-head" }, h("h2", {}, (side === "buy" ? "Buy " : "Sell ") + base), h("span", { class: "vt-practice" }, h("i", { "aria-hidden": "true" }), "PRACTICE")),
      h("label", { class: "vt-dollar" }, h("span", { "aria-hidden": "true" }, "$"), input),
      h("div", { class: "vt-chips" }, quick), estimate, error,
      h("p", { class: "muted small" }, "Pretend money. Your PC prices this from Kraken the moment you confirm, and the $80 shutdown still applies."),
      confirm, cancel));
  const close = () => sheet.remove();
  cancel.addEventListener("click", close);
  sheet.addEventListener("click", (e) => { if (e.target === sheet) { close(); } });
  confirm.addEventListener("click", async () => {
    const amount = all ? "all" : input.value.trim();
    if (!all && !(/^\d+(\.\d{1,2})?$/.test(amount) && Number(amount) > 0)) { error.textContent = "Enter a dollar amount, like 20 or 12.50."; error.hidden = false; return; }
    if (side === "buy" && !all && Number(amount) > cash) { error.textContent = "That's more than your " + usd(cash) + " cash."; error.hidden = false; return; }
    confirm.disabled = true;
    confirm.textContent = side === "buy" ? "Buying…" : "Selling…";
    const r = await post("/m/api/trade/order", { symbol, side, amount });
    confirm.disabled = false;
    confirm.textContent = (side === "buy" ? "Buy " : "Sell ") + base;
    if (!r.ok) { error.textContent = problem(r); error.hidden = false; return; }
    if (!r.body.ok) { error.textContent = (r.body.refused && r.body.refused.message) || "Not traded."; error.hidden = false; return; }
    close();
    const f = r.body.fill;
    VT.flash = f ? (f.side === "buy" ? "Bought " : "Sold ") + qty(f.quantity) + " " + base + " for " + usd(f.notional) + " (fee " + usd(f.fee) + ")." : "Done.";
    VT.account = null;
    route(true);
  });
  document.body.append(sheet);
  update();
  input.focus();
}

/* ---------- Portfolio (stage 2) ---------- */
const SLICE = ["#166FE5", "#0A9D58", "#7C3AED", "#F59E0B", "#DB2777", "#0891B2", "#65A30D", "#EA580C"];
function shutdownCard(a) {
  const equity = Number(a.equity), start = Number(a.starting_cash), floor = Number(a.shutdown_floor);
  const halted = a.status === "halted";
  const room = isFinite(equity) ? Math.max(0, Math.min(100, ((equity - floor) / (start - floor)) * 100)) : 0;
  const fill = h("i");
  fill.style.width = (halted ? 0 : room) + "%";
  const state = halted ? ["Tripped", "red"] : a.guard === "unverified" ? ["Buys paused", "amber"] : room < 40 ? ["Getting close", "amber"] : ["Safe", "green"];
  const h_ = a.halt || {};
  return h("section", { class: "vt-card" },
    h("div", { class: "vt-head" }, h("h2", {}, "$80 shutdown"), h("span", { class: "vt-tag " + state[1] }, state[0])),
    halted
      ? h("p", {}, "Your balance hit " + usd(h_.equity_at_trigger) + " " + ago(h_.at) + ", so everything was sold and trading stopped. Cash left: "
          + usd(h_.cash_after_closing) + ".")
      : h("p", { class: "vt-big vt-mid" }, usd(a.dollars_above_floor), h("span", { class: "muted" }, " above the line")),
    h("div", { class: "vt-gauge" }, h("div", { class: "vt-fuse" + (halted ? " tripped" : room < 40 ? " low" : ""), role: "img",
        "aria-label": halted ? "Shutdown tripped" : Math.round(room) + "% of the way from the shutdown to the starting balance" }, fill),
      h("div", { class: "vt-fuse-labels muted small" }, h("span", {}, cents(floor) + " shutdown"), h("span", {}, cents(start) + " start"))),
    a.guard === "unverified" && !halted ? h("p", { class: "muted" }, "Some prices are over 10 minutes old, so new buys wait until they update.") : null,
    h("p", { class: "muted small" }, "If your balance falls to " + cents(floor) + ", the PC sells everything and stops all trading. Fast drops can land a little below "
      + cents(floor) + ". It's a trigger, not a guaranteed limit."));
}
function allocation(a, holdings) {
  const cash = Number(a.cash);
  const parts = holdings.filter((x) => Number(x.value) > 0).map((x, i) => ({ label: baseOf(x.symbol), value: Number(x.value), color: SLICE[i % SLICE.length] }));
  parts.push({ label: "Cash", value: isFinite(cash) ? cash : 0, color: "#C9CED6" });
  const total = parts.reduce((sum, x) => sum + x.value, 0);
  if (!(total > 0)) { return null; }
  const bar = h("div", { class: "vt-alloc", role: "img", "aria-label": parts.map((x) => x.label + " " + Math.round((x.value / total) * 100) + "%").join(", ") });
  for (const x of parts) { const seg = h("i"); seg.style.width = (x.value / total) * 100 + "%"; seg.style.background = x.color; bar.append(seg); }
  return h("section", { class: "vt-card" }, h("h2", {}, "Where your money is"), bar,
    h("div", { class: "vt-legend" }, parts.map((x) => { const dot = h("i"); dot.style.background = x.color;
      return h("span", {}, dot, x.label + " " + Math.round((x.value / total) * 100) + "%"); })));
}
function holdingRows(holdings) {
  if (!holdings.length) {
    return h("section", { class: "vt-card" }, h("h2", {}, "Holdings"), h("p", { class: "muted" }, "You don't own any coins right now. It's all cash."),
      h("a", { class: "vt-ghost", href: "#market", role: "button" }, "Browse the market"));
  }
  return h("section", { class: "vt-card flush" }, h("div", { class: "pad vt-head" }, h("h2", {}, "Holdings"), h("span", { class: "muted" }, holdings.length + " coin" + (holdings.length === 1 ? "" : "s"))),
    h("div", {}, holdings.map((x) => {
      const value = Number(x.value), cost = Number(x.cost_basis);
      const pnl = isFinite(value) ? value - cost : null;
      return h("a", { class: "vt-coin", href: "#market/coin/" + x.symbol }, badge(baseOf(x.symbol)),
        h("span", { class: "who" }, h("strong", {}, baseOf(x.symbol)),
          h("span", { class: "muted" }, (x.opened_by === "you" ? "You" : "Agents") + " · " + ago(x.opened_at))),
        h("span", { class: "num" }, h("strong", {}, usd(x.value)),
          pnl === null ? h("span", { class: "muted small" }, "no price yet")
            : h("span", { class: "vt-chg " + (pnl > 0 ? "up" : pnl < 0 ? "down" : "flat") }, signed(pnl) + " · " + (cost ? ((pnl >= 0 ? "+" : "−") + Math.abs((pnl / cost) * 100).toFixed(1) + "%") : ""))));
    })),
    h("p", { class: "muted small pad" }, "Values use the last price your PC recorded. Tap a coin for the live price."));
}
function historyCard(trades) {
  const who = VT.history || "all";
  const list = trades.filter((x) => who === "all" || x.kind === "halt" || x.by === who);
  const chips = [["all", "All"], ["you", "You"], ["agents", "Agents"]].map(([key, label]) =>
    h("button", { type: "button", class: "vt-chip" + (who === key ? " on" : ""), "aria-pressed": String(who === key),
      onclick: () => { VT.history = key; route(true); } }, label));
  const rows = list.map((x) => {
    if (x.kind === "halt") {
      return h("div", { class: "vt-trade halt" }, h("span", { class: "vt-side red" }, "!"),
        h("span", { class: "who" }, h("strong", {}, "$80 shutdown tripped"), h("span", { class: "muted" }, new Date(x.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))),
        h("span", { class: "num" }, h("strong", {}, usd(x.equity_at_trigger)), h("span", { class: "muted small" }, "balance at trigger")));
    }
    const buy = x.side === "buy", pnl = x.realized_pnl === null || x.realized_pnl === undefined ? null : Number(x.realized_pnl);
    return h("div", { class: "vt-trade" }, h("span", { class: "vt-side " + (buy ? "green" : "dark") }, buy ? "B" : "S"),
      h("span", { class: "who" }, h("strong", {}, (buy ? "Bought " : "Sold ") + baseOf(x.symbol)),
        h("span", { class: "muted" }, new Date(x.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
          + " · " + (x.by === "you" ? "You" : "Agents") + " · " + qty(x.quantity) + " @ " + usd(x.fill_price || x.price))),
      h("span", { class: "num" }, h("strong", {}, usd(x.notional)),
        pnl === null ? h("span", { class: "muted small" }, "fee " + usd(x.fee))
          : h("span", { class: "small " + (pnl > 0 ? "up" : pnl < 0 ? "down" : "flat") }, signed(pnl) + (pnl < 0 ? " loss" : " profit"))));
  });
  return h("section", { class: "vt-card" }, h("div", { class: "vt-head" }, h("h2", {}, "Trade history"), h("span", { class: "muted" }, "last " + trades.length)),
    h("div", { class: "vt-chips" }, chips),
    rows.length ? h("div", { class: "vt-feed" }, rows) : h("p", { class: "muted" }, who === "all" ? "No trades yet." : "No trades by " + (who === "you" ? "you" : "the agents") + " yet."));
}
async function portfolioScreen() {
  const r = await call("/m/api/trade/account");
  if (r.status === 401) { notPaired(); return; }
  if (!r.ok) { offline(problem(r)); return; }
  VT.account = r.body;
  const body = r.body;
  if (body.needs_start || !body.account) { show(h("h1", { class: "vt-title" }, "Portfolio"), startCard(body)); return; }
  const a = body.account, holdings = body.holdings || [], trades = body.trades || [];
  const equity = Number(a.equity), start = Number(a.starting_cash), pnl = equity - start, realized = Number(a.realized_pnl);
  const invested = holdings.reduce((sum, x) => sum + (Number(x.value) || 0), 0);
  show(flashNote(), h("h1", { class: "vt-title" }, "Portfolio"),
    h("div", { class: "vt-cols" },
      h("div", {},
        h("section", { class: "vt-hero" }, h("p", { class: "vt-label" }, "Practice balance"),
          h("p", { class: "vt-big" }, usd(a.equity)),
          isFinite(pnl) ? h("p", { class: "vt-pnl " + (pnl >= 0 ? "up" : "down") }, signed(pnl) + " (" + (pnl >= 0 ? "+" : "−") + Math.abs((pnl / start) * 100).toFixed(2) + "%) all time") : null),
        h("div", { class: "vt-stats" },
          h("div", { class: "vt-stat" }, h("b", {}, usd(a.cash)), h("span", {}, "Cash")),
          h("div", { class: "vt-stat" }, h("b", {}, usd(invested)), h("span", {}, "In coins")),
          h("div", { class: "vt-stat" }, h("b", { class: realized > 0 ? "up" : realized < 0 ? "down" : "" }, isFinite(realized) ? signed(realized) : "—"), h("span", {}, "From closed trades"))),
        allocation(a, holdings), holdingRows(holdings)),
      h("div", {}, shutdownCard(a), historyCard(trades))));
}

/* ---------- Emergency Stop (autonomous-trading step 1) ---------- */
function emergencyBanner(state) {
  if (!state || !state.engaged) { return null; }
  return h("section", { class: "vt-banner bad", role: "alert" }, h("span", { class: "dot" }),
    h("div", {}, h("strong", {}, "Emergency Stop is ON"),
      h("span", { class: "muted" }, "The agents can't buy, and they sell what they hold on their next check. Your own trades still work. "
        + (state.at ? "Turned on " + ago(state.at) + "." : ""))));
}
function emergencyCard(state) {
  const on = Boolean(state && state.engaged);
  const error = h("p", { class: "vt-error", role: "alert", hidden: true });
  const button = h("button", { type: "button", class: on ? "vt-ghost" : "vt-stop" }, on ? "Turn Emergency Stop off" : "Emergency Stop");
  button.addEventListener("click", async () => {
    const sure = on ? window.confirm("Turn the Emergency Stop off? The agents can start buying again on their next check.")
      : window.confirm("Stop the agents now? They buy nothing and sell every coin they hold within about a minute. Your own coins are not touched.");
    if (!sure) { return; }
    button.disabled = true;
    const r = await post("/m/api/trade/emergency", on ? { engaged: false, confirm: "release-emergency-stop" } : { engaged: true });
    button.disabled = false;
    if (!r.ok) { error.textContent = problem(r); error.hidden = false; return; }
    VT.flash = on ? "Emergency Stop is off. The agents are back to normal." : "Emergency Stop is on. The agents are selling their coins.";
    route(true);
  });
  return h("section", { class: "vt-card" + (on ? " vt-stopcard" : "") },
    h("div", { class: "vt-head" }, h("h2", {}, "Emergency Stop"), h("span", { class: "vt-tag " + (on ? "red" : "green") }, on ? "ON" : "Off")),
    h("p", { class: "muted" }, on ? "The agents are frozen and selling what they hold. Turn it off when you're ready for them to trade again."
      : "One tap freezes the agents and sells every coin they hold. Your own trades aren't touched."),
    button, error);
}
function limitsCard(l) {
  if (!l) { return null; }
  const row = (label, value) => h("div", { class: "vt-row" }, h("span", {}, label), h("span", { class: "val" }, value));
  return h("section", { class: "vt-card" }, h("h2", {}, "Safety limits"),
    row("Per agent trade", usd(l.order_dollars)),
    row("Coins held at once", "up to " + l.max_agent_positions),
    row("Buys per day", "up to " + l.max_entries_per_day),
    l.max_daily_loss_dollars ? row("Daily loss stop", "stops buying at −" + cents(l.max_daily_loss_dollars).replace("−", "")) : null,
    row("Stop-loss", "sell at −" + l.stop_loss_pct + "%"),
    row("Take-profit", "none: winners keep running"),
    l.profit_trail ? l.profit_trail.tiers.map((tier) => row("Profit trail from +" + tier.from_profit_pct + "%",
      "sell after giving back " + tier.giveback_points + " pts")) : null,
    row("Longest hold", l.max_hold_minutes + " min, unless the profit trail is on"),
    row("After " + l.pause_after_losses + " losses in a row", "pause " + Math.round(l.pause_minutes / 60) + " hr"),
    l.profit_trail ? h("p", { class: "muted small" }, profitExample(l.profit_trail)) : null,
    h("p", { class: "muted small" }, "Plus the $80 shutdown on the whole account. Change these in config/trading-desk.json on the PC."));
}
function profitExample(trail) {
  const pts = (best) => { let g = null; for (const t of trail.tiers) { if (best >= Number(t.from_profit_pct)) { g = Number(t.giveback_points); } } return g; };
  const lock = (best) => Math.max(best - pts(best), Number(trail.min_lock_profit_pct || 0));
  return "Example: best profit +20% → sells at +" + lock(20) + "%. Best +30% → +" + lock(30) + "%. Points of profit, after estimated selling costs. "
    + "A fast drop can fill a little lower.";
}
function trailTestCard(test) {
  if (!test) { return null; }
  const label = { current: "Current setting", tight: "Tighter", loose: "Looser", fixed_3pct_take_profit: "Fixed +3% take-profit",
                  no_trail_hold_to_end: "No trail (hold)" };
  return h("section", { class: "vt-card" }, h("div", { class: "vt-head" }, h("h2", {}, "Which profit setting works best?"),
      h("span", { class: "muted small" }, test.closed_positions + " closed trades")),
    test.closed_positions ? h("div", {}, test.results.map((r) => h("div", { class: "vt-row" }, h("span", {}, label[r.name] || words(r.name)),
      h("span", { class: "val " + (Number(r.average_profit_pct) > 0 ? "up" : Number(r.average_profit_pct) < 0 ? "down" : "") },
        r.average_profit_pct === null ? "—" : (Number(r.average_profit_pct) > 0 ? "+" : "") + r.average_profit_pct + "% avg"))))
      : h("p", { class: "muted" }, "Fills in as the agents close trades: each closed trade is replayed under every setting."),
    h("p", { class: "muted small" }, "Replays real paper prices. A few trades prove nothing; look again after a few weeks."));
}

/* ---------- AI Trading Desk (stage 3) ---------- */
const FLOW = ["market_scout", "trend_agent", "strategy_agent", "risk_review", "verifier", "executor"];
const SHORT = { market_scout: "Scout", trend_agent: "Trend", strategy_agent: "Strategy", risk_review: "Risk", verifier: "Verifier",
  executor: "Executor", position_monitor: "Monitor", performance_reviewer: "Scorer" };
const HUE = { market_scout: "#166FE5", trend_agent: "#0891B2", strategy_agent: "#7C3AED", verifier: "#DB2777", risk_review: "#EA580C",
  executor: "#0A9D58", position_monitor: "#65A30D", performance_reviewer: "#B45309" };
const DECISION = { approve: ["Approved", "green"], veto: ["Said no", "red"], executed: ["Traded", "green"], refused: ["Refused", "red"],
  close: ["Wants to sell", "amber"], waiting: ["Waiting", "amber"], paused: ["Paused", "blue"] };
function avatar(id, small) {
  const node = h("span", { class: "vt-avatar" + (small ? " sm" : ""), "aria-hidden": "true" }, (SHORT[id] || "?").slice(0, 2));
  node.style.background = HUE[id] || "#6B7280";
  return node;
}
function flowStrip() {
  const steps = [];
  FLOW.forEach((id, i) => {
    if (i) { steps.push(h("span", { class: "vt-arrow", "aria-hidden": "true" }, "›")); }
    steps.push(h("span", { class: "vt-step" }, avatar(id, true), SHORT[id]));
  });
  return h("section", { class: "vt-card" }, h("h2", {}, "How a trade happens"),
    h("div", { class: "vt-flow", role: "list", "aria-label": "Scout, Trend, Strategy and Risk research; Verifier double-checks; Executor trades" }, steps),
    h("p", { class: "muted" }, "Every few minutes the first four research each coin. A practice trade only happens when they all approve, the Verifier re-checks the math, and Risk Review clears the account. Then the Executor places it."));
}
function agentCard(a) {
  const worked = a.state === "worked";
  const d = a.last_decision;
  const tag = d && DECISION[d.decision];
  return h("article", { class: "vt-agent" },
    h("div", { class: "vt-agent-top" }, avatar(a.id),
      h("div", { class: "who" }, h("strong", {}, a.name), h("span", { class: "muted small" }, a.job)),
      h("span", { class: "vt-dot " + (worked ? "on" : ""), title: worked ? "Has worked" : "Idle" })),
    h("p", { class: "vt-agent-line" }, a.headline || ""),
    (a.now || []).length > 1 ? h("ul", { class: "vt-agent-now" }, a.now.slice(0, 5).map((n) => h("li", {}, h("b", {}, baseOf(n.symbol)), " " + (n.text || words(n.status))))) : null,
    h("div", { class: "vt-agent-foot muted small" }, h("span", {}, a.at ? ago(a.at) : "Not yet"),
      tag ? h("span", { class: "vt-tag " + tag[1] }, "Last: " + tag[0]) : null));
}
function decisionCard(c) {
  const [label, tone] = OUTCOME[c.outcome] || [c.outcome_text || words(c.outcome), ""];
  const body = h("div", { class: "vt-steps" }, (c.steps || []).map((s) => {
    const tag = DECISION[s.decision] || [words(s.decision), ""];
    return h("div", { class: "vt-stepline" }, avatar(s.agent, true),
      h("div", { class: "what" }, h("strong", {}, NAMES[s.agent] || words(s.agent)),
        s.summary ? h("span", { class: "muted" }, s.summary) : null,
        (s.reasons || []).length ? h("span", { class: "muted small" }, "Why: " + s.reasons.map(words).join(", ")) : null),
      h("span", { class: "vt-tag " + tag[1] }, tag[0]));
  }));
  return h("details", { class: "vt-case" },
    h("summary", {}, badge(baseOf(c.symbol), true),
      h("span", { class: "what" }, h("strong", {}, (c.kind === "exit" ? "Sell idea · " : "Buy idea · ") + baseOf(c.symbol)),
        h("span", { class: "muted small" }, ago(c.at) + (c.price ? " · at " + usd(c.price) : "") + " · " + (c.steps || []).length + " agents")),
      h("span", { class: "vt-tag " + tone }, label)), body);
}
async function deskScreen() {
  const [r, account] = await Promise.all([call("/m/api/trade/desk"), call("/m/api/trade/account")]);
  if (r.status === 401) { notPaired(); return; }
  if (!r.ok) { offline(problem(r)); return; }
  if (account.ok) { VT.account = account.body; }
  const d = r.body, w = d.watch || {};
  const agentTrades = ((account.ok && account.body.trades) || []).filter((x) => x.kind === "fill" && x.by === "agents");
  const counts = { approved: 0, vetoed: 0, executed: 0 };
  for (const c of d.decisions || []) {
    if (c.outcome === "executed") { counts.executed += 1; }
    if (c.outcome === "vetoed" || c.outcome === "refused") { counts.vetoed += 1; }
    if (c.outcome === "executed" || c.outcome === "approved_not_executed") { counts.approved += 1; }
  }
  const researched = (d.researched || []).map((m) => {
    const good = m.verdict === "sufficient_for_future_paper_evaluation";
    return h("a", { class: "vt-coin", href: "#market/coin/" + m.symbol }, badge(baseOf(m.symbol)),
      h("span", { class: "who" }, h("strong", {}, baseOf(m.symbol)), h("span", { class: "muted" }, m.error ? "Price problem" : m.verdict_text || "Not researched yet")),
      h("span", { class: "num" }, h("strong", {}, usd(m.price)), h("span", { class: "vt-tag " + (good ? "green" : "") }, good ? "Looks good" : "Wait")));
  });
  show(flashNote(), h("div", { class: "vt-head" }, h("h1", { class: "vt-title" }, "AI Desk"),
      h("span", { class: "vt-tag " + (w.auto_practice_trades ? "green" : "blue") }, w.auto_practice_trades ? "Auto trades on" : "Auto trades off")),
    emergencyBanner(d.emergency_stop),
    VT.account ? watchBanner(VT.account) : null,
    w.desk_error ? note("The desk had a problem last cycle: " + words(w.desk_error)) : null,
    h("div", { class: "vt-stats" },
      h("div", { class: "vt-stat" }, h("b", {}, String(counts.approved)), h("span", {}, "Ideas approved")),
      h("div", { class: "vt-stat" }, h("b", {}, String(counts.vetoed)), h("span", {}, "Stopped")),
      h("div", { class: "vt-stat" }, h("b", { class: counts.executed ? "up" : "" }, String(counts.executed)), h("span", {}, "Trades placed"))),
    h("p", { class: "muted small" }, "Counts cover the recent ideas shown below (today and yesterday)."),
    h("div", { class: "vt-cols" },
      h("div", {},
        emergencyCard(d.emergency_stop),
        opportunitiesCard(VT.account && VT.account.watch && VT.account.watch.radar),
        flowStrip(),
        h("section", {}, h("div", { class: "vt-head" }, h("h2", {}, "The eight agents"), h("span", { class: "muted" }, "live from your PC")),
          h("div", { class: "vt-agents" }, (d.agents || []).map(agentCard)))),
      h("div", {},
        h("section", { class: "vt-card" }, h("div", { class: "vt-head" }, h("h2", {}, "Decisions"), h("span", { class: "muted" }, "tap to see why")),
          (d.decisions || []).length ? h("div", { class: "vt-cases" }, d.decisions.map(decisionCard))
            : h("p", { class: "muted" }, "No trade ideas today or yesterday. An idea only opens when research says a coin looks good.")),
        h("section", { class: "vt-card flush" }, h("div", { class: "pad vt-head" }, h("h2", {}, "Coins researched"), h("span", { class: "muted" }, w.last_cycle_at ? ago(w.last_cycle_at) : "")),
          researched.length ? h("div", {}, researched) : h("p", { class: "muted pad" }, "Nothing researched yet. The agents start when the watch runs.")),
        limitsCard(d.limits),
        trailTestCard(d.trail_test),
        h("section", { class: "vt-card" }, h("h2", {}, "Trades the agents placed"),
          agentTrades.length ? h("div", { class: "vt-feed" }, agentTrades.slice(0, 10).map((x) => {
            const pnl = x.realized_pnl === null || x.realized_pnl === undefined ? null : Number(x.realized_pnl);
            return h("div", { class: "vt-trade" }, h("span", { class: "vt-side " + (x.side === "buy" ? "green" : "dark") }, x.side === "buy" ? "B" : "S"),
              h("span", { class: "who" }, h("strong", {}, (x.side === "buy" ? "Bought " : "Sold ") + baseOf(x.symbol)), h("span", { class: "muted" }, ago(x.at) + " · " + words(x.reason))),
              h("span", { class: "num" }, h("strong", {}, usd(x.notional)), pnl === null ? null : h("span", { class: "small " + (pnl >= 0 ? "up" : "down") }, signed(pnl))));
          })) : h("p", { class: "muted" }, "The agents haven't placed a practice trade on this account yet.")))));
}

/* ---------- Results / Performance (stage 4) ---------- */
function curveChart(curve, start, floor) {
  const values = curve.map((c) => Number(c.balance));
  const label = (c) => c.event === "start" ? "Started" : c.event === "now" ? "Now (with open coins)"
    : "Sold " + baseOf(c.symbol) + " " + signed(c.pnl) + " · " + (c.by === "you" ? "you" : "agents");
  const readout = h("p", { class: "vt-readout" }, usd(values[values.length - 1]));
  const sub = h("p", { class: "vt-readout-sub muted" }, label(curve[curve.length - 1]));
  if (values.length < 2) { return h("section", { class: "vt-chart-card" }, readout, h("div", { class: "vt-chart-empty" }, "The chart starts after the first sale.")); }
  const W = 600, H = 200, top = 12, bottom = 188;
  const low = Math.min(...values, floor), high = Math.max(...values, start), span = high - low || 1;
  const x = (i) => (i / (values.length - 1)) * W;
  const y = (v) => bottom - ((v - low) / span) * (bottom - top);
  let path = "M0 " + y(values[0]).toFixed(1);
  for (let i = 1; i < values.length; i++) { path += " L" + x(i).toFixed(1) + " " + y(values[i - 1]).toFixed(1) + " L" + x(i).toFixed(1) + " " + y(values[i]).toFixed(1); }
  const dir = values[values.length - 1] >= start ? "up" : "down";
  const svg = s("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", class: "vt-chart", role: "img",
    "aria-label": "Locked-in balance after each sale. Touch or hover to see each point." });
  svg.append(s("line", { x1: 0, x2: W, y1: y(start), y2: y(start), class: "base", "vector-effect": "non-scaling-stroke" }),
    s("line", { x1: 0, x2: W, y1: y(floor), y2: y(floor), class: "floorline", "vector-effect": "non-scaling-stroke" }),
    s("path", { d: path + " L" + W + " " + H + " L0 " + H + " Z", class: "area " + dir }),
    s("path", { d: path, class: "line " + dir, "vector-effect": "non-scaling-stroke" }));
  const dot = s("circle", { r: 5, cx: -20, cy: -20, class: "dot " + dir, "vector-effect": "non-scaling-stroke" });
  svg.append(dot);
  const reset = () => { dot.setAttribute("cx", -20); readout.textContent = usd(values[values.length - 1]); sub.textContent = label(curve[curve.length - 1]); };
  const scrub = (event) => {
    const box = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(values.length - 1, Math.round(((event.clientX - box.left) / box.width) * (values.length - 1))));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(values[i]));
    readout.textContent = usd(values[i]);
    sub.textContent = label(curve[i]) + " · " + new Date(curve[i].at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  svg.addEventListener("pointermove", scrub); svg.addEventListener("pointerdown", scrub);
  svg.addEventListener("pointerleave", reset); svg.addEventListener("pointercancel", reset);
  return h("section", { class: "vt-chart-card" }, h("div", { class: "vt-head" }, h("h2", {}, "Balance over time"), h("span", { class: "muted small" }, values.length - 2 + " sales")),
    readout, sub, svg,
    h("div", { class: "vt-legend" }, h("span", {}, h("i", { class: "key start" }), cents(start) + " start"), h("span", {}, h("i", { class: "key floor" }), cents(floor) + " shutdown")),
    h("p", { class: "muted small" }, "Each step is a sale (profit or loss after fees). The last point is today's balance including coins you still hold."));
}
function scoreTile(value, label, tone) { return h("div", { class: "vt-stat" }, h("b", { class: tone || "" }, value), h("span", {}, label)); }
function toneOf(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
function whoColumn(title, sc, fee) {
  const pnl = Number(sc.realized_pnl);
  return h("div", { class: "vt-who" }, h("strong", {}, title),
    h("div", { class: "vt-row" }, h("span", {}, "Closed trades"), h("span", { class: "val" }, String(sc.closed_trades))),
    h("div", { class: "vt-row" }, h("span", {}, "Win rate"), h("span", { class: "val" }, sc.win_rate_pct === null ? "—" : Math.round(Number(sc.win_rate_pct)) + "%")),
    h("div", { class: "vt-row" }, h("span", {}, "P&L"), h("span", { class: "val " + toneOf(pnl) }, signed(pnl))),
    h("div", { class: "vt-row" }, h("span", {}, "Fees"), h("span", { class: "val" }, cents(fee))));
}
async function resultsScreen() {
  const r = await call("/m/api/trade/results");
  if (r.status === 401) { notPaired(); return; }
  if (!r.ok) { offline(problem(r)); return; }
  const d = r.body;
  if (d.needs_start || !d.account) { show(h("h1", { class: "vt-title" }, "Results"), startCard(d)); return; }
  const a = d.account, all = d.scores.all, start = Number(a.starting_cash), equity = Number(a.equity);
  const change = equity - start;
  const winRate = all.win_rate_pct === null ? "—" : Math.round(Number(all.win_rate_pct)) + "%";
  show(h("h1", { class: "vt-title" }, "Results"),
    h("section", { class: "vt-hero" }, h("p", { class: "vt-label" }, "Since you started with " + cents(start)),
      h("p", { class: "vt-big " + toneOf(change) }, isFinite(change) ? signed(change) : "—"),
      h("p", { class: "vt-pnl muted" }, isFinite(change) ? (change >= 0 ? "+" : "−") + Math.abs((change / start) * 100).toFixed(2) + "% · balance " + usd(equity) : "")),
    a.status === "halted" ? note("The $80 shutdown tripped " + ago(a.halt && a.halt.at) + ". These results stop there.") : null,
    h("div", { class: "vt-cols" },
      h("div", {},
        curveChart(d.curve || [], start, Number(a.shutdown_floor)),
        h("div", { class: "vt-stats vt-stats6" },
          scoreTile(winRate, "Win rate · " + all.wins + "W " + all.losses + "L"),
          scoreTile(String(all.closed_trades), "Closed trades"),
          scoreTile(signed(all.realized_pnl), "Locked-in profit", toneOf(Number(all.realized_pnl))),
          scoreTile(Number(d.drawdown.dollars) ? "−" + cents(d.drawdown.dollars).replace("−", "") : "$0.00", "Biggest drop · " + Number(d.drawdown.pct).toFixed(1) + "%", Number(d.drawdown.dollars) ? "down" : ""),
          scoreTile(cents(d.fees.total), "Fees paid"),
          scoreTile(compact(d.volume), "Traded · " + d.fills + " fills")),
        h("section", { class: "vt-card" }, h("h2", {}, "Best and worst"),
          h("div", { class: "vt-row" }, h("span", {}, "Best trade"), h("span", { class: "val " + toneOf(Number(all.best)) }, all.best === null ? "—" : signed(all.best))),
          h("div", { class: "vt-row" }, h("span", {}, "Worst trade"), h("span", { class: "val " + toneOf(Number(all.worst)) }, all.worst === null ? "—" : signed(all.worst))))),
      h("div", {},
        h("section", { class: "vt-card" }, h("h2", {}, "You vs the agents"),
          h("div", { class: "vt-grid2" }, whoColumn("You", d.scores.you, d.fees.you), whoColumn("Agents", d.scores.agents, d.fees.agents))),
        h("section", { class: "vt-card flush" }, h("div", { class: "pad vt-head" }, h("h2", {}, "By coin"), h("span", { class: "muted" }, "closed trades")),
          (d.coins || []).length ? h("div", {}, d.coins.map((c) => h("a", { class: "vt-coin", href: "#market/coin/" + c.symbol }, badge(baseOf(c.symbol)),
            h("span", { class: "who" }, h("strong", {}, baseOf(c.symbol)), h("span", { class: "muted" }, c.trades + " sale" + (c.trades === 1 ? "" : "s"))),
            h("span", { class: "num" }, h("strong", { class: toneOf(Number(c.pnl)) }, signed(c.pnl))))))
            : h("p", { class: "muted pad" }, "No closed trades yet. Results appear after the first sale.")),
        h("p", { class: "muted small" }, "Practice results on pretend money. They don't predict real trading."))));
}

/* ---------- router ---------- */
function quiet() { return document.hidden || document.querySelector(".vt-layer, details[open]") || (document.activeElement && document.activeElement.type === "search"); }
async function route(force) {
  const name = (location.hash || "#home").slice(1) || "home";
  if (!force && name === VT.route) { return; }
  const changed = name !== VT.route;
  VT.route = name;
  clearInterval(VT.timer);
  tabs();
  const [tab, ...rest] = name.split("/");
  if (tab === "market" && rest[0] === "coin" && rest[1]) {
    await coinScreen(rest[1]);
    VT.timer = setInterval(() => { if (!quiet()) { coinScreen(rest[1]); } }, 15000);
  } else if (tab === "market") {
    await marketScreen(rest[0] || "all");
    VT.timer = setInterval(() => { if (!quiet()) { marketScreen(); } }, 20000);
  } else if (tab === "performance") {
    await resultsScreen();
    VT.timer = setInterval(() => { if (!quiet()) { resultsScreen(); } }, 30000);
  } else if (tab === "desk") {
    await deskScreen();
    VT.timer = setInterval(() => { if (!quiet()) { deskScreen(); } }, 30000);
  } else if (tab === "portfolio") {
    await portfolioScreen();
    VT.timer = setInterval(() => { if (!quiet()) { portfolioScreen(); } }, 20000);
  } else {
    await homeScreen();
    VT.timer = setInterval(() => { if (!quiet()) { homeScreen(); } }, 20000);
  }
  if (changed) { window.scrollTo(0, 0); $screen().focus({ preventScroll: true }); }
}
window.addEventListener("hashchange", () => { for (const layer of document.querySelectorAll(".vt-layer")) { layer.remove(); } route(false); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { route(true); } });
document.addEventListener("DOMContentLoaded", () => route(true));
