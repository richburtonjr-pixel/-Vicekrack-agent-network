/*
 * ViceKrack HQ 2.0, Phase 1: the building. Architecture and furniture are simple, optimized placeholder
 * shapes (no downloaded models). Screens are canvas textures that main.js fills with REAL ViceKrack
 * numbers; nothing in this file invents data.
 */
import * as THREE from "/static/vendor/three.module.min.js";
import { BUILDING, UPPER, STAIRS, DEPARTMENTS, WORKSTATIONS } from "/static/hq3d/layout.js";
import { AGENTS } from "/static/hq3d/agents.js";

const B = BUILDING;
const TAU = Math.PI * 2;

function seeded(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

function canvasTexture(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return { canvas, ctx: canvas.getContext("2d"), texture };
}

export function buildWorld() {
  const scene = new THREE.Scene();
  const obstacles = [];          // walking blockers per level (layout.step uses them)
  const interactives = [];       // meshes that open a department panel when clicked
  const screens = {};            // department id -> canvas screen(s) main.js draws real data on
  const labels = {};             // agent id -> name plate sprite
  const shadowCasters = [];

  // ------------------------------------------------------------------ materials
  const M = {
    floor: null,
    stone: new THREE.MeshStandardMaterial({ color: 0xe8e3d9, roughness: 0.38 }),
    white: new THREE.MeshStandardMaterial({ color: 0xf5f4f0, roughness: 0.85 }),
    ceiling: new THREE.MeshStandardMaterial({ color: 0xfbfaf7, roughness: 0.95 }),
    wood: new THREE.MeshStandardMaterial({ color: 0xb4875b, roughness: 0.55 }),
    walnut: new THREE.MeshStandardMaterial({ color: 0x6b4630, roughness: 0.5 }),
    oak: new THREE.MeshStandardMaterial({ color: 0xd2b48c, roughness: 0.6 }),
    metal: new THREE.MeshStandardMaterial({ color: 0x2c3036, roughness: 0.35, metalness: 0.6 }),
    brushed: new THREE.MeshStandardMaterial({ color: 0xb9bcc0, roughness: 0.3, metalness: 0.8 }),
    glass: new THREE.MeshStandardMaterial({ color: 0xd4ecf7, roughness: 0.05, metalness: 0.1, transparent: true,
      opacity: 0.16, depthWrite: false, side: THREE.DoubleSide }),
    railGlass: new THREE.MeshStandardMaterial({ color: 0xcfe9f2, roughness: 0.05, transparent: true, opacity: 0.22,
      depthWrite: false, side: THREE.DoubleSide }),
    leaf: new THREE.MeshStandardMaterial({ color: 0x4f8b48, roughness: 0.8 }),
    leafDark: new THREE.MeshStandardMaterial({ color: 0x3a6f3a, roughness: 0.85 }),
    trunk: new THREE.MeshStandardMaterial({ color: 0x7a5a40, roughness: 0.9 }),
    planter: new THREE.MeshStandardMaterial({ color: 0x3d3f42, roughness: 0.6 }),
    cream: new THREE.MeshStandardMaterial({ color: 0xe9e0d0, roughness: 0.9 }),
    leather: new THREE.MeshStandardMaterial({ color: 0x8a5a3c, roughness: 0.6 }),
    charcoal: new THREE.MeshStandardMaterial({ color: 0x3b3e44, roughness: 0.8 }),
    screenOff: new THREE.MeshStandardMaterial({ color: 0x111418, roughness: 0.25, metalness: 0.2 }),
    accent: new THREE.MeshStandardMaterial({ color: 0x8b6cf0, emissive: 0x5b3fd0, emissiveIntensity: 0.6,
      roughness: 0.4 }),
    softbox: new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xfff4e0, emissiveIntensity: 0.9 }),
  };
  M.floor = floorMaterial();

  const root = new THREE.Group();
  scene.add(root);

  function mesh(geometry, material, x, y, z, opts = {}) {
    const m = new THREE.Mesh(geometry, material);
    m.position.set(x, y, z);
    if (opts.ry) { m.rotation.y = opts.ry; }
    m.castShadow = opts.cast !== false && material !== M.glass && material !== M.railGlass;
    m.receiveShadow = opts.receive !== false;
    (opts.parent || root).add(m);
    if (m.castShadow) { shadowCasters.push(m); }
    return m;
  }
  function box(w, h, d, material, x, y, z, opts) {
    return mesh(new THREE.BoxGeometry(w, h, d), material, x, y + h / 2, z, opts);
  }
  function block(level, cx, cz, w, d) {
    obstacles.push({ level, minX: cx - w / 2, maxX: cx + w / 2, minZ: cz - d / 2, maxZ: cz + d / 2 });
  }
  function round(level, x, z, r) { obstacles.push({ level, x, z, r }); }
  const Y = (level) => (level ? B.upperY : 0);

  // ------------------------------------------------------------------ sky, city and the plaza outside
  scene.background = new THREE.Color(0xcfe6f7);
  scene.fog = new THREE.Fog(0xdcebf5, 160, 520);
  const skyGeo = new THREE.SphereGeometry(600, 32, 16);
  const skyColors = [];
  const top = new THREE.Color(0x6fa8dc), horizon = new THREE.Color(0xe6f1f8);
  const pos = skyGeo.attributes.position;
  for (let i = 0; i < pos.count; i += 1) {
    const t = Math.max(0, pos.getY(i) / 600);
    const c = horizon.clone().lerp(top, Math.pow(t, 0.6));
    skyColors.push(c.r, c.g, c.b);
  }
  skyGeo.setAttribute("color", new THREE.Float32BufferAttribute(skyColors, 3));
  scene.add(new THREE.Mesh(skyGeo, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.BackSide, fog: false })));
  const plaza = mesh(new THREE.PlaneGeometry(900, 900), new THREE.MeshStandardMaterial({ color: 0xc9cbc6, roughness: 1 }),
    0, -0.05, 0, { cast: false });
  plaza.rotation.x = -Math.PI / 2;
  const lawn = mesh(new THREE.PlaneGeometry(70, 30), new THREE.MeshStandardMaterial({ color: 0x7fa86a, roughness: 1 }),
    0, -0.03, -50, { cast: false });
  lawn.rotation.x = -Math.PI / 2;
  city();

  // ------------------------------------------------------------------ floors, walls, roof
  const floor = mesh(new THREE.PlaneGeometry(B.maxX - B.minX, B.maxZ - B.minZ), M.floor, 0, 0.001, 0, { cast: false });
  floor.rotation.x = -Math.PI / 2;
  for (const r of UPPER) {                                   // the upper floor slabs (white underside)
    const w = r.maxX - r.minX, d = r.maxZ - r.minZ, cx = (r.minX + r.maxX) / 2, cz = (r.minZ + r.maxZ) / 2;
    box(w, 0.45, d, M.ceiling, cx, B.upperY - 0.45, cz);
    const top = mesh(new THREE.PlaneGeometry(w, d), M.floor, cx, B.upperY + 0.002, cz, { cast: false });
    top.rotation.x = -Math.PI / 2;
  }
  // North wall: solid on the ground floor, glass above (city views from the executive floor).
  box(B.maxX - B.minX, B.upperY, B.wall, M.white, 0, 0, B.maxZ - B.wall / 2);
  glassWall("x", B.maxZ - 0.1, B.minX, B.maxX, B.upperY, B.roofY);
  glassWall("x", B.minZ + 0.1, B.minX, B.maxX, 0, B.roofY, { door: [-3, 3] });   // south: the entrance facade
  glassWall("z", B.minX + 0.1, B.minZ, B.maxZ, 0, B.roofY);
  glassWall("z", B.maxX - 0.1, B.minZ, B.maxZ, 0, B.roofY);
  // Roof with a large skylight over the atrium.
  const sky = { minX: -22, maxX: 22, minZ: -22, maxZ: 10 };
  const roofT = 0.6;
  box(B.maxX - B.minX, roofT, B.maxZ - sky.maxZ, M.ceiling, 0, B.roofY, (sky.maxZ + B.maxZ) / 2);
  box(B.maxX - B.minX, roofT, sky.minZ - B.minZ, M.ceiling, 0, B.roofY, (B.minZ + sky.minZ) / 2);
  box(sky.minX - B.minX, roofT, sky.maxZ - sky.minZ, M.ceiling, (B.minX + sky.minX) / 2, B.roofY, (sky.minZ + sky.maxZ) / 2);
  box(B.maxX - sky.maxX, roofT, sky.maxZ - sky.minZ, M.ceiling, (sky.maxX + B.maxX) / 2, B.roofY, (sky.minZ + sky.maxZ) / 2);
  for (let x = sky.minX; x <= sky.maxX; x += 4) { box(0.25, 0.5, sky.maxZ - sky.minZ, M.white, x, B.roofY, (sky.minZ + sky.maxZ) / 2); }
  for (let z = sky.minZ; z <= sky.maxZ; z += 4) { box(sky.maxX - sky.minX, 0.5, 0.25, M.white, 0, B.roofY, z); }
  const skylight = mesh(new THREE.PlaneGeometry(sky.maxX - sky.minX, sky.maxZ - sky.minZ), M.glass, 0, B.roofY + 0.45,
    (sky.minZ + sky.maxZ) / 2, { cast: false, receive: false });
  skylight.rotation.x = -Math.PI / 2;

  // ------------------------------------------------------------------ balconies, columns, stairs
  railing("x", 12, -40, STAIRS.minX);
  railing("x", 12, STAIRS.maxX, 40);
  railing("z", -24, -12, 12);
  railing("z", 24, -12, 12);
  railing("x", -12, -40, -24);
  railing("x", -12, 24, 40);
  for (const [x, z] of [[-24, -12], [24, -12], [-24, 12], [24, 12], [-12, 12], [12, 12], [-24, 0], [24, 0]]) {
    mesh(new THREE.CylinderGeometry(0.38, 0.38, B.roofY, 24), M.white, x, B.roofY / 2, z);
    round(0, x, z, 0.5);
    round(1, x, z, 0.5);
  }
  stairs();

  // ------------------------------------------------------------------ the departments
  for (const dept of DEPARTMENTS) {
    const y = Y(dept.level);
    const rug = mesh(new THREE.PlaneGeometry(dept.w - 1, dept.d - 1),
      new THREE.MeshStandardMaterial({ color: dept.id === "command" ? 0xdad5ef : 0xe4dccd, roughness: 1 }),
      dept.x, y + 0.01, dept.z, { cast: false });
    rug.rotation.x = -Math.PI / 2;
    if (dept.id !== "command") { departmentSign(dept); }
  }
  furnishResearch(); furnishStudio(); furnishStrategy(); furnishRisk(); furnishCommand();
  furnishExecutive(); furnishAnalytics(); furnishOperations(); furnishTrends(); lounge();
  for (const agent of AGENTS) { workstation(agent); }

  // ------------------------------------------------------------------ light
  const hemi = new THREE.HemisphereLight(0xf2f7ff, 0xd8cdb8, 1.15);
  scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xfff0d8, 2.6);
  sun.position.set(-60, 90, -45);
  sun.target.position.set(0, 0, 0);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -55, right: 55, top: 45, bottom: -45, near: 10, far: 220 });
  sun.shadow.bias = -0.0004;
  sun.shadow.normalBias = 0.03;
  scene.add(sun, sun.target);
  const fill = new THREE.DirectionalLight(0xdfeaff, 0.5);
  fill.position.set(50, 40, 60);
  scene.add(fill);

  return { scene, obstacles, interactives, screens, labels, sun, shadowCasters };

  // ================================================================== helpers (hoisted)
  function floorMaterial() {
    const { canvas, ctx, texture } = canvasTexture(512, 512);
    ctx.fillStyle = "#ebe6dc"; ctx.fillRect(0, 0, 512, 512);
    const r = seeded(7);
    for (let i = 0; i < 4; i += 1) {
      for (let j = 0; j < 4; j += 1) {
        const v = 226 + Math.floor(r() * 10);
        ctx.fillStyle = `rgb(${v + 6},${v + 2},${v - 6})`;
        ctx.fillRect(i * 128 + 1, j * 128 + 1, 126, 126);
      }
    }
    ctx.strokeStyle = "rgba(160,150,135,0.35)"; ctx.lineWidth = 2;
    for (let k = 0; k <= 512; k += 128) { ctx.beginPath(); ctx.moveTo(k, 0); ctx.lineTo(k, 512); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, k); ctx.lineTo(512, k); ctx.stroke(); }
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(16, 11);
    void canvas;
    return new THREE.MeshStandardMaterial({ map: texture, roughness: 0.32, metalness: 0.02 });
  }

  function city() {
    const rand = seeded(42);
    const count = 140;
    const geo = new THREE.BoxGeometry(1, 1, 1);
    geo.translate(0, 0.5, 0);
    const towers = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ roughness: 0.4, metalness: 0.3 }), count);
    const m = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), p = new THREE.Vector3();
    const palette = [0xb8c6d4, 0xa9b8c8, 0xc9d3dc, 0x9fb0c2, 0xd6dde3, 0x8fa3b8];
    for (let i = 0; i < count; i += 1) {
      const a = rand() * TAU, d = 110 + rand() * 260;
      const w = 14 + rand() * 26, h = 25 + Math.pow(rand(), 1.6) * 150;
      p.set(Math.cos(a) * d, 0, Math.sin(a) * d);
      s.set(w, h, 14 + rand() * 26);
      m.compose(p, q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), rand() * 0.6), s);
      towers.setMatrixAt(i, m);
      towers.setColorAt(i, new THREE.Color(palette[Math.floor(rand() * palette.length)]));
    }
    towers.castShadow = false; towers.receiveShadow = false;
    scene.add(towers);
  }

  function glassWall(axis, at, from, to, y0, y1, opts = {}) {
    const len = to - from, h = y1 - y0, mid = (from + to) / 2;
    const pane = mesh(new THREE.PlaneGeometry(len, h), M.glass, axis === "x" ? mid : at, y0 + h / 2, axis === "x" ? at : mid,
      { cast: false, receive: false });
    if (axis === "z") { pane.rotation.y = Math.PI / 2; }
    for (let k = from; k <= to + 0.01; k += 4) {                 // vertical mullions
      if (opts.door && k > opts.door[0] && k < opts.door[1]) { continue; }
      box(axis === "x" ? 0.12 : 0.18, h, axis === "x" ? 0.18 : 0.12, M.metal, axis === "x" ? k : at, y0, axis === "x" ? at : k,
        { cast: false });
    }
    for (const yy of [y0, ...(y0 === 0 ? [B.upperY] : []), y1 - 0.15]) {      // transoms
      box(axis === "x" ? len : 0.16, 0.15, axis === "x" ? 0.16 : len, M.metal, axis === "x" ? mid : at, yy, axis === "x" ? at : mid,
        { cast: false });
    }
    if (opts.door) {                                               // a tall glass entrance portal
      box(opts.door[1] - opts.door[0] + 0.6, 0.35, 1.2, M.metal, (opts.door[0] + opts.door[1]) / 2, 4.2, at + 0.4, { cast: false });
    }
  }

  function railing(axis, at, from, to) {
    const len = to - from, mid = (from + to) / 2, y = B.upperY;
    const pane = mesh(new THREE.PlaneGeometry(len, 1.05), M.railGlass, axis === "x" ? mid : at, y + 0.55, axis === "x" ? at : mid,
      { cast: false, receive: false });
    if (axis === "z") { pane.rotation.y = Math.PI / 2; }
    box(axis === "x" ? len : 0.08, 0.06, axis === "x" ? 0.08 : len, M.brushed, axis === "x" ? mid : at, y + 1.08,
      axis === "x" ? at : mid, { cast: false });
  }

  function stairs() {
    const n = STAIRS.steps, depth = (STAIRS.maxZ - STAIRS.minZ) / n, rise = B.upperY / n, width = STAIRS.maxX - STAIRS.minX;
    for (let i = 0; i < n; i += 1) {
      const z = STAIRS.minZ + depth * (i + 0.5), topY = rise * (i + 1);
      box(width, 0.08, depth + 0.02, M.oak, 0, topY - 0.08, z);
      box(width, topY - 0.08, depth, M.white, 0, 0, z, { cast: i % 3 === 0 });
    }
    for (const side of [STAIRS.minX, STAIRS.maxX]) {
      const run = STAIRS.maxZ - STAIRS.minZ, len = Math.hypot(run, B.upperY), angle = Math.atan2(B.upperY, run);
      const g = mesh(new THREE.PlaneGeometry(len, 1.05), M.railGlass, side, B.upperY / 2 + 0.55, (STAIRS.minZ + STAIRS.maxZ) / 2,
        { cast: false, receive: false });
      g.rotation.set(0, -Math.PI / 2, 0);
      g.rotateZ(angle);
      const rail = mesh(new THREE.BoxGeometry(0.07, 0.06, len), M.brushed, side, B.upperY / 2 + 1.08, (STAIRS.minZ + STAIRS.maxZ) / 2,
        { cast: false });
      rail.rotation.x = -angle;
      block(0, side, (STAIRS.minZ + STAIRS.maxZ) / 2 + 0.6, 0.3, STAIRS.maxZ - STAIRS.minZ - 1.2);
    }
  }

  function chair(x, y, z, ry, material = M.charcoal) {
    const g = new THREE.Group();
    g.position.set(x, y, z); g.rotation.y = ry;
    root.add(g);
    mesh(new THREE.BoxGeometry(0.55, 0.08, 0.5), material, 0, 0.47, 0, { parent: g });
    mesh(new THREE.BoxGeometry(0.55, 0.6, 0.07), material, 0, 0.8, 0.24, { parent: g });
    mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.42, 8), M.brushed, 0, 0.22, 0, { parent: g });
    mesh(new THREE.CylinderGeometry(0.28, 0.28, 0.04, 16), M.brushed, 0, 0.02, 0, { parent: g });
    return g;
  }

  function desk(x, y, z, ry, opts = {}) {
    const w = opts.w || 1.8, d = opts.d || 0.8;
    const g = new THREE.Group();
    g.position.set(x, y, z); g.rotation.y = ry;
    root.add(g);
    mesh(new THREE.BoxGeometry(w, 0.05, d), opts.top || M.oak, 0, 0.75, 0, { parent: g });
    for (const sx of [-1, 1]) { mesh(new THREE.BoxGeometry(0.05, 0.73, d - 0.1), M.metal, sx * (w / 2 - 0.08), 0.365, 0, { parent: g }); }
    const monitors = opts.monitors === undefined ? 1 : opts.monitors;
    const faces = [];
    for (let i = 0; i < monitors; i += 1) {
      const off = (i - (monitors - 1) / 2) * 0.62;
      mesh(new THREE.BoxGeometry(0.6, 0.36, 0.03), M.screenOff, off, 1.08, -d / 2 + 0.18, { parent: g });
      mesh(new THREE.BoxGeometry(0.05, 0.28, 0.05), M.metal, off, 0.89, -d / 2 + 0.2, { parent: g });
      faces.push({ group: g, offset: off, z: -d / 2 + 0.2 });
    }
    // block in world space (rotation is a multiple of 90 degrees here)
    const quarter = Math.round(ry / (Math.PI / 2)) % 2 !== 0;
    block(y > 1 ? 1 : 0, x, z, quarter ? d + 0.6 : w, quarter ? w : d + 0.6);
    return { group: g, faces };
  }

  function screenOn(group, x, y, z, w, h, deptId, ry = 0) {
    const { canvas, ctx, texture } = canvasTexture(w > 2 ? 1024 : 512, Math.round((w > 2 ? 1024 : 512) * h / w));
    const material = new THREE.MeshBasicMaterial({ map: texture, toneMapped: false });
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
    plane.position.set(x, y, z);
    plane.rotation.y = ry;
    (group || root).add(plane);
    (screens[deptId] = screens[deptId] || []).push({ canvas, ctx, texture, w, h });
    interactives.push({ object: plane, department: deptId });
    return plane;
  }

  function wallScreen(deptId, x, y, z, ry, w = 4.2, h = 2.4) {
    box(w + 0.2, h + 0.2, 0.08, M.metal, x, y - 0.1, z, { ry, cast: false });
    const dz = Math.cos(ry) * 0.06, dx = Math.sin(ry) * 0.06;
    screenOn(null, x + dx, y + h / 2, z + dz, w, h, deptId, ry);
  }

  function departmentSign(dept) {
    const { canvas, ctx, texture } = canvasTexture(512, 160);
    const agent = AGENTS.find((a) => a.department === dept.id);
    ctx.fillStyle = "rgba(20,22,30,0.88)";
    roundRect(ctx, 4, 4, 504, 152, 22); ctx.fill();
    ctx.fillStyle = "#b69cff"; ctx.fillRect(28, 30, 8, 100);
    ctx.fillStyle = "#ffffff"; ctx.font = "600 52px system-ui, Segoe UI, sans-serif";
    ctx.fillText(dept.name, 56, 82);
    ctx.fillStyle = "#c9cdd6"; ctx.font = "32px system-ui, Segoe UI, sans-serif";
    ctx.fillText(agent ? `${agent.name}'s department` : "", 58, 128);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthWrite: false }));
    sprite.scale.set(3.4, 1.06, 1);
    // Signs hang over the back of each department, so they face you as you walk in.
    sprite.position.set(dept.x, Y(dept.level) + (dept.level === 0 && dept.z > -12 && Math.abs(dept.x) > 20 ? 3.6 : 4.2),
      dept.z + dept.d / 2 - 1.2);
    root.add(sprite);
    interactives.push({ object: sprite, department: dept.id });
    void canvas;
  }

  function workstation(agent) {
    const ws = WORKSTATIONS[agent.id];
    const y = Y(ws.level);
    // The agent sits on the south side facing north (ws.facing 0); the monitors face the chair and the
    // visitor walking up behind it, and show the department's real status.
    const d = desk(ws.x, y, ws.z, ws.facing + Math.PI, { monitors: 2, top: M.walnut });
    for (const face of d.faces) { screenOn(face.group, face.offset, 1.08, face.z, 0.56, 0.32, agent.department); }
    const sx = ws.x - Math.sin(ws.facing) * 0.75, sz = ws.z - Math.cos(ws.facing) * 0.75;
    chair(sx, y, sz, ws.facing + Math.PI);
    // Character pending: an empty, marked place, never a stand-in person.
    const ring = mesh(new THREE.RingGeometry(0.45, 0.6, 40), M.accent, sx, y + 0.02, sz, { cast: false, receive: false });
    ring.rotation.x = -Math.PI / 2;
    const { ctx, texture } = canvasTexture(512, 150);
    ctx.fillStyle = "rgba(255,255,255,0.94)"; roundRect(ctx, 4, 4, 504, 142, 20); ctx.fill();
    ctx.fillStyle = "#20232b"; ctx.font = "600 46px system-ui, Segoe UI, sans-serif";
    ctx.fillText(`${agent.name} · ${agent.role}`, 28, 64);
    ctx.fillStyle = "#6b6f7a"; ctx.font = "28px system-ui, Segoe UI, sans-serif";
    ctx.fillText("Character pending reference", 28, 112);
    const plate = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthWrite: false }));
    plate.scale.set(2.2, 0.64, 1);
    plate.position.set(sx, y + 2.15, sz);
    root.add(plate);
    labels[agent.id] = plate;
    interactives.push({ object: plate, department: agent.department });
    interactives.push({ object: d.group, department: agent.department });
  }

  function tree(x, y, z, h = 7, level = 0) {
    mesh(new THREE.CylinderGeometry(1.3, 1.1, 0.9, 28), M.planter, x, y + 0.45, z);
    mesh(new THREE.CylinderGeometry(0.16, 0.24, h * 0.55, 10), M.trunk, x, y + 0.9 + h * 0.27, z);
    const rand = seeded(Math.floor(x * 31 + z * 17 + 999));
    for (let i = 0; i < 6; i += 1) {
      const r = 1.1 + rand() * 1.0;
      mesh(new THREE.IcosahedronGeometry(r, 1), i % 2 ? M.leaf : M.leafDark,
        x + (rand() - 0.5) * 2.2, y + h * 0.62 + rand() * h * 0.35, z + (rand() - 0.5) * 2.2);
    }
    round(level, x, z, 1.4);
  }

  function plant(x, y, z, level = 0) {
    mesh(new THREE.CylinderGeometry(0.35, 0.28, 0.6, 16), M.planter, x, y + 0.3, z);
    mesh(new THREE.IcosahedronGeometry(0.55, 1), M.leaf, x, y + 1.0, z);
    round(level, x, z, 0.45);
  }

  function sofa(x, y, z, ry, len = 2.6, material = M.cream, level = 0) {
    const g = new THREE.Group(); g.position.set(x, y, z); g.rotation.y = ry; root.add(g);
    mesh(new THREE.BoxGeometry(len, 0.42, 0.95), material, 0, 0.21, 0, { parent: g });
    mesh(new THREE.BoxGeometry(len, 0.55, 0.22), material, 0, 0.6, 0.38, { parent: g });
    for (const s of [-1, 1]) { mesh(new THREE.BoxGeometry(0.22, 0.6, 0.95), material, s * (len / 2 - 0.11), 0.3, 0, { parent: g }); }
    const quarter = Math.round(ry / (Math.PI / 2)) % 2 !== 0;
    block(level, x, z, quarter ? 1.0 : len, quarter ? len : 1.0);
  }

  function shelf(x, y, z, ry, level = 0) {
    box(2.4, 2.2, 0.4, M.walnut, x, y, z, { ry });
    const quarter = Math.round(ry / (Math.PI / 2)) % 2 !== 0;
    block(level, x, z, quarter ? 0.5 : 2.4, quarter ? 2.4 : 0.5);
  }

  function furnishResearch() {
    shelf(-38.5, 0, -16, Math.PI / 2); shelf(-38.5, 0, -12.5, Math.PI / 2);
    wallScreen("research", -39.4, 1.4, -19.5, Math.PI / 2, 4.6, 2.6);
    desk(-31, 0, -19, 0, { w: 4.2, d: 1.2, monitors: 3 });
    for (const dx of [-1.4, 0, 1.4]) { chair(-31 + dx, 0, -18.1, 0); }
    desk(-22, 0, -19, 0, { monitors: 2 }); chair(-22, 0, -18.2, 0);
    plant(-19.5, 0, -10.5);
  }

  function furnishStudio() {
    // Cyclorama backdrop (curved white wall) with softbox lights and a camera.
    const cycMaterial = new THREE.MeshStandardMaterial({ color: 0xfafafa, roughness: 0.9, side: THREE.DoubleSide });
    mesh(new THREE.CylinderGeometry(6, 6, 5, 32, 1, true, Math.PI / 2, Math.PI / 2), cycMaterial, 32, 2.5, -18);
    const cycFloor = mesh(new THREE.CircleGeometry(6, 32, 0, Math.PI / 2), cycMaterial, 32, 0.02, -18, { cast: false });
    cycFloor.rotation.x = -Math.PI / 2;
    block(0, 35.5, -21.5, 7, 7);
    for (const [x, z] of [[25.5, -19], [29, -13.5]]) {
      mesh(new THREE.CylinderGeometry(0.03, 0.03, 2.2, 8), M.metal, x, 1.1, z);
      mesh(new THREE.BoxGeometry(0.9, 0.9, 0.2), M.softbox, x, 2.3, z, { cast: false });
      round(0, x, z, 0.5);
    }
    mesh(new THREE.CylinderGeometry(0.02, 0.35, 1.4, 3), M.metal, 26.5, 0.7, -15.5);
    box(0.5, 0.32, 0.3, M.charcoal, 26.5, 1.4, -15.5);
    round(0, 26.5, -15.5, 0.5);
    wallScreen("studio", 39.4, 1.4, -10, -Math.PI / 2, 4.6, 2.6);
    desk(22, 0, -10, 0, { w: 2.6, monitors: 3 }); chair(22, 0, -9.2, 0);
  }

  function furnishStrategy() {
    mesh(new THREE.CylinderGeometry(1.5, 1.5, 0.06, 40), M.oak, -22, 0.75, 6);
    mesh(new THREE.CylinderGeometry(0.12, 0.3, 0.75, 16), M.metal, -22, 0.37, 6);
    round(0, -22, 6, 1.6);
    for (let i = 0; i < 6; i += 1) {
      const a = (i / 6) * TAU;
      chair(-22 + Math.cos(a) * 2.2, 0, 6 + Math.sin(a) * 2.2, -a + Math.PI / 2);
    }
    box(3.6, 1.6, 0.06, M.white, -35, 0.9, 8.6, { cast: false });
    wallScreen("strategy", -39.4, 1.4, 2, Math.PI / 2, 4.2, 2.4);
  }

  function furnishRisk() {
    wallScreen("risk", 39.4, 1.4, 4, -Math.PI / 2, 4.2, 2.4);
    desk(32, 0, 7, 0, { monitors: 2 }); chair(32, 0, 7.8, 0);
    mesh(new THREE.PlaneGeometry(10, 2.2), M.glass, 27, 1.1, -2.6, { cast: false });
    block(0, 27, -2.6, 10, 0.2);
    plant(21, 0, 8);
  }

  function furnishCommand() {
    const c = { x: 0, z: -9 };
    const ring = mesh(new THREE.TorusGeometry(3.4, 0.45, 12, 64), M.walnut, c.x, 0.75, c.z);
    ring.rotation.x = Math.PI / 2;
    ring.scale.set(1, 1, 0.5);
    mesh(new THREE.CylinderGeometry(3.85, 3.85, 0.05, 64, 1, true), M.brushed, c.x, 0.55, c.z);
    mesh(new THREE.CylinderGeometry(0.35, 0.6, 2.4, 24), M.metal, c.x, 1.2, c.z);
    box(3.6, 0.12, 3.6, M.metal, c.x, 4.45, c.z, { cast: false });
    round(0, c.x, c.z, 4.2);
    // Four large screens around a central pylon: north and south show the real overview, east and west
    // the most recent real activity.
    const faces = [[0, "command"], [Math.PI, "command"], [Math.PI / 2, "command-feed"], [-Math.PI / 2, "command-feed"]];
    for (const [ry, id] of faces) {                       // four screens forming a square around the pylon
      const dx = Math.sin(ry) * 1.75, dz = Math.cos(ry) * 1.75;
      box(3.3, 2.0, 0.1, M.metal, c.x + dx * 0.97, 2.5, c.z + dz * 0.97, { ry, cast: false });
      screenOn(null, c.x + dx, 3.5, c.z + dz, 3.1, 1.8, id, ry);
    }
    for (let i = 0; i < 5; i += 1) {
      const a = Math.PI * (0.15 + i * 0.175) + Math.PI;
      chair(c.x + Math.cos(a) * 4.6, 0, c.z + Math.sin(a) * 4.6, -a - Math.PI / 2, M.leather);
    }
  }

  function furnishExecutive() {
    const y = B.upperY, x0 = -9, x1 = 9, z0 = 15, z1 = 27.6;
    for (const [axis, at, a, b] of [["x", z0, x0, -1.2], ["x", z0, 1.2, x1], ["z", x0, z0, z1], ["z", x1, z0, z1]]) {
      const len = b - a, mid = (a + b) / 2;
      const g = mesh(new THREE.PlaneGeometry(len, 3.2), M.glass, axis === "x" ? mid : at, y + 1.6, axis === "x" ? at : mid,
        { cast: false, receive: false });
      if (axis === "z") { g.rotation.y = Math.PI / 2; }
      box(axis === "x" ? len : 0.08, 0.08, axis === "x" ? 0.08 : len, M.metal, axis === "x" ? mid : at, y + 3.2, axis === "x" ? at : mid,
        { cast: false });
      if (axis === "x") { block(1, mid, at, len, 0.2); } else { block(1, at, mid, 0.2, len); }
    }
    const rug = mesh(new THREE.PlaneGeometry(10, 7), new THREE.MeshStandardMaterial({ color: 0xcfc3ad, roughness: 1 }),
      0, y + 0.015, 22, { cast: false });
    rug.rotation.x = -Math.PI / 2;
    sofa(-6.5, y, 20, Math.PI / 2, 2.6, M.leather, 1);
    mesh(new THREE.BoxGeometry(1.0, 0.4, 1.4), M.walnut, -4.8, y + 0.2, 20);
    block(1, -4.8, 20, 1.0, 1.4);
    wallScreen("executive", 0, y + 1.3, 27.3, Math.PI, 4.6, 2.6);
    plant(7.5, y, 26, 1); plant(-7.5, y, 26, 1);
  }

  function furnishAnalytics() {
    const y = B.upperY;
    wallScreen("analytics", -27, y + 1.3, 27.3, Math.PI, 5.2, 2.8);
    desk(-33, y, 22, 0, { monitors: 2 }); chair(-33, y, 22.8, 0);
    sofa(-20, y, 17, 0, 2.6, M.cream, 1);
    plant(-38, y, 26, 1);
  }

  function furnishOperations() {
    const y = B.upperY;
    wallScreen("operations", 27, y + 1.3, 27.3, Math.PI, 5.2, 2.8);
    desk(33, y, 22, 0, { monitors: 2 }); chair(33, y, 22.8, 0);
    desk(21, y, 22, 0, { monitors: 1 }); chair(21, y, 22.8, 0);
    plant(38, y, 26, 1);
  }

  function furnishTrends() {
    const y = B.upperY;
    wallScreen("trends", -39.4, y + 1.3, -4, Math.PI / 2, 4.2, 2.4);
    sofa(-30, y, -8, 0, 2.6, M.cream, 1);
    plant(-26, y, -10, 1);
    // East wing upper floor: a quiet lounge above Risk Review.
    sofa(32, y, -4, Math.PI / 2, 3.0, M.leather, 1);
    sofa(30, y, 6, Math.PI, 2.6, M.cream, 1);
    plant(37, y, -10, 1); plant(26, y, 9, 1);
  }

  function lounge() {
    for (const [x, z, h] of [[-13, -20, 8], [13, -20, 8], [-14, 4, 9], [14, 4, 9], [-8, 8, 7], [8, 8, 7]]) { tree(x, 0, z, h); }
    sofa(-8, 0, -17, Math.PI / 2, 3.0); sofa(8, 0, -17, -Math.PI / 2, 3.0);
    mesh(new THREE.CylinderGeometry(0.9, 0.9, 0.38, 32), M.walnut, -6, 0.19, -17); round(0, -6, -17, 1.0);
    mesh(new THREE.CylinderGeometry(0.9, 0.9, 0.38, 32), M.walnut, 6, 0.19, -17); round(0, 6, -17, 1.0);
    // Reception desk by the entrance with the VK crown.
    box(5, 1.1, 1.0, M.walnut, -8, 0, -24.5); block(0, -8, -24.5, 5, 1.0);
    const { ctx, texture } = canvasTexture(512, 256);
    ctx.fillStyle = "#14161c"; ctx.fillRect(0, 0, 512, 256);
    ctx.strokeStyle = "#b69cff"; ctx.lineWidth = 10; ctx.lineJoin = "round";
    ctx.beginPath(); ctx.moveTo(156, 170); ctx.lineTo(140, 70); ctx.lineTo(205, 125); ctx.lineTo(256, 50); ctx.lineTo(307, 125);
    ctx.lineTo(372, 70); ctx.lineTo(356, 170); ctx.closePath(); ctx.stroke();
    ctx.fillStyle = "#ffffff"; ctx.font = "700 40px system-ui, Segoe UI, sans-serif"; ctx.textAlign = "center";
    ctx.fillText("VICEKRACK", 256, 225);
    const logo = new THREE.Mesh(new THREE.PlaneGeometry(2.4, 1.2), new THREE.MeshBasicMaterial({ map: texture, toneMapped: false }));
    logo.position.set(-8, 0.62, -25.02); logo.rotation.y = Math.PI;
    root.add(logo);
  }
}

export function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}
