/* ViceKrack Living HQ, Step 50: Research.
 *
 * Type any topic -> approve one paid Grok research request -> ViceKrack opens every source page itself
 * and labels each story -> pick a story -> approve one paid Grok script request -> the production opens in
 * Video Studio. Same safety rules as studio.js:
 * - Talks only to /api/studio/... (GET to read, POST to act). Nothing runs until the Research view opens.
 * - Every action is a click. No timer, polling or automatic retry. While one action runs, every action
 *   button is disabled, and each click carries its own request ID.
 * - A paid request needs a ticked approval box for that exact request; the job-specific consent phrase
 *   shown on screen is what is sent, and the server checks it again.
 * - All server text is inserted with textContent / setAttribute, never as HTML. Links are shown only for
 *   plain https URLs and open in a new tab without referrer. Nothing is stored in the browser.
 */
(function () {
  "use strict";
  var R = { loaded: false, session: null, jobs: [], job: null, busy: false, message: null, error: null,
            topics: "", openCard: null };
  var LABEL_TONE = { "MULTIPLE REPORTS": "multi", "RUMOR": "rumor", "CONFIRMED": "confirmed",
                     "ON HOLD": "hold", "NOT USABLE": "no", "NOT CHECKED": "no" };
  var CHECKS = { opened: "opened by ViceKrack", page_does_not_mention_topic: "page doesn't mention the topic",
                 page_has_no_article_text: "no article text on the page", article_unavailable: "couldn't open the page",
                 article_blocked_url: "link not allowed", article_blocked_host: "redirected to another site",
                 article_blocked_path: "link not allowed", article_too_large: "page too large",
                 article_timeout: "page took too long", article_not_html: "not a web article",
                 article_redirect_blocked: "redirected elsewhere", unsafe_link: "unsafe link" };

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
  function money(value) {
    if (value === null || value === undefined) { return "-"; }
    return value < 0.01 ? "under 1¢" : (value < 1 ? Math.round(value * 100) + "¢" : "$" + value.toFixed(2));
  }
  function day(stamp) { return stamp ? String(stamp).slice(0, 10) : "date unknown"; }
  function safeUrl(url) {
    try { var u = new URL(url); return u.protocol === "https:" && u.hostname && !u.username && !u.password ? u.href : null; }
    catch (e) { return null; }
  }
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
  function act(path, body, working, after) {
    if (R.busy || !R.session || !R.session.actions_enabled) { return Promise.resolve(); }
    R.busy = true; R.error = null; R.message = working; render();
    return window.fetch("/api/studio/" + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-VK-Studio-CSRF": R.session.csrf },
      body: JSON.stringify(Object.assign({ request_id: requestId() }, body))
    }).then(function (response) {
      return response.ok ? response.json() : readError(response);
    }).then(function (doc) {
      R.busy = false; R.message = null; R.job = doc;
      if (after) { after(doc); }
      return loadJobs();
    }).catch(function (error) {
      R.busy = false; R.message = null;
      R.error = { code: error.code || "network_error", message: error.message };
      return R.job ? loadJob(R.job.job_id).catch(function () { return null; }) : null;
    }).then(render);
  }

  /* ------------------------------------------------------------------ loading */
  function open() {
    if (R.loaded) { render(); return; }
    R.loaded = true;
    get("/api/studio/session").then(function (doc) {
      R.session = doc;
      if (!doc.actions_enabled) { render(); return null; }
      return loadJobs().then(function () {
        if (R.jobs.length) { return loadJob(R.jobs[0].job_id); }
        return null;
      });
    }).catch(function (error) { R.error = { code: error.code, message: error.message }; }).then(render);
  }
  function loadJobs() {
    return get("/api/studio/research/jobs").then(function (doc) { R.jobs = doc.jobs; });
  }
  function loadJob(id) {
    return get("/api/studio/research/job?id=" + encodeURIComponent(id)).then(function (doc) { R.job = doc; });
  }

  /* ------------------------------------------------------------------ pieces */
  function button(parent, label, onClick, cls, attrs) {
    var b = h("button", Object.assign({ type: "button", class: "studio-btn " + (cls || "") }, attrs || {}), parent, label);
    b.disabled = R.busy || !R.session || !R.session.actions_enabled;
    b.addEventListener("click", function () { if (!b.disabled) { onClick(); } });
    return b;
  }
  function paidBox(parent, title, lines, consent, label, risk, onSend) {
    var box = h("div", { class: "paid-box" + (risk ? " risk" : ""), role: "group", "aria-label": title }, parent);
    h("div", { class: "paid-title" }, box, risk ? "Paid request · may bill twice" : "Paid request");
    h("p", {}, box, title);
    lines.forEach(function (line) { h("p", { class: "small" }, box, line); });
    var id = "rc-" + consent.replace(/[^a-z0-9]/gi, "-");
    var row = h("div", { class: "studio-row" }, box);
    var check = h("input", { type: "checkbox", id: id }, row);
    h("label", { for: id }, row, "I approve this one paid xAI request (" + consent + ")");
    var check2 = null;
    if (risk) {
      var row2 = h("div", { class: "studio-row" }, box);
      check2 = h("input", { type: "checkbox", id: id + "-dup" }, row2);
      h("label", { for: id + "-dup" }, row2, "I checked my xAI usage and accept that this may bill twice");
    }
    var send = button(box, label, function () {
      if (check.checked && (!check2 || check2.checked)) { onSend(consent, !!check2); }
    }, "paid");
    function sync() { send.disabled = R.busy || !check.checked || (check2 && !check2.checked); }
    check.addEventListener("change", sync);
    if (check2) { check2.addEventListener("change", sync); }
    sync();
  }

  /* ------------------------------------------------------------------ render */
  function render() {
    var body = $("research-body");
    if (!body) { return; }
    clear(body);
    var badge = $("research-badge"), msg = $("research-message"), s = R.session;
    $("research-view").setAttribute("data-busy", R.busy ? "true" : "false");
    badge.className = "sim-badge " + (!s ? "" : !s.actions_enabled ? "studio-off" : s.demo ? "studio-demo" : "studio-real");
    badge.textContent = !s ? "LOADING" : !s.actions_enabled ? "OFF · READ-ONLY HQ" :
      s.demo ? "DEMO · MOCK GROK" : "REAL · PAID ON APPROVAL";
    msg.className = "studio-message" + (R.error ? " error" : "") + (R.busy ? " studio-busy" : "");
    msg.textContent = R.error ? "Not done: " + R.error.message + " (" + words(R.error.code) + "). Nothing was retried automatically." :
      (R.message || "");
    if (!s) { return; }
    if (!s.actions_enabled) {
      h("p", { class: "notice" }, body, "Research needs the Studio. Start ViceKrack with the Start ViceKrack REAL icon (or the demo icon).");
      return;
    }
    if (!s.demo && !s.credentials.xai_configured) {
      h("p", { class: "notice warn" }, body, "Your xAI key isn't set in the window that started ViceKrack, so paid requests will be refused.");
    }
    renderAsk(body);
    if (R.job) { renderJob(body, R.job); }
    renderHistory(body);
  }

  function renderAsk(body) {
    var box = h("section", { class: "studio-step research-ask", "aria-label": "What should Kiara research?" }, body);
    h("h3", {}, box, "What should we research?");
    var max = (R.session.research && R.session.research.max_topics) || 3;
    var row = h("div", { class: "research-input-row" }, box);
    var input = h("input", { type: "text", id: "research-topics", maxlength: String(max * 82), autocomplete: "off",
      placeholder: "e.g. GTA 6 trailer, Jordan 4 release, Eagles trade rumors", "aria-describedby": "research-hint" }, row);
    input.value = R.topics;
    input.addEventListener("input", function () { R.topics = input.value; });
    var go = button(row, "Go", function () {
      var topics = R.topics.split(",").map(function (t) { return t.trim(); }).filter(Boolean);
      if (!topics.length) { R.error = { code: "no_topic", message: "Type at least one topic." }; render(); return; }
      act("research/prepare", { topics: topics.slice(0, max) }, "Preparing the research request (free, nothing sent)…");
    }, "go");
    input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !go.disabled) { go.click(); } });
    h("p", { class: "small", id: "research-hint" }, box, "Up to " + max + " topics, separated by commas. Go is free: " +
      "you'll see the exact request and its estimated cost before anything is sent.");
  }

  function renderJob(body, job) {
    var box = h("section", { class: "studio-step", "aria-label": "Research run" }, body);
    h("h3", {}, box, "Research: " + job.topics.join(", "));
    var risky = job.status === "uncertain" || job.status === "submitting";
    if (job.status === "prepared" || job.status === "rejected" || risky) {
      var lines = ["Grok (" + job.model + ") will search the web for: " + job.topics.join(" · ") + ".",
                   "Estimated cost: " + money(job.estimate_usd.low) + " to " + money(job.estimate_usd.high) +
                   " (web searches plus reading). Then ViceKrack opens each source itself, which is free."];
      if (job.status === "rejected") { lines.push("The last attempt was refused by xAI (" + words(job.error_code) + "); nothing was billed."); }
      paidBox(box, "Research these topics with Grok web search", lines, job.consent_phrase,
        "Approve and research (paid)", risky, function (consent, dup) {
          act("research/submit", { job_id: job.job_id, consent: consent, retry_uncertain: dup || undefined,
                                   acknowledge_duplicate_billing: dup || undefined },
              "Kiara is researching with Grok, then ViceKrack opens every source… (about a minute)");
        });
      return;
    }
    var meta = h("p", { class: "small" }, box, (job.demo ? "DEMO stories (mock Grok, not real news). " : "") +
      "Grok's research cost about " + money(job.cost_usd) + ". Labels come from ViceKrack's own checks, not from Grok.");
    meta.setAttribute("data-status", job.status);
    if (!job.cards || !job.cards.length) {
      h("p", { class: "notice" }, box, "Grok didn't return any stories for these topics. Try different words.");
      return;
    }
    var grid = h("div", { class: "research-grid" }, box);
    job.cards.forEach(function (card) { renderCard(grid, job, card); });
  }

  function renderCard(grid, job, card) {
    var tone = LABEL_TONE[card.label] || "no";
    var el = h("article", { class: "story-card " + tone, "data-label": card.label, "data-index": String(card.index) }, grid);
    var top = h("div", { class: "story-top" }, el);
    h("span", { class: "story-label " + tone }, top, card.label);
    h("span", { class: "story-topic" }, top, card.topic);
    h("h4", {}, el, card.headline);
    if (card.summary) {
      var p = h("p", { class: "story-summary" }, el, card.summary);
      h("span", { class: "story-by" }, p, " (summary by " + card.summary_by + ")");
    }
    if (card.reason) { h("p", { class: "story-reason" }, el, card.reason); }
    var list = h("ul", { class: "story-sources", "aria-label": "Sources" }, el);
    card.sources.forEach(function (src) {
      var li = h("li", {}, list);
      var url = safeUrl(src.url);
      var name = src.publisher || (url ? new URL(url).hostname : "source");
      if (url) { h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, li, name); } else { h("span", {}, li, name); }
      h("span", { class: "small" }, li, " · " + day(src.published_at || src.claimed_date) + " · " +
        (CHECKS[src.check] || words(src.check || "not checked")) + (src.check === "opened" ? (src.trusted ? " · trusted outlet" : " · unlisted site") : ""));
    });
    var offer = card.make_video;
    if (!offer) { return; }
    if (offer.production_status === "completed") {
      button(el, "Open in Video Studio", function () { openStudio(offer.production_id); }, "approve");
      return;
    }
    if (R.openCard !== card.index) {
      button(el, offer.production_status ? "Continue making video" : "Make video", function () { R.openCard = card.index; render(); }, "go");
      return;
    }
    var lines = [offer.writer + " writes a 15-second script from this story's checked facts. Estimated cost: " +
                 money(offer.estimate_usd) + ".",
                 offer.rumor ? "This story isn't officially confirmed: the video is labelled " + card.label +
                   " on screen and the script must say it's reported, not fact." : "This story is confirmed by an official source.",
                 "Then ViceKrack builds the story (free) and opens it in Video Studio, where clips and narration are approved separately."];
    paidBox(el, "Write the script with Grok", lines, offer.consent, "Approve and write script (paid)",
      offer.production_status === "uncertain", function (consent, dup) {
        act("research/make-video", { job_id: job.job_id, story: card.index, consent: consent, retry_uncertain: dup || undefined },
          "Grok is writing the script, then ViceKrack builds the story… (up to a minute)", function (doc) {
            R.openCard = null;
            if (doc.made && doc.made.status === "completed") { openStudio(doc.made.production_id); }
            else if (doc.made && doc.made.error) {
              R.error = { code: doc.made.error.code, message: "The story stopped at the " + doc.made.error.stage + " step." };
            }
          });
      });
  }

  function renderHistory(body) {
    if (!R.jobs.length) { return; }
    var box = h("section", { class: "studio-step", "aria-label": "Earlier research" }, body);
    h("h3", {}, box, "Earlier research");
    var row = h("div", { class: "research-history" }, box);
    R.jobs.forEach(function (j) {
      var b = button(row, j.topics.join(", ") + " · " + words(j.status) + (j.stories ? " · " + j.stories + " stories" : ""),
        function () { R.error = null; R.openCard = null; loadJob(j.job_id).then(render); },
        R.job && R.job.job_id === j.job_id ? "selected" : "");
      b.setAttribute("data-job", j.job_id);
    });
  }

  function openStudio(productionId) {
    // Hand the production over first, then switch views, so the Studio loads its list only once.
    if (window.HQStudio && window.HQStudio.show) { window.HQStudio.show(productionId); }
    var nav = document.querySelector('.view-btn[data-view="studio"]');
    if (nav) { nav.click(); }
  }

  document.addEventListener("hq:view", function (event) { if (event.detail === "research") { open(); } });
  window.HQResearch = { state: R };
}());
