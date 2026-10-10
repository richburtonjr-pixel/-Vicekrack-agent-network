/* ViceKrack Living HQ, Step 48: Video Studio.
 *
 * The only screen in the HQ that can act, kept apart from the read-only desks (app.js).
 * Safety rules for this file:
 * - It talks only to /api/studio/... (GET to read, POST to act) and plays videos through the existing
 *   read-only /api/content/media route. Nothing runs until the Studio view is opened.
 * - Every action is a click by a person. There is no timer, polling or automatic retry. While one action
 *   runs, every action button is disabled (duplicate clicks do nothing), and each click carries its own
 *   request ID so the server never performs the same click twice.
 * - A paid request needs a ticked approval box for that exact request; the job-specific consent phrase
 *   shown on screen is what is sent, and the server checks it again.
 * - All text from the server is inserted with textContent / setAttribute, never as HTML. Nothing is
 *   stored in browser storage and no cookie is set by this file.
 */
(function () {
  "use strict";
  var ST = {
    loaded: false, session: null, productions: null, productionId: null, production: null, workflowId: null,
    workflow: null, busy: false, message: null, error: null,
    form: { narration: "silent", speechJob: null, captions: "none", model: null, resolution: "720p", voice: null,
            timestamps: true }
  };

  function $(id) { return document.getElementById(id); }
  function h(tag, attrs, parent, text) {
    var node = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) { node.setAttribute(k, String(attrs[k])); } }); }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    if (parent) { parent.appendChild(node); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function words(code) { return String(code || "").replace(/_/g, " "); }
  function requestId() {
    var bytes = new Uint8Array(12);
    window.crypto.getRandomValues(bytes);
    return "req-" + Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
  }

  /* ------------------------------------------------------------------ server */
  function readError(response) {
    return response.json().catch(function () { return {}; }).then(function (doc) {
      var e = (doc && doc.error) || {};
      var error = new Error(e.message || "The request failed.");
      error.code = e.code || ("http_" + response.status);
      throw error;
    });
  }
  function get(url) {
    return window.fetch(url, { credentials: "same-origin", cache: "no-store" }).then(function (response) {
      return response.ok ? response.json() : readError(response);
    });
  }
  function act(path, body, after) {
    if (ST.busy || !ST.session || !ST.session.actions_enabled) { return Promise.resolve(); }
    ST.busy = true; ST.error = null; ST.message = "Working… (this can take up to a minute while a video renders)";
    render();
    var payload = Object.assign({ request_id: requestId() }, body);
    return window.fetch("/api/studio/" + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-VK-Studio-CSRF": ST.session.csrf },
      body: JSON.stringify(payload)
    }).then(function (response) {
      return response.ok ? response.json() : readError(response);
    }).then(function (doc) {
      ST.busy = false; ST.message = "Done."; ST.error = null;
      return Promise.resolve(after(doc)).then(function () {          // keep the production's lists current
        return ST.productionId ? loadProduction(ST.productionId, true) : null;
      });
    }).catch(function (error) {
      ST.busy = false; ST.message = null;
      ST.error = { code: error.code || "network_error", message: error.message };
      return refresh();
    }).then(render);
  }
  function refresh() {
    var jobs = [];
    if (ST.productionId) { jobs.push(loadProduction(ST.productionId, true)); }
    if (ST.workflowId) { jobs.push(loadWorkflow(ST.workflowId, true)); }
    return Promise.all(jobs).catch(function () { return null; });
  }

  /* ------------------------------------------------------------------ loading */
  function open() {
    if (ST.loaded) { render(); return; }
    ST.loaded = true;
    get("/api/studio/session").then(function (doc) {
      ST.session = doc;
      if (!doc.actions_enabled) { render(); return null; }
      ST.form.model = doc.options.models[0];
      ST.form.voice = doc.options.default_voice;
      return loadProductions();
    }).catch(function (error) {
      ST.error = { code: error.code, message: error.message }; render();
    });
  }
  function loadProductions() {
    return get("/api/studio/productions").then(function (doc) {
      ST.productions = doc.productions;
      if (!ST.productionId) {
        var first = doc.productions.filter(function (p) { return p.eligible; })[0];
        if (first) { return loadProduction(first.production_id); }
      }
      render();
      return null;
    });
  }
  function loadProduction(id, quiet) {
    ST.productionId = id;
    return get("/api/studio/production?id=" + encodeURIComponent(id)).then(function (doc) {
      ST.production = doc;
      var jobs = doc.speech_jobs.filter(function (j) { return j.status === "completed"; });
      if (ST.form.speechJob && !doc.speech_jobs.some(function (j) { return j.job_id === ST.form.speechJob; })) { ST.form.speechJob = null; }
      if (!ST.form.speechJob && jobs.length) { ST.form.speechJob = jobs[0].job_id; }
      if (!quiet) { render(); }
    });
  }
  function loadWorkflow(id, quiet) {
    ST.workflowId = id;
    return get("/api/studio/workflow?id=" + encodeURIComponent(id)).then(function (doc) {
      ST.workflow = doc;
      if (!quiet) { render(); }
    });
  }

  /* ------------------------------------------------------------------ pieces */
  function step(parent, number, title) {
    var box = h("section", { class: "studio-step", "aria-label": title }, parent);
    var head = h("h3", {}, box);
    h("span", { class: "num", "aria-hidden": "true" }, head, String(number));
    head.appendChild(document.createTextNode(title));
    return box;
  }
  function button(parent, label, onClick, cls, attrs) {
    var b = h("button", Object.assign({ type: "button", class: "studio-btn " + (cls || "") }, attrs || {}), parent, label);
    b.disabled = ST.busy || !ST.session || !ST.session.actions_enabled;
    b.addEventListener("click", function () { if (!b.disabled) { onClick(); } });
    return b;
  }
  function table(parent, caption, heads, rows) {
    var wrap = h("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-label": caption }, parent);
    var t = h("table", { class: "table" }, wrap);
    h("caption", { class: "sr" }, t, caption);
    var tr = h("tr", {}, h("thead", {}, t));
    heads.forEach(function (x) { h("th", { scope: "col" }, tr, x); });
    var body = h("tbody", {}, t);
    rows.forEach(function (row) {
      var r = h("tr", {}, body);
      row.forEach(function (cell) {
        var td = h("td", {}, r);
        if (cell && cell.nodeType) { td.appendChild(cell); } else { td.textContent = cell === null || cell === undefined ? "-" : String(cell); }
      });
    });
    return t;
  }
  function chip(text, tone) { return h("span", { class: "state-chip " + (tone || "") }, null, words(text)); }
  function tone(status) {
    if (/completed|exported|pass|ready_to_export|downloaded|done/.test(status)) { return "pos"; }
    if (/fail|blocked|rejected|invalid|uncertain|too_long/.test(status)) { return "warn"; }
    return "";
  }
  /* A paid request: the exact request is shown above; nothing is sent until the box is ticked. */
  function paidBox(parent, title, consent, risk, onSend) {
    var box = h("div", { class: "paid-box" + (risk ? " risk" : ""), role: "group", "aria-label": title }, parent);
    h("div", { class: "paid-title" }, box, risk ? "Paid request · may bill twice" : "Paid request");
    h("p", {}, box, title);
    if (risk) {
      h("p", { class: "small" }, box, "An earlier attempt had an unclear outcome and may already have been billed. It was not retried. " +
        "Check your xAI usage before sending again.");
    }
    var id = "c-" + consent.replace(/[^a-z0-9]/gi, "-");
    var row = h("div", { class: "studio-row" }, box);
    var check = h("input", { type: "checkbox", id: id }, row);
    h("label", { for: id }, row, "I approve this one paid xAI request (" + consent + ")");
    var check2 = null;
    if (risk) {
      var row2 = h("div", { class: "studio-row" }, box);
      check2 = h("input", { type: "checkbox", id: id + "-dup" }, row2);
      h("label", { for: id + "-dup" }, row2, "I checked my xAI usage and accept that this may bill twice");
    }
    var send = button(box, "Approve and send (paid)", function () {
      if (check.checked && (!check2 || check2.checked)) { onSend(consent, !!check2); }
    }, "paid");
    function sync() { send.disabled = ST.busy || !check.checked || (check2 && !check2.checked); }
    check.addEventListener("change", sync);
    if (check2) { check2.addEventListener("change", sync); }
    sync();
    return box;
  }

  /* ------------------------------------------------------------------ render */
  function render() {
    var body = $("studio-body");
    if (!body) { return; }
    clear(body);
    var badge = $("studio-badge"), notice = $("studio-notice"), msg = $("studio-message");
    $("studio-view").setAttribute("data-busy", ST.busy ? "true" : "false");   // lets people and tests see work in progress
    var session = ST.session;
    badge.className = "sim-badge " + (!session ? "" : !session.actions_enabled ? "studio-off" : session.demo ? "studio-demo" : "studio-real");
    badge.textContent = !session ? "LOADING" : !session.actions_enabled ? "OFF · READ-ONLY HQ" :
      session.demo ? "DEMO · MOCK PROVIDERS" : "REAL · PAID ON APPROVAL";
    notice.className = "studio-notice" + (session && session.actions_enabled && !session.demo ? " real" : "");
    notice.textContent = session ? session.notice : "Loading the Studio…";
    msg.className = "studio-message" + (ST.error ? " error" : ST.message ? " ok" : "") + (ST.busy ? " studio-busy" : "");
    msg.textContent = ST.error ? "Not done: " + ST.error.message + " (" + words(ST.error.code) + "). Nothing was retried automatically." :
      (ST.message || "");
    if (!session || !session.actions_enabled) { return; }
    if (!session.credentials.xai_configured) {
      h("p", { class: "notice warn" }, body, "XAI_API_KEY is not set on the computer running the HQ, so paid requests will be " +
        "refused. Set it in that terminal's environment (never in this page) and restart the HQ. Free steps still work.");
    }
    renderProductions(step(body, 1, "Choose a production"));
    if (ST.production) {
      renderProduction(step(body, 2, "Script and scene plan"));
      if (ST.production.eligible) { renderSetup(step(body, 3, "Narration, captions and video settings")); }
    }
    if (ST.workflow) {
      renderWorkflow(step(body, 4, "Generation progress"));
      renderReview(step(body, 5, "Watch, check and review"));
      renderExport(step(body, 6, "Export"));
    }
  }

  function renderProductions(box) {
    if (!ST.productions) { h("p", { class: "muted" }, box, "Loading productions…"); return; }
    if (!ST.productions.length) {
      h("p", { class: "notice" }, box, "No productions are saved here yet. Produce one first (python -m vicekrack produce …).");
      return;
    }
    var grid = h("div", { class: "studio-grid" }, box);
    ST.productions.forEach(function (p) {
      var card = h("button", { type: "button", class: "studio-card" + (p.production_id === ST.productionId ? " selected" : "") +
        (p.eligible ? "" : " ineligible"), "data-production": p.production_id }, grid);
      h("strong", {}, card, p.title || "(no readable script)");
      h("code", {}, card, p.production_id);
      card.appendChild(chip(p.eligible ? "eligible" : "not eligible", p.eligible ? "pos" : "warn"));
      p.reasons.forEach(function (r) { h("span", { class: "why" }, card, r.text); });
      if (p.workflows) { h("span", { class: "small" }, card, p.workflows + " video workflow(s)"); }
      card.addEventListener("click", function () {
        ST.workflowId = null; ST.workflow = null; ST.error = null; ST.message = null;
        loadProduction(p.production_id).catch(function (e) { ST.error = { code: e.code, message: e.message }; render(); });
      });
    });
  }

  function renderProduction(box) {
    var p = ST.production;
    if (!p.eligible) {
      h("p", { class: "notice warn" }, box, "This production is not eligible: " + p.reasons.map(function (r) { return r.text; }).join(" "));
    } else if (p.reasons.length) {
      h("p", { class: "notice" }, box, p.reasons.map(function (r) { return r.text; }).join(" "));
    }
    if (!p.script) { return; }
    var s = p.script;
    h("p", {}, box, s.title + " · " + s.language + (s.unverified_claims ? " · DRAFT: " + s.unverified_claims + " unverified claim(s)" : ""));
    table(box, "Script", ["Beat", "Time", "Narration (spoken)", "On screen", "Visual"], s.beats.map(function (b) {
      return [b.beat, b.start + "–" + b.end + " s", b.narration, b.on_screen_text || "-", b.visual];
    }));
    table(box, "Scene plan", ["Scene", "Beat", "Time", "Method"], p.scene_plan.scenes.map(function (sc) {
      return [String(sc.index), sc.beat, sc.start + "–" + sc.end + " s", words(sc.method)];
    }));
    if (s.disclosures.length) { h("p", { class: "small" }, box, "Disclosures: " + s.disclosures.join(" | ")); }
  }

  function radio(parent, name, value, checked, label, limits, disabled, onChange) {
    var id = name + "-" + value;
    var row = h("div", { class: "studio-choice" + (disabled ? " disabled" : "") }, parent);
    var input = h("input", { type: "radio", name: name, id: id, value: value }, row);
    input.checked = checked; input.disabled = disabled || ST.busy;
    var label_ = h("label", { for: id }, row, label);
    h("small", {}, label_, limits);
    input.addEventListener("change", function () { if (input.checked) { onChange(value); } });
  }

  function renderSetup(box) {
    var p = ST.production, o = ST.session.options, f = ST.form;
    h("h4", {}, box, "Narration");
    o.narration.forEach(function (n) {
      var disabled = !n.available || (n.id === "speech" && !p.features.speech);
      radio(box, "narration", n.id, f.narration === n.id, n.label, n.limits, disabled, function (v) { f.narration = v; render(); });
    });
    if (f.narration === "speech") { renderSpeech(box); }
    h("h4", {}, box, "Captions");
    if (f.narration !== "speech") {
      h("p", { class: "small" }, box, "Captions need Grok-generated narration: they repeat its exact words and are timed against its audio.");
    } else {
      o.captions.forEach(function (c) {
        radio(box, "captions", c.id, f.captions === c.id, c.label, c.limits, !p.features.captions && c.id !== "none",
          function (v) { f.captions = v; render(); });
      });
      if (f.captions !== "none") { renderCaptions(box); }
    }
    h("h4", {}, box, "Video clips");
    var row = h("div", { class: "studio-row" }, box);
    h("label", { for: "studio-model" }, row, "Model");
    var model = h("select", { id: "studio-model" }, row);
    o.models.forEach(function (m) { var opt = h("option", { value: m }, model, m); opt.selected = m === f.model; });
    model.addEventListener("change", function () { f.model = model.value; });
    h("label", { for: "studio-resolution" }, row, "Resolution");
    var res = h("select", { id: "studio-resolution" }, row);
    o.resolutions.forEach(function (r) { var opt = h("option", { value: r }, res, r); opt.selected = r === f.resolution; });
    res.addEventListener("change", function () { f.resolution = res.value; });
    h("p", { class: "small" }, box, "Starting a workflow prepares the four clip requests (free, nothing is sent). You then see each exact " +
      "request and approve each paid one separately. Clips already paid for with the same settings are reused.");
    var ready = f.narration === "silent" || (f.speechJob && completedJob(f.speechJob) && (f.captions === "none" || captionFor()));
    button(box, "Start video workflow (free)", function () {
      act("workflow/start", { production_id: p.production_id, narration: f.narration,
        speech_job_id: f.narration === "speech" ? f.speechJob : null,
        caption_id: f.narration === "speech" && f.captions !== "none" ? captionFor().caption_id : null,
        model: f.model, resolution: f.resolution }, function (doc) {
          ST.workflow = doc; ST.workflowId = doc.workflow_id;
          return loadProduction(p.production_id, true);
        });
    }, "", { id: "studio-start" }).disabled = ST.busy || !ready;
    if (!ready) { h("p", { class: "small" }, box, "Finish the narration (and captions, if chosen) first."); }
    if (p.workflows.length) {
      h("h4", {}, box, "Existing video workflows for this production");
      var list = h("div", { class: "studio-grid" }, box);
      p.workflows.forEach(function (w) {
        var card = h("button", { type: "button", class: "studio-card" + (w.workflow_id === ST.workflowId ? " selected" : ""),
          "data-workflow": w.workflow_id }, list);
        h("code", {}, card, w.workflow_id);
        card.appendChild(chip(w.status || w.error, tone(w.status || "invalid")));
        card.addEventListener("click", function () {
          ST.error = null; ST.message = null;
          loadWorkflow(w.workflow_id).catch(function (e) { ST.error = { code: e.code, message: e.message }; render(); });
        });
      });
    }
  }

  function completedJob(id) {
    return ST.production.speech_jobs.filter(function (j) { return j.job_id === id && j.status === "completed"; })[0];
  }
  function captionFor() {
    var timing = ST.form.captions === "provider" ? "provider_character_timestamps" : "estimated_phrase";
    return ST.production.caption_tracks.filter(function (t) {
      return t.speech_job_id === ST.form.speechJob && t.timing === timing;
    })[0];
  }

  function renderSpeech(box) {
    var p = ST.production, f = ST.form, o = ST.session.options;
    var jobs = p.speech_jobs;
    if (jobs.length) {
      jobs.forEach(function (j) {
        var card = h("div", { class: "studio-card" + (j.job_id === f.speechJob ? " selected" : ""), "data-speech": j.job_id }, box);
        var head = h("div", { class: "studio-row" }, card);
        h("code", {}, head, j.job_id);
        head.appendChild(chip(j.status, tone(j.status)));
        h("span", { class: "small" }, card, "Voice " + j.settings.voice + " · " + j.settings.language + " · " + j.settings.codec +
          " " + j.settings.sample_rate + " Hz · " + (j.with_timestamps ? "with provider timestamps" : "no timestamps") +
          (j.audio ? " · " + j.audio.duration_seconds + " s" : ""));
        h("span", { class: "small" }, card, "Spoken text (exactly the script's narration):");
        h("blockquote", { class: "review-notes" }, card, j.text);
        if (j.status === "too_long") {
          h("p", { class: "why" }, card, "Narration too long for 15 seconds. It was kept, not cut or sped up. Shorten the script and make a new production.");
        }
        if (j.error_code) { h("span", { class: "why" }, card, "Last result: " + words(j.error_code)); }
        if (j.status === "completed") {
          var use = h("div", { class: "studio-row" }, card);
          var r = h("input", { type: "radio", name: "speech-job", id: "use-" + j.job_id }, use);
          r.checked = j.job_id === f.speechJob; r.disabled = ST.busy;
          h("label", { for: "use-" + j.job_id }, use, "Use this narration");
          r.addEventListener("change", function () { f.speechJob = j.job_id; render(); });
        }
        j.actions.forEach(function (a) {
          if (a.action === "speech_submit") {
            paidBox(card, "Send this narration request to xAI text to speech (one paid request).", a.consent, !!a.retry_uncertain,
              function (consent, risk) {
                act("speech/submit", { job_id: j.job_id, consent: consent, retry_uncertain: risk || null,
                  acknowledge_duplicate_billing: risk || null }, function () { return loadProduction(p.production_id, true); });
              });
          } else if (a.action === "speech_recover") {
            button(card, "Recover (offline, sends nothing)", function () {
              act("speech/recover", { job_id: j.job_id }, function () { return loadProduction(p.production_id, true); });
            });
          }
        });
      });
    } else {
      h("p", { class: "small" }, box, "No narration request yet for this production.");
    }
    var row = h("div", { class: "studio-row" }, box);
    h("label", { for: "studio-voice" }, row, "Voice");
    var voice = h("select", { id: "studio-voice" }, row);
    o.voices.forEach(function (v) { var opt = h("option", { value: v }, voice, v); opt.selected = v === f.voice; });
    voice.addEventListener("change", function () { f.voice = voice.value; });
    var ts = h("input", { type: "checkbox", id: "studio-timestamps" }, row);
    ts.checked = f.timestamps;
    ts.addEventListener("change", function () { f.timestamps = ts.checked; });
    h("label", { for: "studio-timestamps" }, row, "Ask for provider timestamps (needed for provider-timed captions)");
    button(box, "Prepare narration request (free, nothing sent)", function () {
      act("speech/prepare", { production_id: p.production_id, voice: f.voice, with_timestamps: f.timestamps }, function (doc) {
        f.speechJob = doc.job_id; return loadProduction(p.production_id, true);
      });
    }, "", { id: "studio-speech-prepare" });
  }

  function renderCaptions(box) {
    var f = ST.form;
    var job = f.speechJob && completedJob(f.speechJob);
    if (!job) { h("p", { class: "small" }, box, "Captions are prepared from a completed narration."); return; }
    if (f.captions === "provider" && !job.with_timestamps) {
      h("p", { class: "notice warn" }, box, "This narration was requested without provider timestamps, so provider-timed captions " +
        "are not possible. Choose estimated timing, or prepare a new narration with timestamps (a new paid request).");
      return;
    }
    var track = captionFor();
    if (track) {
      h("p", {}, box, "Caption track " + track.caption_id + " · " + track.cues + " captions · " + words(track.timing) +
        (track.requires_manual_timing_review ? " · ESTIMATED: check every caption before approving" : ""));
    } else {
      button(box, "Prepare captions (free, nothing sent)", function () {
        act("captions/prepare", { speech_job_id: job.job_id, timing: f.captions }, function () {
          return loadProduction(ST.productionId, true);
        });
      }, "", { id: "studio-captions-prepare" });
    }
  }

  var STATUS_TEXT = {
    waiting_for_consent: "Clip requests are prepared. Read each one, then approve the paid ones you want to send.",
    uncertain_submission: "A clip request had an unclear outcome and may have been billed. It was not retried.",
    waiting_for_provider: "Clips are being generated or not downloaded yet. Check again when you like (no new generation).",
    waiting_for_review: "The video is rendered and checked. Watch it and record your decision.",
    review_rejected: "The latest decision did not approve this video. Record a new decision if you change your mind.",
    ready_to_export: "Approved. You can export the approved preview.",
    exported: "Exported. Download the verified package below.",
    failed: "A stage failed. See the error and the allowed recovery below.",
    quality_failed: "The quality check failed, so this video cannot be approved or exported.",
    blocked: "Blocked: limits were reached or saved inputs changed. Start a new workflow.",
    running: "Working."
  };

  function renderWorkflow(box) {
    var w = ST.workflow;
    var head = h("div", { class: "studio-row" }, box);
    h("code", {}, head, w.workflow_id);
    head.appendChild(chip(w.status, tone(w.status)));
    h("p", {}, box, STATUS_TEXT[w.status] || words(w.status));
    if (w.integrity_problems.length) {
      h("p", { class: "notice warn" }, box, "Integrity problem: " + w.integrity_problems.map(words).join(", ") +
        ". Nothing is repaired automatically; start a new workflow.");
    }
    var stages = h("ol", { class: "stage-list" }, box);
    w.stages.forEach(function (s) {
      var li = h("li", { class: s.status }, stages, words(s.name));
      h("span", { class: "st" }, li, words(s.status) + (s.error_code ? " · " + words(s.error_code) : ""));
    });
    var n = w.narration, c = w.captions;
    h("p", { class: "small" }, box, "Narration: " + (n.present ? (n.speech ? "Grok voice " + n.speech.voice + " (" + n.speech.job_id + ")" :
      "local recording") + ", " + n.source_duration_seconds + " s padded to 15 s" : "none (silent)") + " · Captions: " +
      (c.present ? c.cues + " burned in, " + words(c.timing_method) + (c.requires_manual_timing_review ? " (ESTIMATED: check before approving)" : "") : "none"));
    table(box, "Prepared clip requests (what would be sent to xAI)", ["Scene", "Request", "Status"], w.scenes.map(function (s) {
      var r = s.request;
      return [String(s.scene_index), r ? r.model + " · " + r.duration + " s · " + r.resolution + " · " + r.aspect_ratio +
        " · sound " + (r.generate_audio ? "generated (muted in the preview)" : "none") + " · prompt: " + r.prompt : "not prepared",
        words(s.job_status) + (s.replaced_job_ids.length ? " · replaced " + s.replaced_job_ids.length : "")];
    }));
    w.actions.forEach(function (a) {
      if (a.action === "workflow_submit") {
        var scene = w.scenes[a.scene - 1];
        paidBox(box, "Generate scene " + a.scene + " clip (" + scene.request.duration + " s, " + scene.request.model + ", " +
          scene.request.resolution + "). One paid request.", a.consent, !!a.retry_uncertain, function (consent, risk) {
            act("workflow/submit", { workflow_id: w.workflow_id, scene: a.scene, consent: consent, retry_uncertain: risk || null,
              acknowledge_duplicate_billing: risk || null }, function (doc) { ST.workflow = doc; });
          });
      } else if (a.action === "workflow_resume") {
        button(box, a.label, function () {
          act("workflow/resume", { workflow_id: w.workflow_id, allow_network: a.allow_network }, function (doc) { ST.workflow = doc; });
        }, "", { "data-action": "resume" });
      } else if (a.action === "workflow_retry_scene") {
        h("p", { class: "small" }, box, "Scene " + a.scene + "'s clip failed at the provider. Replacing it prepares a NEW request " +
          "with the lite model (free); you approve that paid request separately.");
        button(box, "Replace scene " + a.scene + " request (free)", function () {
          act("workflow/retry-scene", { workflow_id: w.workflow_id, scene: a.scene, model: "grok-imagine-video-1.5-lite" },
            function (doc) { ST.workflow = doc; });
        });
      }
    });
  }

  function renderReview(box) {
    var w = ST.workflow;
    var grid = h("div", { class: "studio-review" }, box);
    var left = h("div", {}, grid);
    if (w.video && w.video.media_id) {
      h("video", { class: "studio-video", controls: "", preload: "metadata", id: "studio-video",
        src: "/api/content/media?production=" + encodeURIComponent(w.production_id) + "&id=" + encodeURIComponent(w.video.media_id),
        poster: w.video.poster_media_id ? "/api/content/media?production=" + encodeURIComponent(w.production_id) + "&id=" +
          encodeURIComponent(w.video.poster_media_id) : null,
        "aria-label": "Rendered preview video" }, left);
      h("p", { class: "small" }, left, "Exactly the video the quality check and your review apply to." +
        (w.video.captions ? " Captions are burned in." : ""));
    } else if (w.video && w.video.unverified) {
      h("p", { class: "notice warn" }, left, "The rendered video failed its integrity check (" + words(w.video.unverified) +
        "), so it is not played here.");
    } else if (w.video && w.video.superseded) {
      h("p", { class: "notice warn" }, left, "This production's current preview is a different video (another workflow rendered " +
        "after this one). Approvals of this workflow's video no longer apply.");
    } else {
      h("p", { class: "small" }, left, "No video yet.");
    }
    var right = h("div", {}, grid);
    if (!w.quality) { h("p", { class: "small" }, right, "The quality check runs after rendering."); return; }
    var q = w.quality;
    var head = h("div", { class: "studio-row" }, right);
    h("strong", {}, head, "Quality check");
    head.appendChild(chip(q.result, q.result === "pass" ? "pos" : "warn"));
    table(right, "Quality findings", ["Check", "Status", "Reasons"], q.checks.map(function (c) {
      return [words(c.check_id), chip(c.status, c.status === "pass" ? "pos" : c.status === "fail" ? "warn" : ""),
        c.reasons.map(words).join(", ") || "-"];
    }));
    var r = w.review;
    if (!r) { return; }
    if (r.decisions.length) {
      table(right, "Review decisions (newest first)", ["Decision", "By", "Recorded", "Applies to this video"], r.decisions.map(function (d) {
        return [words(d.decision), d.reviewer, d.recorded_at, d.applies_to_this_video ? words(d.applicability) +
          (d.current_preview_approval ? " · current approval" : "") : "no (another version)"];
      }));
    }
    if (r.binding !== "matching") {
      h("p", { class: "notice warn" }, right, "The quality report no longer matches the current files (" + words(r.binding) +
        "), so it cannot be approved.");
      return;
    }
    if (!r.decisions_allowed.length || w.status === "exported") { return; }
    var form = h("div", { class: "review-form", role: "group", "aria-label": "Your review decision" }, right);
    h("p", { class: "small" }, form, "Your decision applies only to this exact video and quality report (binding " +
      r.binding_digest.slice(0, 12) + "…). Approval accepts it as a preview only: not permission to publish.");
    var row = h("div", { class: "studio-row" }, form);
    h("label", { for: "studio-reviewer" }, row, "Your name");
    var name = h("input", { type: "text", id: "studio-reviewer", maxlength: "80", autocomplete: "off" }, row);
    var acks = r.applicable_acknowledgments.map(function (a) {
      var ar = h("div", { class: "studio-row" }, form);
      var cb = h("input", { type: "checkbox", id: "ack-" + a.id, "data-ack": a.id }, ar);
      h("label", { for: "ack-" + a.id }, ar, a.text);
      return cb;
    });
    var notes = h("textarea", { id: "studio-notes", maxlength: "2000", "aria-label": "Notes (optional)", placeholder: "Notes (optional)" }, form);
    var buttons = h("div", { class: "studio-row" }, form);
    function decide(decision) {
      if (!name.value.trim()) { ST.error = { code: "reviewer_required", message: "Enter your name to record a decision." }; render(); return; }
      act("review/record", { workflow_id: w.workflow_id, decision: decision, reviewer: name.value.trim(),
        binding: r.binding_digest, notes: notes.value || null, supersedes: r.latest_review_id,
        acknowledgments: decision === "approved_for_preview" ? acks.filter(function (a) { return a.checked; })
          .map(function (a) { return a.getAttribute("data-ack"); }) : [] }, function (doc) { ST.workflow = doc; });
    }
    if (r.decisions_allowed.indexOf("approved_for_preview") >= 0) {
      button(buttons, "Approve this video (preview only)", function () { decide("approved_for_preview"); }, "approve", { id: "studio-approve" });
    } else {
      h("p", { class: "small" }, form, "Approval is blocked: " + r.approval_blockers.map(words).join(", ") + ".");
    }
    button(buttons, "Request changes", function () { decide("changes_requested"); }, "", { id: "studio-changes" });
    button(buttons, "Reject", function () { decide("rejected"); }, "reject", { id: "studio-reject" });
  }

  function renderExport(box) {
    var w = ST.workflow;
    var exports = w.actions.filter(function (a) { return a.action === "workflow_export"; });
    exports.forEach(function (a) {
      button(box, a.purpose === "approved_preview" ? "Export approved preview" : "Export a review copy (not approved)", function () {
        act("workflow/export", { workflow_id: w.workflow_id, purpose: a.purpose }, function (doc) { ST.workflow = doc; });
      }, a.purpose === "approved_preview" ? "approve" : "", { "data-purpose": a.purpose });
    });
    if (w.export) {
      var e = w.export;
      h("p", {}, box, "Package " + e.package_id + " · " + words(e.purpose) + " · " + (e.verified ? "verified (" + e.files_checked +
        " files, hashes consistent)" : "NOT verified: " + e.problems.map(words).join(", ")) + " · publishable: false");
      if (e.verified) {
        h("a", { class: "studio-download", id: "studio-download", download: e.package_id + ".zip",
          href: "/api/studio/export?workflow=" + encodeURIComponent(w.workflow_id) + "&package=" + encodeURIComponent(e.package_id) },
          box, "Download the verified package (.zip)");
      }
    } else if (!exports.length) {
      h("p", { class: "small" }, box, "Export becomes available after the video is rendered and checked; the approved preview " +
        "needs your current approval.");
    }
  }

  document.addEventListener("hq:view", function (event) { if (event.detail === "studio") { open(); } });
  window.HQStudio = { state: ST };
}());
