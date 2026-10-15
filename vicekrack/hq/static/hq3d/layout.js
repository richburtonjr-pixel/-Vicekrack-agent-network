/*
 * ViceKrack HQ 2.0: the building plan and the walking rules. Pure data and math, no Three.js, so the
 * same file is loaded by the page and by the Node tests (tests/hq3d_layout_test.mjs).
 *
 * Units are metres. +x is east, +z is north, y is up. The entrance is in the south glass wall.
 *
 *   north wall ─────────────────────────────────────────────────────────────── z = 28
 *   │ Analytics (Kai)   │ Executive office (Ricky) │ Operations (Nia)  │  upper floor, y = 6
 *   │───────────────────┴──────── balcony ─────────┴───────────────────│  z = 12
 *   │ Trends (Jax)  ┊                 stairs                ┊ Risk Review (Ace) │ upper wings
 *   │ Strategy      ┊                   ▲                    ┊  (ground: Risk)   │
 *   │ (Layla)       ┊          Command desk ◉               ┊                   │
 *   │ Research      ┊        lounge + trees                 ┊ Video Studio      │
 *   │ (Kiara)       ┊                                       ┊ (Sienna)          │
 *   south glass wall ───────────────── entrance ─────────────────────────────── z = -28
 */

export const BUILDING = { minX: -40, maxX: 40, minZ: -28, maxZ: 28, upperY: 6, roofY: 15, wall: 0.5 };

// The upper floor: a north balcony strip plus east and west wings (the centre is an open atrium).
export const UPPER = [
  { minX: -40, maxX: 40, minZ: 12, maxZ: 28 },     // north balcony (executive office, analytics, operations)
  { minX: -40, maxX: -24, minZ: -12, maxZ: 12 },   // west wing (trends)
  { minX: 24, maxX: 40, minZ: -12, maxZ: 12 },     // east wing (lounge above risk review)
];

// The grand staircase rises northwards from the atrium floor to the north balcony.
export const STAIRS = { minX: -3.5, maxX: 3.5, minZ: 1, maxZ: 12, steps: 24 };

export const PLAYER_RADIUS = 0.45;

// Departments: where they are, which agent works there and which existing screen holds the real work.
// `screen` is a data-view value of the existing navigation (research, studio, ...) or null.
export const DEPARTMENTS = [
  { id: "research", name: "Research", agent: "kiara", level: 0, x: -27, z: -16, w: 18, d: 14, screen: "research",
    screenLabel: "Open Research" },
  { id: "strategy", name: "Strategy", agent: "layla", level: 0, x: -27, z: 3, w: 18, d: 12, screen: "research",
    screenLabel: "Open story cards" },
  { id: "studio", name: "Video Studio", agent: "sienna", level: 0, x: 27, z: -16, w: 18, d: 16, screen: "studio",
    screenLabel: "Open Video Studio" },
  { id: "risk", name: "Risk Review", agent: "ace", level: 0, x: 27, z: 3, w: 18, d: 12, screen: "research",
    screenLabel: "Open story checks" },
  { id: "command", name: "Command", agent: null, level: 0, x: 0, z: -9, w: 14, d: 10, screen: null,
    screenLabel: null },
  { id: "executive", name: "Executive office", agent: "ricky", level: 1, x: 0, z: 21, w: 18, d: 13, screen: "studio",
    screenLabel: "Open videos to review" },
  { id: "analytics", name: "Analytics", agent: "kai", level: 1, x: -27, z: 21, w: 22, d: 12, screen: null,
    screenLabel: null },
  { id: "operations", name: "Operations", agent: "nia", level: 1, x: 27, z: 21, w: 22, d: 12, screen: "studio",
    screenLabel: "Open Video Studio" },
  { id: "trends", name: "Trends", agent: "jax", level: 1, x: -32, z: 0, w: 14, d: 20, screen: null,
    screenLabel: null },
];

// One workstation per agent: the desk centre, the way the seated agent will face (radians, 0 = north),
// and the floor level. The chair is 0.75 m behind the desk. Characters are placed here once their
// models exist.
export const WORKSTATIONS = {
  kiara: { x: -27, z: -13, facing: 0, level: 0 },
  layla: { x: -29, z: 2, facing: 0, level: 0 },
  sienna: { x: 31, z: -9, facing: 0, level: 0 },
  ace: { x: 27, z: 4, facing: 0, level: 0 },
  ricky: { x: 0, z: 24, facing: 0, level: 1 },
  kai: { x: -27, z: 23, facing: 0, level: 1 },
  nia: { x: 27, z: 23, facing: 0, level: 1 },
  jax: { x: -32, z: 2, facing: 0, level: 1 },
};

export const START = { x: 0, z: -17, yaw: 0 };

function inside(rect, x, z, pad) {
  return x >= rect.minX + pad && x <= rect.maxX - pad && z >= rect.minZ + pad && z <= rect.maxZ - pad;
}

export function onStairs(x, z) {
  return x >= STAIRS.minX && x <= STAIRS.maxX && z >= STAIRS.minZ && z <= STAIRS.maxZ;
}

export function stairHeight(z) {
  const t = Math.min(1, Math.max(0, (z - STAIRS.minZ) / (STAIRS.maxZ - STAIRS.minZ)));
  return t * BUILDING.upperY;
}

function inUpper(x, z) {
  return UPPER.some((rect) => inside(rect, x, z, 0));
}

/* On the upper floor with `pad` metres of clearance to its edges (the wings join the balcony seamlessly). */
export function onUpperFloor(x, z, pad = 0) {
  if (!inUpper(x, z)) { return false; }
  if (!pad) { return true; }
  for (let i = 0; i < 8; i += 1) {
    const a = (i / 8) * Math.PI * 2;
    if (!inUpper(x + Math.cos(a) * pad, z + Math.sin(a) * pad)) { return false; }
  }
  return true;
}

function hitsObstacle(obstacles, level, x, z, r) {
  for (const o of obstacles) {
    if (o.level !== level) { continue; }
    if (o.r !== undefined) {
      const dx = x - o.x, dz = z - o.z;
      if (dx * dx + dz * dz < (o.r + r) * (o.r + r)) { return true; }
    } else if (x > o.minX - r && x < o.maxX + r && z > o.minZ - r && z < o.maxZ + r) {
      return true;
    }
  }
  return false;
}

/*
 * Where a walker at (from) ends up when trying to step to (x, z). Returns {x, z, y, level} or null when
 * the step is blocked. Levels: 0 ground, 1 upper. The stairs are the only way between levels, and they
 * can only be entered or left at their ends (side railings).
 */
export function step(from, x, z, obstacles = [], r = PLAYER_RADIUS) {
  const b = BUILDING;
  if (!inside(b, x, z, b.wall + r)) { return null; }
  const wasOnStairs = onStairs(from.x, from.z) && from.y > 0.05 && from.y < b.upperY - 0.05;
  if (onStairs(x, z)) {
    const y = stairHeight(z);
    const enteringFromSide = !onStairs(from.x, from.z) && z > STAIRS.minZ + 0.6 && z < STAIRS.maxZ - 0.6;
    if (enteringFromSide) { return null; }
    if (from.level === 1 && !onStairs(from.x, from.z) && z < STAIRS.maxZ - 0.6) { return null; }
    if (from.level === 0 && !onStairs(from.x, from.z) && z > STAIRS.minZ + 0.6) { return null; }
    return { x, z, y, level: y > b.upperY / 2 ? 1 : 0 };
  }
  if (wasOnStairs) {
    // Leaving the stairs: only at the bottom (onto the ground) or the top (onto the balcony).
    if (z <= STAIRS.minZ) { return hitsObstacle(obstacles, 0, x, z, r) ? null : { x, z, y: 0, level: 0 }; }
    if (z >= STAIRS.maxZ) { return hitsObstacle(obstacles, 1, x, z, r) ? null : { x, z, y: b.upperY, level: 1 }; }
    return null;
  }
  if (from.level === 1) {
    // The balcony edge has a glass railing, except at the top of the stairs.
    const stairTop = x >= STAIRS.minX + r && x <= STAIRS.maxX - r && z >= STAIRS.maxZ && z <= STAIRS.maxZ + r + 0.2;
    if (!(onUpperFloor(x, z, r) || (stairTop && onUpperFloor(x, z, 0)))) { return null; }
    return hitsObstacle(obstacles, 1, x, z, r) ? null : { x, z, y: b.upperY, level: 1 };
  }
  return hitsObstacle(obstacles, 0, x, z, r) ? null : { x, z, y: 0, level: 0 };
}

/* Slide along walls: try the full step, then each axis on its own. */
export function move(from, dx, dz, obstacles = [], r = PLAYER_RADIUS) {
  return step(from, from.x + dx, from.z + dz, obstacles, r)
    || step(from, from.x + dx, from.z, obstacles, r)
    || step(from, from.x, from.z + dz, obstacles, r)
    || from;
}

/* The department a walker is standing in (same level), or null. */
export function departmentAt(x, z, level) {
  return DEPARTMENTS.find((d) => d.level === level && Math.abs(x - d.x) <= d.w / 2 && Math.abs(z - d.z) <= d.d / 2) || null;
}

/* Where to stand to visit a department (quick travel): inside its south edge, facing north towards its
   sign and screens. The Command visit point is south of the desk. */
export function visitPoint(dept) {
  if (dept.id === "command") { return { x: 0, z: -17, y: 0, level: 0 }; }
  const z = dept.z - dept.d / 2 + 1.5;
  if (dept.level === 1) { return { x: dept.x, z, y: BUILDING.upperY, level: 1 }; }
  return { x: dept.x, z, y: 0, level: 0 };
}
