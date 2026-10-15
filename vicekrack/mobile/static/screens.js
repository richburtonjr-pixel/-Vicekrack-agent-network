/* ViceKrack Mobile Command Center: Research and Video Studio screens (PR 3).
 * Uses the helpers in app.js (h, call, act, paidSheet, busy, ...). Every action goes to the PC, which applies
 * the HQ's own rules; paid ones also need the PIN and room under the daily phone limit. */
"use strict";

const LABEL_TONE = { "CONFIRMED": "green", "MULTIPLE REPORTS": "blue", "RUMOR": "orange", "UNVERIFIED SOURCE": "orange",
  "UNVERIFIED SITE": "orange", "YOUR CALL": "orange", "ON HOLD": "gray", "NOT USABLE": "red", "NOT CHECKED": "gray" };
const RESEARCH_STATUS = { prepared: ["Needs approval", "orange"], submitting: ["Researching", "blue"],
  received: ["Checking sources", "blue"], completed: ["Done", "green"], rejected: ["Refused by xAI", "red"],
  uncertain: ["Check on PC", "red"], processing_failed: ["Stopped", "red"] };
const FLOW_STATUS = { waiting_for_consent: ["Needs approval", "orange"], uncertain_submission: ["Check on PC", "red"],
  waiting_for_provider: ["Making clips", "blue"], running: ["Working", "blue"], waiting_for_review: ["Ready to review", "orange"],
  review_rejected: ["Changes requested", "gray"], ready_to_export: ["Approved", "green"], exported: ["Exported", "green"],
  failed: ["Failed", "red"], quality_failed: ["Quality failed", "red"], blocked: ["Blocked", "red"] };
const LITE = "grok-imagine-video-1.5-lite";
const DRAFT = { topics: [] };
let OPTIONS = null;
let flash = null;

function chipFor(map, status) {
  const [text, tone] = map[status] || [words(status), "gray"];
  return h("span", { class: "status " + tone }, text);
}
function takeFlash() {
  const shown = flash ? note(flash.text, flash.tone) : null;
  flash = null;
  return shown;
}
function safeUrl(url) {
  try { const u = new URL(url); return u.protocol === "https:" ? u.href : null; } catch (_) { return null; }
}
async function ready() {
  const status = await refreshStatus();
  if (!status.ok) { offline(); return false; }
  if (!status.body.paired) { pairScreen(); return false; }
  return true;
}
async function options() {
  if (!OPTIONS) { const r = await call("/m/api/options"); OPTIONS = r.ok ? r.body : null; }
  return OPTIONS;
}
function autoRefresh(seconds) {
  const here = location.hash;
  STATE.timer = setInterval(() => { if (location.hash === here && !document.hidden) { route(true); } }, seconds * 1000);
}
function readOnlyNote(opts) {
  if (opts && opts.actions_enabled) { return null; }
  return note("ViceKrack HQ is running without Video Studio, so actions are off. Start it with “Start ViceKrack (PC + Phone)”.");
}
/* Run one action behind the busy screen. Returns the result; puts errors in `flash`. */
async function doAction(action, body, message, requestId) {
  busy(message);
  const result = await act(action, body, requestId);
  busy(null);
  if (!result.ok) { flash = { text: errorText(result), tone: "bad" }; }
  return result;
}

/* =============================== Research =============================== */
SCREENS.research = async (rest) => { if (!(await ready())) { return; } return rest[0] ? researchJob(rest[0]) : researchHome(); };

async function researchHome() {
  const opts = await options();
  const max = (opts && opts.research && opts.research.max_topics) || 3;
  const jobs = await call("/m/api/hq/research/jobs");
  const input = h("input", { id: "topic", maxlength: "80", placeholder: "Type a topic, e.g. GTA 6 trailer", autocomplete: "off",
                             enterkeyhint: "done" });
  const chips = h("div", { class: "chips" });
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const drawChips = () => chips.replaceChildren(...DRAFT.topics.map((topic, i) => h("span", { class: "chip" }, topic,
    h("button", { type: "button", "aria-label": "Remove " + topic, onclick: () => { DRAFT.topics.splice(i, 1); drawChips(); } }, "×"))));
  const add = () => {
    const topic = input.value.trim().replace(/\s+/g, " ");
    error.hidden = true;
    if (!topic) { return true; }
    if (topic.length < 2) { error.textContent = "A topic needs at least 2 characters."; error.hidden = false; return false; }
    if (DRAFT.topics.length >= max) { error.textContent = "Up to " + max + " topics at once."; error.hidden = false; return false; }
    if (!DRAFT.topics.some((t) => t.toLowerCase() === topic.toLowerCase())) { DRAFT.topics.push(topic); }
    input.value = "";
    drawChips();
    return true;
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  input.addEventListener("input", () => { error.hidden = true; });
  drawChips();
  const find = h("button", { class: "primary", type: "button", disabled: !(opts && opts.actions_enabled) }, "Find stories");
  find.addEventListener("click", async () => {
    if (!add()) { return; }
    if (!DRAFT.topics.length) { error.textContent = "Type at least one topic."; error.hidden = false; return; }
    const result = await doAction("research/prepare", { topics: DRAFT.topics.slice(0, max) }, "Getting the cost (free)…");
    if (!result.ok) { route(true); return; }
    DRAFT.topics = [];
    approveResearch(result.body);
  });
  const list = jobs.ok && jobs.body.jobs.length
    ? h("section", { class: "card" }, h("h2", {}, "Your research"), jobs.body.jobs.map((j) =>
        h("a", { class: "tap", href: "#research/" + j.job_id },
          h("span", {}, j.topics.join(", "), h("br"), h("small", { class: "muted" }, ago(j.created_at) +
            (j.stories ? " · " + j.stories + " stories" : ""))),
          chipFor(RESEARCH_STATUS, j.status))))
    : h("p", { class: "empty" }, jobs.ok ? "No research yet." : "Couldn't load your research from the PC.");
  show(h("h1", { class: "page" }, "Research"), h("p", { class: "muted" }, "Kiara searches the web live with Grok"),
    takeFlash(), readOnlyNote(opts),
    h("section", { class: "card" }, chips, h("label", { for: "topic" }, "Add a topic (up to " + max + ")"),
      h("div", { class: "inline" }, input, h("button", { type: "button", onclick: add }, "Add")), error, find,
      h("p", { class: "muted" }, "You'll see the Grok cost and approve it with your PIN before anything is sent.")),
    list);
}

function approveResearch(job) {
  const est = job.estimate_usd || {};
  paidSheet({
    title: "Research with Grok web search",
    lines: ["Topics: " + job.topics.join(" · "),
            "Estimated cost: " + money(est.low) + " to " + money(est.high) + ".",
            "Then ViceKrack opens each source itself (free) and labels every story."],
    estimate: est.high,
    run: async (pin, requestId, fail) => {
      busy("Kiara is researching with Grok, then checking every source… (about a minute)");
      const result = await act("research/submit", { job_id: job.job_id, consent: job.consent_phrase, pin }, requestId);
      busy(null);
      if (!result.ok && result.status !== 0) { fail(errorText(result)); return false; }
      if (result.status === 0) { flash = { text: errorText(result) }; }
      location.hash = "#research/" + job.job_id;
      route(true);
      return true;
    },
  });
}

async function researchJob(id) {
  const r = await call("/m/api/hq/research/job?id=" + encodeURIComponent(id));
  const back = backTo("Research", "#research");
  if (!r.ok) { show(back, note(errorText(r), "bad")); return; }
  const job = r.body;
  const nodes = [back, h("h1", { class: "page" }, job.topics.join(", ")),
    h("div", { class: "chiprow" }, chipFor(RESEARCH_STATUS, job.status)), takeFlash()];
  if (job.demo) { nodes.push(note("Demo stories (mock Grok), not real news.")); }
  if (job.status === "prepared" || job.status === "rejected") {
    if (job.status === "rejected") { nodes.push(note("xAI refused the last try (" + words(job.error_code) + "). Nothing was billed.")); }
    nodes.push(h("button", { class: "orange-btn", type: "button", onclick: async () => { await refreshStatus(); approveResearch(job); } },
      "Approve research · up to " + money((job.estimate_usd || {}).high)));
  } else if (job.status === "submitting" || job.status === "received") {
    nodes.push(note("Kiara is working on this. This page updates by itself."),
      h("button", { class: "ghost", type: "button", onclick: async () => {
        await doAction("research/recover", { job_id: job.job_id }, "Checking (free)…"); route(true); } }, "Check now (free)"));
    autoRefresh(10);
  } else if (job.status === "uncertain") {
    nodes.push(note("Grok didn't answer clearly, so this may already be billed. To be safe, finish it on the PC.", "bad"));
  } else if (job.status === "processing_failed") {
    nodes.push(note("Checking the sources stopped. Trying again is free.", "bad"),
      h("button", { class: "ghost", type: "button", onclick: async () => {
        await doAction("research/recover", { job_id: job.job_id }, "Trying again (free)…"); route(true); } }, "Try again (free)"));
  } else if (job.status === "completed") {
    nodes.push(h("p", { class: "muted" }, "Research cost about " + money(job.cost_usd) +
      ". Labels come from ViceKrack's own checks, not from Grok."));
    if (!job.cards || !job.cards.length) { nodes.push(h("p", { class: "empty" }, "No stories for these topics. Try different words.")); }
    for (const card of job.cards || []) { nodes.push(storyCard(job, card)); }
  }
  show(...nodes);
}

function storyCard(job, card) {
  const sources = (card.sources || []).map((src) => {
    const url = safeUrl(src.url);
    let name = src.publisher;
    if (!name && url) { name = new URL(url).hostname; }
    const link = url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, name || "source") : h("span", {}, name || "source");
    return h("li", {}, link, src.check === "opened" ? (src.trusted ? " · trusted outlet" : " · unlisted site") : " · " + words(src.check || "not checked"));
  });
  const box = h("section", { class: "card story" },
    h("div", { class: "meta" }, h("span", { class: "label " + (LABEL_TONE[card.label] || "gray") }, card.label), card.topic),
    h("h3", {}, card.headline),
    card.summary ? h("p", {}, card.summary) : null,
    card.reason ? h("p", {}, card.reason) : null,
    sources.length ? h("details", {}, h("summary", {}, sources.length + (sources.length === 1 ? " source" : " sources")), h("ul", {}, sources)) : null);
  const offer = card.make_video;
  if (!offer) { return box; }
  if (offer.production_status === "completed") {
    box.append(h("a", { class: "ghost", href: "#studio/p/" + offer.production_id }, "Open in Video Studio"));
  } else if (offer.production_status === "uncertain") {
    box.append(note("The script request may already be billed. Finish this one on the PC.", "bad"));
  } else {
    const label = (offer.production_status ? "Continue making video" : offer.pick ? "Use anyway" : "Make video") +
      " · up to " + money(offer.estimate_usd);
    box.append(h("button", { class: "orange-btn", type: "button", onclick: async () => { await refreshStatus(); approveScript(job, card); } }, label));
  }
  return box;
}

function approveScript(job, card) {
  const offer = card.make_video;
  const lines = [offer.writer + " writes a 15-second script from this story's checked facts.",
    offer.pick ? "Your call: it didn't pass the automatic checks." + (offer.video_label ? " The video is labelled " + offer.video_label + " on screen." : "")
      : offer.rumor ? "Not officially confirmed: the video is labelled " + card.label + " and says it's reported, not fact."
      : "Confirmed by an official source.",
    "Then ViceKrack builds the story (free). Clips are approved separately in Video Studio."];
  paidSheet({
    title: "Write the script with Grok",
    lines, estimate: offer.estimate_usd,
    run: async (pin, requestId, fail) => {
      busy("Grok is writing the script, then ViceKrack builds the story… (up to a minute)");
      const result = await act("research/make-video", { job_id: job.job_id, story: card.index, consent: offer.consent, pin }, requestId);
      busy(null);
      if (!result.ok && result.status !== 0) { fail(errorText(result)); return false; }
      const made = result.ok ? result.body.made : null;
      if (made && made.status === "completed") { location.hash = "#studio/p/" + made.production_id; return true; }
      flash = { text: made && made.error ? "The story stopped at the " + words(made.error.stage) + " step (" + words(made.error.code) + ")."
                                         : errorText(result), tone: "bad" };
      route(true);
      return true;
    },
  });
}

/* ============================== Video Studio ============================== */
SCREENS.studio = async (rest) => {
  if (!(await ready())) { return; }
  if (rest[0] === "p" && rest[1]) { return productionScreen(rest[1]); }
  if (rest[0] === "w" && rest[1]) { return workflowScreen(rest[1]); }
  return studioHome();
};

async function studioHome() {
  const [home, productions] = await Promise.all([call("/m/api/home"), call("/m/api/hq/productions")]);
  const nodes = [h("h1", { class: "page" }, "Video Studio"), h("p", { class: "muted" }, "Sienna builds it, you approve the spend"), takeFlash()];
  const waiting = home.ok ? home.body.needs_you.filter((i) => i.kind !== "research_approval") : [];
  if (waiting.length) {
    nodes.push(h("section", { class: "money" }, h("h2", {}, "Waiting on you"),
      waiting.map((i) => h("a", { class: "item", href: "#studio/w/" + i.workflow_id },
        h("span", { class: "what" }, (i.kind === "video_review" ? "Review · " : "Clips · ") + i.title),
        h("span", { class: "cost" }, i.kind === "video_review" ? "Watch" : (i.estimate_usd != null ? "up to " + money(i.estimate_usd) : "Open"))))));
  }
  const rows = productions.ok ? productions.body.productions.slice().sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")) : [];
  nodes.push(rows.length
    ? h("section", { class: "card" }, h("h2", {}, "Your videos"), rows.map((p) =>
        h("a", { class: "tap", href: "#studio/p/" + p.production_id },
          h("span", {}, p.title || "Untitled", h("br"), h("small", { class: "muted" }, ago(p.updated_at) +
            (p.workflows ? " · " + p.workflows + (p.workflows === 1 ? " video" : " videos") : ""))),
          h("span", { class: "side" }, p.eligible ? "Open" : "Needs PC"))))
    : h("p", { class: "empty" }, productions.ok ? "No stories yet. Make one from Research." : "Couldn't load your videos from the PC."));
  show(...nodes);
}

async function productionScreen(id) {
  const [r, opts] = await Promise.all([call("/m/api/hq/production?id=" + encodeURIComponent(id)), options()]);
  const back = backTo("Video Studio", "#studio");
  if (!r.ok) { show(back, note(errorText(r), "bad")); return; }
  const p = r.body;
  const nodes = [back, h("h1", { class: "page" }, p.script ? p.script.title : "Story"), takeFlash(), readOnlyNote(opts)];
  if (p.script) {
    nodes.push(h("details", { class: "card" }, h("summary", {}, "Read the script (" + p.script.beats.length + " parts)"),
      p.script.beats.map((b) => h("div", { class: "beat" }, h("b", {}, words(b.beat) + " · " + b.start + "–" + b.end + "s"),
        b.narration, b.on_screen_text ? h("div", { class: "muted" }, "On screen: " + b.on_screen_text) : null))));
    if (p.script.unverified_claims) { nodes.push(note(p.script.unverified_claims + " claim(s) are marked unverified in the script.")); }
  }
  if (p.workflows.length) {
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Videos for this story"), p.workflows.map((w) =>
      h("a", { class: "tap", href: "#studio/w/" + w.workflow_id }, h("span", {}, "Video · " + ago(w.updated_at)),
        chipFor(FLOW_STATUS, w.status)))));
  }
  if (!p.eligible) {
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Can't start a video yet"), p.reasons.map((x) => h("p", { class: "muted" }, x.text))));
  } else if (opts && opts.actions_enabled) {
    nodes.push(startCard(p, opts.options || {}));
  }
  show(...nodes);
}

function startCard(p, o) {
  const speech = (p.speech_jobs || []).filter((j) => j.status === "completed");
  const narration = h("select", { id: "narration" }, h("option", { value: "silent" }, "No narration (silent)"),
    speech.map((j) => h("option", { value: j.job_id }, "Narration made on the PC (" + (j.settings && j.settings.voice || "voice") + ")")));
  const models = (o.models || [LITE]);
  const model = h("select", { id: "model" }, models.map((m) => h("option", { value: m, selected: m === LITE }, m === LITE ? "Grok video lite (cheaper)" : m)));
  const resolution = h("select", { id: "res" }, (o.resolutions || []).map((x) => h("option", { value: x }, x)));
  const go = h("button", { class: "primary", type: "button" }, "Prepare the video (free)");
  go.addEventListener("click", async () => {
    const speechJob = narration.value === "silent" ? null : narration.value;
    const tracks = (p.caption_tracks || []).filter((t) => t.speech_job_id === speechJob);
    const caption = tracks.find((t) => t.timing === "provider_character_timestamps") || tracks[0] || null;
    const body = { production_id: p.production_id, narration: speechJob ? "speech" : "silent", speech_job_id: speechJob,
                   caption_id: caption ? caption.caption_id : null, model: model.value, resolution: resolution.value || null };
    const result = await doAction("workflow/start", body, "Preparing the clip requests (free, nothing is sent)…");
    if (result.ok) { location.hash = "#studio/w/" + result.body.workflow_id; } else { route(true); }
  });
  return h("section", { class: "card" }, h("h2", {}, "Make a video"),
    h("label", { for: "narration" }, "Narration"), narration,
    h("p", { class: "muted" }, "New narration is bought on the PC; the phone can use narration that's already made."),
    h("label", { for: "model" }, "Video model"), model,
    h("label", { for: "res" }, "Resolution"), resolution, go,
    h("p", { class: "muted" }, "This prepares the clip requests. You'll see the cost and approve with your PIN next."));
}

async function workflowScreen(id) {
  const r = await call("/m/api/hq/workflow?id=" + encodeURIComponent(id));
  if (!r.ok) { show(backTo("Video Studio", "#studio"), note(errorText(r), "bad")); return; }
  const w = r.body;
  const pr = w.production_id ? await call("/m/api/hq/production?id=" + encodeURIComponent(w.production_id)) : null;
  const title = pr && pr.ok && pr.body.script ? pr.body.script.title : "Video";
  const nodes = [backTo(title.length > 28 ? "Story" : title, w.production_id ? "#studio/p/" + w.production_id : "#studio"),
    h("h1", { class: "page" }, title), h("div", { class: "chiprow" }, chipFor(FLOW_STATUS, w.status)), takeFlash()];
  if (w.demo) { nodes.push(note("Demo video (mock providers).")); }
  if (w.notice) { nodes.push(note(w.notice)); }
  if (w.video && w.video.media_id) {
    const src = (mid) => "/m/api/media?production=" + encodeURIComponent(w.production_id) + "&id=" + encodeURIComponent(mid);
    nodes.push(h("video", { controls: true, playsinline: true, preload: "metadata", src: src(w.video.media_id),
                            poster: w.video.poster_media_id ? src(w.video.poster_media_id) : null }));
  }
  nodes.push(...flowActions(w));
  const stages = w.stages || [];
  if (stages.length) {
    const done = stages.filter((s) => s.status === "completed").length;
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Progress"), bar((done / stages.length) * 100),
      h("div", { class: "stages" }, stages.map((s) => h("div", { class: "stage" }, h("span", {}, words(s.name)),
        h("span", { class: "s-" + s.status }, words(s.status) + (s.error_code ? " · " + words(s.error_code) : "")))))));
  }
  if (w.quality) {
    nodes.push(h("section", { class: "card" }, h("h2", {}, "Quality check"), h("p", { class: "muted" }, words(w.quality.result) +
      (w.quality.reasons && w.quality.reasons.length ? ": " + w.quality.reasons.map(words).join(", ") : ""))));
  }
  if (w.review) { const form = reviewCard(w); if (form) { nodes.push(form); } }
  show(...nodes);
  if (w.status === "waiting_for_provider" || w.status === "running") { autoRefresh(10); }
}

function flowActions(w) {
  const out = [];
  const actions = w.actions || [];
  const all = actions.find((a) => a.action === "workflow_submit_all");
  const scenes = {};
  for (const s of w.scenes || []) { scenes[s.scene_index] = s; }
  const describe = (s) => s && s.request ? s.request.duration + "s · " + (s.request.model === LITE ? "lite" : s.request.model) + " · " + s.request.resolution : "";
  if (all) {
    out.push(h("button", { class: "orange-btn", type: "button", onclick: async () => {
      await refreshStatus();
      paidSheet({ title: "Make all " + all.scenes.length + " clips with Grok video",
        lines: [all.seconds + " seconds of video in " + all.scenes.length + " clips.",
                ...all.scenes.map((n) => "Clip " + n + ": " + describe(scenes[n])),
                "Each clip is its own Grok request. Clips already paid for with the same settings are reused."],
        estimate: all.estimate_usd,
        run: (pin, requestId, fail) => sendPaid("workflow/submit-all", { workflow_id: w.workflow_id, consent: all.consent, pin },
                                                 requestId, fail, "Sending the clips to Grok…") });
    } }, "Approve all " + all.scenes.length + " clips · up to " + money(all.estimate_usd)));
  }
  for (const a of actions) {
    if (a.action === "workflow_submit" && a.duplicate_billing_risk) {
      out.push(note("Clip " + a.scene + " may already be billed (Grok didn't answer clearly). Retry it on the PC only.", "bad"));
    } else if (a.action === "workflow_submit" && !all) {
      out.push(h("button", { class: "orange-btn", type: "button", onclick: async () => {
        await refreshStatus();
        paidSheet({ title: "Make clip " + a.scene + " with Grok video", lines: [describe(scenes[a.scene])], estimate: a.estimate_usd,
          run: (pin, requestId, fail) => sendPaid("workflow/submit", { workflow_id: w.workflow_id, scene: a.scene, consent: a.consent, pin },
                                                   requestId, fail, "Sending clip " + a.scene + " to Grok…") });
      } }, "Approve clip " + a.scene + (a.estimate_usd != null ? " · up to " + money(a.estimate_usd) : "")));
    } else if (a.action === "workflow_resume") {
      out.push(h("button", { class: "ghost", type: "button", onclick: async () => {
        await doAction("workflow/resume", { workflow_id: w.workflow_id, allow_network: !!a.allow_network }, "Checking (free)…");
        route(true); } }, a.label || "Check again (free)"));
    } else if (a.action === "workflow_retry_scene") {
      out.push(h("button", { class: "ghost", type: "button", onclick: async () => {
        await doAction("workflow/retry-scene", { workflow_id: w.workflow_id, scene: a.scene, model: LITE }, "Preparing clip " + a.scene + " again (free)…");
        route(true); } }, "Prepare clip " + a.scene + " again (free)"));
    } else if (a.action === "workflow_export") {
      out.push(h("button", { class: "ghost", type: "button", onclick: async () => {
        await doAction("workflow/export", { workflow_id: w.workflow_id, purpose: a.purpose }, "Packaging the video (free)…");
        route(true); } }, a.purpose === "approved_preview" ? "Export approved video (free)" : "Make a review copy (free)"));
    } else if (a.action === "export_download") {
      out.push(h("a", { class: "primary", href: "/m/api/export?workflow=" + encodeURIComponent(w.workflow_id) +
        "&package=" + encodeURIComponent(a.package_id), download: a.package_id + ".zip" }, "Download video package (.zip)"));
    }
  }
  return out;
}

async function sendPaid(action, body, requestId, fail, message) {
  busy(message);
  const result = await act(action, body, requestId);
  busy(null);
  if (!result.ok && result.status !== 0) { fail(errorText(result)); return false; }
  if (result.status === 0) { flash = { text: errorText(result) }; }
  route(true);
  return true;
}

function reviewCard(w) {
  const r = w.review;
  if (!r.decisions_allowed.length || w.status === "exported") { return null; }
  if (r.binding !== "matching") {
    return h("section", { class: "card" }, h("h2", {}, "Review"),
      note("The quality report no longer matches the current files, so it can't be approved. Check it on the PC.", "bad"));
  }
  let saved = "";
  try { saved = localStorage.getItem("vk-reviewer") || ""; } catch (_) { saved = ""; }
  const name = h("input", { id: "reviewer", maxlength: "80", value: saved, autocomplete: "name" });
  const acks = r.applicable_acknowledgments.map((a) => {
    const box = h("input", { type: "checkbox", id: "ack-" + a.id });
    box.dataset.ack = a.id;
    return { box, row: h("label", { class: "check", for: "ack-" + a.id }, box, a.text) };
  });
  const notes = h("textarea", { id: "notes", maxlength: "2000", placeholder: "Notes (optional)", "aria-label": "Notes" });
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const decide = async (decision) => {
    if (!name.value.trim()) { error.textContent = "Enter your name to record a decision."; error.hidden = false; return; }
    if (decision === "approved_for_preview" && acks.some((a) => !a.box.checked)) {
      error.textContent = "Tick each box to approve."; error.hidden = false; return;
    }
    if (decision === "rejected" && !window.confirm("Reject this video?")) { return; }
    try { localStorage.setItem("vk-reviewer", name.value.trim()); } catch (_) { /* fine without it */ }
    await doAction("review/record", { workflow_id: w.workflow_id, decision, reviewer: name.value.trim(), binding: r.binding_digest,
      notes: notes.value || null, supersedes: r.latest_review_id || null,
      acknowledgments: decision === "approved_for_preview" ? acks.map((a) => a.box.dataset.ack) : [] }, "Saving your decision…");
    route(true);
  };
  const canApprove = r.decisions_allowed.includes("approved_for_preview");
  return h("section", { class: "card" }, h("h2", {}, "Your review"),
    h("p", { class: "muted" }, "Approval accepts this exact video as a preview. It never publishes anything."),
    h("label", { for: "reviewer" }, "Your name"), name, acks.map((a) => a.row), notes, error,
    canApprove ? h("button", { class: "primary", type: "button", onclick: () => decide("approved_for_preview") }, "Approve video")
               : note("Approval is blocked: " + r.approval_blockers.map(words).join(", ") + "."),
    h("div", { class: "two" },
      h("button", { class: "ghost", type: "button", onclick: () => decide("changes_requested") }, "Request changes"),
      h("button", { class: "danger", type: "button", onclick: () => decide("rejected") }, "Reject")));
}
