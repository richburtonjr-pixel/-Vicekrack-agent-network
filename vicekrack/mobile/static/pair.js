/* ViceKrack Mobile Command Center: pairing and status (PR 1). Text is only ever set with textContent. */
"use strict";

const $ = (id) => document.getElementById(id);
let csrf = null;

async function call(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  let body = {};
  try { body = await response.json(); } catch (_) { body = {}; }
  return { ok: response.ok, status: response.status, body };
}

function show(which) {
  $("pair").hidden = which !== "pair";
  $("status").hidden = which !== "status";
}

function money(value) { return "$" + Number(value || 0).toFixed(2); }

async function loadStatus() {
  $("subtitle").textContent = "Checking your PC…";
  let result;
  try { result = await call("/m/api/status"); } catch (_) { result = null; }
  if (!result || !result.ok) {
    $("subtitle").textContent = "Can't reach your PC. Is it on, with Tailscale connected?";
    show("status");
    $("pc").className = "banner bad";
    $("pc").textContent = "PC offline";
    return;
  }
  const s = result.body;
  if (!s.paired) { $("subtitle").textContent = "Not paired yet"; show("pair"); return; }
  $("subtitle").textContent = s.device.name + " · paired";
  $("pc").className = "banner ok";
  $("pc").textContent = "PC online";
  const modes = { real: "Running · Real mode", demo: "Running · Demo mode", read_only: "Running · view only",
                  offline: "Not running" };
  $("hq").textContent = modes[s.hq.mode] || "Unknown";
  $("key").textContent = s.hq.xai_key ? "Saved" : (s.hq.reachable ? "Missing" : "—");
  $("via").textContent = s.connection.via === "tailscale" ? "Tailscale · private" : "This PC";
  $("pin").textContent = s.pin_set ? "On" : "Not set";
  $("spend").textContent = money(s.spending.today_usd) + " of " + money(s.spending.cap_usd);
  const share = s.spending.cap_usd > 0 ? Math.min(100, (s.spending.today_usd / s.spending.cap_usd) * 100) : 0;
  $("meter").style.width = share + "%";
  show("status");
}

async function pair() {
  $("pair-error").hidden = true;
  const code = $("code").value.trim();
  if (!/^[0-9]{6}$/.test(code)) {
    $("pair-error").textContent = "Enter the 6-digit code from your PC.";
    $("pair-error").hidden = false;
    return;
  }
  $("pair-button").disabled = true;
  const result = await call("/m/api/pair", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, name: $("name").value.trim() || "iPhone" }),
  });
  $("pair-button").disabled = false;
  if (!result.ok) {
    $("pair-error").textContent = (result.body.error && result.body.error.message) || "Pairing failed.";
    $("pair-error").hidden = false;
    return;
  }
  csrf = result.body.csrf;
  await loadStatus();
}

async function unpair() {
  if (!window.confirm("Unpair this phone? You'll need a new code from the PC to use it again.")) { return; }
  if (!csrf) {
    const session = await call("/m/api/session");
    if (!session.ok) { await loadStatus(); return; }
    csrf = session.body.csrf;
  }
  await call("/m/api/unpair", { method: "POST", headers: { "Content-Type": "application/json", "X-VK-Mobile-CSRF": csrf },
                                body: "{}" });
  csrf = null;
  await loadStatus();
}

document.addEventListener("DOMContentLoaded", () => {
  $("pair-button").addEventListener("click", pair);
  $("code").addEventListener("input", () => { $("pair-error").hidden = true; });
  $("refresh").addEventListener("click", loadStatus);
  $("unpair").addEventListener("click", unpair);
  loadStatus();
});
