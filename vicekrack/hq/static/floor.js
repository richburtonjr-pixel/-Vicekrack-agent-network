/* ViceKrack Trading Floor (read-only).
 * One screen: the market (live candles), the agents at work, and the practice money.
 * Calls only GET /api/trading/floor (what the Trading Watch saved on this PC) and
 * GET /api/trading/alpaca (Alpaca PAPER connection light + stock candles, cached on the server).
 * Every word about an agent comes from the watch's saved records; nothing is invented or animated
 * to look busy. Nothing here places, changes or cancels a trade.
 */
(function () {
  "use strict";

  var REFRESH_MS = 30000;
  var SVGNS = "http://www.w3.org/2000/svg";
  var state = { floor: null, alpaca: null, floorError: null, selected: null, timer: null, open: false, generation: 0 };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    if (parent) { parent.appendChild(n); }
    return n;
  }

  function svg(tag, attrs, parent) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (parent) { parent.appendChild(n); }
    return n;
  }

  function getJSON(path) {
    return window.fetch(path, { method: "GET", credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) { throw new Error("status " + r.status); }
      return r.json();
    });
  }

  function money(v) {
    if (v === null || v === undefined || v === "") { return "-"; }
    var n = Number(v);
    if (!isFinite(n)) { return String(v); }
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function price(v) {
    if (v === null || v === undefined) { return "-"; }
    var n = Number(v);
    if (!isFinite(n)) { return String(v); }
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: n < 10 ? 4 : 2 });
  }

  function ago(iso) {
    if (!iso) { return ""; }
    var t = Date.parse(iso);
    if (!isFinite(t)) { return ""; }
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) { return "just now"; }
    if (s < 3600) { return Math.round(s / 60) + " min ago"; }
    if (s < 86400) { return Math.round(s / 3600) + " hr ago"; }
    return Math.round(s / 86400) + " days ago";
  }

  function localTime(iso) {
    var t = Date.parse(iso);
    return isFinite(t) ? new Date(t).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  }

  function nice(name) {
    return ({ BTCUSD: "Bitcoin", ETHUSD: "Ethereum", SPY: "S&P 500 (SPY)", AAPL: "Apple (AAPL)" })[name] || name;
  }

  /* ------------------------------------------------------------------ data */
  function markets() {
    var list = [];
    ((state.floor && state.floor.markets) || []).forEach(function (m) { list.push(m); });
    ((state.alpaca && state.alpaca.stocks) || []).forEach(function (m) { list.push(m); });
    return list;
  }

  function load() {
    var generation = ++state.generation;
    var floor = getJSON("/api/trading/floor").then(function (d) {
      if (generation !== state.generation) { return; }
      state.floor = d; state.floorError = null;
    }).catch(function () {
      if (generation !== state.generation) { return; }
      state.floorError = "The Trading Watch records could not be read.";
    });
    var alpaca = getJSON("/api/trading/alpaca").then(function (d) {
      if (generation !== state.generation) { return; }
      state.alpaca = d;
    }).catch(function () {
      if (generation !== state.generation) { return; }
      state.alpaca = { connected: false, message: "The Alpaca check could not run." };
    });
    floor.then(function () { if (generation === state.generation) { render(); } });
    return Promise.all([floor, alpaca]).then(function () { if (generation === state.generation) { render(); } });
  }

  /* ------------------------------------------------------------------ pieces */
  function statusBar(root) {
    var bar = el("div", "fl-status", null, root);
    el("div", "fl-title", "Trading Floor", bar);
    var w = (state.floor && state.floor.watch) || {};
    var watch = el("span", "fl-pill " + (w.running ? "good" : "bad"), null, bar);
    el("span", "fl-dot", null, watch);
    el("span", null, w.running ? "Watch running · checked " + ago(w.last_cycle_at)
      : w.found ? "Watch stopped · last check " + ago(w.last_cycle_at) : "Watch not started", watch);
    var a = state.alpaca;
    var alp = el("span", "fl-pill " + (!a ? "wait" : a.connected ? "good" : "bad"), null, bar);
    el("span", "fl-dot", null, alp);
    el("span", null, !a ? "Checking Alpaca…" : a.connected ? "Alpaca connected" : "Alpaca not connected", alp);
    if (a && a.market) {
      el("span", "fl-pill " + (a.market.is_open ? "good" : "wait"), a.market.is_open ? "Stock market open" : "Stock market closed", bar);
    }
    el("span", "fl-pill sim", "Practice money only", bar);
    var spacer = el("span", "fl-spacer", null, bar); spacer.setAttribute("aria-hidden", "true");
    var btn = el("button", "fl-btn", "Refresh", bar);
    btn.type = "button";
    btn.addEventListener("click", load);
    if (w.found === false || (w.found && !w.running)) {
      el("p", "fl-help", "To see live crypto, double-click \"Start Trading Watch\" (scripts → windows) and keep that window open.", root);
    }
    if (a && !a.connected && a.message) { el("p", "fl-help", "Alpaca: " + a.message, root); }
    if (state.floorError) { el("p", "fl-help bad", state.floorError, root); }
  }

  function tickerRow(root) {
    var row = el("div", "fl-tickers", null, root);
    var list = markets();
    if (!list.length) {
      el("p", "fl-muted", "No prices yet. They appear after the watch's first check (every 5 minutes) and once Alpaca connects.", row);
      return;
    }
    if (!state.selected || !list.some(function (m) { return m.symbol === state.selected; })) { state.selected = list[0].symbol; }
    list.forEach(function (m) {
      var c = m.candles || [], first = c.length ? c[0].o : null, last = c.length ? c[c.length - 1].c : null;
      var change = first && last ? ((last - first) / first) * 100 : null;
      var b = el("button", "fl-ticker" + (m.symbol === state.selected ? " on" : ""), null, row);
      b.type = "button";
      b.setAttribute("aria-pressed", m.symbol === state.selected ? "true" : "false");
      el("span", "fl-tk-name", nice(m.symbol), b);
      el("span", "fl-tk-price", m.price !== null && m.price !== undefined ? price(m.price) : (m.error ? "unavailable" : "-"), b);
      if (change !== null) {
        el("span", "fl-tk-chg " + (change >= 0 ? "up" : "down"), (change >= 0 ? "▲ " : "▼ ") + Math.abs(change).toFixed(2) + "%", b);
      }
      el("span", "fl-tk-kind", m.kind === "crypto" ? "Crypto · 24/7" : "Stock", b);
      b.addEventListener("click", function () { state.selected = m.symbol; render(); });
    });
  }

  function chart(root) {
    var m = markets().filter(function (x) { return x.symbol === state.selected; })[0];
    var box = el("div", "fl-card fl-chart-card", null, root);
    if (!m) { el("p", "fl-muted", "The chart appears here as soon as prices arrive.", box); return; }
    var head = el("div", "fl-chart-head", null, box);
    el("h3", null, nice(m.symbol), head);
    el("span", "fl-big", price(m.price), head);
    el("span", "fl-muted", (m.source || "") + (m.price_at ? " · last candle " + localTime(m.price_at) : ""), head);
    if (m.verdict_text) { el("span", "fl-verdict", "Agents' verdict: " + m.verdict_text, head); }
    var candles = m.candles || [];
    if (candles.length < 2) {
      el("p", "fl-muted", m.error ? "Prices for this market could not be loaded right now." : "Waiting for price candles.", box);
      return;
    }
    var W = 900, H = 300, padL = 8, padR = 70, padT = 12, padB = 24;
    var hi = -Infinity, lo = Infinity;
    candles.forEach(function (c) { hi = Math.max(hi, c.h); lo = Math.min(lo, c.l); });
    var span = (hi - lo) || hi * 0.001 || 1; hi += span * 0.06; lo -= span * 0.06;
    var y = function (v) { return padT + (hi - v) / (hi - lo) * (H - padT - padB); };
    var step = (W - padL - padR) / candles.length, bw = Math.max(1.5, step * 0.62);
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, class: "fl-chart", role: "img",
      "aria-label": nice(m.symbol) + " price, last " + candles.length + " candles" }, box);
    for (var g = 0; g <= 4; g++) {
      var gv = lo + (hi - lo) * g / 4, gy = y(gv);
      svg("line", { x1: padL, x2: W - padR, y1: gy, y2: gy, class: "fl-grid" }, s);
      var t = svg("text", { x: W - padR + 6, y: gy + 4, class: "fl-axis" }, s); t.textContent = price(gv);
    }
    candles.forEach(function (c, i) {
      var x = padL + i * step + step / 2, up = c.c >= c.o;
      svg("line", { x1: x, x2: x, y1: y(c.h), y2: y(c.l), class: up ? "fl-wick up" : "fl-wick down" }, s);
      var top = y(Math.max(c.o, c.c)), h = Math.max(1, Math.abs(y(c.o) - y(c.c)));
      svg("rect", { x: x - bw / 2, y: top, width: bw, height: h, rx: 1, class: up ? "fl-body up" : "fl-body down" }, s);
    });
    var lastY = y(candles[candles.length - 1].c);
    svg("line", { x1: padL, x2: W - padR, y1: lastY, y2: lastY, class: "fl-last" }, s);
    [0, Math.floor(candles.length / 2), candles.length - 1].forEach(function (i) {
      var tt = svg("text", { x: Math.min(W - padR - 40, Math.max(padL, padL + i * step)), y: H - 6, class: "fl-axis" }, s);
      tt.textContent = localTime(candles[i].t);
    });
    // Trades the Executor placed on this market, from the practice ledger.
    var trades = ((state.floor && state.floor.trades) || []).filter(function (t) { return t.symbol === m.symbol; });
    if (trades.length) {
      el("p", "fl-muted", "Practice trades on this market: " + trades.map(function (t) {
        return (t.side === "buy" ? "bought " : "sold ") + price(t.price) + " at " + localTime(t.at);
      }).join(" · "), box);
    }
  }

  function agentsPanel(root) {
    var card = el("div", "fl-card", null, root);
    el("h3", null, "Your agents", card);
    el("p", "fl-muted", "Every 5 minutes the first four research each coin. A trade only happens when all of them, the Verifier and Risk Review approve.", card);
    var grid = el("div", "fl-agents", null, card);
    ((state.floor && state.floor.agents) || []).forEach(function (a, i) {
      var c = el("div", "fl-agent " + (a.state === "worked" ? "worked" : "idle"), null, grid);
      var top = el("div", "fl-agent-top", null, c);
      el("span", "fl-agent-num", String(i + 1), top);
      el("strong", null, a.name, top);
      if (a.at) { el("span", "fl-agent-at", ago(a.at), top); }
      el("div", "fl-agent-job", a.job, c);
      el("div", "fl-agent-now", a.headline, c);
      var d = a.last_decision;
      if (d && (a.id === "verifier" || a.id === "risk_review" || a.id === "executor" || a.id === "position_monitor")) {
        el("div", "fl-agent-dec " + d.decision, "Last call (" + d.symbol + ", " + localTime(d.at) + "): " + d.decision +
          (d.reasons && d.reasons.length ? " - " + d.reasons.join(", ") : ""), c);
      }
    });
  }

  function moneyPanel(root) {
    var card = el("div", "fl-card", null, root);
    el("h3", null, "Practice money", card);
    var m = state.floor && state.floor.money;
    if (!m) {
      el("p", "fl-muted", "The practice account appears after the watch's first check.", card);
    } else {
      var big = el("div", "fl-equity", null, card);
      el("span", "fl-muted", "Worth now", big);
      el("span", "fl-big", money(m.equity), big);
      var change = Number(m.equity) - Number(m.starting_cash);
      if (isFinite(change)) { el("span", "fl-tk-chg " + (change >= 0 ? "up" : "down"), (change >= 0 ? "+" : "") + money(change) + " since start", big); }
      // How far above the $80 floor: a simple gauge from the floor to the starting amount.
      var floorV = Number(m.shutdown_floor), start = Number(m.starting_cash), eq = Number(m.equity);
      var pct = isFinite(eq) ? Math.max(0, Math.min(100, (eq - floorV) / (start - floorV) * 100)) : 0;
      var gauge = el("div", "fl-gauge", null, card);
      var fill = el("span", pct > 50 ? "ok" : pct > 20 ? "warn" : "bad", null, gauge);
      fill.style.width = pct.toFixed(1) + "%";
      el("p", "fl-muted", money(m.dollars_above_floor) + " above the " + money(m.shutdown_floor) +
        " safety floor. At the floor, plain code sells everything and stops.", card);
      var rows = el("dl", "fl-rows", null, card);
      [["Cash", money(m.cash)], ["Started with", money(m.starting_cash)], ["Profit from closed trades", money(m.realized_pnl)],
       ["Safety guard", m.guard === "ok" ? "OK" : String(m.guard || "-").replace(/_/g, " ")],
       ["Account", m.status === "active" ? "Active" : String(m.status || "-")]].forEach(function (r) {
        el("dt", null, r[0], rows); el("dd", null, r[1], rows);
      });
      var pos = m.positions || {}, names = Object.keys(pos);
      el("h4", null, "Open trades", card);
      if (!names.length) { el("p", "fl-muted", "None right now.", card); }
      names.forEach(function (n) {
        var p = pos[n];
        el("p", "fl-pos", nice(n) + ": " + p.quantity + " (cost " + money(p.cost_basis) + ")", card);
      });
      var w = state.floor.watch || {};
      if (w.auto_practice_trades === false) {
        el("p", "fl-help", "Automatic practice trades are switched off, so approved ideas are recorded but not placed.", card);
      }
    }
    var a = state.alpaca;
    if (a && a.connected && a.account) {
      var ac = el("div", "fl-alpaca", null, card);
      el("h4", null, "Alpaca paper account", ac);
      var r2 = el("dl", "fl-rows", null, ac);
      [["Practice balance", money(a.account.equity)], ["Buying power", money(a.account.buying_power)],
       ["Status", a.account.status === "ACTIVE" ? "Active" : a.account.status]].forEach(function (r) {
        el("dt", null, r[0], r2); el("dd", null, r[1], r2);
      });
      el("p", "fl-muted", "Connected and ready. The agents do not trade stocks yet; that is the next step.", ac);
    }
  }

  function decisionsPanel(root) {
    var card = el("div", "fl-card", null, root);
    el("h3", null, "Trade decisions", card);
    var list = (state.floor && state.floor.decisions) || [];
    if (!list.length) {
      el("p", "fl-muted", "No trade ideas yet. A case only opens when the research says a coin looks good, or an open trade needs closing. Quiet is normal.", card);
    }
    list.forEach(function (c) {
      var item = el("details", "fl-case " + (c.outcome || ""), null, card);
      var sum = el("summary", null, null, item);
      el("strong", null, (c.kind === "exit" ? "Sell " : "Buy ") + nice(c.symbol), sum);
      el("span", "fl-case-out", c.outcome_text || "", sum);
      el("span", "fl-muted", localTime(c.at) + (c.price ? " · " + price(c.price) : ""), sum);
      var ol = el("ol", "fl-steps", null, item);
      (c.steps || []).forEach(function (s) {
        var li = el("li", "fl-step " + s.decision, null, ol);
        el("strong", null, String(s.agent || "").replace(/_/g, " ") + ": " + s.decision, li);
        if (s.summary) { el("span", null, " " + s.summary, li); }
      });
    });
    var trades = (state.floor && state.floor.trades) || [];
    if (trades.length) {
      el("h4", null, "Recent practice trades", card);
      var t = el("table", "fl-table", null, card);
      var hr = el("tr", null, null, el("thead", null, null, t));
      ["Time", "Market", "Side", "Price", "Amount", "Profit"].forEach(function (h) { el("th", null, h, hr); });
      var tb = el("tbody", null, null, t);
      trades.forEach(function (x) {
        var tr = el("tr", null, null, tb);
        [localTime(x.at), nice(x.symbol), x.side, price(x.price), money(x.dollars), x.profit === null ? "-" : money(x.profit)]
          .forEach(function (v) { el("td", null, v, tr); });
      });
    }
  }

  function render() {
    var body = $("floor-body");
    if (!body || !state.open) { return; }
    var scrollY = body.scrollTop;
    body.textContent = "";
    var root = el("div", "fl-root", null, body);
    statusBar(root);
    tickerRow(root);
    var main = el("div", "fl-main", null, root);
    var left = el("div", "fl-left", null, main);
    chart(left);
    agentsPanel(left);
    var right = el("div", "fl-right", null, main);
    moneyPanel(right);
    decisionsPanel(right);
    body.scrollTop = scrollY;
  }

  function open() {
    state.open = true;
    render();
    load();
    if (!state.timer) { state.timer = window.setInterval(load, REFRESH_MS); }
  }

  function close() {
    state.open = false;
    if (state.timer) { window.clearInterval(state.timer); state.timer = null; }
  }

  window.HQFloor = { open: open, close: close, load: load };
}());
