/* ViceKrack Living HQ core logic (Step 32). Pure functions only: no DOM, no network.
 * Loaded by the page as a classic script (window.HQCore) and by Node tests (module.exports).
 *
 * Rules that keep the house honest:
 * - Status always comes from the scene frame at the replay position (or "now" data);
 *   movement is cosmetic and never changes a status.
 * - Decorative idle behaviour is a seeded, local choice of where to stand. It has no
 *   access to the network or the scene's events and triggers nothing.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) { module.exports = api; } else { root.HQCore = api; }
}(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var STATES = {
    idle: { label: "Idle", color: "#9aa4b2", icon: "idle" },
    working: { label: "Working", color: "#2fd4e6", icon: "gear" },
    waiting: { label: "Waiting", color: "#f5b942", icon: "hourglass" },
    blocked: { label: "Blocked", color: "#f2723c", icon: "noentry" },
    completed: { label: "Complete", color: "#3ddc84", icon: "check" },
    failed: { label: "Failed", color: "#ff4d6d", icon: "cross" },
    unknown: { label: "Unknown", color: "#8b93a7", icon: "question" }
  };
  var STATE_ORDER = ["idle", "working", "waiting", "blocked", "completed", "failed", "unknown"];
  var MODES = {
    demo: { badge: "DEMO DATA · SYNTHETIC", tone: "demo" },
    recorded_replay: { badge: "RECORDED REPLAY", tone: "recorded" },
    reconstructed: { badge: "RECONSTRUCTED HISTORY", tone: "reconstructed" },
    observed: { badge: "LIVE OBSERVED", tone: "live" }
  };
  var SPEEDS = [0.5, 1, 2, 4, 8];
  var ID_PATTERN = /^(demo|tl-[0-9a-f]{24}|rar-[0-9a-f]{24}|srun-[0-9a-f]{24}|prod-[0-9a-f]{24}|wfr-[0-9a-f]{32})$/;

  function stateStyle(state) { return STATES[state] || STATES.unknown; }

  /* Where a bot should be for a recorded state. Decoration only applies to "free". */
  function intentFor(state, completedLingerOver) {
    switch (state) {
      case "working": return "station";
      case "failed": return "station";
      case "completed": return completedLingerOver ? "free" : "station";
      case "blocked": return "door";
      case "waiting": return "seat";
      case "unknown": return "hold";
      default: return "free";
    }
  }

  /* ---------------------------------------------------------------- replay */
  function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }

  function Replay(count, options) {
    options = options || {};
    this.count = Math.max(0, count | 0);
    this.index = -1;                 // -1 = before the first recorded event
    this.playing = false;
    this.speed = 1;
    this.baseMs = options.baseMs || 1100;
    this.loop = !!options.loop;
    this.loopPauseMs = options.loopPauseMs || 3500;
    this.acc = 0;
  }
  Replay.prototype.seek = function (index) {
    this.index = clamp(Math.round(Number(index) || 0), -1, this.count - 1);
    this.acc = 0;
    return this.index;
  };
  Replay.prototype.step = function (delta) { return this.seek(this.index + delta); };
  Replay.prototype.restart = function () { return this.seek(-1); };
  Replay.prototype.toEnd = function () { return this.seek(this.count - 1); };
  Replay.prototype.play = function () {
    if (this.count === 0) { return false; }
    if (this.index >= this.count - 1) { this.seek(-1); }
    this.playing = true;
    return true;
  };
  Replay.prototype.pause = function () { this.playing = false; };
  Replay.prototype.setSpeed = function (speed) {
    speed = Number(speed);
    if (SPEEDS.indexOf(speed) >= 0) { this.speed = speed; }
    return this.speed;
  };
  Replay.prototype.setCount = function (count) {
    this.count = Math.max(0, count | 0);
    this.index = clamp(this.index, -1, this.count - 1);
  };
  /* Advance by elapsed time. Returns the number of events advanced (0 if none). */
  Replay.prototype.tick = function (dtMs) {
    if (!this.playing || this.count === 0) { return 0; }
    this.acc += Math.max(0, Number(dtMs) || 0) * this.speed;
    var advanced = 0;
    while (true) {
      if (this.index < this.count - 1) {
        if (this.acc < this.baseMs) { break; }
        this.acc -= this.baseMs;
        this.index += 1;
        advanced += 1;
      } else if (this.loop) {
        if (this.acc < this.loopPauseMs) { break; }
        this.acc = 0;
        this.index = -1;
        advanced += 1;
      } else {
        this.playing = false;
        this.acc = 0;
        break;
      }
    }
    return advanced;
  };

  /* ---------------------------------------------------------------- paths */
  function Graph(nodes, edges) {
    this.nodes = nodes;
    this.adj = {};
    var self = this;
    Object.keys(nodes).forEach(function (id) { self.adj[id] = []; });
    edges.forEach(function (edge) {
      if (!(edge[0] in nodes) || !(edge[1] in nodes)) { throw new Error("edge to unknown node"); }
      self.adj[edge[0]].push(edge[1]);
      self.adj[edge[1]].push(edge[0]);
    });
  }
  Graph.prototype.path = function (from, to) {
    if (!(from in this.nodes) || !(to in this.nodes)) { return null; }
    if (from === to) { return [from]; }
    var previous = {}, queue = [from], seen = {};
    seen[from] = true;
    while (queue.length) {
      var current = queue.shift();
      var next = this.adj[current];
      for (var i = 0; i < next.length; i += 1) {
        if (seen[next[i]]) { continue; }
        seen[next[i]] = true;
        previous[next[i]] = current;
        if (next[i] === to) {
          var route = [to];
          while (route[0] !== from) { route.unshift(previous[route[0]]); }
          return route;
        }
        queue.push(next[i]);
      }
    }
    return null;
  };
  Graph.prototype.nearest = function (point) {
    var best = null, bestDistance = Infinity, self = this;
    Object.keys(this.nodes).forEach(function (id) {
      var node = self.nodes[id];
      var d = Math.pow(node.x - point.x, 2) + Math.pow(node.y - point.y, 2) + Math.pow((node.z - point.z) * 2, 2);
      if (d < bestDistance) { bestDistance = d; best = id; }
    });
    return best;
  };

  /* ---------------------------------------------------------------- decoration */
  function hashSeed(text) {
    var h = 2166136261;
    for (var i = 0; i < text.length; i += 1) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function rng(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  /* Deterministic idle choice for (bot, cycle): a spot and a dwell time. Decoration only.
   * Occupancy (Step 33): spots in `taken` (claimed by other bots) are skipped, so idle bots spread
   * out; if every spot is taken the bot uses its first (own) spot. */
  function wanderChoice(botId, cycle, spots, taken) {
    var random = rng(hashSeed(botId + ":" + cycle));
    if (taken) {
      var free = spots.filter(function (spot) { return !taken[spot.node]; });
      spots = free.length ? free : spots.slice(0, 1);
    }
    var total = 0;
    spots.forEach(function (spot) { total += spot.weight; });
    var pick = random() * total, chosen = spots[spots.length - 1];
    for (var i = 0; i < spots.length; i += 1) {
      pick -= spots[i].weight;
      if (pick <= 0) { chosen = spots[i]; break; }
    }
    return { node: chosen.node, activity: chosen.activity, dwellMs: 3500 + Math.floor(random() * 5500) };
  }

  /* ---------------------------------------------------------------- text */
  function plain(value) {
    if (value === null || value === undefined) { return ""; }
    return String(value).replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 200);
  }
  /* Longer explanatory text from the server (still control-free and bounded). */
  function prose(value) {
    if (value === null || value === undefined) { return ""; }
    return String(value).replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 600);
  }
  /* Review notes (Step 37): user text, shown as text only. Keeps line breaks and tabs; bounded. */
  function noteText(value) {
    if (value === null || value === undefined) { return ""; }
    return String(value).replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "").slice(0, 2000);
  }
  function words(code) { return plain(code).replace(/_/g, " "); }
  function formatTime(stamp) {
    if (!stamp) { return "not recorded"; }
    var text = plain(stamp);
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(text) ? text.replace("T", " ").replace("Z", " UTC") : "invalid";
  }
  function describeEvent(event, roomLabel) {
    if (!event) { return "Before the first recorded event"; }
    var text = (roomLabel || words(event.component)) + " · " + words(event.event_type);
    if (event.status && event.event_type.indexOf(event.status) === -1) { text += " (" + words(event.status) + ")"; }
    if (event.details && event.details.conclusion) { text += " · " + words(event.details.conclusion); }
    if (event.details && event.details.verdict) { text += " · " + words(event.details.verdict); }
    if (event.event_type === "stage_reused") { text += " · finished in an earlier attempt (not run again)"; }
    if (event.details && event.details.attempt > 1) { text += " · attempt " + event.details.attempt; }
    return text;
  }
  function validScene(scene) {
    return !!scene && scene.contract === "hq_scene" && scene.version === "1.1" && MODES.hasOwnProperty(scene.mode) &&
      Array.isArray(scene.frames) && Array.isArray(scene.rooms) && Array.isArray(scene.stations);
  }
  /* What a bot's movement means, in words: movement is never evidence of activity. */
  var MOVEMENT = {
    station: "At its station because of recorded work",
    seat: "Waiting in its room (queued in a recorded workflow)",
    door: "Standing at its door: blocked by an earlier recorded failure",
    hold: "Holding position: the current status is unknown",
    free: "Decorative idle roaming: not evidence that this agent is running"
  };
  function movementFor(intent) { return MOVEMENT[intent] || MOVEMENT.free; }
  /* Room states at a replay position: before the first event everything is idle. */
  function roomStatesAt(scene, index, rooms) {
    if (index >= 0 && scene.frames[index]) { return scene.frames[index].room_states; }
    var states = {};
    rooms.forEach(function (room) { states[room] = "idle"; });
    return states;
  }
  function counters(scene, index) {
    var result = { accepted: 0, rejected: 0, pending: 0, fills: 0 };
    for (var i = 0; i <= index && i < scene.frames.length; i += 1) {
      var event = scene.frames[i].event;
      if (event.event_type === "simulated_fill") { result.fills += 1; }
      if (event.event_type === "order_decision") {
        if (event.status === "accepted") { result.accepted += 1; }
        if (event.status === "rejected") { result.rejected += 1; }
        if (event.status === "pending_at_end_of_data") { result.pending += 1; }
      }
    }
    return result;
  }
  function lastEventFor(scene, index, components) {
    for (var i = Math.min(index, scene.frames.length - 1); i >= 0; i -= 1) {
      if (components.indexOf(scene.frames[i].event.component) >= 0) { return scene.frames[i]; }
    }
    return null;
  }

  /* ---------------------------------------------------------------- results desk (Step 34) */
  var DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;
  /* Exact decimal text -> grouped money text. No float conversion, no rounding: every digit the
   * server sent is kept (at least 2 decimals are shown). "signed" adds "+" to positive values. */
  function money(text, signed) {
    var m = DECIMAL.exec(plain(text));
    if (!m) { return "invalid"; }
    var whole = m[2].replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    var fraction = (m[3] || "");
    while (fraction.length < 2) { fraction += "0"; }
    var zero = /^0*$/.test(m[2] + (m[3] || ""));
    var sign = m[1] && !zero ? "-" : (signed && !zero ? "+" : "");
    return sign + "$" + whole + "." + fraction;
  }
  /* Round decimal text half-even to `places` decimals, as text (display only). */
  function roundText(text, places) {
    var m = DECIMAL.exec(plain(text));
    if (!m) { return null; }
    var digits = m[2] + ((m[3] || "") + new Array(places + 2).join("0")).slice(0, places);
    var rest = (m[3] || "").slice(places);
    var up = false;
    if (rest.length) {
      var first = rest.charAt(0), tail = /[1-9]/.test(rest.slice(1));
      var last = Number(digits.charAt(digits.length - 1));
      up = first > "5" || (first === "5" && (tail || last % 2 === 1));
    }
    var chars = digits.split(""), i = chars.length - 1;
    while (up && i >= 0) {
      if (chars[i] === "9") { chars[i] = "0"; i -= 1; } else { chars[i] = String(Number(chars[i]) + 1); up = false; }
    }
    if (up) { chars.unshift("1"); }
    var all = chars.join("");
    var whole = all.slice(0, all.length - places).replace(/^0+(?=\d)/, "") || "0";
    var out = places ? whole + "." + all.slice(all.length - places) : whole;
    return (m[1] && /[1-9]/.test(all) ? "-" : "") + out;
  }
  function percent(text, signed) {
    var rounded = roundText(text, 2);
    if (rounded === null) { return "invalid"; }
    return (signed && rounded.charAt(0) !== "-" && /[1-9]/.test(rounded) ? "+" : "") + rounded + "%";
  }
  var REASONS = {
    no_closed_trades: "no closed trades",
    no_winning_trades: "no winning trades",
    no_losing_trades: "no losing trades",
    no_bars_processed: "no bars processed",
    analytics_report_missing: "no matching Step 30 analytics report is saved",
    analytics_report_rejected: "the only analytics reports for this run were rejected",
    analytics_report_ambiguous: "more than one analytics report matches; none is chosen",
    analytics_report_mismatch: "its run, policy or dataset hashes differ from the run",
    analytics_report_inconsistent: "its account figures differ from the run",
    report_corrupt: "it failed validation",
    no_bar_closed_yet: "no bar had closed yet at this simulated time",
    timeline_not_complete: "the timeline is not complete",
    timeline_has_issues: "the timeline has recorded issues",
    timeline_missing_trade_events: "some order or fill events are missing from the timeline",
    timeline_not_chronological: "events are not in simulated-time order",
    simulated_time_missing: "an event has no simulated time",
    replay_state_inconsistent: "folding the events does not reproduce the saved run",
    simulator_not_started_at_position: "the simulator had not started at this replay position"
  };
  function reasonText(code) { return REASONS[code] || words(code); }
  /* A Step 30-style metric {status, value, reason}: the value, or "Unavailable (reason)". Never zero. */
  function metricText(metric, format) {
    if (!metric || metric.status !== "available") {
      return "Unavailable (" + reasonText(metric && metric.reason ? metric.reason : "unknown") + ")";
    }
    return format ? format(String(metric.value)) : plain(metric.value);
  }
  /* Replay index (-1 = before the first event) -> results position (events applied). */
  function resultsPosition(index, frames, nowMode) {
    if (nowMode) { return frames; }
    return clamp((index | 0) + 1, 0, frames);
  }
  function validResults(doc, view) {
    return !!doc && doc.contract === "hq_results" && doc.version === "1.0" && doc.simulated === true &&
      doc.read_only === true && doc.view === view;
  }
  /* Chart geometry from decimal text (floats are used for drawing only, never for displayed values). */
  function scale(points, key, width, height, pad, xDomain) {
    var xs = points.map(function (p) { return Date.parse(p.at_utc); });
    var ys = points.map(function (p) { return Number(p[key]); });
    var x0 = xDomain ? xDomain[0] : Math.min.apply(null, xs), x1 = xDomain ? xDomain[1] : Math.max.apply(null, xs);
    var y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (!(x1 > x0)) { x1 = x0 + 1; }
    if (!(y1 > y0)) { y0 -= 1; y1 += 1; }
    var span = y1 - y0;
    y0 -= span * 0.08; y1 += span * 0.08;
    return {
      x0: x0, x1: x1, y0: y0, y1: y1,
      px: function (t) { return pad.l + (t - x0) / (x1 - x0) * (width - pad.l - pad.r); },
      py: function (v) { return pad.t + (1 - (v - y0) / (y1 - y0)) * (height - pad.t - pad.b); },
      points: points.map(function (p, i) { return { x: xs[i], y: ys[i], point: p }; })
    };
  }
  function nearestIndex(items, x) {
    var best = -1, distance = Infinity;
    for (var i = 0; i < items.length; i += 1) {
      var d = Math.abs(items[i] - x);
      if (d < distance) { distance = d; best = i; }
    }
    return best;
  }

  /* ---------------------------------------------------------------- content desk (Step 35) */
  function validContent(doc, view) {
    return !!doc && doc.contract === "hq_content" && doc.version === "1.0" && doc.read_only === true && doc.view === view &&
      !!doc.restrictions && doc.restrictions.publishable === false && doc.restrictions.preview_only === true;
  }
  /* A clickable link only for a plain http(s) URL with a host and no credentials; null otherwise. */
  function safeLink(url) {
    if (typeof url !== "string" || url.length < 8 || url.length > 2048 || /[\u0000-\u0020\u007f]/.test(url)) { return null; }
    var parsed;
    try { parsed = new URL(url); } catch (error) { return null; }
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname || parsed.username ||
        parsed.password) { return null; }
    return parsed.href;
  }

  return {
    validContent: validContent, safeLink: safeLink,
    prose: prose, noteText: noteText, money: money, roundText: roundText, percent: percent, REASONS: REASONS, reasonText: reasonText, metricText: metricText,
    resultsPosition: resultsPosition, validResults: validResults, scale: scale, nearestIndex: nearestIndex,
    STATES: STATES, STATE_ORDER: STATE_ORDER, MODES: MODES, SPEEDS: SPEEDS, ID_PATTERN: ID_PATTERN,
    stateStyle: stateStyle, intentFor: intentFor, Replay: Replay, Graph: Graph, hashSeed: hashSeed, rng: rng,
    wanderChoice: wanderChoice, movementFor: movementFor, MOVEMENT: MOVEMENT, plain: plain, words: words, formatTime: formatTime, describeEvent: describeEvent,
    validScene: validScene, roomStatesAt: roomStatesAt, counters: counters, lastEventFor: lastEventFor
  };
}));
