/* ViceKrack Mobile Command Center: Trade tab (Trading PR 4). PRACTICE MONEY ONLY.
 * Layout like a crypto exchange app: balance, holdings, every Kraken USD coin, coin page with chart, buy/sell.
 * Every price and balance comes from the PC; orders are priced on the PC, never here. Text is inserted as text. */
"use strict";

const TRADE = { markets: null, filter: "all", query: "", shown: 60, range: "1D", account: null };

/* ---------- formatting ---------- */
function usd(value) {
  const n = Number(value);
  if (!isFinite(n)) { return "—"; }
  if (Math.abs(n) >= 1) { return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  if (n === 0) { return "$0.00"; }
  const places = Math.min(12, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(n)))));
  return (n < 0 ? "−$" : "$") + Math.abs(n).toFixed(places).replace(/(\.\d*?[1-9])0+$/, "$1");
}
function cents(value) {
  const n = Number(value);
  return isFinite(n) ? (n < 0 ? "−$" : "$") + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}
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
function change(pct) {
  if (pct === null || pct === undefined) { return h("span", { class: "chg flat" }, "—"); }
  const n = Number(pct);
  return h("span", { class: "chg " + (n > 0 ? "up" : n < 0 ? "down" : "flat") }, (n > 0 ? "+" : "") + n.toFixed(2) + "%");
}
function badge(base) { return h("span", { class: "coin-badge", "aria-hidden": "true" }, base.slice(0, 4)); }
function baseOf(symbol) { return symbol.replace(/USD$/, ""); }
function practicePill() { return h("span", { class: "pill practice" }, "Practice money"); }
function tradeError(result) {
  if (result.status === 0) { return "No answer from your PC. Check Activity before trying again."; }
  return (result.body.error && result.body.error.message) || "That didn't work. Nothing was traded.";
}

/* ---------- Trade home ---------- */
async function tradeHome() {
  const [account, markets] = await Promise.all([call("/m/api/trade/account"), call("/m/api/trade/markets")]);
  if (account.status === 401 || markets.status === 401) { pairScreen(); return; }
  if (!account.ok) { show(h("h1", { class: "page" }, "Trade"), note(tradeError(account), "bad")); return; }
  TRADE.account = account.body;
  if (markets.ok) { TRADE.markets = markets.body; }
  const nodes = [h("div", { class: "trade-top" }, h("h1", { class: "page" }, "Trade"), practicePill())];
  if (account.body.needs_start || !account.body.account) {
    nodes.push(startCard(account.body));
  } else {
    nodes.push(balanceCard(account.body), holdingsCard(account.body),
      h("div", { class: "trade-links" },
        h("a", { class: "link-card", href: "#trade/desk" }, h("strong", {}, "Agent desk"), h("span", { class: "muted" }, deskLine(account.body))),
        h("a", { class: "link-card", href: "#trade/activity" }, h("strong", {}, "Activity"), h("span", { class: "muted" }, account.body.trades.length + " recent trades"))));
  }
  nodes.push(marketsCard());
  show(nodes);
}
function deskLine(body) {
  const w = body.watch || {};
  if (!w.last_cycle_at) { return "Watch isn't running"; }
  const picks = ((w.radar || {}).candidates || []).length;
  return "Checked " + ago(w.last_cycle_at) + " · " + picks + " streak" + (picks === 1 ? "" : "s");
}
function startCard(body) {
  const changed = body.problem && body.problem.code === "practice_config_changed";
  const button = h("button", { class: "primary", type: "button" }, "Start my $100 practice account");
  button.addEventListener("click", async () => {
    button.disabled = true;
    const r = await post("/m/api/trade/restart", { confirm: "restart-practice" });
    if (r.ok) { route(true); } else { button.disabled = false; button.after(note(tradeError(r), "bad")); }
  });
  return h("section", { class: "card" }, h("h2", {}, changed ? "Start fresh for the whole market" : "Start practicing"),
    h("p", { class: "muted" }, changed
      ? "Trading now covers every Kraken coin, so the practice account starts over at $100. Your old account is kept in history on the PC."
      : "You get $100 of pretend money. If it falls to $80, everything is sold and trading stops."),
    button);
}
function balanceCard(body) {
  const a = body.account;
  const equity = Number(a.equity), start = Number(a.starting_cash), floor = Number(a.shutdown_floor);
  const pnl = isFinite(equity) ? equity - start : null;
  const halted = a.status === "halted";
  const room = isFinite(equity) ? Math.max(0, Math.min(100, ((equity - floor) / (start - floor)) * 100)) : 0;
  const fuse = h("div", { class: "fuse" + (halted ? " tripped" : room < 40 ? " low" : "") }, h("i"));
  fuse.firstChild.style.width = (halted ? 0 : room) + "%";
  return h("section", { class: "balance" },
    h("p", { class: "muted" }, "Portfolio value"),
    h("p", { class: "big" }, isFinite(equity) ? usd(equity) : "Checking prices…"),
    pnl === null ? null : h("p", { class: "pnl " + (pnl >= 0 ? "up" : "down") },
      (pnl >= 0 ? "+" : "−") + cents(Math.abs(pnl)) + " (" + (pnl >= 0 ? "+" : "−") + Math.abs((pnl / start) * 100).toFixed(2) + "%) since start"),
    h("div", { class: "row" }, h("span", {}, "Cash"), h("span", { class: "val" }, usd(a.cash))),
    h("div", { class: "row" }, h("span", {}, halted ? "Stopped by the $80 shutdown" : "$80 shutdown"),
      h("span", { class: "val" }, halted ? "Tripped " + ago(a.halt.at) : a.dollars_above_floor === null ? "Waiting for prices" : usd(a.dollars_above_floor) + " above")),
    fuse,
    halted ? startAgain() : null,
    a.guard === "unverified" && !halted ? note("Some prices are over 10 minutes old, so new buys are paused. Is the Trading Watch running on the PC?") : null);
}
function startAgain() {
  const button = h("button", { class: "ghost", type: "button" }, "Start a fresh $100 account");
  button.addEventListener("click", async () => {
    if (!window.confirm("Start over with $100 of practice money? The stopped account stays in history on the PC.")) { return; }
    const r = await post("/m/api/trade/restart", { confirm: "restart-practice" });
    if (r.ok) { route(true); } else { button.after(note(tradeError(r), "bad")); }
  });
  return button;
}
function holdingsCard(body) {
  if (!body.holdings.length) {
    return h("section", { class: "card" }, h("h2", {}, "Your coins"),
      h("p", { class: "muted" }, "Nothing yet. Pick a coin below and tap Buy, or let the agents find a streak."));
  }
  return h("section", { class: "card flush" }, h("h2", { class: "pad" }, "Your coins"),
    body.holdings.map((p) => {
      const value = Number(p.value), cost = Number(p.cost_basis);
      const pct = isFinite(value) && cost > 0 ? ((value - cost) / cost) * 100 : null;
      return h("a", { class: "coin-row", href: "#trade/coin/" + p.symbol }, badge(baseOf(p.symbol)),
        h("span", { class: "who" }, h("strong", {}, baseOf(p.symbol)),
          h("span", { class: "muted" }, qty(p.quantity) + " · " + (p.opened_by === "you" ? "You" : "Agents"))),
        h("span", { class: "num" }, h("strong", {}, isFinite(value) ? usd(value) : "—"), change(pct)));
    }));
}
function marketsCard() {
  const search = h("input", { type: "search", class: "search", placeholder: "Search coins", "aria-label": "Search coins",
                              value: TRADE.query, autocomplete: "off", autocapitalize: "characters" });
  const list = h("div", { class: "market-list" });
  const chips = [["all", "All"], ["gainers", "Gainers"], ["losers", "Losers"], ["radar", "Agents' radar"]].map(([key, label]) => {
    const chip = h("button", { type: "button", class: "tchip" + (TRADE.filter === key ? " on" : ""), "aria-pressed": String(TRADE.filter === key) }, label);
    chip.addEventListener("click", () => { TRADE.filter = key; TRADE.shown = 60; route(true); });
    return chip;
  });
  const draw = () => list.replaceChildren(...marketRows());
  search.addEventListener("input", () => { TRADE.query = search.value.trim().toUpperCase(); TRADE.shown = 60; draw(); });
  draw();
  return h("section", { class: "card flush" },
    h("div", { class: "pad market-head" }, h("h2", {}, "Markets"),
      h("span", { class: "muted" }, TRADE.markets ? TRADE.markets.coins.length + " coins · " + ago(TRADE.markets.updated_at) : "Kraken unreachable")),
    h("div", { class: "pad" }, search), h("div", { class: "chips pad" }, chips), list);
}
function marketRows() {
  if (!TRADE.markets) { return [h("p", { class: "muted pad" }, "Couldn't load Kraken prices. Try again in a minute.")]; }
  let coins = TRADE.markets.coins;
  if (TRADE.query) { coins = coins.filter((c) => c.base.includes(TRADE.query) || c.name.toUpperCase().includes(TRADE.query)); }
  if (TRADE.filter === "gainers") { coins = coins.filter((c) => Number(c.change_today_pct) > 0).sort((a, b) => b.change_today_pct - a.change_today_pct); }
  if (TRADE.filter === "losers") { coins = coins.filter((c) => Number(c.change_today_pct) < 0).sort((a, b) => a.change_today_pct - b.change_today_pct); }
  if (TRADE.filter === "radar") {
    const watching = TRADE.markets.agents_watching || [];
    coins = coins.filter((c) => watching.includes(c.symbol));
    if (!coins.length) { return [h("p", { class: "muted pad" }, "No streaks on the agents' radar right now. They check every minute while the Trading Watch runs.")]; }
  }
  const rows = coins.slice(0, TRADE.shown).map((c) => h("a", { class: "coin-row", href: "#trade/coin/" + c.symbol }, badge(c.base),
    h("span", { class: "who" }, h("strong", {}, c.base), h("span", { class: "muted" }, c.name !== c.base ? c.name : compact(c.volume_24h_usd) + " vol")),
    h("span", { class: "num" }, h("strong", {}, usd(c.last)), change(c.change_today_pct))));
  if (coins.length > TRADE.shown) {
    const more = h("button", { type: "button", class: "ghost more" }, "Show more (" + (coins.length - TRADE.shown) + ")");
    more.addEventListener("click", () => { TRADE.shown += 100; more.parentNode.replaceChildren(...marketRows()); });
    rows.push(more);
  }
  if (!rows.length) { rows.push(h("p", { class: "muted pad" }, "No coins match.")); }
  return rows;
}

/* ---------- coin page ---------- */
async function coinPage(symbol) {
  const r = await call("/m/api/trade/coin?symbol=" + encodeURIComponent(symbol) + "&range=" + TRADE.range);
  if (r.status === 401) { pairScreen(); return; }
  if (!r.ok) { show(backTo("Trade", "#trade"), note(tradeError(r), "bad")); return; }
  const c = r.body, t = c.ticker || {};
  const base = baseOf(symbol);
  const readout = h("p", { class: "big" }, usd(t.last));
  const ranges = ["1H", "1D", "1W", "1M", "1Y"].map((key) => {
    const chip = h("button", { type: "button", class: "tchip" + (TRADE.range === key ? " on" : "") }, key);
    chip.addEventListener("click", () => { TRADE.range = key; route(true); });
    return chip;
  });
  const holding = c.holding;
  const nodes = [backTo("Trade", "#trade"),
    h("div", { class: "coin-title" }, badge(base), h("div", {}, h("h1", { class: "page" }, base), h("p", { class: "muted" }, t.name || base))),
    readout, h("p", {}, change(t.change_today_pct), h("span", { class: "muted" }, " today (UTC)")),
    priceChart(c.chart, readout, t.last), h("div", { class: "chips" }, ranges),
    h("section", { class: "card" },
      h("div", { class: "row" }, h("span", {}, "Buy price (ask)"), h("span", { class: "val" }, usd(t.ask))),
      h("div", { class: "row" }, h("span", {}, "Sell price (bid)"), h("span", { class: "val" }, usd(t.bid))),
      h("div", { class: "row" }, h("span", {}, "24h high / low"), h("span", { class: "val" }, usd(t.high_24h) + " / " + usd(t.low_24h))),
      h("div", { class: "row" }, h("span", {}, "24h volume"), h("span", { class: "val" }, compact(t.volume_24h_usd))))];
  if (holding) {
    const value = Number(holding.quantity) * Number(t.bid);
    const pnl = value - Number(holding.cost_basis);
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Your " + base),
      h("div", { class: "row" }, h("span", {}, "Amount"), h("span", { class: "val" }, qty(holding.quantity) + " " + base)),
      h("div", { class: "row" }, h("span", {}, "Worth (at sell price)"), h("span", { class: "val" }, usd(value))),
      h("div", { class: "row" }, h("span", {}, "Profit / loss"), h("span", { class: "val " + (pnl >= 0 ? "up" : "down") }, (pnl >= 0 ? "+" : "−") + cents(Math.abs(pnl)))),
      h("div", { class: "row" }, h("span", {}, "Bought by"), h("span", { class: "val" }, holding.opened_by === "you" ? "You" : "The agents"))));
  }
  if (c.agents) {
    const verdict = c.agents.verdict === "sufficient_for_future_paper_evaluation" ? "Agents liked it" : "Agents passed";
    nodes.push(h("section", { class: "card" }, h("h2", {}, "What the agents saw"),
      h("p", { class: "muted" }, verdict + " · checked " + ago(c.agents.checked_at)),
      c.agents.radar ? h("p", {}, "Streak: +" + c.agents.radar.rise_5m_pct + "% in 5 min, +" + c.agents.radar.rise_15m_pct + "% in 15 min") : null,
      Object.entries(c.agents.summaries || {}).map(([role, text]) => h("p", { class: "muted" }, words(role) + ": " + text))));
  }
  const agentsHold = holding && holding.opened_by !== "you";
  const buy = h("button", { class: "buy-btn", type: "button", disabled: t.tradeable && !agentsHold ? null : true }, "Buy");
  const sell = h("button", { class: "sell-btn", type: "button", disabled: holding ? null : true }, "Sell");
  buy.addEventListener("click", () => orderSheet(symbol, "buy", t, holding));
  sell.addEventListener("click", () => orderSheet(symbol, "sell", t, holding));
  if (!t.tradeable) { nodes.push(note("This coin's price is too tiny to practice-trade accurately. You can still watch it.")); }
  else if (agentsHold) { nodes.push(note("The agents are holding " + base + " right now. You can buy it after they sell, or sell theirs.")); }
  nodes.push(h("div", { class: "trade-bar" }, buy, sell));
  show(nodes);
}
function priceChart(points, readout, last) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 320 160");
  svg.setAttribute("class", "chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Price chart");
  if (!points || points.length < 2) { return h("p", { class: "muted" }, "No chart data for this range."); }
  const closes = points.map((p) => Number(p[1]));
  const low = Math.min(...closes), high = Math.max(...closes), span = high - low || 1;
  const x = (i) => (i / (closes.length - 1)) * 320, y = (v) => 150 - ((v - low) / span) * 140;
  const up = closes[closes.length - 1] >= closes[0];
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", closes.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" "));
  line.setAttribute("class", "line " + (up ? "up" : "down"));
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("r", "4");
  dot.setAttribute("class", "dot " + (up ? "up" : "down"));
  dot.setAttribute("cx", "-10");
  svg.append(line, dot);
  const scrub = (event) => {
    const box = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(closes.length - 1, Math.round(((event.clientX - box.left) / box.width) * (closes.length - 1))));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(closes[i]));
    readout.textContent = usd(closes[i]) + "  ·  " + new Date(points[i][0] * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  svg.addEventListener("pointermove", scrub);
  svg.addEventListener("pointerdown", scrub);
  svg.addEventListener("pointerleave", () => { dot.setAttribute("cx", "-10"); readout.textContent = usd(last); });
  return svg;
}

/* ---------- buy / sell sheet ---------- */
function orderSheet(symbol, side, t, holding) {
  const base = baseOf(symbol);
  const cash = Number(((TRADE.account || {}).account || {}).cash || 0);
  const worth = holding ? Number(holding.quantity) * Number(t.bid) : 0;
  const price = Number(side === "buy" ? t.ask : t.bid);
  const input = h("input", { type: "text", inputmode: "decimal", class: "amount", "aria-label": "Amount in dollars", placeholder: "0.00", autocomplete: "off" });
  let all = false;
  const estimate = h("p", { class: "muted" });
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const update = () => {
    const dollars = all ? (side === "buy" ? cash / 1.0025 : worth) : Number(input.value);
    error.hidden = true;
    estimate.textContent = dollars > 0 && price > 0
      ? (side === "buy" ? "You get about " : "You sell about ") + qty(dollars / price) + " " + base + " at ~" + usd(price) + " · fee about " + cents(Math.max(0.01, dollars * 0.0025))
      : (side === "buy" ? "Cash available: " + usd(cash) : "You have " + usd(worth) + " of " + base);
  };
  const quick = (side === "buy" ? [["$5", "5"], ["$10", "10"], ["$25", "25"], ["Max", "all"]] : [["25%", 0.25], ["50%", 0.5], ["All", "all"]]).map(([label, value]) => {
    const chip = h("button", { type: "button", class: "tchip" }, label);
    chip.addEventListener("click", () => {
      all = value === "all";
      input.value = all ? "" : side === "buy" ? value : (worth * value).toFixed(2);
      input.placeholder = all ? (side === "buy" ? "Max " + usd(cash) : "All " + usd(worth)) : "0.00";
      update();
    });
    return chip;
  });
  input.addEventListener("input", () => { all = false; input.placeholder = "0.00"; update(); });
  const confirm = h("button", { class: side === "buy" ? "buy-btn" : "sell-btn", type: "button" }, (side === "buy" ? "Buy " : "Sell ") + base);
  const cancel = h("button", { class: "ghost", type: "button" }, "Cancel");
  const sheet = h("div", { class: "layer", role: "dialog", "aria-modal": "true", "aria-label": (side === "buy" ? "Buy " : "Sell ") + base },
    h("div", { class: "sheet" }, h("div", { class: "trade-top" }, h("h2", {}, (side === "buy" ? "Buy " : "Sell ") + base), practicePill()),
      h("label", { class: "dollar" }, h("span", { "aria-hidden": "true" }, "$"), input),
      h("div", { class: "chips" }, quick), estimate, error,
      h("p", { class: "muted small" }, "Priced on your PC from Kraken the moment you confirm. Pretend money only; the $80 shutdown still applies."),
      confirm, cancel));
  const close = () => sheet.remove();
  cancel.addEventListener("click", close);
  confirm.addEventListener("click", async () => {
    const amount = all ? "all" : input.value.trim();
    if (!all && !(/^\d+(\.\d{1,2})?$/.test(amount) && Number(amount) > 0)) {
      error.textContent = "Enter a dollar amount, like 20 or 12.50."; error.hidden = false; return;
    }
    if (side === "buy" && !all && Number(amount) > cash) { error.textContent = "That's more than your " + usd(cash) + " cash."; error.hidden = false; return; }
    confirm.disabled = true;
    busy(side === "buy" ? "Buying " + base + "…" : "Selling " + base + "…");
    const r = await post("/m/api/trade/order", { symbol, side, amount });
    busy(null);
    confirm.disabled = false;
    if (!r.ok) { error.textContent = tradeError(r); error.hidden = false; return; }
    if (!r.body.ok) { error.textContent = (r.body.refused && r.body.refused.message) || "Not traded."; error.hidden = false; return; }
    close();
    const f = r.body.fill;
    TRADE.flash = f ? (f.side === "buy" ? "Bought " : "Sold ") + qty(f.quantity) + " " + base + " for " + usd(f.notional) + " (fee " + usd(f.fee) + ")." : "Done.";
    route(true);
  });
  document.body.append(sheet);
  update();
  input.focus();
}

/* ---------- agent desk and activity ---------- */
const CASE_TONE = { executed: "green", vetoed: "gray", refused: "red", waiting: "orange", approved_not_executed: "blue" };
async function deskPage() {
  const r = await call("/m/api/trade/account");
  if (!r.ok) { show(backTo("Trade", "#trade"), note(tradeError(r), "bad")); return; }
  const b = r.body, w = b.watch || {}, radar = w.radar || {}, stats = radar.stats || {};
  const nodes = [backTo("Trade", "#trade"), h("h1", { class: "page" }, "Agent desk"),
    h("section", { class: "card" }, h("h2", {}, "Radar"),
      h("p", { class: "muted" }, w.last_cycle_at ? "Last check " + ago(w.last_cycle_at) + " · every minute" : "The Trading Watch isn't running on the PC."),
      radar.ok ? h("p", {}, "Scanned " + stats.coins_priced + " coins · " + stats.liquid + " trade enough · " + stats.candidates + " on a streak") : null,
      radar.ok && stats.with_history === 0 ? h("p", { class: "muted" }, "Warming up: streaks need 15 minutes of price history.") : null,
      (radar.candidates || []).map((c) => h("a", { class: "coin-row", href: "#trade/coin/" + c.symbol }, badge(c.base),
        h("span", { class: "who" }, h("strong", {}, c.base), h("span", { class: "muted" }, c.up_minutes_of_last_5 + " of last 5 min up")),
        h("span", { class: "num" }, change(c.rise_15m_pct), h("span", { class: "muted" }, "15 min"))))),
    b.scores ? scoreCard(b.scores) : null,
    h("h2", {}, "Case files")];
  if (!(b.cases || []).length) { nodes.push(h("p", { class: "muted" }, "No trade ideas yet. When a streak passes research, every agent signs it here.")); }
  for (const c of b.cases || []) {
    nodes.push(h("section", { class: "card" },
      h("div", { class: "row" }, h("strong", {}, (c.kind === "entry" ? "Buy idea · " : "Sell · ") + baseOf(c.symbol)),
        h("span", { class: "status " + (CASE_TONE[c.outcome] || "gray") }, words(c.outcome))),
      h("p", { class: "muted" }, ago(c.opened_at)),
      h("ul", { class: "signatures" }, c.signatures.map((s) => h("li", { class: s.decision },
        h("strong", {}, words(s.agent) + (s.stage ? " (account)" : "")), " " + words(s.decision) + (s.reasons && s.reasons.length ? " · " + s.reasons.map(words).join(", ") : ""))))));
  }
  show(nodes);
}
function scoreCard(scores) {
  const line = (label, s) => h("div", { class: "row" }, h("span", {}, label),
    h("span", { class: "val" }, s.closed_trades ? s.wins + "W " + s.losses + "L · " + (Number(s.realized_pnl) >= 0 ? "+" : "−") + cents(Math.abs(Number(s.realized_pnl))) : "No closed trades"));
  return h("section", { class: "card" }, h("h2", {}, "Scoreboard"), line("You", scores.you), line("Agents", scores.agents),
    h("p", { class: "muted small" }, "Practice results with pretend money; not a prediction."));
}
async function activityPage() {
  const r = await call("/m/api/trade/account");
  if (!r.ok) { show(backTo("Trade", "#trade"), note(tradeError(r), "bad")); return; }
  const trades = r.body.trades || [];
  show(backTo("Trade", "#trade"), h("h1", { class: "page" }, "Activity"),
    trades.length ? h("section", { class: "card flush" }, trades.map((t) => t.kind === "halt"
      ? h("div", { class: "coin-row" }, h("span", { class: "who" }, h("strong", {}, "$80 shutdown"), h("span", { class: "muted" }, "Everything sold · " + ago(t.at))))
      : h("a", { class: "coin-row", href: "#trade/coin/" + t.symbol }, badge(baseOf(t.symbol)),
        h("span", { class: "who" }, h("strong", {}, (t.side === "buy" ? "Bought " : "Sold ") + baseOf(t.symbol)),
          h("span", { class: "muted" }, ({ you: "You", agents: "Agents", guard: "Shutdown" }[t.by] || "") + " · " + ago(t.at))),
        h("span", { class: "num" }, h("strong", {}, usd(t.notional)),
          t.realized_pnl !== null && t.realized_pnl !== undefined ? h("span", { class: "chg " + (Number(t.realized_pnl) >= 0 ? "up" : "down") }, (Number(t.realized_pnl) >= 0 ? "+" : "−") + cents(Math.abs(Number(t.realized_pnl)))) : h("span", { class: "muted" }, qty(t.quantity))))))
      : h("p", { class: "muted" }, "No trades yet."));
}

/* ---------- router hook ---------- */
SCREENS.trade = async (rest) => {
  if (!(await ready())) { return; }
  const flashText = TRADE.flash;
  TRADE.flash = null;
  if (rest[0] === "coin" && rest[1]) {
    await coinPage(rest[1]);
    STATE.timer = setInterval(() => { if (!document.hidden && !document.querySelector(".layer")) { coinPage(rest[1]); } }, 15000);
  } else if (rest[0] === "desk") { await deskPage(); }
  else if (rest[0] === "activity") { await activityPage(); }
  else {
    await tradeHome();
    STATE.timer = setInterval(() => {
      if (!document.hidden && !document.querySelector(".layer") && document.activeElement && document.activeElement.type !== "search") { tradeHome(); }
    }, 20000);
  }
  if (flashText) { screen().prepend(note(flashText)); }
};

/* An open buy/sell sheet never outlives the page it was opened on. */
window.addEventListener("hashchange", () => { for (const layer of document.querySelectorAll(".layer")) { layer.remove(); } });
