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

  return {
    STATES: STATES, STATE_ORDER: STATE_ORDER, MODES: MODES, SPEEDS: SPEEDS, ID_PATTERN: ID_PATTERN,
    stateStyle: stateStyle, intentFor: intentFor, Replay: Replay, Graph: Graph, hashSeed: hashSeed, rng: rng,
    wanderChoice: wanderChoice, movementFor: movementFor, MOVEMENT: MOVEMENT, plain: plain, words: words, formatTime: formatTime, describeEvent: describeEvent,
    validScene: validScene, roomStatesAt: roomStatesAt, counters: counters, lastEventFor: lastEventFor
  };
}));
