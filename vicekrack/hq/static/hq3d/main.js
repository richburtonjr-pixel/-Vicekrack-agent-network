/*
 * ViceKrack HQ 2.0, Phase 1: an explorable 3D headquarters over the existing app.
 *
 * Honesty rules (docs/hq3d.md):
 * - Every number, status and activity line comes from GET /api/studio/overview (saved ViceKrack records).
 *   When that is unavailable the HQ says so; it never fills in example data.
 * - Nothing here writes, acts or triggers a paid request (read-only GETs). Department panels only open existing screens.
 * - Agents have no stand-in bodies or faces: each workstation is marked "character pending".
 * - The frame is rendered on demand (when you move, look or new numbers arrive), so an idle HQ uses no GPU.
 */
import * as THREE from "/static/vendor/three.module.min.js";
import { BUILDING, DEPARTMENTS, START, WORKSTATIONS, departmentAt, move, visitPoint } from "/static/hq3d/layout.js";
import { AGENTS, agentById, characterStatus } from "/static/hq3d/agents.js";
import { buildWorld, roundRect } from "/static/hq3d/world.js";

const $ = (id) => document.getElementById(id);
const STATE_TEXT = { working: "Working now", idle: "Has saved work", attention: "Needs your attention",
                     empty: "Nothing yet", coming_soon: "Coming soon", unavailable: "Unavailable" };
const STATE_COLOR = { working: "#2dd4bf", idle: "#c4b5fd", attention: "#fbbf24", empty: "#9aa3af",
                      coming_soon: "#9aa3af", unavailable: "#f87171" };
const REFRESH_MS = 30000;

const H = {
  started: false, active: false, renderer: null, camera: null, world: null, dirty: true,
  player: { x: START.x, z: START.z, y: 0, level: 0, heading: START.yaw }, avatar: null,
  look: { yaw: START.yaw, pitch: 0.42, dist: 11 }, keys: new Set(), vel: { x: 0, z: 0 },
  data: null, dataError: null, mode: "loading", updatedAt: null, panel: null, quality: "high", timer: null,
  last: 0, raycaster: null, pad: { x: 0, z: 0 },
};

function storageGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
function storageSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* optional */ } }

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch (e) { return false; }
}

// ------------------------------------------------------------------ startup
function start() {
  if (H.started) { return true; }
  H.started = true;
  const host = $("hq3d-canvas");
  try {
    H.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  } catch (e) {
    $("hq3d-fallback").hidden = false;
    return false;
  }
  H.quality = storageGet("vk-hq3d-quality") === "low" ? "low" : "high";
  const r = H.renderer;
  r.outputColorSpace = THREE.SRGBColorSpace;
  r.toneMapping = THREE.ACESFilmicToneMapping;
  r.toneMappingExposure = 1.05;
  r.shadowMap.enabled = true;
  r.shadowMap.type = THREE.PCFSoftShadowMap;
  r.shadowMap.autoUpdate = false;                    // the building is static: shadows are drawn once
  r.domElement.setAttribute("aria-label", "3D headquarters. Use the department list for a keyboard-friendly tour.");
  r.domElement.setAttribute("role", "img");
  r.domElement.tabIndex = 0;
  host.appendChild(r.domElement);

  H.world = buildWorld();
  H.camera = new THREE.PerspectiveCamera(55, 1, 0.1, 1500);
  H.raycaster = new THREE.Raycaster();
  H.avatar = visitor();
  H.world.scene.add(H.avatar);
  applyQuality();
  resize();
  wireInput(r.domElement);
  buildHud();
  new ResizeObserver(resize).observe(host);
  drawAllScreens();
  return true;
}

/* You, the visitor: a plain, faceless marker (not one of the agents). */
function visitor() {
  const g = new THREE.Group();
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.24, 0.95, 6, 16),
    new THREE.MeshStandardMaterial({ color: 0x5b5f6b, roughness: 0.55 }));
  body.position.y = 0.72;
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.52, 40),
    new THREE.MeshBasicMaterial({ color: 0xb69cff, transparent: true, opacity: 0.9 }));
  ring.rotation.x = -Math.PI / 2; ring.position.y = 0.02;
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.09, 0.22, 12), new THREE.MeshBasicMaterial({ color: 0xb69cff }));
  nose.rotation.x = Math.PI / 2; nose.position.set(0, 1.05, 0.3);
  g.add(body, ring, nose);
  return g;
}

function applyQuality() {
  const high = H.quality === "high";
  H.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, high ? 2 : 1));
  H.renderer.shadowMap.enabled = high;
  H.world.sun.castShadow = high;
  H.world.scene.traverse((o) => { if (o.material) { o.material.needsUpdate = true; } });
  H.renderer.shadowMap.needsUpdate = true;
  const btn = $("hq3d-quality");
  if (btn) { btn.textContent = high ? "Graphics: high" : "Graphics: low"; btn.setAttribute("aria-pressed", high ? "false" : "true"); }
  H.dirty = true;
}

function resize() {
  const host = $("hq3d-canvas");
  const w = Math.max(1, host.clientWidth), h = Math.max(1, host.clientHeight);
  H.renderer.setSize(w, h, false);
  H.camera.aspect = w / h;
  H.camera.updateProjectionMatrix();
  H.dirty = true;
}

// ------------------------------------------------------------------ input
function wireInput(canvas) {
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, moved: 0 };
    canvas.setPointerCapture(e.pointerId);
    canvas.focus({ preventScroll: true });
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) { hover(e); return; }
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.moved += Math.abs(dx) + Math.abs(dy);
    drag.x = e.clientX; drag.y = e.clientY;
    H.look.yaw -= dx * 0.0055;
    H.look.pitch = Math.min(1.25, Math.max(-0.1, H.look.pitch + dy * 0.004));
    H.dirty = true;
  });
  canvas.addEventListener("pointerup", (e) => {
    if (drag && drag.moved < 6) { pick(e); }
    drag = null;
  });
  canvas.addEventListener("pointercancel", () => { drag = null; });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    H.look.dist = Math.min(18, Math.max(2.2, H.look.dist * (e.deltaY > 0 ? 1.1 : 0.9)));
    H.dirty = true;
  }, { passive: false });
  window.addEventListener("keydown", (e) => {
    if (!H.active) { return; }
    const tag = (e.target && e.target.tagName) || "";
    if (/INPUT|SELECT|TEXTAREA/.test(tag) || e.ctrlKey || e.metaKey || e.altKey) { return; }
    const k = e.key.toLowerCase();
    if (["w", "a", "s", "d", "arrowup", "arrowdown", "arrowleft", "arrowright", "shift"].includes(k)) {
      if (k.startsWith("arrow") && tag === "BUTTON") { return; }
      H.keys.add(k); e.preventDefault(); H.dirty = true;
    } else if (k === "e") {
      const near = nearby();
      if (near) { openPanel(near.id); e.preventDefault(); }
    } else if (k === "escape" && H.panel) {
      closePanel(); e.preventDefault();
    }
  });
  window.addEventListener("keyup", (e) => { H.keys.delete(e.key.toLowerCase()); });
  window.addEventListener("blur", () => { H.keys.clear(); });
}

function pointerRay(e) {
  const rect = H.renderer.domElement.getBoundingClientRect();
  const v = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  H.raycaster.setFromCamera(v, H.camera);
  const objects = H.world.interactives.map((i) => i.object);
  const hit = H.raycaster.intersectObjects(objects, true)[0];
  if (!hit || hit.distance > 45) { return null; }
  let o = hit.object;
  while (o) {
    const found = H.world.interactives.find((i) => i.object === o);
    if (found) { return found.department; }
    o = o.parent;
  }
  return null;
}
function hover(e) { H.renderer.domElement.style.cursor = pointerRay(e) ? "pointer" : "grab"; }
function pick(e) {
  const dept = pointerRay(e);
  if (dept) { openPanel(dept === "command-feed" ? "command" : dept); }
}

// ------------------------------------------------------------------ the frame
function frame(now) {
  if (!H.active) { return; }
  const dt = Math.min(0.1, (now - (H.last || now)) / 1000);
  H.last = now;
  const moving = updatePlayer(dt);
  if (moving || H.dirty) {
    placeCamera();
    H.renderer.render(H.world.scene, H.camera);
    H.dirty = false;
    updatePrompt();
  }
  window.requestAnimationFrame(frame);
}

function updatePlayer(dt) {
  const k = H.keys;
  let ix = (k.has("d") || k.has("arrowright") ? 1 : 0) - (k.has("a") || k.has("arrowleft") ? 1 : 0) + H.pad.x;
  let iz = (k.has("w") || k.has("arrowup") ? 1 : 0) - (k.has("s") || k.has("arrowdown") ? 1 : 0) + H.pad.z;
  const len = Math.hypot(ix, iz);
  if (len > 1) { ix /= len; iz /= len; }
  const speed = k.has("shift") ? 8 : 4.2;
  // Camera-relative: forward is the direction the camera looks (yaw), right is perpendicular.
  const fx = Math.sin(H.look.yaw), fz = Math.cos(H.look.yaw);
  const tx = (fx * iz - fz * ix) * speed, tz = (fz * iz + fx * ix) * speed;
  const blend = Math.min(1, dt * 10);
  H.vel.x += (tx - H.vel.x) * blend;
  H.vel.z += (tz - H.vel.z) * blend;
  if (Math.abs(H.vel.x) < 0.01 && Math.abs(H.vel.z) < 0.01 && !len) { H.vel.x = H.vel.z = 0; return false; }
  const next = move(H.player, H.vel.x * dt, H.vel.z * dt, H.world.obstacles);
  if (next !== H.player) { Object.assign(H.player, next); }
  if (len) { H.player.heading = Math.atan2(H.vel.x, H.vel.z); }
  H.avatar.position.set(H.player.x, H.player.y, H.player.z);
  H.avatar.rotation.y = H.player.heading;
  return true;
}

function placeCamera() {
  const p = H.player, target = new THREE.Vector3(p.x, p.y + 1.8, p.z);
  let dist = H.look.dist;
  const dir = new THREE.Vector3(-Math.sin(H.look.yaw) * Math.cos(H.look.pitch), Math.sin(H.look.pitch),
    -Math.cos(H.look.yaw) * Math.cos(H.look.pitch));
  const cam = new THREE.Vector3();
  for (; dist > 1.2; dist -= 0.25) {                 // keep the camera inside the building and off the ceiling
    cam.copy(target).addScaledVector(dir, dist);
    const inside = cam.x > BUILDING.minX + 0.8 && cam.x < BUILDING.maxX - 0.8 && cam.z > BUILDING.minZ + 0.8 &&
      cam.z < BUILDING.maxZ - 0.8 && cam.y < BUILDING.roofY - 0.8 && cam.y > p.y + 0.3;
    const underSlab = p.level === 0 && cam.y > BUILDING.upperY - 0.8 && onSlab(cam.x, cam.z);
    if (inside && !underSlab) { break; }
  }
  H.camera.position.copy(cam);
  H.camera.lookAt(target);
  H.avatar.position.set(p.x, p.y, p.z);
}
function onSlab(x, z) {
  return (z > 12 && z < 28) || (Math.abs(x) > 24 && z > -12 && z < 12);
}

// ------------------------------------------------------------------ HUD, panel and prompts
function buildHud() {
  const list = $("hq3d-departments");
  for (const dept of DEPARTMENTS) {
    const agent = AGENTS.find((a) => a.department === dept.id);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "hq3d-dept";
    b.dataset.department = dept.id;
    const name = document.createElement("span"); name.textContent = dept.name;
    const who = document.createElement("small"); who.textContent = agent ? agent.name : "Live overview";
    const dot = document.createElement("i"); dot.className = "hq3d-dot"; dot.setAttribute("aria-hidden", "true");
    b.append(dot, name, who);
    b.addEventListener("click", () => travel(dept.id));
    list.appendChild(b);
  }
  $("hq3d-quality").addEventListener("click", () => {
    H.quality = H.quality === "high" ? "low" : "high";
    storageSet("vk-hq3d-quality", H.quality);
    applyQuality();
  });
  $("hq3d-refresh").addEventListener("click", () => load());
  $("hq3d-close").addEventListener("click", closePanel);
  $("hq3d-help-toggle").addEventListener("click", () => {
    const help = $("hq3d-help"); help.hidden = !help.hidden;
    $("hq3d-help-toggle").setAttribute("aria-expanded", help.hidden ? "false" : "true");
  });
  // Touch: a simple hold-to-walk pad (a path towards full mobile and controller support).
  for (const b of document.querySelectorAll(".hq3d-pad button")) {
    const set = (on) => {
      const [x, z] = b.dataset.dir.split(",").map(Number);
      H.pad.x = on ? x : 0; H.pad.z = on ? z : 0; H.dirty = true;
    };
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); set(true); });
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) { b.addEventListener(ev, () => set(false)); }
  }
}

function travel(deptId) {
  const dept = DEPARTMENTS.find((d) => d.id === deptId);
  const p = visitPoint(dept);
  Object.assign(H.player, p);
  H.vel.x = H.vel.z = 0;
  H.look.yaw = 0;
  H.player.heading = 0;
  H.dirty = true;
  openPanel(deptId);
}

function nearby() {
  const p = H.player;
  for (const agent of AGENTS) {
    const ws = WORKSTATIONS[agent.id];
    if (ws.level === p.level && Math.hypot(ws.x - p.x, ws.z - p.z) < 3.2) {
      return DEPARTMENTS.find((d) => d.id === agent.department);
    }
  }
  if (p.level === 0 && Math.hypot(p.x, p.z + 9) < 6.5) { return DEPARTMENTS.find((d) => d.id === "command"); }
  return departmentAt(p.x, p.z, p.level);
}

function updatePrompt() {
  const near = nearby(), el = $("hq3d-prompt");
  if (near && (!H.panel || H.panel !== near.id)) {
    el.hidden = false;
    el.textContent = `Press E to open ${near.name}`;
  } else {
    el.hidden = true;
  }
  for (const b of document.querySelectorAll(".hq3d-dept")) {
    b.classList.toggle("here", !!near && b.dataset.department === near.id);
  }
}

function el(tag, attrs, parent, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) { if (v !== null && v !== undefined) { n.setAttribute(k, v); } }
  if (text !== undefined && text !== null) { n.textContent = text; }
  if (parent) { parent.appendChild(n); }
  return n;
}

function deptData(id) {
  if (!H.data) { return null; }
  return H.data.departments[id] || null;
}

function openPanel(id) {
  H.panel = id;
  const panel = $("hq3d-panel"), body = $("hq3d-panel-body");
  panel.hidden = false;
  body.textContent = "";
  const dept = DEPARTMENTS.find((d) => d.id === id);
  el("h3", { id: "hq3d-panel-title" }, body, dept.name);
  if (id === "command") {
    el("p", { class: "hq3d-sub" }, body, "The live overview of every department.");
    renderCommand(body);
  } else {
    const agentId = (deptData(id) || {}).agent || (AGENTS.find((a) => a.department === id) || {}).id;
    const agent = agentById(agentId);
    if (agent) {
      el("p", { class: "hq3d-sub" }, body, `${agent.name} · ${agent.role}`);
      el("p", { class: "hq3d-pending" }, body, characterStatus(agent));
    }
    renderDept(body, id);
  }
  if (dept.screen) {
    const go = el("button", { type: "button", class: "hq3d-go" }, body, dept.screenLabel);
    go.addEventListener("click", () => {
      const nav = document.querySelector(`.view-btn[data-view="${dept.screen}"]`);
      if (nav) { nav.click(); }
    });
  }
  sourceLine(body);
  updatePrompt();
}

function closePanel() {
  H.panel = null;
  $("hq3d-panel").hidden = true;
  updatePrompt();
  H.renderer.domElement.focus({ preventScroll: true });
}

function sourceLine(body) {
  let text;
  if (H.mode === "real") { text = `From your saved ViceKrack records. Checked ${clock(H.updatedAt)}.`; }
  else if (H.mode === "demo") { text = `Practice data from the offline demo, not your real work. Checked ${clock(H.updatedAt)}.`; }
  else if (H.mode === "off") { text = "Real numbers are unavailable: the Studio is off. Start ViceKrack with the ViceKrack button."; }
  else if (H.mode === "error") { text = "Real numbers could not be loaded. Use Refresh numbers to try again."; }
  else { text = "Loading your saved records…"; }
  el("p", { class: "hq3d-source" }, body, text);
}

function renderDept(body, id) {
  const d = deptData(id);
  if (!d) { el("p", { class: "hq3d-state unavailable" }, body, STATE_TEXT.unavailable); return; }
  const state = el("p", { class: "hq3d-state " + d.state }, body, STATE_TEXT[d.state] || d.state);
  void state;
  if (d.facts.length) {
    const dl = el("dl", { class: "hq3d-facts" }, body);
    for (const f of d.facts) { el("dt", {}, dl, f.label); el("dd", {}, dl, String(f.value)); }
  }
  if (d.latest) { el("p", { class: "hq3d-latest" }, body, `Latest: ${d.latest.text} (${d.latest.status}, ${clock(d.latest.at)})`); }
  if (d.note) { el("p", { class: "hq3d-note" }, body, d.note); }
}

function renderCommand(body) {
  if (!H.data) { el("p", { class: "hq3d-state unavailable" }, body, STATE_TEXT.unavailable); return; }
  const t = H.data.totals;
  const dl = el("dl", { class: "hq3d-facts" }, body);
  for (const [k, v] of [["Research runs", t.research_runs], ["Stories built", t.productions], ["Video workflows", t.workflows],
                        ["Waiting for your review", t.waiting_for_review]]) { el("dt", {}, dl, k); el("dd", {}, dl, String(v)); }
  el("h4", {}, body, "Recent activity");
  if (!H.data.activity.length) { el("p", { class: "hq3d-note" }, body, "No saved activity yet. Start with Research."); return; }
  const ol = el("ol", { class: "hq3d-activity" }, body);
  for (const a of H.data.activity.slice(0, 8)) {
    const li = el("li", {}, ol);
    el("time", { datetime: a.at }, li, clock(a.at));
    el("span", {}, li, ` ${agentById(a.agent) ? agentById(a.agent).name : ""}: ${a.text}`);
  }
}

function clock(iso) {
  if (!iso) { return "unknown time"; }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) { return "unknown time"; }
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// ------------------------------------------------------------------ real data
async function load() {
  try {
    const session = await (await fetch("/api/studio/session", { credentials: "same-origin", cache: "no-store" })).json();
    if (!session.actions_enabled) {
      H.mode = "off"; H.data = null;
    } else {
      const response = await fetch("/api/studio/overview", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) { throw new Error("overview"); }
      const doc = await response.json();
      if (doc.contract !== "hq_overview") { throw new Error("overview"); }
      H.data = doc;
      H.mode = doc.demo ? "demo" : "real";
    }
    H.dataError = null;
  } catch (e) {
    H.mode = "error"; H.data = null; H.dataError = "load_failed";
  }
  H.updatedAt = new Date().toISOString();
  renderBadge();
  drawAllScreens();
  if (H.panel) { openPanel(H.panel); }
}

function renderBadge() {
  const b = $("hq3d-badge");
  const text = { real: "Real data", demo: "Practice data (demo)", off: "Studio off: no real data",
                 error: "Data unavailable", loading: "Loading" }[H.mode];
  b.textContent = text;
  b.className = "hq3d-badge " + H.mode;
  for (const btn of document.querySelectorAll(".hq3d-dept")) {
    const d = deptData(btn.dataset.department);
    const dot = btn.querySelector(".hq3d-dot");
    dot.style.background = btn.dataset.department === "command" ? (H.data ? "#2dd4bf" : STATE_COLOR.unavailable)
      : STATE_COLOR[d ? d.state : "unavailable"];
  }
}

// ------------------------------------------------------------------ the in-world screens (real data only)
function drawAllScreens() {
  if (!H.world) { return; }
  for (const [id, list] of Object.entries(H.world.screens)) {
    for (const s of list) { drawScreen(id, s); s.texture.needsUpdate = true; }
  }
  H.dirty = true;
}

function drawScreen(id, s) {
  const { ctx, canvas } = s, W = canvas.width, Hh = canvas.height, u = W / 512;
  ctx.fillStyle = "#0f1218"; ctx.fillRect(0, 0, W, Hh);
  ctx.fillStyle = "#1a1f29"; ctx.fillRect(0, 0, W, 54 * u);
  ctx.fillStyle = "#b69cff"; ctx.fillRect(0, 54 * u - 3 * u, W, 3 * u);
  ctx.textBaseline = "middle";
  const title = id === "command" ? "ViceKrack overview" : id === "command-feed" ? "Recent activity"
    : (DEPARTMENTS.find((d) => d.id === id) || { name: id }).name;
  text(ctx, title, 18 * u, 27 * u, 26 * u, "#ffffff", "600");
  const tag = { real: "REAL DATA", demo: "PRACTICE DATA", off: "STUDIO OFF", error: "UNAVAILABLE", loading: "LOADING" }[H.mode];
  ctx.textAlign = "right"; text(ctx, tag, W - 16 * u, 27 * u, 15 * u, H.mode === "real" ? "#5eead4" : "#fbbf24", "600");
  ctx.textAlign = "left";
  const top = 80 * u, line = 34 * u;
  if (!H.data) {
    text(ctx, H.mode === "off" ? "Real numbers need the Studio." : H.mode === "loading" ? "Loading saved records…"
      : "Saved records could not be loaded.", 18 * u, top + 10 * u, 22 * u, "#c9cdd6");
    return;
  }
  if (id === "command") {
    const t = H.data.totals;
    const rows = [["Research runs", t.research_runs], ["Stories built", t.productions], ["Video workflows", t.workflows],
                  ["Waiting for your review", t.waiting_for_review]];
    rows.forEach(([k, v], i) => { row(ctx, k, v, top + i * line * 1.15, W, u); });
    return;
  }
  if (id === "command-feed") {
    const items = H.data.activity.slice(0, 6);
    if (!items.length) { text(ctx, "No saved activity yet.", 18 * u, top + 10 * u, 22 * u, "#c9cdd6"); return; }
    items.forEach((a, i) => {
      const who = agentById(a.agent);
      text(ctx, `${clock(a.at)}  ${who ? who.name : ""}`, 18 * u, top + i * line * 1.25, 15 * u, "#9aa3af");
      text(ctx, a.text.slice(0, 44), 18 * u, top + i * line * 1.25 + 17 * u, 18 * u, "#e6ebf1");
    });
    return;
  }
  const d = deptData(id);
  if (!d) { text(ctx, "No data for this department.", 18 * u, top, 22 * u, "#c9cdd6"); return; }
  text(ctx, STATE_TEXT[d.state] || d.state, 18 * u, top, 24 * u, STATE_COLOR[d.state] || "#fff", "600");
  if (!d.facts.length) { text(ctx, (d.note || "").slice(0, 48), 18 * u, top + line, 17 * u, "#c9cdd6"); return; }
  d.facts.slice(0, 3).forEach((f, i) => row(ctx, f.label, f.value, top + line * (i + 1.1), W, u));
}

function row(ctx, label, value, y, W, u) {
  text(ctx, label.length > 34 ? label.slice(0, 33) + "…" : label, 18 * u, y, 19 * u, "#c9cdd6");
  ctx.textAlign = "right"; text(ctx, String(value), W - 18 * u, y, 26 * u, "#ffffff", "700"); ctx.textAlign = "left";
}
function text(ctx, s, x, y, size, color, weight = "400") {
  ctx.font = `${weight} ${Math.round(size)}px system-ui, "Segoe UI", sans-serif`;
  ctx.fillStyle = color;
  ctx.fillText(s, x, y);
}
void roundRect;

// ------------------------------------------------------------------ joining the existing app
function show() {
  if (!webglAvailable()) { $("hq3d-fallback").hidden = false; return; }
  if (!start()) { return; }
  H.active = true;
  H.dirty = true;
  resize();
  load();
  window.clearInterval(H.timer);
  H.timer = window.setInterval(() => { if (H.active && !document.hidden) { load(); } }, REFRESH_MS);
  H.last = 0;
  window.requestAnimationFrame(frame);
}
function hide() {
  H.active = false;
  H.keys.clear();
  window.clearInterval(H.timer);
}

document.addEventListener("hq:view", (e) => { if (e.detail === "hq3d") { show(); } else if (H.active) { hide(); } });
if (document.body.classList.contains("hq3d-open")) { show(); }
window.HQ3D = { state: H, show, hide, travel, webglAvailable };
