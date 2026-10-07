/* Step 32: Living HQ client logic (run by tests/test_hq.py through Node when Node is available). */
"use strict";
const assert = require("assert");
const path = require("path");
const C = require(path.join(__dirname, "..", "vicekrack", "hq", "static", "hq-core.js"));

let passed = 0;
function test(name, fn) { fn(); passed += 1; }

test("every display state has a distinct colour, icon and text label", () => {
  const states = ["idle", "working", "waiting", "blocked", "completed", "failed", "unknown"];
  assert.deepStrictEqual(C.STATE_ORDER, states);
  const colors = new Set(states.map((s) => C.stateStyle(s).color));
  const icons = new Set(states.map((s) => C.stateStyle(s).icon));
  const labels = new Set(states.map((s) => C.stateStyle(s).label));
  assert.strictEqual(colors.size, 7); assert.strictEqual(icons.size, 7); assert.strictEqual(labels.size, 7);
  assert.strictEqual(C.stateStyle("anything-else").label, "Unknown");
});

test("intent rules: recorded work goes to the station; decoration only when free", () => {
  assert.strictEqual(C.intentFor("working"), "station");
  assert.strictEqual(C.intentFor("failed"), "station");
  assert.strictEqual(C.intentFor("completed", false), "station");
  assert.strictEqual(C.intentFor("completed", true), "free");
  assert.strictEqual(C.intentFor("blocked"), "door");
  assert.strictEqual(C.intentFor("waiting"), "seat");
  assert.strictEqual(C.intentFor("unknown"), "hold");
  assert.strictEqual(C.intentFor("idle"), "free");
});

test("replay seeking clamps and starts before the first event", () => {
  const r = new C.Replay(5);
  assert.strictEqual(r.index, -1);
  assert.strictEqual(r.seek(3), 3);
  assert.strictEqual(r.seek(99), 4);
  assert.strictEqual(r.seek(-7), -1);
  assert.strictEqual(r.step(2), 1);
  assert.strictEqual(r.restart(), -1);
  assert.strictEqual(r.toEnd(), 4);
  assert.strictEqual(new C.Replay(0).seek(3), -1);
});

test("replay ticks advance in order, honour speed, and stop at the end", () => {
  const r = new C.Replay(3, { baseMs: 1000 });
  assert.strictEqual(r.tick(5000), 0);           // paused: nothing moves
  r.play();
  assert.strictEqual(r.tick(999), 0);
  assert.strictEqual(r.tick(1), 1);
  assert.strictEqual(r.index, 0);
  r.setSpeed(4);
  assert.strictEqual(r.tick(500), 2);            // 2000 ms of replay time: two events
  assert.strictEqual(r.index, 2);
  r.tick(10000);
  assert.strictEqual(r.playing, false);
  assert.strictEqual(r.index, 2);
  assert.strictEqual(r.setSpeed(3), 4);          // unsupported speeds are ignored
  r.play();                                       // play at the end restarts from the beginning
  assert.strictEqual(r.index, -1);
});

test("demo replay loops after a pause; recorded replays do not", () => {
  const r = new C.Replay(2, { baseMs: 100, loop: true, loopPauseMs: 1000 });
  r.play();
  r.tick(200);
  assert.strictEqual(r.index, 1);
  r.tick(999);
  assert.strictEqual(r.index, 1);
  r.tick(2);
  assert.strictEqual(r.index, -1);
  assert.strictEqual(r.playing, true);
});

test("paths follow only declared edges", () => {
  const nodes = { a: { x: 0, y: 0, z: 0 }, b: { x: 1, y: 0, z: 0 }, c: { x: 2, y: 0, z: 0 }, d: { x: 9, y: 9, z: 0 } };
  const g = new C.Graph(nodes, [["a", "b"], ["b", "c"]]);
  assert.deepStrictEqual(g.path("a", "c"), ["a", "b", "c"]);
  assert.deepStrictEqual(g.path("c", "c"), ["c"]);
  assert.strictEqual(g.path("a", "d"), null);
  assert.strictEqual(g.path("a", "nowhere"), null);
  assert.strictEqual(g.nearest({ x: 1.9, y: 0.1, z: 0 }), "c");
  assert.throws(() => new C.Graph(nodes, [["a", "zz"]]));
});

test("decorative wandering is deterministic and only returns a place to stand", () => {
  const spots = [{ node: "seat", weight: 3, activity: "sit" }, { node: "kitchen", weight: 1, activity: "coffee" }];
  const first = C.wanderChoice("creator", 4, spots), again = C.wanderChoice("creator", 4, spots);
  assert.deepStrictEqual(first, again);
  assert.deepStrictEqual(Object.keys(first).sort(), ["activity", "dwellMs", "node"]);
  assert.ok(first.dwellMs >= 3500 && first.dwellMs < 9000);
  const seen = new Set();
  for (let i = 0; i < 200; i += 1) { seen.add(C.wanderChoice("analyst", i, spots).node); }
  assert.deepStrictEqual([...seen].sort(), ["kitchen", "seat"]);
  // Wandering never touches replay state.
  const r = new C.Replay(4); r.seek(2);
  for (let i = 0; i < 50; i += 1) { C.wanderChoice("market_scout", i, spots); }
  assert.strictEqual(r.index, 2);
});

test("untrusted text is plain, bounded and control-free", () => {
  assert.strictEqual(C.plain("<img src=x onerror=alert(1)>"), "<img src=x onerror=alert(1)>");
  assert.strictEqual(C.plain("a\u0000b\u001bc"), "abc");
  assert.strictEqual(C.plain("x".repeat(500)).length, 200);
  assert.strictEqual(C.plain(null), "");
  assert.strictEqual(C.formatTime("2026-01-05T14:30:00Z"), "2026-01-05 14:30:00 UTC");
  assert.strictEqual(C.formatTime(null), "not recorded");
  assert.strictEqual(C.formatTime("<b>"), "invalid");
});

test("scene helpers read only recorded frames", () => {
  const frames = [
    { event: { component: "trading.research.trend_agent", event_type: "stage_started", status: "started", details: {} },
      room_states: { trend_agent: "working" } },
    { event: { component: "trading.simulation.engine", event_type: "order_decision", status: "rejected", details: {} },
      room_states: { trend_agent: "working" } },
    { event: { component: "trading.simulation.engine", event_type: "simulated_fill", status: "filled", details: {} },
      room_states: { trend_agent: "completed" } }];
  const scene = { contract: "hq_scene", version: "1.1", mode: "recorded_replay", frames, rooms: [], stations: [] };
  assert.ok(C.validScene(scene));
  assert.ok(!C.validScene({ ...scene, mode: "made_up" }));
  assert.ok(!C.validScene({ ...scene, contract: "other" }));
  assert.ok(!C.validScene({ ...scene, version: "1.0" }));
  assert.ok(C.ID_PATTERN.test("wfr-" + "a".repeat(32)) && !C.ID_PATTERN.test("wfr-" + "a".repeat(24)));
  assert.deepStrictEqual(C.roomStatesAt(scene, -1, ["trend_agent", "creator"]), { trend_agent: "idle", creator: "idle" });
  assert.deepStrictEqual(C.roomStatesAt(scene, 2, []), { trend_agent: "completed" });
  assert.deepStrictEqual(C.counters(scene, 2), { accepted: 0, rejected: 1, pending: 0, fills: 1 });
  assert.deepStrictEqual(C.counters(scene, 0), { accepted: 0, rejected: 0, pending: 0, fills: 0 });
  assert.strictEqual(C.lastEventFor(scene, 2, ["trading.research.trend_agent"]), frames[0]);
  assert.strictEqual(C.lastEventFor(scene, -1, ["trading.research.trend_agent"]), null);
  assert.strictEqual(C.describeEvent(frames[0].event, "Trend Agent"), "Trend Agent · stage started");
  assert.ok(C.ID_PATTERN.test("demo") && C.ID_PATTERN.test("srun-" + "a".repeat(24)));
  assert.ok(!C.ID_PATTERN.test("../etc/passwd") && !C.ID_PATTERN.test("tl-" + "a".repeat(23)));
});

test("occupancy: idle bots never claim the same shared spot", () => {
  const shared = ["sofa1", "sofa2", "coffee", "stool1", "stool2"].map((node) => ({ node, weight: 1, activity: "relax" }));
  const bots = ["market_scout", "trend_agent", "strategy_agent", "risk_review", "researcher", "analyst", "reviewer", "creator"];
  for (let cycle = 0; cycle < 40; cycle += 1) {
    const taken = {};
    bots.forEach((bot) => {
      const own = [{ node: "seat_" + bot, weight: 3, activity: "sit" }, { node: "nook_" + bot, weight: 2, activity: "idle" }];
      const choice = C.wanderChoice(bot, cycle, own.concat(shared), taken);
      assert.ok(!taken[choice.node], "two bots chose " + choice.node);
      taken[choice.node] = true;
    });
  }
  // When every spot is taken, a bot falls back to its own first spot.
  assert.strictEqual(C.wanderChoice("x", 1, [{ node: "own", weight: 1 }, { node: "s", weight: 1 }], { own: true, s: true }).node, "own");
});

test("movement is described honestly and never as activity when roaming", () => {
  assert.match(C.movementFor("free"), /not evidence/);
  assert.match(C.movementFor("station"), /recorded work/);
  ["station", "seat", "door", "hold", "free"].forEach((intent) => assert.ok(C.movementFor(intent).length > 10));
  assert.strictEqual(C.movementFor("anything"), C.MOVEMENT.free);
  assert.ok(C.describeEvent({ component: "content.workflow.researcher", event_type: "stage_reused", status: "completed",
    details: { attempt: 1 } }, "Researcher").includes("not run again"));
});

console.log(JSON.stringify({ passed }));
