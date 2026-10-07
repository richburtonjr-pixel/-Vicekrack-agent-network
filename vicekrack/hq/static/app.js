/* ViceKrack Living HQ (Step 32): read-only visual Command Center.
 *
 * Safety rules for this file:
 * - Only GET requests to /api/timelines and /api/scene. No other network use.
 * - All text from the server is inserted with textContent / setAttribute, never as HTML.
 * - Status shown for a room always comes from the scene frame at the replay position (or the
 *   "now" data). Bot movement is decoration: it can lag behind, but it never changes a status.
 * - Idle wandering is a seeded, purely visual choice of where a bot stands; it triggers nothing.
 */
(function () {
  "use strict";
  var C = window.HQCore;
  var NS = "http://www.w3.org/2000/svg";

  /* ------------------------------------------------------------------ geometry */
  var U = 20, DX = 0.5, DY = 0.42, W = 12, D = 9, FH = 11, SLAB = 0.7, WALL = FH - SLAB, TD = 12;
  var OX = 11 * U, OY = 30 * U;
  function P(x, y, z) { return [OX + x * U + y * DX * U, OY - y * DY * U - z * U]; }
  function pts(list) { return list.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" "); }

  var ROOM_SLOTS = {
    market_scout: { i: 0, z: FH }, trend_agent: { i: 1, z: FH }, strategy_agent: { i: 2, z: FH }, risk_review: { i: 3, z: FH },
    researcher: { i: 0, z: 0 }, analyst: { i: 1, z: 0 }, reviewer: { i: 2, z: 0 }, creator: { i: 3, z: 0 }
  };
  var BOT_ROOMS = ["market_scout", "trend_agent", "strategy_agent", "risk_review", "researcher", "analyst", "reviewer", "creator"];
  var ALL_ROOMS = BOT_ROOMS.concat(["operations", "lounge", "kitchen"]);
  var LOOKS = {
    market_scout: { accent: "#22d3c5", body: "#eef2f5", extra: "dish" },
    trend_agent: { accent: "#38bdf8", body: "#e7edf3", extra: "headset" },
    strategy_agent: { accent: "#818cf8", body: "#f1f1f4", extra: "visor" },
    risk_review: { accent: "#fbbf24", body: "#ece8e1", extra: "helmet" },
    researcher: { accent: "#c4b5fd", body: "#f2eff7", extra: "glasses" },
    analyst: { accent: "#a78bfa", body: "#e9e6f2", extra: "clipboard" },
    reviewer: { accent: "#f472b6", body: "#f4eef1", extra: "badge" },
    creator: { accent: "#d946ef", body: "#f3ecf6", extra: "beret" }
  };

  /* ------------------------------------------------------------------ dom helpers */
  function el(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { node.setAttribute(k, String(attrs[k])); }); }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function svgText(parent, x, y, value, attrs) {
    var node = el("text", Object.assign({ x: x, y: y }, attrs || {}), parent);
    node.textContent = C.plain(value);
    return node;
  }
  function h(tag, attrs, parent, value) {
    var node = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { node.setAttribute(k, String(attrs[k])); }); }
    if (value !== undefined && value !== null) { node.textContent = C.plain(value); }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function $(id) { return document.getElementById(id); }
  function poly(parent, points, attrs) { return el("polygon", Object.assign({ points: pts(points) }, attrs || {}), parent); }
  function face(parent, x, y, z, w, hgt, attrs) {           // a rectangle in a constant-y plane
    var a = P(x, y, z + hgt);
    return el("rect", Object.assign({ x: a[0], y: a[1], width: w * U, height: hgt * U }, attrs || {}), parent);
  }
  function box(parent, x, y, z, w, d, hgt, colors) {
    var g = el("g", {}, parent);
    poly(g, [P(x + w, y, z), P(x + w, y + d, z), P(x + w, y + d, z + hgt), P(x + w, y, z + hgt)], { fill: colors[2] });
    poly(g, [P(x, y, z), P(x + w, y, z), P(x + w, y, z + hgt), P(x, y, z + hgt)], { fill: colors[1] });
    poly(g, [P(x, y, z + hgt), P(x + w, y, z + hgt), P(x + w, y + d, z + hgt), P(x, y + d, z + hgt)], { fill: colors[0] });
    return g;
  }
  function glow(parent, x, y, z, rx, ry, fill, opacity) {
    var c = P(x, y, z);
    return el("ellipse", { cx: c[0], cy: c[1], rx: rx * U, ry: ry * U, fill: fill, opacity: opacity === undefined ? 1 : opacity }, parent);
  }

  /* ------------------------------------------------------------------ app state */
  var S = {
    scene: null, replay: null, view: "house", source: "replay", selected: null, labels: true,
    reduced: window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    presenting: false, list: false, timelines: [], timelineId: "demo", focusRoom: null, filter: "all",
    following: false, polls: 0, pollTimer: null, lastIndex: -1, completedAt: {}, layers: {}, roomNodes: {},
    bots: {}, tokens: [], viewBox: [0, 0, 1500, 660], viewTarget: null, error: null
  };
  var MAX_POLLS = 900, POLL_MS = 2000;

  /* ------------------------------------------------------------------ nav graph (walkable paths) */
  var nodes = {}, edges = [];
  function node(id, x, y, z) { nodes[id] = { x: x, y: y, z: z }; }
  BOT_ROOMS.forEach(function (room) {
    var s = ROOM_SLOTS[room], x0 = s.i * W, z = s.z;
    node("st_" + room, x0 + 6.0, 3.3, z);
    node("seat_" + room, x0 + 2.4, 2.3, z);
    node("nook_" + room, x0 + 9.8, 2.0, z);
    node("door_" + room, x0 + 6.0, 0.7, z);
    edges.push(["door_" + room, "st_" + room], ["door_" + room, "seat_" + room], ["door_" + room, "nook_" + room],
      ["seat_" + room, "st_" + room], ["nook_" + room, "st_" + room]);
    edges.push(["door_" + room, (z > 0 ? "bal_" : "cor_") + s.i]);
  });
  for (var i = 0; i < 4; i += 1) {
    node("bal_" + i, i * W + 6, -0.9, FH);
    node("cor_" + i, i * W + 6, -1.2, 0);
    if (i > 0) { edges.push(["bal_" + (i - 1), "bal_" + i], ["cor_" + (i - 1), "cor_" + i]); }
  }
  node("land", 49.8, -0.9, FH); node("sb", 58.4, -0.9, 0); node("cor_w", -1.2, -1.2, 0); node("cor_e", 52.5, -1.2, 0);
  node("l_entry", 6, -4.0, 0); node("l_sofa1", 2.8, -6.6, 0); node("l_sofa2", 9.8, -9.2, 0);
  node("l_sofa3", 2.8, -8.8, 0); node("l_chair", 11.8, -6.4, 0);
  node("o_entry", 24, -4.0, 0); node("o_left", 17.2, -6.6, 0); node("o_right", 32.6, -6.6, 0);
  node("o_fl", 18.0, -11.6, 0); node("o_front", 24.8, -11.6, 0); node("o_fr", 31.4, -11.6, 0);
  node("k_gate", 44.2, -3.9, 0); node("k_entry", 41, -6.6, 0); node("k_coffee", 38.0, -5.6, 0);
  node("k_fridge", 46.2, -6.2, 0); node("k_table", 41.6, -11.4, 0); node("k_counter", 41.2, -5.8, 0);
  node("k_stool1", 38.8, -11.9, 0); node("k_stool2", 44.8, -11.9, 0);
  edges.push(["bal_3", "land"], ["land", "sb"], ["cor_w", "cor_0"], ["cor_3", "cor_e"], ["cor_e", "sb"],
    ["cor_0", "l_entry"], ["cor_1", "o_entry"], ["cor_2", "o_entry"], ["cor_3", "k_gate"], ["k_gate", "k_entry"],
    ["l_entry", "l_sofa1"], ["l_entry", "l_sofa2"], ["l_sofa1", "l_sofa2"], ["o_entry", "o_left"], ["o_entry", "o_right"],
    ["o_left", "o_fl"], ["o_fl", "o_front"], ["o_front", "o_fr"], ["o_fr", "o_right"], ["k_entry", "k_coffee"],
    ["k_entry", "k_fridge"], ["k_entry", "k_table"], ["k_coffee", "k_table"], ["l_sofa2", "o_fl"], ["o_fr", "k_table"],
    ["l_entry", "l_sofa3"], ["l_sofa1", "l_sofa3"], ["l_entry", "l_chair"], ["k_entry", "k_counter"],
    ["k_table", "k_stool1"], ["k_table", "k_stool2"]);
  var graph = new C.Graph(nodes, edges);

  /* Idle destinations (decoration). The first two are the bot's own; shared spots hold one bot at a
   * time (occupancy is checked in targetFor), so idle bots spread out instead of piling up. */
  function spotsFor(room) {
    var spots = [
      { node: "seat_" + room, weight: 3, activity: "sit" }, { node: "nook_" + room, weight: 2, activity: "idle" },
      { node: "l_sofa1", weight: 1, activity: "relax" }, { node: "l_sofa2", weight: 1, activity: "relax" },
      { node: "l_sofa3", weight: 1, activity: "relax" }, { node: "l_chair", weight: 1, activity: "relax" },
      { node: "k_coffee", weight: 2, activity: "coffee" }, { node: "k_counter", weight: 1, activity: "coffee" },
      { node: "k_stool1", weight: 1, activity: "chat" }, { node: "k_stool2", weight: 1, activity: "chat" },
      { node: "k_fridge", weight: 1, activity: "snack" }, { node: "o_fl", weight: 1, activity: "browse" },
      { node: "o_fr", weight: 1, activity: "browse" }];
    if (ROOM_SLOTS[room].z > 0) { spots.push({ node: "bal_" + ROOM_SLOTS[room].i, weight: 2, activity: "view" }); }
    return spots;
  }

  /* ------------------------------------------------------------------ static house */
  function defs(svg) {
    var d = el("defs", {}, svg);
    function linear(id, stops, x2, y2) {
      var g = el("linearGradient", { id: id, x1: 0, y1: 0, x2: x2 === undefined ? 0 : x2, y2: y2 === undefined ? 1 : y2 }, d);
      stops.forEach(function (s) { el("stop", { offset: s[0], "stop-color": s[1], "stop-opacity": s[2] === undefined ? 1 : s[2] }, g); });
    }
    function radial(id, color, opacity) {
      var g = el("radialGradient", { id: id }, d);
      el("stop", { offset: 0, "stop-color": color, "stop-opacity": opacity }, g);
      el("stop", { offset: 1, "stop-color": color, "stop-opacity": 0 }, g);
    }
    linear("sky", [[0, "#06090d"], [0.6, "#0b121a"], [1, "#101820"]]);
    linear("wall", [[0, "#1a1f27"], [1, "#12161c"]]);
    linear("wallSide", [[0, "#151a21"], [1, "#0f1318"]], 1, 0);
    linear("floorWood", [[0, "#3b2c21"], [1, "#2a2018"]]);
    linear("floorStone", [[0, "#22272e"], [1, "#191d23"]]);
    linear("slab", [[0, "#252b33"], [1, "#1a1f26"]]);
    linear("screenTeal", [[0, "#0f4b57"], [1, "#082730"]]);
    linear("screenViolet", [[0, "#3b1f5e"], [1, "#1d1033"]]);
    linear("screenWarm", [[0, "#4a3412"], [1, "#2a1d09"]]);
    linear("glass", [[0, "#9be7ff", 0.10], [1, "#9be7ff", 0.03]]);
    radial("warm", "#ffbe6e", 0.55);
    radial("warmSoft", "#ffcf8a", 0.28);
    radial("teal", "#2dd4bf", 0.45);
    radial("violet", "#a855f7", 0.45);
    var f = el("filter", { id: "soft", x: "-50%", y: "-50%", width: "200%", height: "200%" }, d);
    el("feGaussianBlur", { stdDeviation: 3 }, f);
  }

  function seededLine(seed, count, x, y, w, hgt) {
    var r = C.rng(seed), out = [], v = 0.5;
    for (var k = 0; k < count; k += 1) {
      v = Math.max(0.1, Math.min(0.9, v + (r() - 0.45) * 0.3));
      out.push([x + (w * k) / (count - 1), y + hgt * (1 - v)]);
    }
    return out;
  }

  function drawBackground(layer) {
    el("rect", { x: -3000, y: -3000, width: 8000, height: 6000, fill: "#070a0e" }, layer);
    el("rect", { x: -3000, y: -1400, width: 8000, height: 1400 + OY, fill: "url(#sky)" }, layer);
    var r = C.rng(7);
    for (var k = 0; k < 90; k += 1) {
      el("circle", { cx: -600 + r() * 2800, cy: -500 + r() * 520, r: r() * 1.2 + 0.3, fill: "#c9d6e3", opacity: 0.2 + r() * 0.3 }, layer);
    }
    poly(layer, [P(-60, -40, 0), P(120, -40, 0), P(120, 14, 0), P(-60, 14, 0)], { fill: "#0c1015" });
    [[-7, 4], [-9, -5], [64, 6], [66, -7], [-6, 11], [63, 12]].forEach(function (t, n) {
      var base = P(t[0], t[1], 0);
      el("rect", { x: base[0] - 3, y: base[1] - 46, width: 6, height: 46, fill: "#2a2017" }, layer);
      for (var j = 0; j < 4; j += 1) {
        el("circle", { cx: base[0] + (j - 1.5) * 13, cy: base[1] - 60 - (j % 2) * 18, r: 26 + (n % 3) * 4, fill: j % 2 ? "#14261c" : "#1a3124" }, layer);
      }
    });
  }

  function plant(layer, x, y, z, scale) {
    scale = scale || 1;
    box(layer, x, y, z, 0.9 * scale, 0.9 * scale, 0.9 * scale, ["#3b3f46", "#2b2f35", "#23262b"]);
    var c = P(x + 0.45 * scale, y + 0.45 * scale, z + 0.9 * scale);
    [[0, -30, 16], [-11, -20, 12], [11, -22, 13], [0, -42, 11]].forEach(function (l, n) {
      el("ellipse", { cx: c[0] + l[0] * scale, cy: c[1] + l[1] * scale, rx: l[2] * scale, ry: l[2] * 0.8 * scale, fill: n % 2 ? "#2f6b45" : "#3f8a57" }, layer);
    });
  }

  function lamp(layer, x, y, z) {
    var top = P(x, y, z + 6.3), bulb = P(x, y, z + 5.5);
    el("line", { x1: top[0], y1: top[1], x2: bulb[0], y2: bulb[1], stroke: "#555", "stroke-width": 1 }, layer);
    el("circle", { cx: bulb[0], cy: bulb[1] + 4, r: 5, fill: "#ffd79a" }, layer);
    el("circle", { cx: bulb[0], cy: bulb[1] + 4, r: 18, fill: "url(#warm)" }, layer);
  }

  function drawRoom(layer, room, meta) {
    var s = ROOM_SLOTS[room], x0 = s.i * W, z = s.z;
    var trading = meta.department === "trading";
    var accent = trading ? "#2dd4bf" : "#a78bfa";
    var screen = trading ? "url(#screenTeal)" : "url(#screenViolet)";
    var g = el("g", { class: "room", "data-room": room }, layer);
    poly(g, [P(x0, D, z), P(x0 + W, D, z), P(x0 + W, D, z + WALL), P(x0, D, z + WALL)], { fill: "url(#wall)" });
    poly(g, [P(x0, 0, z), P(x0, D, z), P(x0, D, z + WALL), P(x0, 0, z + WALL)],
      { fill: "url(#wallSide)", opacity: s.i === 0 ? 1 : 0.92 });
    var floor = poly(g, [P(x0, 0, z), P(x0 + W, 0, z), P(x0 + W, D, z), P(x0, D, z)], { fill: "url(#floorWood)", class: "floor" });
    S.roomNodes[room] = { floor: floor };
    poly(g, [P(x0 + 1.5, 1.2, z + 0.01), P(x0 + W - 1.2, 1.2, z + 0.01), P(x0 + W - 1.2, 6.8, z + 0.01), P(x0 + 1.5, 6.8, z + 0.01)],
      { fill: accent, opacity: 0.07 });
    var a1 = P(x0 + 0.2, D, z + 0.18), a2 = P(x0 + W, D, z + 0.18);
    el("line", { x1: a1[0], y1: a1[1], x2: a2[0], y2: a2[1], stroke: accent, "stroke-width": 2, opacity: 0.8 }, g);
    glow(g, x0 + 6, 4.5, z, 5.6, 2.4, "url(#warmSoft)");

    var wy = D - 0.02;                                       // back-wall decor plane
    if (room === "market_scout") {
      face(g, x0 + 1.0, wy, z + 2.3, 4.8, 2.3, { fill: screen, stroke: accent, "stroke-width": 1, rx: 3 });
      var map = C.rng(11);
      for (var m = 0; m < 34; m += 1) {
        var p = P(x0 + 1.3 + map() * 4.2, wy, z + 2.6 + map() * 1.7);
        el("circle", { cx: p[0], cy: p[1], r: 1.6, fill: "#5eead4", opacity: 0.4 + map() * 0.5 }, g);
      }
      var rc = P(x0 + 8.4, wy, z + 3.4);
      el("circle", { cx: rc[0], cy: rc[1], r: 24, fill: "url(#screenTeal)", stroke: accent }, g);
      [8, 16].forEach(function (r) { el("circle", { cx: rc[0], cy: rc[1], r: r, fill: "none", stroke: "#5eead4", opacity: 0.4 }, g); });
      el("line", { x1: rc[0], y1: rc[1], x2: rc[0] + 18, y2: rc[1] - 14, stroke: "#5eead4", "stroke-width": 1.5, class: "sweep" }, g);
    } else if (room === "trend_agent") {
      [[1.0, 101], [5.8, 102]].forEach(function (cfg) {
        var r = face(g, x0 + cfg[0], wy, z + 2.3, 4.4, 2.2, { fill: screen, stroke: accent, "stroke-width": 1, rx: 3 });
        var bx = Number(r.getAttribute("x")), by = Number(r.getAttribute("y"));
        el("polyline", { points: pts(seededLine(cfg[1], 14, bx + 8, by + 8, 4.4 * U - 16, 2.2 * U - 16)), fill: "none",
          stroke: cfg[1] === 101 ? "#5eead4" : "#93c5fd", "stroke-width": 2 }, g);
      });
    } else if (room === "strategy_agent") {
      var board = face(g, x0 + 1.2, wy, z + 2.1, 7.0, 2.6, { fill: "#1f1c19", stroke: "#3a332b", rx: 3 });
      var bx2 = Number(board.getAttribute("x")), by2 = Number(board.getAttribute("y")), sr = C.rng(23);
      for (var n = 0; n < 9; n += 1) {
        el("rect", { x: bx2 + 8 + (n % 5) * 26, y: by2 + 8 + Math.floor(n / 5) * 24 + sr() * 4, width: 18, height: 14,
          fill: n % 3 ? "#e5d6b8" : "#9fd8e8", opacity: 0.85 }, g);
      }
      el("polyline", { points: pts([[bx2 + 17, by2 + 15], [bx2 + 69, by2 + 40], [bx2 + 121, by2 + 15]]), fill: "none", stroke: "#2dd4bf", opacity: 0.7 }, g);
    } else if (room === "risk_review") {
      var list = face(g, x0 + 1.4, wy, z + 2.0, 5.6, 2.8, { fill: "url(#screenWarm)", stroke: "#fbbf24", rx: 3 });
      var lx = Number(list.getAttribute("x")), ly = Number(list.getAttribute("y"));
      for (var q = 0; q < 4; q += 1) {
        el("rect", { x: lx + 10, y: ly + 9 + q * 12, width: 7, height: 7, fill: "none", stroke: "#fbbf24" }, g);
        el("polyline", { points: pts([[lx + 11, ly + 12 + q * 12], [lx + 13, ly + 15 + q * 12], [lx + 17, ly + 9 + q * 12]]), fill: "none", stroke: "#fde68a" }, g);
        el("rect", { x: lx + 24, y: ly + 11 + q * 12, width: 50 + (q % 2) * 20, height: 3, fill: "#fde68a", opacity: 0.6 }, g);
      }
    } else if (room === "researcher") {
      var shelf = face(g, x0 + 0.8, wy, z + 0.2, 3.4, 4.4, { fill: "#2a1f17", stroke: "#4a3727" });
      var sx = Number(shelf.getAttribute("x")), sy = Number(shelf.getAttribute("y")), br = C.rng(31);
      for (var row = 0; row < 4; row += 1) {
        for (var b = 0; b < 8; b += 1) {
          el("rect", { x: sx + 4 + b * 7.6, y: sy + 6 + row * 21, width: 5.5, height: 15 - br() * 4,
            fill: ["#8b5cf6", "#c084fc", "#e9d5ff", "#7dd3fc", "#f0abfc"][Math.floor(br() * 5)], opacity: 0.8 }, g);
        }
      }
      var gc = P(x0 + 7.8, wy, z + 3.4);
      el("rect", { x: gc[0] - 48, y: gc[1] - 26, width: 96, height: 52, rx: 4, fill: screen, stroke: accent }, g);
      el("circle", { cx: gc[0], cy: gc[1], r: 17, fill: "none", stroke: "#c4b5fd" }, g);
      el("ellipse", { cx: gc[0], cy: gc[1], rx: 7, ry: 17, fill: "none", stroke: "#c4b5fd", opacity: 0.7 }, g);
    } else if (room === "analyst") {
      var cork = face(g, x0 + 1.4, wy, z + 2.0, 6.4, 2.8, { fill: "#4a3829", stroke: "#6b5039", rx: 2 });
      var cx = Number(cork.getAttribute("x")), cy = Number(cork.getAttribute("y")), cr = C.rng(41), pins = [];
      for (var t = 0; t < 8; t += 1) {
        var nx = cx + 8 + (t % 4) * 30 + cr() * 6, ny = cy + 8 + Math.floor(t / 4) * 26 + cr() * 4;
        el("rect", { x: nx, y: ny, width: 20, height: 16, fill: t % 3 ? "#f5efe0" : "#d8b4fe" }, g);
        pins.push([nx + 10, ny + 2]);
      }
      el("polyline", { points: pts([pins[0], pins[5], pins[2], pins[7]]), fill: "none", stroke: "#ef4444", "stroke-width": 1.2 }, g);
    } else if (room === "reviewer") {
      var chk = face(g, x0 + 1.4, wy, z + 2.0, 5.2, 2.8, { fill: "#16131d", stroke: accent, rx: 3 });
      var kx = Number(chk.getAttribute("x")), ky = Number(chk.getAttribute("y"));
      for (var k2 = 0; k2 < 4; k2 += 1) {
        el("polyline", { points: pts([[kx + 10, ky + 13 + k2 * 12], [kx + 13, ky + 16 + k2 * 12], [kx + 18, ky + 9 + k2 * 12]]), fill: "none", stroke: "#86efac" }, g);
        el("rect", { x: kx + 24, y: ky + 11 + k2 * 12, width: 46 + (k2 % 3) * 12, height: 3, fill: "#e9d5ff", opacity: 0.6 }, g);
      }
      var mg = P(x0 + 8.6, wy, z + 3.4);
      el("circle", { cx: mg[0], cy: mg[1], r: 14, fill: "none", stroke: "#f9a8d4", "stroke-width": 3 }, g);
      el("line", { x1: mg[0] + 10, y1: mg[1] + 10, x2: mg[0] + 22, y2: mg[1] + 22, stroke: "#f9a8d4", "stroke-width": 4 }, g);
    } else if (room === "creator") {
      [[0.8, 201], [5.6, 202]].forEach(function (cfg) {
        var r2 = face(g, x0 + cfg[0], wy, z + 2.3, 4.6, 2.2, { fill: screen, stroke: "#d946ef", rx: 3 });
        var vx = Number(r2.getAttribute("x")), vy = Number(r2.getAttribute("y"));
        var wave = [], wr = C.rng(cfg[1]);
        for (var w = 0; w < 30; w += 1) { wave.push([vx + 6 + w * 2.8, vy + 22 + (wr() - 0.5) * (w % 3 ? 26 : 10)]); }
        el("polyline", { points: pts(wave), fill: "none", stroke: "#f0abfc", "stroke-width": 1.5 }, g);
      });
      var tb = P(x0 + 1.6, 5.4, z), tt = P(x0 + 1.6, 5.4, z + 2.4);
      el("line", { x1: tb[0] - 9, y1: tb[1], x2: tt[0], y2: tt[1], stroke: "#555", "stroke-width": 2 }, g);
      el("line", { x1: tb[0] + 9, y1: tb[1], x2: tt[0], y2: tt[1], stroke: "#555", "stroke-width": 2 }, g);
      el("rect", { x: tt[0] - 12, y: tt[1] - 12, width: 24, height: 14, rx: 3, fill: "#222", stroke: "#d946ef" }, g);
    }
    // desk, monitors, chair, plants and a cabinet (furniture back to front)
    box(g, x0 + 9.4, 6.6, z, 1.8, 1.6, 1.8, ["#3a3f47", "#2a2e35", "#22262c"]);
    box(g, x0 + 3.0, 4.6, z, 6.2, 1.5, 1.05, ["#4a3828", "#35281d", "#2b2018"]);
    [[3.5, 2.3], [6.1, 2.3]].forEach(function (mtr) {
      face(g, x0 + mtr[0], 5.7, z + 1.1, mtr[1], 1.35, { fill: screen, stroke: "#0b0f14", "stroke-width": 2, rx: 2 });
    });
    glow(g, x0 + 6.0, 5.2, z + 0.02, 3.6, 1.1, trading ? "url(#teal)" : "url(#violet)", 0.8);
    box(g, x0 + 5.3, 2.4, z, 1.4, 1.2, 0.75, ["#2e3239", "#24272d", "#1d2025"]);
    plant(g, x0 + 10.4, 4.2, z, 1.0);
    plant(g, x0 + 0.6, 6.8, z, 0.8);
    box(g, x0 + 1.4, 1.6, z, 1.6, 1.4, 0.8, ["#3c3226", "#2c241b", "#241d16"]);
    lamp(g, x0 + 6, 4.4, z);
    floor.addEventListener("click", function () { select(room); });
  }

  function drawStructure(layer) {
    // right exterior wall, slabs, roof
    poly(layer, [P(48, 0, 0), P(48, D, 0), P(48, D, 2 * FH), P(48, 0, 2 * FH)], { fill: "#14181e" });
    for (var f = 0; f < 2; f += 1) {
      for (var k = 0; k < 3; k += 1) {
        poly(layer, [P(48, 1.5 + k * 2.6, f * FH + 2.5), P(48, 3.3 + k * 2.6, f * FH + 2.5), P(48, 3.3 + k * 2.6, f * FH + 6.5), P(48, 1.5 + k * 2.6, f * FH + 6.5)],
          { fill: "#22303a", opacity: 0.8 });
      }
    }
  }

  function drawSlabs(layer) {
    [FH, 2 * FH].forEach(function (zTop, n) {
      poly(layer, [P(0, 0, zTop), P(48, 0, zTop), P(48, D, zTop), P(0, D, zTop)], { fill: n ? "#1b2027" : "none" });
      face(layer, 0, 0, zTop - SLAB, 48, SLAB, { fill: "url(#slab)" });
      var l1 = P(0, 0, zTop - SLAB), l2 = P(48, 0, zTop - SLAB);
      el("line", { x1: l1[0], y1: l1[1], x2: l2[0], y2: l2[1], stroke: "#ffcf8a", "stroke-width": 2, opacity: 0.8 }, layer);
    });
    for (var c = 0; c <= 4; c += 1) {
      box(layer, c * W - 0.18, -0.2, 0, 0.36, 0.36, 2 * FH, ["#2a3038", "#20252c", "#1a1e24"]);
    }
    if (true) {
      var r = C.rng(5);
      for (var p = 0; p < 7; p += 1) { plant(layer, 2 + p * 7 + r() * 2, 2 + r() * 4, 2 * FH, 0.9); }
    }
  }

  function drawBalconyAndStairs(layer) {
    poly(layer, [P(0, -1.8, FH), P(51.4, -1.8, FH), P(51.4, 0, FH), P(0, 0, FH)], { fill: "url(#floorStone)" });
    face(layer, 0, -1.8, FH - 0.3, 51.4, 0.3, { fill: "#1d2229" });
    var steps = 12;
    for (var s = 0; s < steps; s += 1) {
      var zz = FH - (s + 1) * (FH / steps);
      box(layer, 48.6 + s * 0.78, -1.8, zz, 0.78, 1.8, FH / steps, ["#2d333b", "#23282f", "#1c2026"]);
    }
    poly(layer, [P(48.6, -1.8, FH), P(58, -1.8, 0), P(58, -1.8, -0.2), P(48.6, -1.8, FH - 0.6)], { fill: "#161a20" });
  }

  function drawRailings(layer) {
    poly(layer, [P(0, -1.8, FH), P(51.4, -1.8, FH), P(51.4, -1.8, FH + 1.1), P(0, -1.8, FH + 1.1)], { fill: "url(#glass)", stroke: "#9be7ff", "stroke-opacity": 0.25 });
    var a = P(0, -1.8, FH + 1.1), b = P(51.4, -1.8, FH + 1.1);
    el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: "#c9a46a", "stroke-width": 2 }, layer);
    var c1 = P(48.6, -1.8, FH + 1.1), c2 = P(58, -1.8, 1.1);
    el("line", { x1: c1[0], y1: c1[1], x2: c2[0], y2: c2[1], stroke: "#c9a46a", "stroke-width": 2 }, layer);
  }

  function drawTerrace(layer) {
    poly(layer, [P(-3, -TD, 0), P(60, -TD, 0), P(60, 0, 0), P(-3, 0, 0)], { fill: "url(#floorStone)" });
    for (var t = -3; t <= 60; t += 3) {
      var a = P(t, -TD, 0.01), b = P(t, 0, 0.01);
      el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: "#2a3038", "stroke-width": 0.6 }, layer);
    }
    poly(layer, [P(-3, -2.4, 0.02), P(55, -2.4, 0.02), P(55, 0, 0.02), P(-3, 0, 0.02)], { fill: "#262c34", opacity: 0.9 });
    var e1 = P(-3, -TD, 0), e2 = P(60, -TD, 0);
    el("line", { x1: e1[0], y1: e1[1], x2: e2[0], y2: e2[1], stroke: "#ffcf8a", "stroke-width": 2, opacity: 0.6 }, layer);
    // lounge
    glow(layer, 6, -7.5, 0, 7, 2.2, "url(#warmSoft)");
    var rug = P(6, -7.6, 0);
    el("ellipse", { cx: rug[0], cy: rug[1], rx: 5.4 * U, ry: 1.5 * U, fill: "#2b2620", opacity: 0.85 }, layer);
    box(layer, 0.2, -6.4, 0, 1.6, 3.6, 1.0, ["#3a3f46", "#2e3238", "#262a2f"]);
    box(layer, 7.4, -11.0, 0, 5.0, 1.5, 1.0, ["#3a3f46", "#2e3238", "#262a2f"]);
    box(layer, 4.0, -8.4, 0, 2.8, 1.5, 0.5, ["#5a4330", "#46331f", "#3a2a1a"]);
    plant(layer, -2.2, -3.8, 0, 1.3); plant(layer, 13, -4.6, 0, 1.0);
    // operations pavilion: console wall (its face holds the status board) and a front desk
    box(layer, 15.5, -11.9, 0, 18.5, 8.7, 0.25, ["#262c34", "#1d2128", "#181b20"]);
    glow(layer, 24.7, -9.6, 0.3, 8, 2.4, "url(#warm)", 0.7);
    box(layer, 18.6, -8.4, 0.25, 12.8, 0.6, 3.8, ["#1c2129", "#141920", "#10141a"]);
    box(layer, 19.8, -11.2, 0.25, 10.2, 1.3, 1.0, ["#2b2f36", "#1f2329", "#1a1d22"]);
    [20.6, 23.1, 25.6, 28.1].forEach(function (mx) {
      face(layer, mx, -10.0, 1.25, 1.8, 0.75, { fill: "url(#screenTeal)", stroke: "#0b0f14" });
    });
    plant(layer, 16.0, -11.5, 0.25, 1.1); plant(layer, 32.6, -11.5, 0.25, 1.1);
    // kitchen
    glow(layer, 41, -7.6, 0, 7, 2.2, "url(#warmSoft)");
    box(layer, 35.0, -4.6, 0, 8.0, 1.3, 1.2, ["#4b4f57", "#33373d", "#2a2d33"]);
    box(layer, 37.0, -4.3, 1.2, 1.1, 0.9, 0.9, ["#20242a", "#16191d", "#121417"]);
    box(layer, 45.2, -5.2, 0, 2.0, 1.8, 3.4, ["#c9ced6", "#aeb4bd", "#8f959e"]);
    box(layer, 39.4, -10.4, 0, 4.8, 1.8, 0.95, ["#5a4330", "#46331f", "#3a2a1a"]);
    [[38.6, -11.3], [44.6, -11.3]].forEach(function (st) { box(layer, st[0], st[1], 0, 0.8, 0.8, 0.7, ["#2e3239", "#24272d", "#1d2025"]); });
    plant(layer, 48.6, -11.0, 0, 1.2); plant(layer, 34.0, -11.2, 0, 0.9);
    [[-2.2, -11.4], [56.4, -11.4]].forEach(function (lp) {
      var base = P(lp[0], lp[1], 0);
      el("rect", { x: base[0] - 2, y: base[1] - 44, width: 4, height: 44, fill: "#2a3038" }, layer);
      el("circle", { cx: base[0], cy: base[1] - 48, r: 8, fill: "#ffe2b0" }, layer);
      el("circle", { cx: base[0], cy: base[1] - 48, r: 30, fill: "url(#warm)" }, layer);
    });
  }

  /* ------------------------------------------------------------------ bots */
  function icon(group, kind) {
    var c = "#0b0f14";
    if (kind === "check") { el("polyline", { points: "-5,0 -1.5,4 5,-4", fill: "none", stroke: c, "stroke-width": 2.4 }, group); }
    else if (kind === "cross") { el("path", { d: "M-4,-4 L4,4 M4,-4 L-4,4", stroke: c, "stroke-width": 2.4 }, group); }
    else if (kind === "hourglass") { el("path", { d: "M-4,-5 H4 L-4,5 H4 Z", fill: "none", stroke: c, "stroke-width": 1.8 }, group); }
    else if (kind === "noentry") { el("rect", { x: -5, y: -1.4, width: 10, height: 2.8, fill: c }, group); }
    else if (kind === "question") { svgText(group, 0, 4, "?", { "text-anchor": "middle", class: "glyph" }); }
    else if (kind === "gear") { el("circle", { r: 3.6, fill: "none", stroke: c, "stroke-width": 2.2, "stroke-dasharray": "3 2" }, group); }
    else { el("rect", { x: -4, y: -1, width: 8, height: 2, fill: c, opacity: 0.6 }, group); }
  }

  function makeBot(layer, room, label) {
    var look = LOOKS[room];
    var g = el("g", { class: "bot", "data-bot": room, tabindex: 0, role: "button", "aria-label": label + " bot" }, layer);
    var body = el("g", { class: "bot-body" }, g);
    el("ellipse", { cx: 0, cy: 1, rx: 15, ry: 5, fill: "#000", opacity: 0.35 }, body);
    el("rect", { x: -9, y: -30, width: 18, height: 22, rx: 8, fill: look.body }, body);
    el("rect", { x: -6, y: -26, width: 12, height: 9, rx: 3, fill: look.accent, opacity: 0.9 }, body);
    el("rect", { x: -13, y: -27, width: 5, height: 13, rx: 2.5, fill: look.body }, body);
    el("rect", { x: 8, y: -27, width: 5, height: 13, rx: 2.5, fill: look.body }, body);
    el("rect", { x: -7, y: -10, width: 5, height: 9, rx: 2, fill: "#cfd5dc" }, body);
    el("rect", { x: 2, y: -10, width: 5, height: 9, rx: 2, fill: "#cfd5dc" }, body);
    var head = el("g", { class: "head" }, body);
    el("rect", { x: -13, y: -54, width: 26, height: 23, rx: 10, fill: look.body }, head);
    var face = el("g", { class: "face" }, head);
    el("rect", { x: -10, y: -50, width: 20, height: 14, rx: 6, fill: "#0e141b" }, face);
    var eyes = el("g", { class: "eyes" }, face);
    el("circle", { cx: -4.5, cy: -43, r: 2.4, fill: "#67e8f9" }, eyes);
    el("circle", { cx: 4.5, cy: -43, r: 2.4, fill: "#67e8f9" }, eyes);
    el("rect", { x: -9, y: -50, width: 18, height: 12, rx: 5, fill: look.body, class: "back-of-head" }, head);
    if (look.extra === "dish") {
      el("line", { x1: 0, y1: -54, x2: 0, y2: -61, stroke: "#9aa4b2", "stroke-width": 2 }, head);
      el("path", { d: "M-6,-62 Q0,-68 6,-62", fill: "none", stroke: look.accent, "stroke-width": 2.5 }, head);
    } else if (look.extra === "headset") {
      el("path", { d: "M-14,-42 Q-14,-58 0,-58 Q14,-58 14,-42", fill: "none", stroke: "#334155", "stroke-width": 3 }, head);
      el("rect", { x: -16, y: -46, width: 5, height: 9, rx: 2, fill: look.accent }, head);
    } else if (look.extra === "visor") {
      el("rect", { x: -14, y: -47, width: 28, height: 6, rx: 3, fill: look.accent, opacity: 0.85 }, head);
    } else if (look.extra === "helmet") {
      el("path", { d: "M-15,-50 Q0,-64 15,-50 Z", fill: look.accent }, head);
    } else if (look.extra === "glasses") {
      el("circle", { cx: -4.5, cy: -43, r: 4.5, fill: "none", stroke: look.accent, "stroke-width": 1.6 }, head);
      el("circle", { cx: 4.5, cy: -43, r: 4.5, fill: "none", stroke: look.accent, "stroke-width": 1.6 }, head);
    } else if (look.extra === "clipboard") {
      el("rect", { x: 9, y: -24, width: 9, height: 12, rx: 1.5, fill: "#e9d5ff", stroke: look.accent }, body);
    } else if (look.extra === "badge") {
      el("circle", { cx: -4, cy: -19, r: 3.5, fill: look.accent }, body);
    } else if (look.extra === "beret") {
      el("ellipse", { cx: -2, cy: -55, rx: 12, ry: 4.5, fill: look.accent }, head);
      el("path", { d: "M-15,-44 Q-15,-58 0,-58 Q15,-58 15,-44", fill: "none", stroke: "#2b2b2b", "stroke-width": 2.5 }, head);
    }
    var prop = el("g", { class: "prop" }, g);
    el("rect", { x: 12, y: -22, width: 7, height: 8, rx: 1.5, fill: "#f5efe0" }, prop);
    el("path", { d: "M19,-20 Q23,-18 19,-15", fill: "none", stroke: "#f5efe0", "stroke-width": 1.5 }, prop);
    var bubble = el("g", { class: "bubble", transform: "translate(0,-70)" }, g);
    el("circle", { r: 12, class: "ring", fill: "none", "stroke-width": 2 }, bubble);
    el("circle", { r: 9, class: "dot" }, bubble);
    var glyph = el("g", { class: "glyph-wrap" }, bubble);
    var nameTag = el("g", { class: "name-tag" }, g);
    el("rect", { x: -40, y: -101, width: 80, height: 15, rx: 7.5, fill: "#0b0f14", opacity: 0.85 }, nameTag);
    svgText(nameTag, 0, -90, label, { "text-anchor": "middle", class: "name" });
    var title = el("title", {}, g);
    g.addEventListener("click", function (event) { event.stopPropagation(); select(room); });
    g.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(room); }
    });
    var start = nodes["seat_" + room];
    return { room: room, g: g, bubble: bubble, glyph: glyph, title: title, pos: { x: start.x, y: start.y, z: start.z },
      node: "seat_" + room, path: [], goal: "seat_" + room, cycle: 0, dwellUntil: 0, activity: "sit", state: "idle",
      jitter: (C.rng(C.hashSeed(room))() - 0.5) * 1.2 };
  }

  /* ------------------------------------------------------------------ build */
  function build() {
    var svg = $("stage");
    S.viewBox = viewFor("house", null);
    applyViewBox();
    defs(svg);
    ["bg", "ground", "structure", "upper", "slabs", "terrace", "balcony", "bots", "fx", "fg", "labels"].forEach(function (name) {
      S.layers[name] = el("g", { class: "layer-" + name }, svg);
    });
    drawBackground(S.layers.bg);
    drawStructure(S.layers.structure);
    drawTerrace(S.layers.terrace);
    drawBalconyAndStairs(S.layers.balcony);
    drawRailings(S.layers.fg);
    svg.addEventListener("click", function () { if (S.presenting) { focusRoom(null); } });
  }

  function roomMeta(room) {
    var found = null;
    (S.scene ? S.scene.rooms : []).forEach(function (r) { if (r.room === room) { found = r; } });
    return found;
  }

  function buildRooms() {
    ["ground", "upper", "slabs", "bots", "labels"].forEach(function (name) { clear(S.layers[name]); });
    S.bots = {};
    BOT_ROOMS.forEach(function (room) {
      var meta = roomMeta(room);
      drawRoom(ROOM_SLOTS[room].z > 0 ? S.layers.upper : S.layers.ground, room, meta);
    });
    drawSlabs(S.layers.slabs);
    BOT_ROOMS.forEach(function (room) { S.bots[room] = makeBot(S.layers.bots, room, roomMeta(room).label); });
    buildLabels();
    buildBotButtons();
  }

  function buildLabels() {
    var layer = S.layers.labels;
    S.chips = {};
    BOT_ROOMS.forEach(function (room) {
      var meta = roomMeta(room), s = ROOM_SLOTS[room], x0 = s.i * W, trading = meta.department === "trading";
      var anchor = P(x0 + 0.5, D, s.z + 6.05);
      var g = el("g", { class: "room-label", transform: "translate(" + anchor[0].toFixed(1) + "," + anchor[1].toFixed(1) + ")" }, layer);
      el("rect", { x: 0, y: 0, width: 212, height: 24, rx: 5, class: "chip-bg" }, g);
      el("rect", { x: 0, y: 0, width: 4, height: 24, rx: 2, fill: trading ? "#2dd4bf" : "#a78bfa" }, g);
      svgText(g, 12, 16.5, meta.label, { class: "room-name" });
      var status = el("g", { class: "room-status", transform: "translate(134,12)" }, g);
      var dot = el("circle", { r: 4.5 }, status);
      var text = svgText(status, 9, 4, "", { class: "room-state" });
      var placardAt = P(x0 + 6, 2.2, s.z);
      var placard = el("g", { class: "placard", transform: "translate(" + placardAt[0].toFixed(1) + "," + (placardAt[1] - 12).toFixed(1) + ")" }, layer);
      el("rect", { x: -78, y: -13, width: 156, height: 24, rx: 12, class: "placard-bg" }, placard);
      svgText(placard, 0, 4, "No recorded activity", { "text-anchor": "middle", class: "placard-text" });
      S.chips[room] = { g: g, dot: dot, text: text, placard: placard };
    });
    // operations console board
    var at = P(18.6, -8.4, 4.35);
    var board = el("g", { class: "ops-board", transform: "translate(" + at[0].toFixed(1) + "," + at[1].toFixed(1) + ")" }, layer);
    el("rect", { x: -1 * U, y: 0, width: 15 * U, height: 4.1 * U, rx: 4, class: "board-bg" }, board);
    svgText(board, -8, 14, "OPERATIONS STATIONS", { class: "sign" });
    S.ops = {};
    S.scene.stations.forEach(function (station, n) {           // 3 x 3 lights; full labels in the station strip
      var cell = el("g", { transform: "translate(" + (-8 + (n % 3) * 98) + "," + (32 + Math.floor(n / 3) * 15) + ")" }, board);
      var dot = el("circle", { r: 4, cy: -4 }, cell);
      svgText(cell, 8, 0, station.short, { class: "ops-name" });
      var title = el("title", {}, cell);
      if (station.station === "simulator") {                  // Step 34: the simulator light opens the results desk
        cell.setAttribute("class", "ops-link");
        cell.setAttribute("tabindex", "0");
        cell.setAttribute("role", "button");
        cell.setAttribute("aria-label", "Simulator: open the trading results desk");
        el("rect", { x: -8, y: -12, width: 94, height: 15, rx: 3, class: "ops-hit" }, cell);
        cell.addEventListener("click", function (event) { event.stopPropagation(); openResults(); });
        cell.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); openResults(); }
        });
      }
      S.ops[station.component] = { dot: dot, title: title, label: station.label };
    });
    S.opsCounters = svgText(board, -8, 79, "", { class: "ops-name counters" });
    [["Lounge", -1.5, -TD + 0.4], ["Kitchen", 34.5, -TD + 0.4]].forEach(function (lbl) {
      var p = P(lbl[1], lbl[2], 0);
      var g2 = el("g", { class: "room-label area", transform: "translate(" + p[0].toFixed(1) + "," + (p[1] - 30).toFixed(1) + ")" }, layer);
      el("rect", { x: 0, y: 0, width: 80, height: 22, rx: 5, class: "chip-bg" }, g2);
      svgText(g2, 10, 15, lbl[0], { class: "room-name small" });
    });
  }

  function buildBotButtons() {
    var list = $("bot-buttons");
    clear(list);
    S.scene.rooms.forEach(function (meta) {
      if (meta.room === "lounge" || meta.room === "kitchen") { return; }
      var li = h("li", {}, list);
      var button = h("button", { type: "button", "data-room": meta.room, "aria-pressed": "false",
        class: "room-btn " + (meta.department === "trading" ? "trading" : meta.department === "content" ? "content" : "shared") }, li);
      h("span", { class: "dot", "aria-hidden": "true" }, button);
      h("span", { class: "label" }, button, meta.label);
      h("span", { class: "state" }, button, "");
      button.addEventListener("click", function () { select(meta.room); });
    });
  }

  /* ------------------------------------------------------------------ status rendering */
  function statesNow() {
    if (!S.scene) { return {}; }
    if (S.source === "now" && S.scene.current) { return S.scene.current.room_states; }
    return C.roomStatesAt(S.scene, S.replay.index, ALL_ROOMS);
  }
  function componentStatesNow() {
    if (!S.scene) { return {}; }
    if (S.source === "now" && S.scene.current) { return S.scene.current.component_states; }
    var frame = S.scene.frames[S.replay.index];
    return frame ? frame.component_states : {};
  }

  function renderStatuses(stepped) {
    if (!S.scene) { return; }
    var states = statesNow(), components = componentStatesNow(), now = performance.now();
    BOT_ROOMS.forEach(function (room) {
      var meta = roomMeta(room), state = states[room] || "idle", style = C.stateStyle(state), chip = S.chips[room];
      var active = meta.has_activity;
      chip.dot.setAttribute("fill", active ? style.color : "#4b5563");
      chip.text.textContent = active ? style.label : "No activity";
      chip.placard.setAttribute("visibility", active ? "hidden" : "visible");
      var bot = S.bots[room];
      var latest = C.lastEventFor(S.scene, S.source === "now" ? S.scene.frames.length - 1 : S.replay.index, meta.components);
      if (bot.state !== state && state === "completed") {
        // Finished in an earlier attempt: no visit to the station, it did not run now.
        S.completedAt[room] = latest && latest.event.event_type === "stage_reused" ? -1e12 : now;
      }
      bot.state = state;
      bot.g.setAttribute("data-state", state);
      bot.bubble.querySelector(".dot").setAttribute("fill", style.color);
      bot.bubble.querySelector(".ring").setAttribute("stroke", style.color);
      clear(bot.glyph);
      icon(bot.glyph, style.icon);
      bot.title.textContent = meta.label + ": " + style.label + (active ? "" : " (no recorded activity)");
    });
    Object.keys(S.ops).forEach(function (component) {
      var state = components[component] || "idle", style = C.stateStyle(state);
      var seen = S.scene.frames.some(function (f) { return f.event.component === component; });
      S.ops[component].dot.setAttribute("fill", seen ? style.color : "#4b5563");
      S.ops[component].title.textContent = S.ops[component].label + ": " + (seen ? style.label : "No recorded activity");
    });
    var counts = C.counters(S.scene, S.source === "now" ? S.scene.frames.length - 1 : S.replay.index);
    S.opsCounters.textContent = "Simulated orders: " + counts.accepted + " accepted · " + counts.rejected + " rejected · " +
      counts.fills + " fills";
    renderStations(components);
    renderReplay();
    renderList();
    renderButtons(states);
    renderInspector();
    renderEventRows();
    renderFeed();
    syncResults();
    if (stepped) { announce(); }
  }

  function renderButtons(states) {
    Array.prototype.forEach.call(document.querySelectorAll(".room-btn"), function (button) {
      var room = button.getAttribute("data-room"), meta = roomMeta(room), state = states[room] || "idle";
      var style = C.stateStyle(state);
      button.querySelector(".dot").style.background = meta.has_activity ? style.color : "#4b5563";
      button.querySelector(".state").textContent = meta.has_activity ? style.label : "No activity";
      button.setAttribute("aria-pressed", S.selected === room ? "true" : "false");
      button.setAttribute("aria-label", meta.label + ", " + (meta.has_activity ? style.label : "no recorded activity"));
    });
  }

  function endpoint(room, station) {
    var named = station && S.scene.stations.filter(function (s) { return s.station === station; })[0];
    return named ? named.label : roomMeta(room).label;
  }
  /* Where an event happened: the room, or the named operations station. */
  function whereLabel(frame) {
    if (!frame || !frame.room) { return frame ? "(not mapped)" : null; }
    if (frame.room === "operations") {
      var station = S.scene.stations.filter(function (s) { return s.component === frame.event.component; })[0];
      if (station) { return station.label; }
    }
    return roomMeta(frame.room).label;
  }
  function frameLabel(frame) {
    return C.describeEvent(frame ? frame.event : null, whereLabel(frame));
  }

  function renderReplay() {
    var count = S.scene.frames.length, index = S.replay.index, frame = S.scene.frames[index];
    var scrub = $("scrubber");
    scrub.max = String(count);
    scrub.value = String(index + 1);
    scrub.setAttribute("aria-valuetext", "Event " + (index + 1) + " of " + count);
    $("position").textContent = (index + 1) + " / " + count;
    var play = $("btn-play");
    play.textContent = S.replay.playing ? "⏸" : "▶";
    play.setAttribute("aria-label", S.replay.playing ? "Pause" : "Play");
    var nowMode = S.source === "now" && S.scene.current;
    var caption = nowMode ? "Showing the current state as loaded (" + C.words(S.scene.timeline.completeness) + ")" :
      (frame ? "Event " + (index + 1) + "/" + count + " · " + frameLabel(frame) +
        " · recorded " + C.formatTime(frame.event.recorded_at) +
        (frame.event.sim_time_utc ? " · simulated " + C.formatTime(frame.event.sim_time_utc) : "") +
        (frame.handoff ? " · handoff " + endpoint(frame.handoff.from, frame.handoff.from_station) + " → " +
          endpoint(frame.handoff.to, frame.handoff.to_station) : "")
        : (count ? "Before the first recorded event: press Play" : "This timeline has no events"));
    $("caption").textContent = caption;
    $("src-now").disabled = !S.scene.current;
    $("src-replay").setAttribute("aria-pressed", S.source === "replay" ? "true" : "false");
    $("src-now").setAttribute("aria-pressed", S.source === "now" ? "true" : "false");
  }

  function announce() {
    var frame = S.scene.frames[S.replay.index];
    $("announcer").textContent = frame ? "Event " + (S.replay.index + 1) + ": " + frameLabel(frame) : "Before the first event";
  }

  /* ------------------------------------------------------------------ mode banner */
  function renderMode() {
    var scene = S.scene, info = C.MODES[scene.mode], badge = $("mode-badge");
    badge.textContent = info.badge;
    badge.className = "mode mode-" + info.tone;
    badge.setAttribute("title", scene.mode_label);
    var t = scene.timeline;
    $("timeline-name").textContent = t.timeline_id === "demo" ? "Demo HQ" : C.words(t.kind) + " · " + t.timeline_id;
    var chip = $("completeness-chip");
    chip.textContent = C.words(t.completeness);
    chip.className = "chip completeness-" + t.completeness;
    $("timeline-meta").textContent = scene.mode_label + (t.outcome ? " · outcome: " + C.words(t.outcome) : "") +
      (t.time_basis === "simulated_only" ? " · no wall-clock times were saved" : "");
    $("timeline-issues").textContent = scene.issues.length ? "Issues: " + scene.issues.map(C.words).join(", ") : "";
    var live = $("live-indicator");
    live.hidden = scene.mode !== "observed";
    $("footnote").textContent = (scene.mode === "demo" ? "Demo data is synthetic and deterministic; it is not a real run. " : "") +
      "Read-only. Idle wandering is decoration: it never runs agents, trades, publishing or messages.";
    $("stage-title").textContent = "ViceKrack headquarters, " + info.badge.toLowerCase();
  }

  /* ------------------------------------------------------------------ inspector */
  function row(parent, label, value) {
    var r = h("div", { class: "kv" }, parent);
    h("span", { class: "k" }, r, label);
    var v = h("span", { class: "v" }, r);
    if (value instanceof Node) { v.appendChild(value); } else { v.textContent = C.plain(value); }
    return r;
  }
  function stateChip(state, note) {
    var style = C.stateStyle(state), span = h("span", { class: "state-chip" });
    var dot = h("span", { class: "dot", "aria-hidden": "true" }, span);
    dot.style.background = style.color;
    h("span", {}, span, style.label + (note ? " · " + C.words(note) : ""));
    return span;
  }

  function renderInspector() {
    var body = $("inspector-body");
    clear(body);
    var scene = S.scene;
    if (!scene) { return; }
    if (!S.selected) {
      var dept = S.view === "trading" || S.view === "results" ? "trading" : S.view === "content" ? "content" : null;
      h("h2", { class: "panel-title" }, body, dept ? (dept === "trading" ? "Trading department" : "Content department") : "Headquarters");
      row(body, "Data", scene.mode_label);
      row(body, "Timeline", scene.timeline.timeline_id);
      row(body, "Completeness", C.words(scene.timeline.completeness));
      if (scene.timeline.outcome) { row(body, "Outcome", C.words(scene.timeline.outcome)); }
      row(body, "Events", String(scene.frames.length));
      if (scene.timeline.source) {
        row(body, "Source", C.words(scene.timeline.source.kind) + " " + C.plain(scene.timeline.source.id));
        row(body, "Saved at", C.formatTime(scene.timeline.source.saved_at));
      }
      if (scene.issues.length) { row(body, "Issues", scene.issues.map(C.words).join(", ")); }
      if (scene.unmapped_components.length) { row(body, "Not shown in a room", scene.unmapped_components.join(", ")); }
      var states = statesNow();
      var table = h("ul", { class: "mini-list" }, body);
      scene.rooms.forEach(function (meta) {
        if (!meta.components.length || (dept && meta.department !== dept)) { return; }
        var li = h("li", {}, table);
        var b = h("button", { type: "button", class: "link" }, li, meta.label);
        b.addEventListener("click", function () { select(meta.room); });
        li.appendChild(meta.has_activity ? stateChip(states[meta.room] || "idle") : h("span", { class: "muted" }, null, "No recorded activity"));
      });
      if (dept === "trading") {
        var open = h("button", { type: "button", class: "action" }, body, "Open trading results desk");
        open.addEventListener("click", openResults);
        h("p", { class: "muted small" }, body, "Simulated results of the selected simulation run (read-only).");
      }
      if (dept) {
        h("h3", {}, body, "Timelines in this department");
        var picks = h("ul", { class: "mini-list" }, body);
        var mine = S.timelines.filter(function (t) { return t.department === dept; }).slice(0, 6);
        mine.forEach(function (t) {
          var li = h("li", {}, picks);
          var b = h("button", { type: "button", class: "link" }, li, timelineName(t));
          b.disabled = t.readable === false;
          b.addEventListener("click", function () { loadScene(t.id); });
          h("span", { class: "muted small" }, li, t.origin);
        });
        if (!mine.length) { h("li", { class: "muted" }, picks, "None saved yet. See the Timeline view for how to record one."); }
      }
      h("p", { class: "muted small" }, body, "Select a bot or room to inspect it. " + scene.notice);
      return;
    }
    var meta = roomMeta(S.selected);
    var head = h("div", { class: "panel-head" }, body);
    h("span", { class: "accent " + meta.department }, head);
    h("h2", { class: "panel-title" }, head, meta.label);
    var close = h("button", { type: "button", class: "close", "aria-label": "Close inspector" }, head, "×");
    close.addEventListener("click", function () { select(null); });
    h("p", { class: "role" }, body, meta.role);
    row(body, "Driven by", meta.components.length ? meta.components.join(", ") : "Nothing (decorative space)");
    row(body, "Mapping", meta.mapping);
    if (S.selected === "operations") { renderStationDetails(body); return; }
    if (S.bots[S.selected]) { row(body, "Movement", C.movementFor(S.bots[S.selected].intent)); }
    var frameStates = C.roomStatesAt(scene, S.replay.index, ALL_ROOMS);
    if (!meta.has_activity) {
      row(body, "Recorded status", "No recorded activity in this timeline");
    } else {
      row(body, "At replay position", stateChip(frameStates[S.selected] || "idle"));
      if (scene.current) {
        var notes = meta.components.map(function (c) { return scene.current.notes[c]; }).filter(Boolean);
        row(body, "Now (as loaded)", stateChip(scene.current.room_states[S.selected] || "idle", notes[0]));
      }
      var last = C.lastEventFor(scene, S.replay.index, meta.components);
      h("h3", {}, body, "Last recorded event");
      if (!last) {
        h("p", { class: "muted" }, body, "None yet at this replay position.");
      } else {
        var e = last.event;
        row(body, "Event", "#" + e.sequence + " " + C.words(e.event_type) + " (" + C.words(e.status) + ")");
        row(body, "Component", e.component);
        row(body, "Recorded at", C.formatTime(e.recorded_at));
        row(body, "Simulated time", e.sim_time_utc ? C.formatTime(e.sim_time_utc) : "none");
        var codes = h("span", { class: "codes" });
        (e.reason_codes.length ? e.reason_codes : ["none"]).forEach(function (code) { h("code", {}, codes, code); });
        row(body, "Reason codes", codes);
        Object.keys(e.details || {}).forEach(function (key) {
          if (e.details[key] !== null) { row(body, C.words(key), String(e.details[key])); }
        });
        var refs = h("span", { class: "codes" });
        (e.refs.length ? e.refs : [{ kind: "none", id: "" }]).forEach(function (ref) {
          h("code", {}, refs, C.words(ref.kind) + (ref.id ? " " + ref.id : "") + (scene.mode === "demo" && ref.id ? " (demo)" : ""));
        });
        row(body, "References", refs);
      }
    }
    h("h3", {}, body, "Role");
    row(body, "Inputs", meta.inputs);
    row(body, "Decisions", meta.decisions);
    row(body, "Outputs", meta.outputs);
  }

  /* Operations stations: automated stages and workflow controllers, each with a text status. */
  function renderStations(components) {
    var list = $("stations");
    clear(list);
    S.scene.stations.forEach(function (station) {
      var state = components[station.component] || "idle", style = C.stateStyle(state);
      var li = h("li", {}, list);
      var b = h("button", { type: "button", class: "station-btn " + station.department }, li);
      var dot = h("span", { class: "dot", "aria-hidden": "true" }, b);
      dot.style.background = station.has_activity ? style.color : "#4b5563";
      h("span", { class: "station-name" }, b, station.label);
      h("span", { class: "station-state" }, b, station.has_activity ? style.label : "No recorded activity");
      if (station.station === "simulator") {
        h("span", { class: "station-open" }, b, "Open results");
        b.setAttribute("aria-label", "Simulator station, " + (station.has_activity ? style.label : "no recorded activity") +
          ". Open the trading results desk");
        b.addEventListener("click", openResults);
        return;
      }
      b.setAttribute("aria-label", station.label + " station, " + (station.has_activity ? style.label : "no recorded activity"));
      b.addEventListener("click", function () { select("operations"); });
    });
  }
  function renderStationDetails(body) {
    var scene = S.scene, index = S.source === "now" ? scene.frames.length - 1 : S.replay.index;
    var states = S.source === "now" && scene.current ? scene.current.component_states :
      (scene.frames[index] ? scene.frames[index].component_states : {});
    h("h3", {}, body, "Stations");
    scene.stations.forEach(function (station) {
      var box = h("div", { class: "station-card" }, body);
      h("strong", {}, box, station.label);
      h("p", { class: "muted small" }, box, station.role);
      if (station.station === "simulator") {
        var open = h("button", { type: "button", class: "action" }, box, "Open trading results");
        open.addEventListener("click", openResults);
      }
      if (!station.has_activity) { h("p", { class: "muted small" }, box, "No recorded activity in this timeline"); return; }
      row(box, "Status", stateChip(states[station.component] || "idle"));
      var last = C.lastEventFor(scene, index, [station.component]);
      if (last) {
        row(box, "Last event", "#" + last.event.sequence + " " + C.words(last.event.event_type));
        row(box, "Recorded at", C.formatTime(last.event.recorded_at));
        if (last.event.reason_codes.length) { row(box, "Reason codes", last.event.reason_codes.join(", ")); }
      }
    });
  }

  /* Recent events up to the replay position (newest first): a readable companion to the house. */
  function renderFeed() {
    var list = $("feed");
    clear(list);
    var end = S.source === "now" ? S.scene.frames.length - 1 : S.replay.index;
    for (var n = end; n >= 0 && n > end - 6; n -= 1) {
      var frame = S.scene.frames[n], style = C.stateStyle(stateForEvent(frame.event));
      var li = h("li", {}, list);
      var dot = h("span", { class: "dot", "aria-hidden": "true" }, li);
      dot.style.background = style.color;
      h("span", { class: "feed-seq" }, li, "#" + frame.event.sequence);
      h("span", { class: "feed-text" }, li, frameLabel(frame));
      h("span", { class: "feed-time" }, li, frame.event.recorded_at ? C.formatTime(frame.event.recorded_at) :
        (frame.event.sim_time_utc ? "sim " + C.formatTime(frame.event.sim_time_utc) : "time not recorded"));
    }
    if (end < 0) { h("li", { class: "muted" }, list, S.scene.frames.length ? "No events yet at this replay position." : "This timeline has no events."); }
  }
  function stateForEvent(event) {
    var map = { stage_started: "working", stage_completed: "completed", stage_failed: "failed", stage_blocked: "blocked",
      stage_interrupted: "unknown", stage_reused: "completed", order_decision: event.status === "rejected" ? "blocked" : "working", simulated_fill: "completed" };
    return map[event.event_type] || "unknown";
  }

  /* ------------------------------------------------------------------ list + timeline views */
  function renderList() {
    if (!S.list && S.view !== "timeline") { return; }
    var body = $("list-body");
    clear(body);
    var states = C.roomStatesAt(S.scene, S.replay.index, ALL_ROOMS);
    S.scene.rooms.forEach(function (meta) {
      if (!meta.components.length) { return; }
      var tr = h("tr", {}, body);
      h("th", { scope: "row" }, tr, meta.label);
      h("td", {}, tr, meta.components.join(", "));
      h("td", {}, tr, meta.has_activity ? C.stateStyle(states[meta.room]).label : "No recorded activity");
      h("td", {}, tr, S.scene.current ? C.stateStyle(S.scene.current.room_states[meta.room]).label : "Demo (no live state)");
      var last = C.lastEventFor(S.scene, S.replay.index, meta.components);
      h("td", {}, tr, last ? "#" + last.event.sequence + " " + C.words(last.event.event_type) : "-");
      h("td", {}, tr, last ? C.formatTime(last.event.recorded_at) : "-");
      h("td", {}, tr, last && last.event.reason_codes.length ? last.event.reason_codes.join(", ") : "-");
    });
  }

  function renderEventRows() {
    if (S.view !== "timeline") { return; }
    var body = $("event-body");
    if (body.getAttribute("data-scene") !== S.scene.timeline.timeline_id + ":" + S.scene.frames.length) {
      clear(body);
      body.setAttribute("data-scene", S.scene.timeline.timeline_id + ":" + S.scene.frames.length);
      S.scene.frames.forEach(function (frame, n) {
        var tr = h("tr", { "data-index": n }, body);
        h("td", {}, tr, String(frame.event.sequence));
        h("td", {}, tr, whereLabel(frame));
        h("td", {}, tr, C.words(frame.event.event_type));
        h("td", {}, tr, C.words(frame.event.status));
        h("td", {}, tr, frame.event.sim_time_utc ? C.formatTime(frame.event.sim_time_utc) : "-");
        h("td", {}, tr, C.formatTime(frame.event.recorded_at));
        h("td", {}, tr, frame.event.reason_codes.join(", ") || "-");
        var td = h("td", {}, tr);
        var go = h("button", { type: "button", class: "link" }, td, "Go");
        go.setAttribute("aria-label", "Go to event " + frame.event.sequence);
        go.addEventListener("click", function () { seek(n); });
      });
    }
    Array.prototype.forEach.call(body.children, function (tr) {
      tr.classList.toggle("current", Number(tr.getAttribute("data-index")) === S.replay.index);
    });
  }

  function timelineName(item) {
    if (item.id === "demo") { return item.label; }
    return C.words(item.kind || "timeline") + (item.attempts > 1 ? " · attempt " + item.attempt + " of " + item.attempts : "") +
      " · " + item.id;
  }
  function renderTimelines() {
    var list = $("timeline-list");
    clear(list);
    Array.prototype.forEach.call(document.querySelectorAll(".filter-btn"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-filter") === S.filter ? "true" : "false");
    });
    S.timelines.filter(function (item) {
      return S.filter === "all" || item.department === S.filter || item.id === "demo";
    }).forEach(function (item) {
      var li = h("li", {}, list);
      var button = h("button", { type: "button", class: "timeline-item" + (item.id === S.timelineId ? " active" : ""),
        "aria-pressed": item.id === S.timelineId ? "true" : "false" }, li);
      h("span", { class: "tl-origin origin-" + item.origin }, button, item.origin === "demo" ? "demo" : item.origin);
      h("span", { class: "tl-label" }, button, timelineName(item));
      h("span", { class: "tl-meta" }, button, [item.department, item.completeness ? C.words(item.completeness) : null,
        item.live ? "live" : null, item.event_count !== undefined && item.event_count !== null ? item.event_count + " events" : null,
        item.correlation_id ? "run " + item.correlation_id.slice(4, 12) : null,
        item.readable === false ? "unreadable" : null].filter(Boolean).join(" · "));
      button.disabled = item.readable === false || !C.ID_PATTERN.test(item.id);
      button.addEventListener("click", function () { loadScene(item.id); });
    });
    if (!S.timelines.length) { h("li", { class: "muted" }, list, "No timelines found."); }
  }

  /* ------------------------------------------------------------------ selection, views, camera */
  function select(room) {
    S.selected = room && roomMeta(room) ? room : null;
    Object.keys(S.roomNodes).forEach(function (r) { S.roomNodes[r].floor.classList.toggle("selected", r === S.selected); });
    Object.keys(S.bots).forEach(function (r) { S.bots[r].g.classList.toggle("selected", r === S.selected); });
    if (S.presenting && room && ROOM_SLOTS[room]) { focusRoom(room); }
    renderStatuses(false);
  }

  function bounds(points, pad) {
    var xs = points.map(function (p) { return p[0]; }), ys = points.map(function (p) { return p[1]; });
    var x = Math.min.apply(null, xs) - pad, y = Math.min.apply(null, ys) - pad;
    return [x, y, Math.max.apply(null, xs) - x + pad, Math.max.apply(null, ys) - y + pad];
  }
  function viewFor(view, room) {
    if (room && ROOM_SLOTS[room]) {                         // a fixed-size window centred on the room
      var s = ROOM_SLOTS[room], c = P(s.i * W + W / 2, D / 2, s.z + 3.2), stage = $("stage").getBoundingClientRect();
      var width = 1000, height = width * Math.max(0.35, Math.min(1.2, (stage.height || 450) / (stage.width || 1000)));
      return [c[0] - width / 2, c[1] - height / 2, width, height];
    }
    if (view === "trading") { return bounds([P(-1, -2, FH - 0.5), P(59, D, 2 * FH + 1.5), P(-1, D, 2 * FH + 1.5)], 18); }
    if (view === "content") { return bounds([P(-3, -TD, 0), P(60, D, FH - 0.5), P(-3, D, FH - 0.5), P(60, -TD, 0)], 18); }
    return bounds([P(-4, -TD - 0.5, 0), P(60, -TD - 0.5, 0), P(60, D, 2 * FH + 1.8), P(-4, D, 2 * FH + 1.8)], 14);
  }
  function setCamera(box) {
    S.viewTarget = box;
    if (S.reduced) { S.viewBox = box.slice(); applyViewBox(); }
  }
  function applyViewBox() { $("stage").setAttribute("viewBox", S.viewBox.map(function (v) { return v.toFixed(1); }).join(" ")); }
  function focusRoom(room) { S.focusRoom = room; setCamera(viewFor(S.view, room)); }

  function setView(view) {
    S.view = view;
    Array.prototype.forEach.call(document.querySelectorAll(".view-btn"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-view") === view ? "true" : "false");
    });
    var timeline = view === "timeline", results = view === "results";
    $("timeline-view").hidden = !timeline;
    $("results-view").hidden = !results;
    $("stage-wrap").hidden = timeline || results || S.list;
    $("list-view").hidden = !(S.list && !timeline && !results);
    document.body.classList.toggle("results-open", results);
    setCamera(viewFor(view, S.focusRoom));
    if (timeline) { renderTimelines(); }
    R.renderedKey = null;
    renderStatuses(false);
  }

  /* ------------------------------------------------------------------ replay control */
  function seek(index) {
    var before = S.replay.index;
    S.replay.seek(index);
    S.source = "replay";
    S.following = false;
    afterIndexChange(before, true);
  }
  function afterIndexChange(before, stepped) {
    var frame = S.scene.frames[S.replay.index];
    if (frame && frame.handoff && S.replay.index === before + 1 && !S.reduced) { spawnToken(frame.handoff); }
    renderStatuses(stepped);
  }
  function togglePlay() {
    if (!S.scene) { return; }
    if (S.replay.playing) { S.replay.pause(); } else { S.source = "replay"; S.replay.play(); }
    renderStatuses(false);
  }

  /* ------------------------------------------------------------------ animation loop (decoration) */
  function screenOf(pos) { return P(pos.x, pos.y, pos.z); }
  function targetFor(bot, now) {
    var linger = !S.completedAt[bot.room] || now - S.completedAt[bot.room] > 3500;
    var intent = C.intentFor(bot.state, linger);
    bot.intent = intent;
    if (intent === "station") { return { node: "st_" + bot.room, activity: "work" }; }
    if (intent === "seat") { return { node: "seat_" + bot.room, activity: "wait" }; }
    if (intent === "door") { return { node: "door_" + bot.room, activity: "blocked" }; }
    if (intent === "hold") { return null; }
    if (S.reduced) { return { node: "seat_" + bot.room, activity: "sit" }; }
    if (now >= bot.dwellUntil || !bot.decor) {
      bot.cycle += 1;
      var taken = {};
      Object.keys(S.bots).forEach(function (other) {           // occupancy: one bot per shared idle spot
        if (other !== bot.room && S.bots[other].goal) { taken[S.bots[other].goal] = true; }
      });
      var choice = C.wanderChoice(bot.room, bot.cycle, spotsFor(bot.room), taken);
      bot.decor = choice;
      bot.dwellUntil = now + choice.dwellMs + 4000;
    }
    return { node: bot.decor.node, activity: bot.decor.activity };
  }

  function moveBot(bot, dt, now) {
    var target = targetFor(bot, now);
    if (!target) { bot.path = []; }
    else if (target.node !== bot.goal) {
      bot.goal = target.node;
      var start = bot.path.length ? bot.path[0] : bot.node;
      bot.path = graph.path(start, target.node) || [target.node];
    }
    bot.activity = target ? target.activity : "hold";
    if (S.reduced && target) {
      var spot = nodes[target.node];
      bot.pos = { x: spot.x, y: spot.y, z: spot.z };
      bot.node = target.node;
      bot.path = [];
    }
    var speed = 3.6 * dt / 1000;
    while (bot.path.length && speed > 0) {
      var next = nodes[bot.path[0]];
      var tx = next.x, ty = next.y, tz = next.z;
      var dx = tx - bot.pos.x, dy = ty - bot.pos.y, dz = tz - bot.pos.z;
      var dist = Math.sqrt(dx * dx + dy * dy + dz * dz * 0.25);
      if (dist <= speed) {
        bot.pos = { x: tx, y: ty, z: tz };
        bot.node = bot.path.shift();
        speed -= dist;
      } else {
        bot.pos = { x: bot.pos.x + dx / dist * speed, y: bot.pos.y + dy / dist * speed, z: bot.pos.z + dz / dist * speed };
        speed = 0;
      }
    }
    var walking = bot.path.length > 0;
    var p = screenOf(bot.pos);
    var bob = (!S.reduced && walking) ? Math.sin(now / 90) * 1.5 : 0;
    var sit = !walking && (bot.activity === "sit" || bot.activity === "relax" || bot.activity === "wait") ? 4 : 0;
    bot.g.setAttribute("transform", "translate(" + p[0].toFixed(1) + "," + (p[1] + bob + sit).toFixed(1) + ") scale(1.25)");
    bot.g.classList.toggle("back", !walking && bot.activity === "work");
    bot.g.classList.toggle("coffee", !walking && bot.activity === "coffee");
  }

  function spawnToken(handoff) {
    var a = "st_" + handoff.from, b = "st_" + handoff.to;
    if (!nodes[a]) { a = "o_entry"; }
    if (!nodes[b]) { b = "o_entry"; }
    var route = graph.path(a, b);
    if (!route) { return; }
    var g = el("g", { class: "token" }, S.layers.fx);
    el("circle", { r: 16, fill: "url(#warm)" }, g);
    el("rect", { x: -6, y: -8, width: 12, height: 15, rx: 2, fill: "#fff7e6", stroke: "#f5b942" }, g);
    el("line", { x1: -3, y1: -3, x2: 3, y2: -3, stroke: "#c08a2b" }, g);
    el("line", { x1: -3, y1: 1, x2: 3, y2: 1, stroke: "#c08a2b" }, g);
    S.tokens.push({ g: g, route: route.map(function (id) { return nodes[id]; }), t: 0 });
  }
  function moveTokens(dt) {
    S.tokens = S.tokens.filter(function (token) {
      token.t += dt * 4.5 * S.replay.speed / 1000;
      var seg = Math.floor(token.t);
      if (seg >= token.route.length - 1) { token.g.parentNode.removeChild(token.g); return false; }
      var a = token.route[seg], b = token.route[seg + 1], f = token.t - seg;
      var p = P(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, a.z + (b.z - a.z) * f + 3.2);
      token.g.setAttribute("transform", "translate(" + p[0].toFixed(1) + "," + p[1].toFixed(1) + ")");
      return true;
    });
  }

  var lastFrame = performance.now(), orderKey = "";
  function loop(now) {
    var dt = Math.min(100, now - lastFrame);
    lastFrame = now;
    if (S.scene && S.replay) {
      var before = S.replay.index;
      var advanced = S.replay.tick(dt);
      if (advanced) { afterIndexChange(before, false); }
      Object.keys(S.bots).forEach(function (room) { moveBot(S.bots[room], dt, now); });
      var order = Object.keys(S.bots).sort(function (a, b) {
        return (S.bots[b].pos.y - S.bots[a].pos.y) || (S.bots[a].pos.z - S.bots[b].pos.z);
      });
      if (order.join() !== orderKey) {
        orderKey = order.join();
        order.forEach(function (room) { S.layers.bots.appendChild(S.bots[room].g); });
      }
      moveTokens(dt);
    }
    if (S.viewTarget) {
      var done = true;
      S.viewBox = S.viewBox.map(function (v, k) {
        var target = S.viewTarget[k], next = S.reduced ? target : v + (target - v) * Math.min(1, dt / 160);
        if (Math.abs(target - next) > 0.5) { done = false; return next; }
        return target;
      });
      applyViewBox();
      if (done) { S.viewTarget = null; }
    }
    window.requestAnimationFrame(loop);
  }

  /* ------------------------------------------------------------------ trading results desk (Step 34)
   * Read-only. Three GET documents per simulation timeline: an index (no results), the portfolio at
   * one replay position (the server sends nothing from later events), and a separately labelled
   * completed-run summary. Only the latest requested position is ever drawn. */
  var R = { timelineId: null, index: null, at: null, summary: null, tab: "position", inflightAt: false,
    inflightSummary: false, wanted: null, shown: null, error: null, generation: 0 };
  var ROW_LIMIT = 200;

  function resultsAllowed() {
    var t = S.scene && S.scene.timeline;
    return !!t && (t.timeline_id === "demo" || (t.department === "trading" && t.kind === "simulation"));
  }
  function currentPosition() {
    var frames = S.scene ? S.scene.frames.length : 0;
    return C.resultsPosition(S.replay ? S.replay.index : -1, frames, S.source === "now" && !!S.scene.current);
  }
  function openResults() {
    S.focusRoom = null;
    setView("results");
  }
  function resetResults() {
    R.generation += 1;
    R.timelineId = null; R.index = null; R.at = null; R.summary = null; R.error = null; R.wanted = null; R.shown = null;
    R.inflightAt = false; R.inflightSummary = false; R.renderedKey = null;
  }
  /* Called whenever the view, timeline or replay position changes. */
  function syncResults() {
    if (S.view !== "results" || !S.scene) { return; }
    if (!resultsAllowed()) { renderResults(); return; }
    if (R.timelineId !== S.timelineId) {
      resetResults();
      R.timelineId = S.timelineId;
      var generation = R.generation;
      getJSON("/api/results?timeline=" + encodeURIComponent(S.timelineId)).then(function (doc) {
        if (generation !== R.generation) { return; }
        if (!C.validResults(doc, "index")) { throw new Error("invalid_results"); }
        R.index = doc;
        syncResults();
      }).catch(function (error) {
        if (generation !== R.generation) { return; }
        R.error = C.plain(error.message);
        renderResults();
      });
      renderResults();
      return;
    }
    if (!R.index) { renderResults(); return; }
    if (R.tab === "summary") { loadSummary(); } else { requestAt(currentPosition()); }
    renderResults();
  }
  function requestAt(position) {
    R.wanted = position;
    if (R.inflightAt || (R.at && R.at.position === position && R.shown === position)) { return; }
    R.inflightAt = true;
    var generation = R.generation;
    getJSON("/api/results/at?timeline=" + encodeURIComponent(R.timelineId) + "&position=" + position).then(function (doc) {
      if (generation !== R.generation) { return; }
      R.inflightAt = false;
      if (!C.validResults(doc, "replay_position") || doc.timeline_id !== R.index.timeline.timeline_id) {
        throw new Error("invalid_results");
      }
      R.error = null;
      if (doc.position === R.wanted) { R.at = doc; R.shown = doc.position; renderResults(); }
      else { requestAt(R.wanted); }                       // a newer position was asked for meanwhile
    }).catch(function (error) {
      if (generation !== R.generation) { return; }
      R.inflightAt = false;
      R.error = C.plain(error.message);
      renderResults();
    });
  }
  function loadSummary() {
    if (R.summary || R.inflightSummary) { return; }
    R.inflightSummary = true;
    var generation = R.generation;
    getJSON("/api/results/summary?timeline=" + encodeURIComponent(R.timelineId)).then(function (doc) {
      if (generation !== R.generation) { return; }
      R.inflightSummary = false;
      if (!C.validResults(doc, "completed_run_summary")) { throw new Error("invalid_results"); }
      R.summary = doc;
      renderResults();
    }).catch(function (error) {
      if (generation !== R.generation) { return; }
      R.inflightSummary = false;
      R.error = C.plain(error.message);
      renderResults();
    });
  }
  function setResultsTab(tab) {
    R.tab = tab;
    $("res-tab-position").setAttribute("aria-pressed", tab === "position" ? "true" : "false");
    $("res-tab-summary").setAttribute("aria-pressed", tab === "summary" ? "true" : "false");
    syncResults();
  }

  /* ---- small render helpers (text only) */
  function tile(parent, label, value, note, tone) {
    var box = h("div", { class: "tile" + (tone ? " " + tone : "") }, parent);
    h("span", { class: "tile-label" }, box, label);
    h("span", { class: "tile-value" + (String(value).length > 12 ? " long" : "") }, box, value);
    if (note) { h("span", { class: "tile-note" }, box, note); }
    return box;
  }
  function signedTone(text) {
    var value = String(text || "");
    return /^-/.test(value) && /[1-9]/.test(value) ? "neg" : /[1-9]/.test(value) ? "pos" : "";
  }
  function metricTile(parent, label, metric, signed) {
    if (!metric || metric.status !== "available") {
      return tile(parent, label, "Unavailable", C.reasonText(metric && metric.reason ? metric.reason : "unknown"), "na");
    }
    return tile(parent, label, C.money(metric.value, signed), null, signed ? signedTone(metric.value) : "");
  }
  function codeList(codes) {
    var span = h("span", { class: "codes" });
    (codes && codes.length ? codes : ["none"]).forEach(function (code) { h("code", {}, span, code); });
    return span;
  }
  function table(parent, caption, headers, rows, note) {
    var wrap = h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": C.plain(caption) }, parent);
    var t = h("table", { class: "table results-table" }, wrap);
    h("caption", {}, t, caption);
    var head = h("tr", {}, h("thead", {}, t));
    headers.forEach(function (label) { h("th", { scope: "col" }, head, label); });
    var body = h("tbody", {}, t);
    var shown = rows.length > ROW_LIMIT ? rows.slice(rows.length - ROW_LIMIT) : rows;
    shown.forEach(function (cells) {
      var tr = h("tr", {}, body);
      cells.forEach(function (cell, n) {
        var td = h(n === 0 ? "th" : "td", n === 0 ? { scope: "row" } : {}, tr);
        if (cell instanceof Node) { td.appendChild(cell); } else { td.textContent = C.plain(cell); }
      });
    });
    if (!rows.length) {
      var empty = h("tr", {}, body);
      h("td", { colspan: headers.length, class: "muted" }, empty, note || "None.");
    } else if (rows.length > ROW_LIMIT) {
      h("p", { class: "muted small" }, wrap, "Showing the last " + ROW_LIMIT + " of " + rows.length + " rows.");
    }
    return t;
  }
  function para(tag, attrs, parent, text) {             // long server explanations (bounded by C.prose)
    var node = h(tag, attrs, parent);
    node.textContent = C.prose(text);
    return node;
  }
  function idCode(value) { return value ? h("code", {}, null, value) : h("span", { class: "muted" }, null, "none"); }
  function sim(stamp) { return stamp ? C.formatTime(stamp) : "-"; }

  /* ---- charts: one series each, one y-axis each, recessive grid, crosshair tooltip, data table */
  function chart(parent, spec) {
    var points = spec.points, wrap = h("figure", { class: "chart" }, parent);
    h("figcaption", { class: "chart-title" }, wrap, spec.title);
    if (!points.length) {
      h("p", { class: "muted small" }, wrap, spec.empty);
      return wrap;
    }
    var width = spec.width, height = 190, pad = { l: 64, r: 12, t: 10, b: 26 };
    var g = C.scale(points, spec.key, width, height, pad, spec.xDomain);
    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, class: "chart-svg", role: "img", focusable: "false" }, wrap);
    el("title", {}, svg).textContent = spec.label;
    [g.y0 + (g.y1 - g.y0) * 0.1, (g.y0 + g.y1) / 2, g.y1 - (g.y1 - g.y0) * 0.1].forEach(function (v) {
      el("line", { x1: pad.l, x2: width - pad.r, y1: g.py(v), y2: g.py(v), class: "grid" }, svg);
      svgText(svg, pad.l - 6, g.py(v) + 4, spec.tick(v), { class: "tick", "text-anchor": "end" });
    });
    if (spec.reference !== undefined) {
      var ry = g.py(spec.reference);
      el("line", { x1: pad.l, x2: width - pad.r, y1: ry, y2: ry, class: "reference" }, svg);
      svgText(svg, width - pad.r, ry - 4, spec.referenceLabel, { class: "tick", "text-anchor": "end" });
    }
    var line = g.points.map(function (p) { return g.px(p.x).toFixed(1) + "," + g.py(p.y).toFixed(1); });
    if (spec.area) {
      el("polygon", { points: g.px(g.points[0].x).toFixed(1) + "," + g.py(0).toFixed(1) + " " + line.join(" ") + " " +
        g.px(g.points[g.points.length - 1].x).toFixed(1) + "," + g.py(0).toFixed(1), class: "area " + spec.tone }, svg);
    }
    el("polyline", { points: line.join(" "), class: "series " + spec.tone }, svg);
    var last = g.points[g.points.length - 1];
    el("circle", { cx: g.px(last.x), cy: g.py(last.y), r: 4, class: "marker " + spec.tone }, svg);
    var first = C.formatTime(new Date(g.x0).toISOString().slice(0, 19) + "Z");
    var end = C.formatTime(new Date(g.x1).toISOString().slice(0, 19) + "Z");
    svgText(svg, pad.l, height - 6, first.slice(0, 16), { class: "tick" });
    svgText(svg, width - pad.r, height - 6, end.slice(0, 10) === first.slice(0, 10) ? end.slice(11) : end,
      { class: "tick", "text-anchor": "end" });
    var cross = el("line", { y1: pad.t, y2: height - pad.b, class: "crosshair", visibility: "hidden" }, svg);
    var tip = h("div", { class: "chart-tip", role: "presentation", hidden: "hidden" }, wrap);
    var hit = el("rect", { x: pad.l, y: pad.t, width: width - pad.l - pad.r, height: height - pad.t - pad.b, class: "hit" }, svg);
    var xs = g.points.map(function (p) { return g.px(p.x); });
    function show(event) {
      var box = svg.getBoundingClientRect();
      if (!box.width) { return; }
      var x = (event.clientX - box.left) * width / box.width;
      var n = C.nearestIndex(xs, x), p = g.points[n];
      cross.setAttribute("x1", xs[n]); cross.setAttribute("x2", xs[n]); cross.setAttribute("visibility", "visible");
      tip.textContent = C.formatTime(p.point.at_utc) + " · " + spec.tipText(p.point);
      tip.hidden = false;
      tip.style.left = Math.min(Math.max(xs[n] / width * 100, 8), 72) + "%";
    }
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerdown", show);
    hit.addEventListener("pointerleave", function () { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
    var details = h("details", { class: "chart-data" }, wrap);
    h("summary", {}, details, "Data table (" + points.length + " points)");
    table(details, spec.title + " data", ["Bar close (simulated time)", spec.column],
      points.map(function (p) { return [C.formatTime(p.at_utc), spec.cell(p)]; }));
    return wrap;
  }
  /* Draw at the column's real pixel width so chart text stays at its CSS size (one column under 680px). */
  function chartWidth(container) {
    var total = container.clientWidth || 640;
    return Math.round(Math.max(280, Math.min(900, total >= 680 ? (total - 10) / 2 - 20 : total - 20)));
  }
  function equityChart(parent, points, initial, xDomain, title) {
    return chart(parent, { title: title, width: chartWidth(parent), points: points, key: "equity", tone: "equity", xDomain: xDomain,
      label: title + ": " + points.length + " bar-close points, simulated", empty: "No bar had closed yet at this simulated time.",
      reference: Number(initial), referenceLabel: "initial cash " + C.money(initial),
      tick: function (v) { return "$" + v.toFixed(v >= 1000 ? 0 : 2).replace(/\B(?=(\d{3})+(?!\d))/g, ","); },
      tipText: function (p) { return "equity " + C.money(p.equity); }, column: "Equity", cell: function (p) { return C.money(p.equity); } });
  }
  function drawdownChart(parent, points, xDomain, title) {
    var rows = points.map(function (p) { return Object.assign({}, p, { negative: "-" + p.drawdown_percent }); });
    return chart(parent, { title: title, width: chartWidth(parent), points: rows, key: "negative", tone: "drawdown", xDomain: xDomain, area: true,
      label: title + ": drawdown below the running peak, percent", empty: "No bar had closed yet at this simulated time.",
      tick: function (v) { var t = v.toFixed(2); return (t === "-0.00" ? "0.00" : t) + "%"; },
      tipText: function (p) { return "drawdown " + C.percent(p.drawdown_percent) + " (" + C.money(p.drawdown) + ")"; },
      column: "Drawdown", cell: function (p) { return C.percent(p.drawdown_percent) + " · " + C.money(p.drawdown); } });
  }
  function windowDomain() {
    var w = R.index && R.index.replay_window;
    return w ? [Date.parse(w.start_utc), Date.parse(w.end_utc)] : null;
  }

  /* ---- order, fill and trade tables (shared by both views) */
  function orderRows(orders) {
    return orders.map(function (o) {
      var last = o.history[o.history.length - 1];
      var why = h("span", { class: "codes" });
      o.history.forEach(function (entry) {
        h("span", { class: "hist" }, why, C.words(entry.status) + " " + sim(entry.at_utc) + ": ");
        why.appendChild(codeList(entry.reason_codes));
      });
      var source = o.source.kind === "research_signal" ?
        h("span", {}, null, "signal " + (o.source.strategy || "?") + " · ") : h("span", {}, null, "exit rule " + C.words(o.source.rule));
      if (o.source.signal_id) { source.appendChild(idCode(o.source.signal_id)); }
      return [idCode(o.order_id), C.words(o.purpose) + " · " + o.side, o.quantity === null ? "-" : String(o.quantity),
        C.words(o.status), "bar " + o.source.decision_bar_sequence + " · " + sim(o.source.decision_at_utc), source,
        o.estimate.notional ? C.money(o.estimate.notional) : "-", why];
    });
  }
  function fillRows(fills) {
    return fills.map(function (f) {
      return [idCode(f.fill_id), idCode(f.order_id), f.side, String(f.quantity), "bar " + f.bar_sequence + " · " + sim(f.bar_open_utc),
        C.money(f.open_price), C.money(f.fill_price), C.money(f.notional), C.money(f.fee), C.money(f.cash_change, true),
        f.gap_before_fill ? f.gap_before_fill + " missing" : "none"];
    });
  }
  var ORDER_HEAD = ["Order", "Purpose · side", "Qty", "Status", "Decided (simulated)", "Supporting reference", "Estimated notional",
    "History: simulator checks and reasons"];
  var FILL_HEAD = ["Fill", "Order", "Side", "Qty", "Filled at bar open (simulated)", "Open", "Fill price", "Notional", "Fee",
    "Cash change", "Gap before fill"];

  function renderResultsHeader(body) {
    var idx = R.index;
    var demo = idx.demo;
    var badge = $("results-badge");
    badge.textContent = demo ? "DEMO DATA · SYNTHETIC · SIMULATED" : "SAVED SIMULATION · SIMULATED";
    badge.className = "sim-badge " + (demo ? "demo" : "saved");
    var facts = h("div", { class: "facts" }, body);
    row(facts, "Run", idCode(idx.run.run_id));
    row(facts, "Timeline", C.words(idx.timeline.origin) + " · " + idx.timeline.timeline_id + " · " + C.words(idx.timeline.completeness));
    row(facts, "Dataset", idx.dataset.symbol + " · " + idx.dataset.interval + " · " + idx.dataset.data_label + " · " + idx.dataset.dataset_id);
    row(facts, "Strategies", idx.strategies.map(function (s) { return s.name; }).join(", "));
    row(facts, "Costs", idx.policy.costs.slippage_bps + " bps slippage · fee " + C.money(idx.policy.costs.fee_per_order) +
      " + " + idx.policy.costs.fee_bps + " bps per order");
    row(facts, "Kill switch", idx.kill_switch.engaged ? "engaged (" + C.words(idx.kill_switch.source) + ")" : "not engaged");
    var analytics = idx.correlation.analytics;
    row(facts, "Analytics", analytics.status === "available" ? "report " + analytics.report_id + " (matched by hashes)" :
      "unavailable: " + C.reasonText(analytics.reason));
    if (analytics.rejected_reports.length) {
      row(facts, "Rejected reports", analytics.rejected_reports.map(function (r) {
        return r.report_id + " (" + C.reasonText(r.code) + ")";
      }).join("; "));
    }
    row(facts, "Correlation", para("span", {}, null, idx.correlation.method));
    para("p", { class: "separation" }, body, idx.separation);
  }

  function renderPosition(body) {
    var doc = R.at, idx = R.index;
    if (idx.replay_state.status !== "available") {
      var box = h("div", { class: "notice warn" }, body);
      h("strong", {}, box, "Portfolio at a replay position is unavailable for this timeline.");
      h("p", {}, box, "Reasons: " + idx.replay_state.reasons.map(C.reasonText).join("; ") +
        ". No intermediate state is invented. The completed run summary is still available.");
      return;
    }
    if (!doc) { h("p", { class: "muted" }, body, "Loading the portfolio at this position..."); return; }
    if (doc.status === "not_started") {
      h("p", { class: "notice" }, body, "Before the simulator's first event: no simulated orders, fills or cash changes yet. " +
        "Step forward to the simulator's events.");
      return;
    }
    if (doc.status !== "available") {
      h("p", { class: "notice warn" }, body, "Unavailable: " + doc.reasons.map(C.reasonText).join("; "));
      return;
    }
    var p = doc.portfolio;
    var tiles = h("div", { class: "tiles" }, body);
    tile(tiles, "Initial cash", C.money(p.initial_cash));
    tile(tiles, "Cash", C.money(p.cash));
    metricTile(tiles, "Equity", p.equity);
    tile(tiles, "Realized P&L", C.money(p.realized_pnl, true), null, signedTone(p.realized_pnl));
    metricTile(tiles, "Unrealized P&L", p.unrealized_pnl, true);
    tile(tiles, "Fees so far", C.money(p.fees));
    tile(tiles, "Open position", p.open_quantity ? p.open_quantity + " shares" : "none",
      p.open_quantity && p.mark ? "marked at " + C.money(p.mark.price) + " (bar " + p.mark.bar_sequence + " close)" : null);
    var dd = p.drawdown_so_far;
    tile(tiles, "Max drawdown so far", dd.max_percent.status === "available" ? C.percent(dd.max_percent.value) : "Unavailable",
      dd.max_dollars.status === "available" ? C.money(dd.max_dollars.value) : C.reasonText(dd.max_dollars.reason),
      dd.max_percent.status === "available" ? "" : "na");
    h("p", { class: "muted small" }, body, p.mark_basis + " Equity and drawdown points appear only once their bar has closed.");
    var charts = h("div", { class: "charts" }, body);
    if (p.equity_curve_status.status !== "available") {
      h("p", { class: "notice" }, charts, "Equity and drawdown charts are unavailable: " + C.reasonText(p.equity_curve_status.reason) + ".");
    } else {
      equityChart(charts, p.equity_curve, p.initial_cash, windowDomain(), "Equity so far (simulated)");
      drawdownChart(charts, p.equity_curve, windowDomain(), "Drawdown so far (simulated)");
    }
    table(body, "Open position at this position", ["Entry fill", "Strategy · signal", "Qty", "Opened (simulated)", "Entry price",
      "Cost basis", "Mark", "Market value", "Unrealized P&L"], p.open_positions.map(function (o) {
      var src = h("span", {}, null, (o.strategy || "-") + " · "); src.appendChild(idCode(o.signal_id));
      return [idCode(o.entry_fill_id), src, String(o.quantity), sim(o.opened_at_utc), C.money(o.entry_price), C.money(o.cost_basis),
        o.mark_price ? C.money(o.mark_price) + " @ " + sim(o.marked_at_utc) : "Unavailable",
        C.metricText(o.market_value, C.money), C.metricText(o.unrealized_pnl, function (v) { return C.money(v, true); })];
    }), "No open position at this replay position.");
    table(body, "Closed trades so far", ["Entry → exit fill", "Strategy", "Qty", "Opened", "Closed", "Entry", "Exit", "Realized P&L",
      "Outcome"], p.closed_trades.map(function (t) {
      var ids = h("span", { class: "codes" }); ids.appendChild(idCode(t.entry_fill_id)); ids.appendChild(idCode(t.exit_fill_id));
      return [ids, t.strategy || "-", String(t.quantity), sim(t.opened_at_utc), sim(t.closed_at_utc), C.money(t.entry_price),
        C.money(t.exit_price), C.money(t.realized_pnl, true), t.outcome];
    }), "No closed trades yet at this replay position.");
    table(body, "Simulated orders so far (simulator policy decisions)", ORDER_HEAD, orderRows(p.orders), "No orders decided yet.");
    table(body, "Simulated fills so far", FILL_HEAD, fillRows(p.fills), "No fills yet.");
  }

  function statRow(label, metric, format) { return [label, C.metricText(metric, format)]; }
  function renderSummary(body) {
    var doc = R.summary;
    if (!doc) { h("p", { class: "muted" }, body, "Loading the completed run summary..."); return; }
    h("p", { class: "notice summary-label" }, body, doc.label);
    var a = doc.account, tiles = h("div", { class: "tiles" }, body);
    tile(tiles, "Initial cash", C.money(a.initial_cash));
    tile(tiles, "Ending cash", C.money(a.ending_cash));
    tile(tiles, "Ending equity", C.money(a.ending_equity));
    tile(tiles, "Net return", C.money(a.net_return, true), C.metricText(a.net_return_percent, function (v) { return C.percent(v, true); }) +
      " · not annualized", signedTone(a.net_return));
    tile(tiles, "Realized P&L", C.money(a.realized_pnl, true), null, signedTone(a.realized_pnl));
    tile(tiles, "Unrealized P&L", C.money(a.unrealized_pnl, true), "no exit costs assumed", signedTone(a.unrealized_pnl));
    tile(tiles, "Fees", C.money(a.fees_total));
    tile(tiles, "Open at end", a.open_position_quantity ? a.open_position_quantity + " shares" : "none",
      a.last_close ? "marked at last close " + C.money(a.last_close) : null);
    var report = doc.analytics.report;
    var charts = h("div", { class: "charts" }, body);
    if (!report) {
      h("p", { class: "notice warn" }, charts, "Analytics unavailable: " + C.reasonText(doc.analytics.reason) +
        ". Closed-trade statistics, the equity curve and drawdown need a matching Step 30 report " +
        "(analytics-generate RUN_ID --save). Run figures above come from the validated run itself.");
    } else {
      equityChart(charts, report.equity_curve, a.initial_cash, null, "Equity, whole run (simulated)");
      drawdownChart(charts, report.equity_curve, null, "Drawdown, whole run (simulated)");
      var ct = report.closed_trades;
      table(body, "Closed-trade statistics (report " + report.report_id + ")", ["Statistic", "Value"], [
        ["Closed trades", ct.count + " (" + ct.wins + " wins · " + ct.losses + " losses · " + ct.breakeven + " breakeven)"],
        ["Net P&L of closed trades", C.money(ct.net_pnl, true)], ["Fees in closed trades", C.money(ct.fees)],
        statRow("Win rate", ct.win_rate_percent, C.percent), statRow("Average net per trade (expectancy)", ct.expectancy, C.money),
        statRow("Average win", ct.average_win, C.money), statRow("Average loss", ct.average_loss, C.money),
        statRow("Profit factor", ct.profit_factor, function (v) { return C.roundText(v, 2); }),
        ["Max drawdown", C.money(report.drawdown.max_dollars) + " at " + sim(report.drawdown.max_dollars_at_utc) + " · " +
          C.percent(report.drawdown.max_percent) + " at " + sim(report.drawdown.max_percent_at_utc)],
        statRow("Average bars held (closed)", report.holding.closed_average_bars, function (v) { return C.roundText(v, 2); }),
        statRow("Exposure (bars with a position)", report.exposure.exposure_percent, C.percent)]);
      table(body, "Closed trades", ["Position", "Strategy", "Qty", "Opened", "Closed", "Entry", "Exit", "Gross", "Fees", "Net P&L", "Outcome"],
        ct.trades.map(function (t) {
          return [String(t.position), t.strategy, String(t.quantity), sim(t.opened_at_utc), sim(t.closed_at_utc), C.money(t.entry_price),
            C.money(t.exit_price), C.money(t.gross_pnl, true), C.money(t.fees), C.money(t.net_pnl, true), t.outcome];
        }), "No closed trades in this run: statistics that need them are shown as unavailable, never as zero.");
      table(body, "Rejected orders by reason", ["Reason code", "Orders"], Object.keys(report.orders.rejected_by_reason).map(function (k) {
        return [h("code", {}, null, k), String(report.orders.rejected_by_reason[k])];
      }), "No rejected orders.");
      var attribution = h("section", { class: "attribution" }, body);
      h("h3", {}, attribution, "Strategy attribution" + (report.attribution.shared_account ? " (shared account)" : ""));
      para("p", { class: "muted small" }, attribution, report.attribution.explanation);
      table(attribution, "Per-strategy share of this run", ["Strategy", "Closed", "Net P&L", "Win rate", "Open", "Unrealized",
        "Signals accepted", "Rejected", "Blocked by shared account"], report.attribution.strategies.map(function (s) {
        return [s.strategy, String(s.closed_trades), C.money(s.net_pnl, true), C.metricText(s.win_rate_percent, C.percent),
          String(s.open_positions), C.money(s.unrealized_pnl, true), String(s.signals_accepted), String(s.signals_rejected),
          String(s.blocked_by_shared_account)];
      }));
    }
    table(body, "All simulated orders (simulator policy decisions)", ORDER_HEAD, orderRows(doc.orders), "No orders.");
    table(body, "All simulated fills", FILL_HEAD, fillRows(doc.fills), "No fills.");
  }

  function renderLimitations(body, rows) {
    var box = h("section", { class: "limitations" }, body);
    h("h3", {}, box, "Limitations (dataset, strategy, cost model, simulation)");
    var list = h("ul", {}, box);
    rows.forEach(function (item) {
      var li = h("li", {}, list);
      h("strong", {}, li, C.words(item.area) + ": ");
      para("span", {}, li, item.text);
    });
  }

  var RESULT_ERRORS = {
    sim_run_not_found: "The simulation run behind this timeline was not saved (record it with sim-run DATASET_ID --save --record-events).",
    sim_run_corrupt: "The saved simulation run failed validation, so no results are shown.",
    timeline_run_mismatch: "This timeline's events do not match the saved run, so they are not combined.",
    results_run_missing: "This timeline does not name a saved simulation run.",
    results_not_simulation: "This timeline is not a simulation."
  };
  function renderResults() {
    if (S.view !== "results") { return; }
    var key = [S.timelineId, R.tab, currentPosition(), R.at ? R.at.position : "-", R.summary ? "s" : "-", R.index ? "i" : "-",
      R.error, resultsAllowed(), S.timelines.length].join("|");
    if (key === R.renderedKey) { return; }
    R.renderedKey = key;
    var body = $("results-body");
    clear(body);
    var where = $("results-where");
    if (!resultsAllowed()) {
      $("results-badge").textContent = "SIMULATED";
      where.textContent = "";
      h("p", { class: "notice" }, body, "The selected timeline (" + (S.scene ? C.words(S.scene.timeline.kind) : "none") +
        ") is not a trading simulation. Results exist for simulation runs only. Research-agent and content timelines stay in the house.");
      var picks = S.timelines.filter(function (t) { return t.kind === "simulation" || t.id === "demo"; });
      var list = h("ul", { class: "mini-list" }, body);
      picks.forEach(function (t) {
        var li = h("li", {}, list);
        var b = h("button", { type: "button", class: "link" }, li, timelineName(t));
        b.disabled = t.readable === false;
        b.addEventListener("click", function () { loadScene(t.id).then(function () { setView("results"); }); });
        h("span", { class: "muted small" }, li, t.origin);
      });
      if (picks.length <= 1) { h("li", { class: "muted" }, list, "No saved simulations yet: sim-run DATASET_ID --save --record-events."); }
      return;
    }
    if (R.error) {
      h("p", { class: "notice warn" }, body, (RESULT_ERRORS[R.error] || "Could not load results (" + R.error + ").") +
        " Nothing was changed.");
      if (!R.index) { return; }
    }
    if (!R.index) { h("p", { class: "muted" }, body, "Loading results..."); return; }
    var frames = S.scene.frames.length, position = currentPosition();
    var event = R.at && R.at.event;
    where.textContent = R.tab === "summary" ? "Completed run summary: end-of-run figures, independent of the replay position." :
      "At replay position " + position + " of " + frames + (R.at && R.at.position === position && event ?
        " · " + C.words(event.event_type) + " · simulated market time " + sim(event.sim_time_utc) +
        " · recorded " + (event.recorded_at ? C.formatTime(event.recorded_at) : "not recorded (reconstructed)") : "");
    renderResultsHeader(body);
    if (R.tab === "summary") { renderSummary(body); } else { renderPosition(body); }
    renderLimitations(body, R.tab === "summary" && R.summary ? R.summary.limitations : R.index.limitations);
  }

  /* ------------------------------------------------------------------ data (GET only) */
  function getJSON(path) {
    return window.fetch(path, { method: "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) { throw new Error(body && body.error ? C.plain(body.error.code) : "request_failed"); }
          return body;
        });
      });
  }

  function loadScene(id, keepPosition) {
    if (!C.ID_PATTERN.test(id)) { return Promise.resolve(); }
    return getJSON("/api/scene?timeline=" + encodeURIComponent(id)).then(function (scene) {
      if (!C.validScene(scene)) { throw new Error("invalid_scene"); }
      var sameTimeline = S.scene && S.timelineId === id;
      var previous = S.replay ? S.replay.index : -1;
      S.scene = scene;
      S.timelineId = id;
      S.error = null;
      S.replay = S.replay && sameTimeline ? S.replay : new C.Replay(scene.frames.length, { loop: scene.mode === "demo" });
      S.replay.setCount(scene.frames.length);
      S.replay.loop = scene.mode === "demo";
      if (!sameTimeline) {
        buildRooms();
        S.source = "replay";
        S.completedAt = {};
        if (scene.mode === "demo") { S.replay.play(); }
        else if (scene.mode === "observed") { S.following = true; S.replay.toEnd(); }
        else { S.replay.restart(); }
      } else if (keepPosition && S.following) {
        S.replay.toEnd();
      } else {
        S.replay.seek(previous);
      }
      renderMode();
      if (S.selected && !roomMeta(S.selected)) { S.selected = null; }
      renderStatuses(false);
      renderTimelines();
      schedulePoll();
    }).catch(function (error) {
      S.error = error.message;
      $("caption").textContent = "Could not load that timeline (" + C.plain(error.message) + "). Nothing was changed.";
    });
  }

  function loadTimelines() {
    return getJSON("/api/timelines").then(function (data) {
      S.timelines = Array.isArray(data.items) ? data.items : [];
      renderTimelines();
    }).catch(function () { S.timelines = [{ id: "demo", label: "Demo HQ (synthetic)", origin: "demo", department: "both" }]; renderTimelines(); });
  }

  /* Bounded refresh: only while an observed (live) timeline is open and the page is visible. */
  function schedulePoll() {
    window.clearTimeout(S.pollTimer);
    var live = $("live-indicator");
    if (!S.scene || S.scene.mode !== "observed") { S.polls = 0; return; }
    if (S.polls >= MAX_POLLS) { live.textContent = "Live refresh stopped (limit reached). Reload to continue."; return; }
    live.textContent = "● Live · refreshed " + new Date().toLocaleTimeString() + (S.following ? " · following" : "");
    S.pollTimer = window.setTimeout(function () {
      if (document.hidden) { schedulePoll(); return; }
      S.polls += 1;
      loadScene(S.timelineId, true);
    }, POLL_MS);
  }

  /* ------------------------------------------------------------------ controls */
  function toggle(id, key) {
    S[key] = !S[key];
    $(id).setAttribute("aria-pressed", S[key] ? "true" : "false");
  }
  function setPresenting(on) {
    S.presenting = on;
    document.body.classList.toggle("presenting", on);
    $("btn-present").setAttribute("aria-pressed", on ? "true" : "false");
    if (!on) { focusRoom(null); }
    R.renderedKey = null;                                    // redraw results charts at the new layout width
    renderResults();
  }
  function wire() {
    Array.prototype.forEach.call(document.querySelectorAll(".view-btn"), function (b) {
      b.addEventListener("click", function () { S.focusRoom = null; setView(b.getAttribute("data-view")); });
    });
    $("btn-play").addEventListener("click", togglePlay);
    $("btn-restart").addEventListener("click", function () { seek(-1); });
    $("btn-back").addEventListener("click", function () { seek(S.replay.index - 1); });
    $("btn-forward").addEventListener("click", function () { seek(S.replay.index + 1); });
    $("scrubber").addEventListener("input", function (e) { S.replay.pause(); seek(Number(e.target.value) - 1); });
    $("speed").addEventListener("change", function (e) { S.replay.setSpeed(e.target.value); });
    $("src-replay").addEventListener("click", function () { S.source = "replay"; renderStatuses(false); });
    $("src-now").addEventListener("click", function () { if (S.scene.current) { S.source = "now"; S.replay.pause(); renderStatuses(false); } });
    $("btn-labels").addEventListener("click", function () { toggle("btn-labels", "labels"); document.body.classList.toggle("no-labels", !S.labels); });
    $("btn-motion").addEventListener("click", function () { toggle("btn-motion", "reduced"); document.body.classList.toggle("reduced", S.reduced); });
    $("btn-list").addEventListener("click", function () { toggle("btn-list", "list"); setView(S.view === "timeline" ? "house" : S.view); });
    $("btn-present").addEventListener("click", function () { setPresenting(!S.presenting); });
    $("present-exit").addEventListener("click", function () { setPresenting(false); });
    $("btn-refresh").addEventListener("click", loadTimelines);
    $("res-tab-position").addEventListener("click", function () { setResultsTab("position"); });
    $("res-tab-summary").addEventListener("click", function () { setResultsTab("summary"); });
    var resizeTimer = null;
    window.addEventListener("resize", function () {           // redraw charts at the new width (text stays 1:1)
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(function () { R.renderedKey = null; renderResults(); }, 200);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".filter-btn"), function (b) {
      b.addEventListener("click", function () { S.filter = b.getAttribute("data-filter"); renderTimelines(); });
    });
    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      if (/INPUT|SELECT|TEXTAREA/.test(tag) || e.ctrlKey || e.metaKey || e.altKey) { return; }
      if (e.key === " " && tag !== "BUTTON") { e.preventDefault(); togglePlay(); }
      else if (e.key === "ArrowLeft") { seek(S.replay.index - 1); }
      else if (e.key === "ArrowRight") { seek(S.replay.index + 1); }
      else if (e.key === "Home") { seek(-1); }
      else if (e.key === "End") { seek(S.scene.frames.length - 1); }
      else if (e.key === "p" || e.key === "P") { setPresenting(!S.presenting); }
      else if (e.key === "l" || e.key === "L") { $("btn-labels").click(); }
      else if (e.key === "m" || e.key === "M") { $("btn-motion").click(); }
      else if (e.key === "v" || e.key === "V") { $("btn-list").click(); }
      else if (e.key === "r" || e.key === "R") { if (S.view === "results") { setView("house"); } else { openResults(); } }
      else if (e.key === "Escape") { if (S.presenting) { setPresenting(false); } else { select(null); } }
      else if (S.presenting && /^[0-8]$/.test(e.key)) { focusRoom(e.key === "0" ? null : BOT_ROOMS[Number(e.key) - 1]); }
    });
    var legend = $("legend");
    C.STATE_ORDER.forEach(function (state) {
      var style = C.stateStyle(state), item = h("span", { class: "legend-item" }, legend);
      var dot = h("span", { class: "dot", "aria-hidden": "true" }, item);
      dot.style.background = style.color;
      h("span", {}, item, style.label);
    });
    document.body.classList.toggle("reduced", S.reduced);
    $("btn-motion").setAttribute("aria-pressed", S.reduced ? "true" : "false");
  }

  build();
  wire();
  loadTimelines();
  loadScene("demo").then(function () { setView("house"); });
  window.requestAnimationFrame(loop);
}());
