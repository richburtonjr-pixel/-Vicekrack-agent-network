/*
 * Step 54: Finished videos (Auto Studio). Pick a story's settings, approve once, and watch the video get made:
 * script -> voices -> timing -> scene clips -> music -> assembly -> verification. Finished videos play here,
 * download as one MP4, and stay after a restart. Reads use GET; the only actions are prepare (free),
 * approve, stop and retry, each a click, through the Studio's CSRF-protected API.
 */
(function () {
  "use strict";
  var V = { session: null, options: null, videos: [], open: null, maker: null, busy: false, error: null,
            message: null, timer: null, visible: false };
  var STEP_TEXT = { script: "Script", voices: "Voices", timing: "Timing", scenes: "Scene clips", music: "Music",
                    assembly: "Assembly", verify: "Check the file" };
  var STATUS_TEXT = { prepared: "Waiting for your approval", approved: "Starting", scripting: "Writing the script",
                      voicing: "Recording the voices", planning: "Timing the scenes", generating: "Making the scene clips",
                      assembling: "Putting it together", verifying: "Checking the finished file",
                      completed: "Ready to watch", failed: "Needs attention", stopped: "Stopped" };
  var ACTIVE = ["approved", "scripting", "voicing", "planning", "generating", "assembling", "verifying"];

  function $(id) { return document.getElementById(id); }
  function h(tag, attrs, parent, text) {
    var node = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) { node.setAttribute(k, String(attrs[k])); } }); }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function usd(v) { return v === null || v === undefined ? "-" : "$" + Number(v).toFixed(2); }
  function secs(v) { if (!v && v !== 0) { return "-"; } v = Math.round(v); return Math.floor(v / 60) + ":" + ("0" + (v % 60)).slice(-2); }
  function when(stamp) {
    if (!stamp) { return ""; }
    var d = new Date(stamp);
    return isNaN(d.getTime()) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function requestId() {
    var bytes = new Uint8Array(12);
    window.crypto.getRandomValues(bytes);
    return "req-" + Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }
  function fail(response) {
    return response.json().catch(function () { return {}; }).then(function (doc) {
      var e = (doc && doc.error) || {}, error = new Error(e.message || "The request failed.");
      error.code = e.code || "http_" + response.status;
      throw error;
    });
  }
  function get(url) {
    return window.fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : fail(r); });
  }
  function act(path, body) {
    if (V.busy || !V.session || !V.session.actions_enabled) { return Promise.reject(new Error("busy")); }
    V.busy = true; V.error = null; render();
    return window.fetch("/api/studio/" + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-VK-Studio-CSRF": V.session.csrf },
      body: JSON.stringify(Object.assign({ request_id: requestId() }, body))
    }).then(function (r) { return r.ok ? r.json() : fail(r); }).then(function (doc) {
      V.busy = false; return doc;
    }, function (error) {
      V.busy = false; V.error = { code: error.code, message: error.message }; render(); throw error;
    });
  }

  /* ------------------------------------------------------------------ loading */
  function boot() {
    if (V.session) { return Promise.resolve(); }
    return get("/api/studio/session").then(function (s) {
      V.session = s;
      if (!s.actions_enabled) { return null; }
      return get("/api/studio/auto/options").then(function (o) { V.options = o; });
    });
  }
  function refresh() {
    if (!V.session || !V.session.actions_enabled) { render(); return Promise.resolve(); }
    var jobs = [get("/api/studio/auto/videos").then(function (d) { V.videos = d.videos; })];
    if (V.open) { jobs.push(get("/api/studio/auto/video?id=" + encodeURIComponent(V.open.production_id)).then(function (d) { V.open = d; })); }
    return Promise.all(jobs).catch(function (e) { V.error = { code: e.code, message: e.message }; }).then(render);
  }
  function schedule() {
    window.clearTimeout(V.timer);
    if (!V.visible) { return; }
    var busy = V.videos.some(function (v) { return ACTIVE.indexOf(v.status) >= 0; }) ||
               (V.open && ACTIVE.indexOf(V.open.status) >= 0);
    V.timer = window.setTimeout(function () { refresh().then(schedule); }, busy ? 4000 : 20000);
  }
  function show() {
    V.visible = true;
    boot().then(refresh).then(schedule).catch(function (e) { V.error = { code: e.code, message: e.message }; render(); });
  }

  /* ------------------------------------------------------------------ the maker (one approval) */
  function openMaker(job, card) {
    V.maker = { job_id: job.job_id, story: card.index, headline: card.headline, label: card.label, length: 30,
                type: "news", audio: "narrator", music: true, prepared: null };
    V.open = null;
    var nav = document.querySelector('.view-btn[data-view="videos"]');
    if (nav) { nav.click(); } else { show(); }
    render();
  }

  function renderMaker(body) {
    var m = V.maker, o = V.options;
    var box = h("section", { class: "studio-step auto-maker", "aria-label": "Make a video" }, body);
    h("h3", {}, box, "Make a video");
    h("p", { class: "auto-story" }, box, m.headline);
    h("p", { class: "small" }, box, "Story label: " + m.label + ". The video keeps this label on screen.");
    function group(title, name, values, current, onPick) {
      var fs = h("fieldset", { class: "auto-group" }, box);
      h("legend", {}, fs, title);
      Object.keys(values).forEach(function (key) {
        var id = "auto-" + name + "-" + key;
        var row = h("label", { class: "auto-choice" + (String(current) === String(key) ? " on" : ""), for: id }, fs);
        var input = h("input", { type: "radio", name: "auto-" + name, id: id, value: key }, row);
        input.checked = String(current) === String(key);
        input.disabled = !!m.prepared;
        input.addEventListener("change", function () { onPick(key); render(); });
        h("span", {}, row, values[key]);
      });
    }
    var lengths = {};
    o.lengths.forEach(function (n) { lengths[n] = n < 60 ? n + " sec" : (n === 60 ? "60 sec" : (n / 60) + " min"); });
    group("Finished video length", "length", lengths, m.length, function (v) { m.length = Number(v); });
    group("Production type", "type", o.types, m.type, function (v) { m.type = v; });
    group("Audio style", "audio", o.audio, m.audio, function (v) { m.audio = v; });
    var musicRow = h("label", { class: "auto-choice" + (m.music ? " on" : ""), for: "auto-music" }, box);
    var music = h("input", { type: "checkbox", id: "auto-music" }, musicRow);
    music.checked = m.music; music.disabled = !!m.prepared;
    music.addEventListener("change", function () { m.music = music.checked; render(); });
    h("span", {}, musicRow, "Include background music (original, made by ViceKrack)");
    var est = o.estimates[String(m.length)];
    h("p", { class: "auto-summary" }, box, m.length + "-second " + o.types[m.type].toLowerCase() + " with " +
      o.audio[m.audio].toLowerCase() + (m.music ? ", music" : "") + " and captions. About " + est.scenes +
      " scenes, about " + usd(est.expected_usd) + " (" + o.model.replace("grok-imagine-video-", "Grok video ") + ", " +
      o.resolution + ").");
    if (!m.prepared) {
      var go = h("button", { type: "button", class: "studio-btn go" }, box, "Get the exact estimate (free)");
      go.disabled = V.busy;
      go.addEventListener("click", function () {
        act("auto/prepare", { job_id: m.job_id, story: m.story, length: m.length, type: m.type, audio: m.audio, music: m.music })
          .then(function (doc) {
            if (doc.status !== "prepared") { V.maker = null; V.open = doc; return refresh(); }
            m.prepared = doc; render(); return null;
          }).catch(function () { return null; });
      });
      h("button", { type: "button", class: "studio-btn" }, box, "Cancel").addEventListener("click", function () {
        V.maker = null; render();
      });
      return;
    }
    var p = m.prepared;
    var paid = h("div", { class: "paid-box", role: "group", "aria-label": "Approve this video" }, box);
    h("div", { class: "paid-title" }, paid, "One approval for the whole video");
    h("p", {}, paid, "Estimated cost " + usd(p.estimate.expected_usd) + ". It will never spend more than " +
      usd(p.estimate.cap_usd) + " (room for automatic retries of failed scenes).");
    h("p", { class: "small" }, paid, "After you approve, ViceKrack writes the script with Grok, records the voices, makes " +
      p.estimate.scenes + "-ish scene clips with xAI, composes the music, edits everything into one MP4 and checks it. " +
      "You can stop it at any time; nothing is published.");
    var row = h("div", { class: "studio-row" }, paid);
    var check = h("input", { type: "checkbox", id: "auto-approve" }, row);
    h("label", { for: "auto-approve" }, row, "I approve this video and up to " + usd(p.estimate.cap_usd) + " of xAI requests");
    var send = h("button", { type: "button", class: "studio-btn paid" }, paid, "Approve and make the video");
    send.disabled = true;
    check.addEventListener("change", function () { send.disabled = V.busy || !check.checked; });
    send.addEventListener("click", function () {
      if (!check.checked) { return; }
      act("auto/approve", { production_id: p.production_id, consent: p.consent_phrase }).then(function (doc) {
        V.maker = null; V.open = doc; return refresh().then(schedule);
      }).catch(function () { return null; });
    });
    h("button", { type: "button", class: "studio-btn" }, box, "Change settings").addEventListener("click", function () {
      m.prepared = null; render();
    });
  }

  /* ------------------------------------------------------------------ one video */
  function fileUrl(id, name, download) {
    return "/api/studio/auto/file?id=" + encodeURIComponent(id) + "&name=" + name + (download ? "&download=1" : "");
  }

  function renderVideo(body) {
    var v = V.open;
    var box = h("section", { class: "studio-step auto-detail", "aria-label": "Video" }, body);
    var top = h("div", { class: "studio-row" }, box);
    h("button", { type: "button", class: "studio-btn" }, top, "All videos").addEventListener("click", function () {
      V.open = null; render();
    });
    h("span", { class: "auto-status " + v.status }, top, STATUS_TEXT[v.status] || v.status);
    if (v.demo) { h("span", { class: "auto-status demo" }, top, "PRACTICE (demo)"); }
    h("h3", {}, box, v.title);
    var grid = h("div", { class: "auto-detail-grid" }, box);
    var left = h("div", {}, grid), right = h("div", {}, grid);
    if (v.has_video) {
      h("video", { class: "auto-player", controls: "", preload: "metadata", src: fileUrl(v.production_id, "final"),
                   poster: fileUrl(v.production_id, "thumb"), "aria-label": "Finished video" }, left);
      var actions = h("div", { class: "studio-row" }, left);
      h("a", { class: "studio-btn approve", href: fileUrl(v.production_id, "final", true),
               download: "vicekrack-" + v.production_id + ".mp4" }, actions, "Download MP4");
    } else {
      h("div", { class: "auto-placeholder" }, left, ACTIVE.indexOf(v.status) >= 0 ? "Making your video…" : "No video file yet.");
    }
    var facts = h("dl", { class: "auto-facts" }, right);
    function fact(k, val) { h("dt", {}, facts, k); h("dd", {}, facts, val); }
    fact("Length", v.final && v.final.duration ? secs(v.final.duration) : v.settings.length + " s planned");
    fact("Type", V.options ? V.options.types[v.settings.type] : v.settings.type);
    fact("Audio", (V.options ? V.options.audio[v.settings.audio] : v.settings.audio) + (v.settings.music ? " + music" : ""));
    fact("Spent", usd(v.cost.spent_usd) + " of " + usd(v.estimate.cap_usd) + " approved");
    fact("Made", when(v.created_at));
    var steps = h("ol", { class: "stage-list" }, right);
    v.steps.forEach(function (s) {
      var li = h("li", { class: s.status }, steps, STEP_TEXT[s.name] || s.name);
      h("span", { class: "st" }, li, s.status + (s.note ? " · " + s.note : ""));
    });
    if (ACTIVE.indexOf(v.status) >= 0) {
      h("button", { type: "button", class: "studio-btn" }, right, "Stop (no new paid requests)").addEventListener("click", function () {
        act("auto/stop", { production_id: v.production_id }).then(function (doc) { V.open = doc; render(); }).catch(function () { return null; });
      });
    }
    if (v.retry_consent) {
      var paid = h("div", { class: "paid-box risk", role: "group", "aria-label": "Retry" }, right);
      h("div", { class: "paid-title" }, paid, "Try again from where it stopped");
      h("p", { class: "small" }, paid, "Finished parts are kept. This allows up to " + usd(v.estimate.expected_usd) + " more.");
      var row = h("div", { class: "studio-row" }, paid);
      var check = h("input", { type: "checkbox", id: "auto-retry" }, row);
      h("label", { for: "auto-retry" }, row, "I approve the retry and up to " + usd(v.estimate.expected_usd) + " more");
      var send = h("button", { type: "button", class: "studio-btn paid" }, paid, "Retry");
      send.disabled = true;
      check.addEventListener("change", function () { send.disabled = !check.checked; });
      send.addEventListener("click", function () {
        act("auto/retry", { production_id: v.production_id, consent: v.retry_consent })
          .then(function (doc) { V.open = doc; return refresh().then(schedule); }).catch(function () { return null; });
      });
    }
    if (v.final && v.final.checks && v.final.checks.length) {
      h("h4", {}, box, v.final.verified ? "File checks: all passed" : "File checks");
      var ul = h("ul", { class: "auto-checks" }, box);
      v.final.checks.forEach(function (c) { h("li", { class: c.passed ? "pass" : "fail" }, ul, (c.passed ? "✓ " : "✗ ") + c.detail); });
    }
    if (v.scenes.length) {
      h("h4", {}, box, "Scenes");
      var strip = h("div", { class: "auto-scenes" }, box);
      v.scenes.forEach(function (s) {
        var cell = h("figure", { class: "auto-scene " + s.status }, strip);
        if (s.has_still) { h("img", { src: fileUrl(v.production_id, "scene-" + ("00" + s.index).slice(-3)), alt: "Scene " + (s.index + 1) }, cell); }
        else { h("div", { class: "auto-scene-empty" }, cell, s.status); }
        h("figcaption", {}, cell, (s.index + 1) + " · " + s.duration + " s" + (s.attempts > 1 ? " · tries " + s.attempts : ""));
      });
    }
    if (v.errors.length) {
      h("h4", {}, box, "Problems");
      var errs = h("ul", { class: "auto-checks" }, box);
      v.errors.slice(-5).forEach(function (e) { h("li", { class: "fail" }, errs, when(e.at) + " · " + e.message); });
    }
    h("h4", {}, box, "What happened");
    var log = h("ol", { class: "auto-log" }, box);
    v.log.slice().reverse().forEach(function (l) { h("li", {}, log, when(l.at) + " · " + l.text); });
  }

  /* ------------------------------------------------------------------ the library */
  function renderLibrary(body) {
    var box = h("section", { class: "studio-step", "aria-label": "Finished videos" }, body);
    if (!V.videos.length) {
      h("p", {}, box, "No videos yet. Open Research, pick a story card and press Make video.");
      return;
    }
    var grid = h("div", { class: "auto-grid" }, box);
    V.videos.forEach(function (v) {
      var card = h("button", { type: "button", class: "auto-card " + v.status, "data-id": v.production_id }, grid);
      if (v.final) { h("img", { src: fileUrl(v.production_id, "thumb"), alt: "" }, card); }
      else { h("div", { class: "auto-thumb-empty" }, card, STATUS_TEXT[v.status] || v.status); }
      h("strong", {}, card, v.title);
      h("span", { class: "small" }, card, (v.final && v.final.duration ? secs(v.final.duration) : v.settings.length + " s") +
        " · " + (STATUS_TEXT[v.status] || v.status) + (v.demo ? " · demo" : "") + " · " + when(v.created_at));
      card.addEventListener("click", function () {
        get("/api/studio/auto/video?id=" + encodeURIComponent(v.production_id)).then(function (d) { V.open = d; render(); schedule(); });
      });
    });
  }

  function render() {
    var body = $("videos-body");
    if (!body) { return; }
    clear(body);
    var badge = $("videos-badge");
    if (badge) {
      badge.textContent = !V.session ? "LOADING" : !V.session.actions_enabled ? "OFF" : V.session.demo ? "PRACTICE (demo)" : "REAL";
    }
    if (V.error) { h("p", { class: "notice warn", role: "alert" }, body, V.error.message); }
    if (V.busy) { h("p", { class: "notice", role: "status" }, body, "Working…"); }
    if (!V.session) { h("p", {}, body, "Loading…"); return; }
    if (!V.session.actions_enabled) {
      h("p", {}, body, "Finished videos need the Studio. Start ViceKrack with the ViceKrack button on your Desktop.");
      return;
    }
    if (V.maker && V.options) { renderMaker(body); return; }
    if (V.open) { renderVideo(body); return; }
    renderLibrary(body);
  }

  document.addEventListener("hq:view", function (e) {
    if (e.detail === "videos") { show(); } else { V.visible = false; window.clearTimeout(V.timer); }
  });
  window.HQVideos = { state: V, openMaker: openMaker, show: show };
}());
