// HQ 2.0 walking rules (vicekrack/hq/static/hq3d/layout.js), run by tests/test_hq3d.py with Node.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const L = await import(pathToFileURL(resolve(process.argv[2], "vicekrack/hq/static/hq3d/layout.js")).href);
const A = await import(pathToFileURL(resolve(process.argv[2], "vicekrack/hq/static/hq3d/agents.js")).href);

function walk(from, dx, dz, steps, obstacles = []) {
  let p = { ...from };
  for (let i = 0; i < steps; i += 1) { p = L.move(p, dx, dz, obstacles); }
  return p;
}
const ground = (x, z) => ({ x, z, y: 0, level: 0 });

// The walls hold.
assert.ok(walk(ground(0, -17), 0, -0.2, 200).z > L.BUILDING.minZ);
assert.ok(walk(ground(0, -17), 0.2, 0, 400).x < L.BUILDING.maxX);

// Up the stairs to the balcony, then along it into the west wing, and back down.
let p = walk(ground(0, -1), 0, 0.1, 160);
assert.equal(p.level, 1);
assert.equal(p.y, L.BUILDING.upperY);
assert.ok(p.z > L.STAIRS.maxZ);
p = walk(p, 0, 0.1, 30);
p = walk(p, -0.2, 0, 160);
assert.equal(p.level, 1);
assert.ok(p.x < -30, "walked along the north balcony");
p = walk(p, 0, -0.2, 120);
assert.ok(p.z < 0 && p.level === 1, "walked into the west wing on the upper floor");
const fall = walk(p, 0.2, 0, 200);
assert.ok(fall.x <= -24, "the balcony railing stops you falling into the atrium");
let down = walk({ x: 0, z: 13, y: L.BUILDING.upperY, level: 1 }, 0, -0.1, 200);
assert.equal(down.level, 0);
assert.equal(down.y, 0);

// No stepping onto the stairs from the side, or off them halfway up.
const side = walk(ground(-6, 6), 0.1, 0, 60);
assert.ok(side.x < L.STAIRS.minX, "the stair side is closed");
const mid = { x: 0, z: 6, y: L.stairHeight(6), level: 0 };
assert.equal(L.step(mid, -4, 6), null);

// Ground under the balcony is walkable; obstacles block.
assert.equal(walk(ground(-30, 14), 0, 0.1, 50).level, 0);
const block = [{ level: 0, minX: -1, maxX: 1, minZ: -12, maxZ: -10 }];
assert.ok(walk(ground(0, -17), 0, 0.1, 100, block).z < -12);

// Departments, agents and workstations line up with docs/visual-direction.md.
assert.deepEqual(A.AGENTS.map((a) => a.id), ["ricky", "kiara", "jax", "layla", "ace", "sienna", "kai", "nia"]);
assert.equal(A.AGENTS.filter((a) => a.gender === "woman").length, 4);
for (const agent of A.AGENTS) {
  assert.equal(agent.model, null, "no character model until the owner approves one");
  assert.ok(L.WORKSTATIONS[agent.id], agent.id);
  const dept = L.DEPARTMENTS.find((d) => d.id === agent.department);
  assert.ok(dept, agent.department);
  assert.equal(L.WORKSTATIONS[agent.id].level, dept.level);
  assert.ok(A.characterStatus(agent).startsWith("Character pending"));
}
for (const dept of L.DEPARTMENTS) {
  const v = L.visitPoint(dept);
  assert.ok(L.step(v, v.x, v.z), `visit point of ${dept.id} is walkable`);
}
console.log("hq3d layout ok");
