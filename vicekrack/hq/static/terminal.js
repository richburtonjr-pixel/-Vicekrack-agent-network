/* ViceKrack Trading terminal (read-only).
 * Shows the trading sessions the HQ already reads from disk: watchlist, research progress, selected
 * session details and a status bar. It calls only GET /api/sessions and GET /api/departments (the
 * trading agents come from the one department registry, config/departments.json).
 * There is no market-data feed here, so no prices or candles are drawn. Nothing here runs, trades or
 * changes a session.
 */
(function () {
  "use strict";

  var TOTAL_STAGES = 5;
  var state = { items: [], agents: [], agentsError: null, selected: null, error: null, loaded: false, generation: 0 };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    return n;
  }

  function words(code) {
    return String(code || "unknown").replace(/_/g, " ");
  }

  /* Colour from a real session status. Anything not recognised is grey ("waiting"), never a guess. */
  function tone(status) {
    if (status === "completed") { return "done"; }
    if (status === "failed" || status === "blocked" || status === "error") { return "bad"; }
    if (status === "running" || status === "in_progress" || status === "working") { return "busy"; }
    return "wait";
  }

  function getJSON(path) {
    return window.fetch(path, { method: "GET", credentials: "same-origin", cache: "no-store",
      headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) { throw new Error("status " + r.status); }
      return r.json();
    });
  }

  /* The trading department's agents, from the registry. On failure the panel says so; nothing is guessed. */
  function loadAgents(generation) {
    return getJSON("/api/departments").then(function (data) {
      if (generation !== state.generation) { return; }
      var all = Array.isArray(data.agents) ? data.agents : [];
      state.agents = all.filter(function (a) { return a && a.department === "trading"; });
      state.agentsError = null;
    }).catch(function () {
      if (generation !== state.generation) { return; }
      state.agents = [];
      state.agentsError = "The agent list could not be read. Nothing was changed.";
    });
  }

  function load() {
    var generation = ++state.generation;
    var agentsLoaded = loadAgents(generation);
    return getJSON("/api/sessions").then(function (data) {
      if (generation !== state.generation) { return; }
      state.items = Array.isArray(data.items) ? data.items : [];
      state.error = null;
    }).catch(function () {
      if (generation !== state.generation) { return; }
      state.items = [];
      state.error = "The session list could not be read. Nothing was changed.";
    }).then(function () { return agentsLoaded; }).then(function () {
      if (generation !== state.generation) { return; }
      state.loaded = true;
      if (!state.selected || !state.items.some(function (i) { return i.session_id === state.selected; })) {
        state.selected = state.items.length ? state.items[0].session_id : null;
      }
      render();
    });
  }

  function selectedItem() {
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].session_id === state.selected) { return state.items[i]; }
    }
    return null;
  }

  function statusBar(root) {
    var bar = el("div", "term-status");
    bar.appendChild(el("span", "term-brand", "ViceKrack Trading"));
    bar.appendChild(el("span", "term-pill wait", "Market data: not connected"));
    bar.appendChild(el("span", "term-pill", "Read-only · no orders"));
    bar.appendChild(el("span", "term-pill", state.items.length + " session" + (state.items.length === 1 ? "" : "s") + " on disk"));
    root.appendChild(bar);
  }

  function watchlist(root) {
    var box = el("section", "term-panel term-watch");
    box.appendChild(el("h3", "term-h", "Watchlist · research progress"));
    if (state.error) { box.appendChild(el("p", "term-note bad", state.error)); }
    if (!state.items.length && !state.error) {
      box.appendChild(el("p", "term-note", "No trading sessions yet."));
    }
    var list = el("ul", "term-list");
    state.items.forEach(function (item) {
      var li = el("li");
      var btn = el("button", "term-row" + (item.session_id === state.selected ? " on" : ""));
      btn.type = "button";
      btn.setAttribute("aria-pressed", item.session_id === state.selected ? "true" : "false");
      var head = el("div", "term-row-head");
      head.appendChild(el("strong", null, item.symbol || item.session_id));
      head.appendChild(el("span", "term-muted", item.interval || ""));
      btn.appendChild(head);
      var t = tone(item.status);
      var pct = Math.round(((item.stages_completed || 0) / TOTAL_STAGES) * 100);
      var bar = el("div", "term-bar " + t);
      var fill = el("span");
      fill.style.width = (item.readable === false ? 0 : pct) + "%";
      bar.appendChild(fill);
      btn.appendChild(bar);
      btn.appendChild(el("div", "term-row-foot", item.readable === false
        ? "Unreadable session" : (item.stages_completed || 0) + " of " + TOTAL_STAGES + " stages · " + words(item.status)));
      btn.addEventListener("click", function () { state.selected = item.session_id; render(); });
      li.appendChild(btn);
      list.appendChild(li);
    });
    box.appendChild(list);
    root.appendChild(box);
  }

  function chart(root) {
    var box = el("section", "term-panel term-chart");
    var item = selectedItem();
    box.appendChild(el("h3", "term-h", item ? (item.symbol || item.session_id) + " · " + (item.interval || "") : "No asset selected"));
    var empty = el("div", "term-empty");
    empty.appendChild(el("strong", null, "No market data connected"));
    empty.appendChild(el("p", null, "Prices and candles appear here only when a market-data provider is configured. "
      + "Nothing is drawn or estimated in its place."));
    box.appendChild(empty);
    root.appendChild(box);
  }

  function agents(root) {
    var box = el("section", "term-panel term-agents");
    box.appendChild(el("h3", "term-h", "Trading agents"));
    if (state.agentsError) { box.appendChild(el("p", "term-note bad", state.agentsError)); }
    var list = el("ul", "term-list");
    state.agents.forEach(function (a) {
      var li = el("li", "term-agent");
      li.appendChild(el("span", "term-dot wait"));
      li.appendChild(el("strong", null, a.name));
      li.appendChild(el("span", "term-muted", "No activity available"));
      list.appendChild(li);
    });
    box.appendChild(list);
    root.appendChild(box);
  }

  function pipeline(root) {
    var box = el("section", "term-panel term-pipe");
    var item = selectedItem();
    box.appendChild(el("h3", "term-h", "Research pipeline"));
    if (!item) {
      box.appendChild(el("p", "term-note", "Select a session to see its research progress."));
      root.appendChild(box);
      return;
    }
    var done = item.stages_completed || 0;
    var t = tone(item.status);
    box.appendChild(el("p", "term-note", (item.session_id) + " · " + words(item.status) + " · "
      + done + " of " + TOTAL_STAGES + " stages complete"));
    var bar = el("div", "term-bar " + t);
    var fill = el("span");
    fill.style.width = (item.readable === false ? 0 : Math.round((done / TOTAL_STAGES) * 100)) + "%";
    bar.appendChild(fill);
    box.appendChild(bar);
    root.appendChild(box);
  }

  function render() {
    var host = $("terminal-body");
    if (!host) { return; }
    host.textContent = "";
    var root = el("div", "term-root");
    statusBar(root);
    var grid = el("div", "term-grid");
    watchlist(grid);
    chart(grid);
    agents(grid);
    root.appendChild(grid);
    pipeline(root);
    host.appendChild(root);
  }

  window.HQTerminal = { load: load, render: render };
}());
