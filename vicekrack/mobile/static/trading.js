/* ViceKrack Trading 2.0 (stage 1: Home dashboard + Market). PRACTICE MONEY ONLY.
 *
 * A standalone trading app page served at /trading by the same Mobile Command Center server, so it uses the same
 * pairing, the same doorman and the same read/order requests as the Trade tab (/m/api/trade/*). Nothing new runs on
 * the PC: every price, balance, agent decision and trade comes from those requests. Orders are priced on the PC from
 * a fresh Kraken request and still pass the $80 guard; the page never sends a price.
 * Text is always inserted as text, never parsed as HTML. */
"use strict";

const VT = { csrf: null, timer: null, route: null, markets: null, account: null, filter: "all", query: "", shown: 60,
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
  const liquid = VT.markets.coins.filter((c) => Number(c.volume_24h_usd) >= MOVER_MIN_VOLUME && c.change_today_pct !== null);
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
    radar.ok && stats ? h("p", { class: "muted" }, "Last scan: " + stats.coins_priced + " coins priced · " + stats.liquid + " trade enough · "
      + stats.candidates + " on a streak") : null,
    (radar.candidates || []).length ? h("div", { class: "vt-chips" }, radar.candidates.slice(0, 6).map((c) =>
      h("a", { class: "vt-chip", href: "#market/coin/" + c.symbol }, c.base + " " + (Number(c.rise_15m_pct) >= 0 ? "+" : "") + c.rise_15m_pct + "%"))) : null,
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
  right.push(activityCard(body));
  if (!markets.ok) { right.push(note("Kraken prices couldn't load right now. They refresh by themselves.")); }
  show(flashNote(), watchBanner(body), h("div", { class: "vt-cols" }, h("div", {}, left), h("div", {}, right)));
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

/* ---------- sections arriving in the next stages ---------- */
const SOON = {
  portfolio: ["Portfolio", "Stage 2", ["Practice balance and cash", "Every holding with profit/loss", "Full trade history", "The $80 shutdown gauge"],
    "#trade", "Until then, holdings and trades are in the Command Center's Trade tab."],
  desk: ["AI Trading Desk", "Stage 3", ["All eight agents and what each did last", "Coins they researched", "Approvals, vetoes and why", "Simulated trades they placed"],
    "#trade/desk", "Until then, the agents' case files are in the Command Center's Agent desk."],
  performance: ["Results", "Stage 4", ["Balance over time (equity chart)", "Win rate, drawdown and fees", "You vs the agents"],
    "#trade/desk", "Until then, the scoreboard is in the Command Center's Agent desk."],
};
function soonScreen(key) {
  const [title, stage, items, link, text] = SOON[key];
  show(h("h1", { class: "vt-title" }, title),
    h("section", { class: "vt-card vt-soon" }, h("div", { class: "vt-head" }, h("h2", {}, "Coming next"), h("span", { class: "vt-tag blue" }, stage)),
      h("ul", {}, items.map((i) => h("li", {}, i)))),
    h("p", { class: "muted" }, text), h("a", { class: "vt-ghost", href: "/" + link, role: "button" }, "Open the Command Center"));
}

/* ---------- router ---------- */
function quiet() { return document.hidden || document.querySelector(".vt-layer") || (document.activeElement && document.activeElement.type === "search"); }
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
  } else if (SOON[tab]) {
    soonScreen(tab);
  } else {
    await homeScreen();
    VT.timer = setInterval(() => { if (!quiet()) { homeScreen(); } }, 20000);
  }
  if (changed) { window.scrollTo(0, 0); $screen().focus({ preventScroll: true }); }
}
window.addEventListener("hashchange", () => { for (const layer of document.querySelectorAll(".vt-layer")) { layer.remove(); } route(false); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { route(true); } });
document.addEventListener("DOMContentLoaded", () => route(true));
