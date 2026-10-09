const $ = (id) => document.getElementById(id);
const tiles = new Map();          // job id -> tile element, kept across redraws
let jobs = [];
let byId = new Map();             // job id -> job, for hover lookups
let scoringOn = true;
let t0 = 0, timer = null;

// Open the page as /?demo and Scan replays a scan from data already on file
// instead of starting a real one, so trying the effects never spends tokens.
const DEMO_SCAN = new URLSearchParams(location.search).has("demo");

// Where jobs, the profile and scans come from. The local app asks its own
// server; the hosted site loads hosted.js first, which keeps everything in
// the visitor's browser and supplies the same four calls.
const source = window.JobRadarSource || {
  state: () => fetch("/api/state").then((r) => r.json()),
  profile: () => fetch("/api/profile").then((r) => r.json()),
  search: (q) => fetch(`/api/search?q=${encodeURIComponent(q)}`).then((r) => r.json()).then((d) => d.ids),
  scan(onEvent) {
    const es = new EventSource("/api/scan?limit=3000");
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      onEvent(d);
      if (d.type === "done" || d.type === "error") es.close();
    };
    // A dropped connection. Close rather than let EventSource reconnect,
    // which would start a second scan.
    es.onerror = () => { es.close(); onEvent({ type: "error", message: "scan connection lost" }); };
  },
};

const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const motionOK = () => !reduceMotion();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const EASE = "cubic-bezier(.16,1,.3,1)";

// FLIP with the browser's own animation API: measure, change, measure again,
// then play each element from where it was. GSAP's Flip plugin did the same
// but took ~12s per filter change across ~1,200 tiles; this is one layout pass.
function playFrom(el, dx, dy, scale = 1, ms = 450, delay = 0) {
  if (!dx && !dy && scale === 1) return;
  el.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${scale})` }, { transform: "none" }],
    { duration: ms, easing: EASE, delay, fill: "backwards" });
}
function fadeIn(el, ms = 320, delay = 0, from = "scale(.6)") {
  el.animate([{ opacity: 0, transform: from }, { opacity: 1, transform: "none" }],
    { duration: ms, easing: EASE, delay, fill: "backwards" });
}

// key -> the plain-language claim the bar is measuring.
const CHECKS = [
  ["skills",    "you know the stack"],
  ["seniority", "level fits you"],
  ["location",  "you can work there"],
  ["domain",    "field you've worked in"],
  ["recency",   "experience is current"],
  ["reach",     "realistic to land"],
];

// Count a number from what is on screen to `to`, easing out. A call that
// lands mid-count carries on from the current value instead of jumping.
function tweenNumber(el, to, decimals = 0, ms = 450) {
  const target = Number(to) || 0;
  const write = (v) => { el._val = v; el._shown = v.toFixed(decimals); el.textContent = el._shown; };
  cancelAnimationFrame(el._raf);
  if (reduceMotion()) { write(target); return; }
  const from = el.textContent === el._shown ? el._val : (parseFloat(el.textContent) || 0);
  const start = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - start) / ms);
    write(from + (target - from) * (1 - Math.pow(1 - k, 3)));
    if (k < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

function bucket(score) {
  if (score === null || score === undefined) return "";
  return "s" + Math.min(5, Math.max(1, Math.ceil(score / 2)));
}

function logo(domain) {
  return domain ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64` : "";
}

function attachLogo(el, job, px) {
  // Google 404s with a generic globe for domains it has no icon for, so a
  // failed load falls back to the company's initials.
  const src = logo(job.domain);
  if (!src) { addInitial(el, job.company); return; }
  const img = new Image();
  img.src = src;
  img.alt = "";
  if (px) { img.width = px; img.height = px; }
  img.onerror = () => { img.remove(); addInitial(el, job.company); };
  el.appendChild(img);
}

function addInitial(el, company) {
  const s = document.createElement("span");
  s.className = "init";
  s.textContent = (company || "?").slice(0, 2).toUpperCase();
  el.appendChild(s);
}

function makeTile(job) {
  const el = document.createElement("div");
  el.className = "tile " + bucket(job.score);
  el.dataset.id = job.id;
  attachLogo(el, job);
  el.onclick = () => { if (job.url) window.open(job.url, "_blank", "noopener"); };
  return el;
}

// --- filtering -------------------------------------------------------------
const WEEK = 7 * 24 * 3600;

function currentFilter() {
  return {
    q: ($("q").value || "").trim().toLowerCase(),
    min: Number($("minscore").value || 0),
    company: $("co").value || "",
    newOnly: $("newonly").checked,
  };
}

// Ids matching the current text query, resolved server-side so descriptions
// are searchable without shipping megabytes of them to the browser.
// null means "no query running", not "nothing matched".
let searchIds = null;

function matches(job, f) {
  if (f.company && job.company !== f.company) return false;
  if (f.min && (job.score === null || job.score === undefined || job.score < f.min)) return false;
  if (f.newOnly && (!job.first_seen || Date.now() / 1000 - job.first_seen > WEEK)) return false;
  if (f.q && searchIds && !searchIds.has(job.id)) return false;
  return true;
}

async function runSearch() {
  const q = ($("q").value || "").trim();
  if (!q) { searchIds = null; renderGrid(); return; }
  try {
    const ids = await source.search(q);
    searchIds = ids === null ? null : new Set(ids);
  } catch {
    searchIds = null;        // a failed lookup shows everything, not nothing
  }
  renderGrid();
}

// Tiles are created once and kept. A filter only hides or shows them, and the
// survivors glide to their new cells, so the grid never flashes.
let introDone = false;

function renderGrid() {
  const grid = $("grid");
  const f = currentFilter();
  if (grid.classList.contains("loading")) { grid.textContent = ""; grid.classList.remove("loading"); }
  let before = null;
  if (introDone && motionOK()) {
    before = new Map();
    for (const [id, el] of tiles) if (!el.hidden) before.set(id, [el.offsetLeft, el.offsetTop]);
  }
  hideHover();

  const live = new Set();
  let shown = 0;
  let prev = null;
  for (const j of jobs) {
    live.add(j.id);
    let el = tiles.get(j.id);
    const cls = "tile " + bucket(j.score);
    if (!el) { el = makeTile(j); tiles.set(j.id, el); }
    else if (el.className !== cls) el.className = cls;
    const show = matches(j, f);
    if (el.hidden === show) el.hidden = !show;
    if (show && !introDone) el.style.setProperty("--i", Math.min(shown, 300));
    if (show) shown++;
    // DOM order follows `jobs`; a node only moves when the order changed.
    const want = prev ? prev.nextElementSibling : grid.firstElementChild;
    if (want !== el) grid.insertBefore(el, want);
    prev = el;
  }
  for (const [id, el] of tiles) if (!live.has(id)) { el.remove(); tiles.delete(id); }

  if (!introDone && shown) {
    introDone = true;
    grid.classList.add("intro");
    setTimeout(() => grid.classList.remove("intro"), 1600);
  }
  if (before) {
    // Only tiles on screen are animated; the rest simply take their place.
    const top = grid.getBoundingClientRect().top;
    const lo = -top - 120, hi = window.innerHeight - top + 120;
    // Movers get their own transform; newcomers share one CSS animation,
    // which is far cheaper than hundreds of separate animation objects.
    let entering = 0;
    const entered = [];
    for (const [id, el] of tiles) {
      if (el.hidden) continue;
      const x = el.offsetLeft, y = el.offsetTop;
      if (y < lo || y > hi) continue;
      const was = before.get(id);
      if (was) playFrom(el, was[0] - x, was[1] - y);
      else { el.style.setProperty("--d", `${Math.min(entering++ * 3, 200)}ms`); entered.push(el); }
    }
    for (const el of entered) el.classList.add("enter");
    setTimeout(() => { for (const el of entered) el.classList.remove("enter"); }, 700);
  }

  const filtered = f.q || f.min || f.company || f.newOnly;
  if (!jobs.length) {
    $("gridnote").textContent = "no jobs yet · press Scan";
  } else if (!shown) {
    $("gridnote").textContent = "nothing matches · clear the filters";
  } else if (f.q && !searchIds) {
    $("gridnote").textContent = `${shown} jobs · searching…`;
  } else {
    $("gridnote").textContent =
      (filtered ? `${shown} of ${jobs.length} jobs` : `${jobs.length} jobs`) +
      " · click a tile to open it";
  }
}

function fillCompanies() {
  const sel = $("co");
  const chosen = sel.value;
  const names = [...new Set(jobs.map((j) => j.company))].sort();
  sel.textContent = "";
  const all = document.createElement("option");
  all.value = ""; all.textContent = "all companies";
  sel.appendChild(all);
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  }
  sel.value = chosen;
}

let debounce = null;
function onFilterChange() {
  clearTimeout(debounce);
  debounce = setTimeout(runSearch, 220);
}

function renderStats(s) {
  tweenNumber($("s-checked"), s.checked);
  tweenNumber($("s-total"), s.total);
  tweenNumber($("s-hire"), s.would_hire);
  tweenNumber($("s-avg"), Number(s.avg), 2);
}

// --- top five ----------------------------------------------------------------
function renderTops() {
  const scored = jobs.filter((j) => j.score !== null && j.score !== undefined);
  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, 5);
  const box = $("tops");
  // Cards that only changed rank slide to their new place.
  const before = new Map();
  if (motionOK()) {
    for (const c of box.querySelectorAll(".top")) {
      const r = c.getBoundingClientRect();
      before.set(c.dataset.id, [r.left, r.top]);
    }
  }
  box.textContent = "";
  if (!top.length) {
    box.innerHTML = `<div class="muted">${scoringOn ? "nothing scored yet" : "ranking needs a model"}</div>`;
    return;
  }
  for (const j of top) {
    const a = document.createElement("a");
    a.className = "top"; a.href = j.url || "#"; a.target = "_blank"; a.rel = "noopener";
    a.dataset.id = j.id;
    const mark = document.createElement("div");
    mark.className = "toplogo";
    mark.dataset.id = j.id;
    attachLogo(mark, j, 24);
    a.appendChild(mark);
    const pct = document.createElement("div");
    pct.className = "pct"; pct.textContent = `${j.score * 10}%`;
    const co = document.createElement("div");
    co.className = "co"; co.textContent = j.company;
    a.append(pct, co);
    box.appendChild(a);
  }
  if (before.size) {
    for (const c of box.querySelectorAll(".top")) {
      const was = before.get(c.dataset.id);
      const r = c.getBoundingClientRect();
      if (was) playFrom(c, was[0] - r.left, was[1] - r.top, 1, 500);
      else fadeIn(c, 300, 0, "translateY(6px)");
    }
  }
}

// A soft spotlight follows the pointer across the five cards.
$("tops").addEventListener("pointermove", (e) => {
  for (const c of $("tops").querySelectorAll(".top")) {
    const r = c.getBoundingClientRect();
    c.style.setProperty("--x", `${e.clientX - r.left}px`);
    c.style.setProperty("--y", `${e.clientY - r.top}px`);
  }
});
$("tops").addEventListener("pointerleave", () => {
  for (const c of $("tops").querySelectorAll(".top")) c.style.removeProperty("--x");
});

// --- checking bars -------------------------------------------------------------
function renderChecks(job) {
  const ul = $("checks");
  if (!scoringOn) {
    ul.innerHTML = '<li class="offnote">Scoring is off. Openings, closures and the ' +
      'weekly email still work; set <code>llm.provider</code> in config.yaml to rank them.</li>';
    return;
  }
  let bd = {};
  if (job && job.breakdown) {
    bd = typeof job.breakdown === "string" ? safeParse(job.breakdown) : job.breakdown;
  }
  // The six rows are built once; after that only the bars and numbers move,
  // so switching jobs slides them to the new values instead of redrawing.
  if (ul.children.length !== CHECKS.length || ul.querySelector(".offnote")) {
    ul.textContent = "";
    for (const [key, claim] of CHECKS) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="k">${key}</span>
        <span class="claim">${claim}</span>
        <span class="bar"><i></i></span>
        <span class="n">0</span>`;
      ul.appendChild(li);
    }
    void ul.offsetWidth;            // commit scaleX(0) so the first values grow in
  }
  CHECKS.forEach(([key], i) => {
    const val = Math.max(0, Math.min(100, Number(bd[key]) || 0));
    const li = ul.children[i];
    li.querySelector(".bar i").style.setProperty("--v", val / 100);
    tweenNumber(li.querySelector(".n"), val);
  });
}

// --- hover: ring, card and panel preview -----------------------------------------
let hovered = null;
let swapTimer = null;

function previewJob(job) {
  if ($("scan").disabled) return;           // a running scan owns the panel
  const box = $("looking");
  box.classList.add("swap");
  clearTimeout(swapTimer);
  swapTimer = setTimeout(() => {
    $("lk-company").textContent = job.company;
    $("lk-title").textContent = job.title;
    box.classList.remove("swap");
  }, 90);
  const lk = $("lk-score");
  if (job.score !== null && job.score !== undefined) tweenNumber(lk, job.score / 10, 2);
  else { cancelAnimationFrame(lk._raf); lk.textContent = ""; }
  renderChecks(job);
}

// One outline glides from tile to tile, so sweeping the grid reads as one
// motion rather than a hundred separate pops.
function moveRing(el) {
  const ring = $("ring");
  const first = !ring.classList.contains("on");
  ring.classList.toggle("snap", first);         // appear in place, then glide
  ring.style.width = `${el.offsetWidth}px`;
  ring.style.height = `${el.offsetHeight}px`;
  ring.style.transform = `translate(${el.offsetLeft}px, ${el.offsetTop}px)`;
  if (first) void ring.offsetWidth;
  ring.classList.remove("snap");
  ring.classList.add("on");
}

function showTip(el, job) {
  const tip = $("tip");
  const scored = job.score !== null && job.score !== undefined;
  tip.innerHTML = `
    <div class="t-co"><span class="t-dot"></span><span>${escapeHtml(job.company)}</span>
      <span class="t-score">${scored ? `${job.score}/10` : "not scored"}</span></div>
    <div class="t-title">${escapeHtml(job.title)}</div>
    ${job.reason ? `<div class="t-why">${escapeHtml(job.reason)}</div>` : ""}`;
  tip.querySelector(".t-dot").style.background = getComputedStyle(el).backgroundColor;
  const r = el.getBoundingClientRect();
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = r.left + r.width / 2 - w / 2;
  x = Math.max(8, Math.min(window.innerWidth - w - 8, x));
  let y = r.top - h - 10;
  if (y < 8) y = r.bottom + 10;                  // no room above: drop below
  tip.style.setProperty("--tx", `${Math.round(x)}px`);
  tip.style.setProperty("--ty", `${Math.round(y)}px`);
  tip.classList.add("on");
}

function hideHover() {
  hovered = null;
  $("ring").classList.remove("on");
  $("tip").classList.remove("on");
}

$("grid").addEventListener("mouseover", (e) => {
  const el = e.target.closest(".tile");
  if (!el || el === hovered || $("scan").disabled) return;   // a scan owns the ring
  hovered = el;
  const job = byId.get(el.dataset.id);
  if (!job) return;
  moveRing(el);
  showTip(el, job);
  previewJob(job);
});
$("grid").addEventListener("mouseleave", hideHover);
window.addEventListener("scroll", () => $("tip").classList.remove("on"), { passive: true });

function safeParse(s) { try { return JSON.parse(s) || {}; } catch { return {}; } }

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- data ------------------------------------------------------------------------
async function loadState() {
  const d = await source.state();
  jobs = d.jobs;
  byId = new Map(jobs.map((j) => [j.id, j]));
  scoringOn = d.scoring !== false;
  fillCompanies();
  renderGrid(); renderStats(d.stats); renderTops();
  renderChecks(jobs.find((j) => j.score !== null) || null);
}

async function loadProfile() {
  const d = await source.profile();
  if (d.name) {
    $("p-name").textContent = d.name;
    $("who").textContent = d.name.split(" ")[0];
    $("avatar").textContent = d.name.split(" ").map((w) => w[0]).slice(0, 2).join("");
  }
  $("p-headline").textContent = d.headline || "";
  $("p-location").textContent = d.location || "";

  const LABELS = { github: "GitHub", linkedin: "LinkedIn", email: "Email" };
  const links = $("p-links");
  links.textContent = "";
  for (const l of d.links || []) {
    if (!/^(https:|mailto:)/.test(l.url || "")) continue;
    const a = document.createElement("a");
    a.href = l.url;
    a.textContent = LABELS[l.kind] || l.kind;
    a.title = l.label;
    if (l.kind !== "email") { a.target = "_blank"; a.rel = "noopener"; }
    links.appendChild(a);
  }

  fillEntries($("exp"), d.experience, d.error || "no resume parsed");
  $("edu-sec").hidden = !(d.education || []).length;
  fillEntries($("edu"), d.education);

  const skills = $("skills");
  skills.textContent = "";
  (d.skills || []).forEach((s, i) => {
    const chip = document.createElement("span");
    chip.className = "rise";
    chip.style.setProperty("--i", i);
    chip.textContent = s;
    skills.appendChild(chip);
  });
  $("skills-sec").hidden = !(d.skills || []).length;

  $("scorer").hidden = !d.scorer_profile;
  $("scorer-text").textContent = (d.scorer_kind === "raw"
    ? "No model summary yet, so the scorer is reading the raw resume text:\n\n" : "")
    + (d.scorer_profile || "");
}

function fillEntries(ul, entries, emptyMsg = "") {
  ul.textContent = "";
  if (!(entries || []).length) {
    if (emptyMsg) ul.innerHTML = `<li class="muted">${escapeHtml(emptyMsg)}</li>`;
    return;
  }
  entries.forEach((e, i) => {
    const li = document.createElement("li");
    li.className = "rise";
    li.style.setProperty("--i", i);
    li.innerHTML = `<div><div class="org">${escapeHtml(e.org)}</div>
                    <div class="role">${escapeHtml(e.role)}</div></div>
                    <div class="yr">${escapeHtml(e.years)}</div>`;
    ul.appendChild(li);
  });
}

// --- scanning ----------------------------------------------------------------------
let lit = [];
function clearLit() {
  for (const el of lit) el.classList.remove("active");
  lit = [];
}

function highlightCompany(company) {
  clearLit();
  let first = null;
  for (const j of jobs) {
    if (j.company !== company) continue;
    const el = tiles.get(j.id);
    if (!el || el.hidden) continue;
    el.classList.add("active");
    lit.push(el);
    if (!first) first = el;
    if (lit.length >= 40) break;
  }
  if (first) first.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// A radar ping spreads from the tile being scored.
function ping(el) {
  const p = $("ping");
  p.style.width = `${el.offsetWidth}px`;
  p.style.height = `${el.offsetHeight}px`;
  p.style.setProperty("--at", `translate(${el.offsetLeft}px, ${el.offsetTop}px)`);
  p.classList.remove("go");
  void p.offsetWidth;
  p.classList.add("go");
}

function beginScan() {
  const btn = $("scan");
  btn.disabled = true;
  btn.classList.add("scanning");
  btn.querySelector(".lbl").textContent = "Scanning…";
  $("topbar").classList.add("scanning");
  hideHover();
  t0 = Date.now();
  timer = setInterval(() => { $("s-time").textContent = ((Date.now() - t0) / 1000).toFixed(1); }, 100);
}

function endScan() {
  const btn = $("scan");
  clearLit(); clearInterval(timer);
  btn.disabled = false;
  btn.classList.remove("scanning");
  btn.querySelector(".lbl").textContent = "Scan";
  $("topbar").classList.remove("scanning");
  $("lk-company").textContent = "idle"; $("lk-title").textContent = ""; $("lk-score").textContent = "";
}

function handleScanEvent(d) {
  if (d.type === "fetch") {
    $("lk-company").textContent = d.company;
    $("lk-title").textContent = d.error ? "fetch failed" : `${d.count} open`;
    $("lk-score").textContent = "";
    // Fetching is the long half of a scan. Light up that company's tiles so
    // the grid shows progress instead of sitting still for two minutes.
    highlightCompany(d.company);
  } else if (d.type === "fetched") {
    $("gridnote").textContent = `${d.new} new · ${d.closed} closed · ${d.total} tracked`;
  } else if (d.type === "scoring") {
    $("gridnote").textContent = `scoring ${d.count} of ${d.backlog} unscored`;
  } else if (d.type === "looking") {
    clearLit();
    const active = tiles.get(d.id) || null;
    if (active && !active.hidden) {
      active.classList.add("active");
      lit.push(active);
      active.scrollIntoView({ block: "nearest", behavior: "smooth" });
      moveRing(active);
      ping(active);
    }
    $("lk-company").textContent = d.company;
    $("lk-title").textContent = d.title;
    $("lk-score").textContent = "…";
  } else if (d.type === "scored") {
    const el = tiles.get(d.id);
    if (el) el.className = "tile active " + bucket(d.score);
    const j = byId.get(d.id);
    if (j) { j.score = d.score; j.reason = d.reason; j.breakdown = d.breakdown || {}; }
    tweenNumber($("lk-score"), d.score / 10, 2, 300);
    renderChecks(j || null);
    renderTops();
    const scored = jobs.filter((x) => x.score !== null && x.score !== undefined);
    renderStats({
      checked: scored.length, total: jobs.length,
      would_hire: scored.filter((x) => x.score >= 6).length,
      avg: scored.length ? scored.reduce((a, b) => a + b.score, 0) / scored.length / 10 : 0,
    });
  } else if (d.type === "throttled") {
    $("gridnote").textContent = `rate limited after ${d.scored}; press Scan again to continue`;
  } else if (d.type === "error") {
    $("gridnote").textContent = d.message;
    $("ring").classList.remove("on");
    endScan();
  } else if (d.type === "done") {
    $("s-time").textContent = d.elapsed.toFixed(1);
    $("ring").classList.remove("on");
    endScan();
    // Score state only lands in `jobs` via the stream; reload first so the
    // finale ranks the full board, not just this run's batch.
    loadState().then(showMatchMade);
  }
}

function startScan() {
  beginScan();
  source.scan(handleScanEvent);
}

// Replays a scan from what is already on file: a few companies are "fetched",
// then a sample of scored jobs is "scored" again with their stored results.
async function demoScan() {
  beginScan();
  const companies = [...new Set(jobs.map((j) => j.company))].slice(0, 10);
  for (const c of companies) {
    handleScanEvent({ type: "fetch", company: c, count: jobs.filter((j) => j.company === c).length, error: "" });
    await sleep(260);
  }
  handleScanEvent({ type: "fetched", new: 0, closed: 0, total: jobs.length });
  const pool = jobs.filter((j) => j.score !== null && j.score !== undefined && tiles.get(j.id) && !tiles.get(j.id).hidden);
  const picks = pool.sort(() => Math.random() - 0.5).slice(0, 24);
  handleScanEvent({ type: "scoring", count: picks.length, backlog: picks.length });
  for (const j of picks) {
    const el = tiles.get(j.id);
    if (el) el.className = "tile";               // unscored, so it visibly lights up again
    handleScanEvent({ type: "looking", id: j.id, company: j.company, title: j.title });
    await sleep(170);
    handleScanEvent({ type: "scored", id: j.id, score: j.score, reason: j.reason,
      breakdown: typeof j.breakdown === "string" ? safeParse(j.breakdown) : j.breakdown });
    await sleep(110);
  }
  handleScanEvent({ type: "done", scored: picks.length, elapsed: (Date.now() - t0) / 1000 });
}

// --- the finale ----------------------------------------------------------------------
function showMatchMade() {
  const scored = jobs.filter((j) => j.score !== null && j.score !== undefined);
  if (!scored.length) return;
  scored.sort((a, b) => b.score - a.score);
  const top = scored.slice(0, 5);

  const name = $("p-name").textContent;
  const initials = $("avatar").textContent;
  const cards = top.map((j) => `
    <a class="mm-card" href="${escapeHtml(j.url || "#")}" target="_blank" rel="noopener"
       title="${escapeHtml(j.title)} · ${escapeHtml(j.reason || "")}">
      <div class="mm-tile" data-id="${escapeHtml(j.id)}" data-domain="${escapeHtml(j.domain || "")}"
           data-company="${escapeHtml(j.company || "?")}"></div>
      <div class="mm-pct">${j.score * 10}%</div>
      <div class="mm-co">${escapeHtml(j.company)}</div>
    </a>`).join("");

  // The five logos fly out of the panel into the big cards.
  const from = new Map();
  if (motionOK()) {
    for (const m of document.querySelectorAll("#tops .toplogo")) {
      const r = m.getBoundingClientRect();
      if (r.width) from.set(m.dataset.id, r);
    }
  }
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `
    <div class="mm">
      <div class="mm-profile">
        <div class="mm-av">${escapeHtml(initials)}</div>
        <div><div class="kicker">Profile</div>
             <div class="pname">${escapeHtml(name)}</div>
             <div class="pmeta">${escapeHtml($("p-headline").textContent)}</div></div>
      </div>
      <div class="kicker mm-kicker">Match made</div>
      <h2 class="mm-head">High probability of interviewing you</h2>
      <div class="mm-cards">${cards}</div>
      <button class="mm-close">Back to the grid</button>
    </div>`;
  document.body.appendChild(ov);
  for (const slot of ov.querySelectorAll(".mm-tile")) {
    attachLogo(slot, { domain: slot.dataset.domain, company: slot.dataset.company }, 56);
  }

  let close;
  if (motionOK()) {
    ov.classList.add("driven");
    ov.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 350, easing: EASE });
    ov.querySelectorAll(".mm-tile").forEach((t, i) => {
      const was = from.get(t.dataset.id);
      const r = t.getBoundingClientRect();
      if (was) {
        playFrom(t, was.left + was.width / 2 - (r.left + r.width / 2),
          was.top + was.height / 2 - (r.top + r.height / 2), was.width / r.width, 800, 50 + i * 60);
      } else fadeIn(t, 500, 100 + i * 60);
    });
    ov.querySelectorAll(".mm-profile,.mm-kicker,.mm-head,.mm-pct,.mm-co,.mm-close")
      .forEach((el, i) => fadeIn(el, 450, 300 + i * 40, "translateY(10px)"));
    close = () => {
      if (ov._closing) return;
      ov._closing = true;
      ov.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 220, easing: "ease-in", fill: "forwards" })
        .onfinish = () => ov.remove();
    };
  } else {
    requestAnimationFrame(() => ov.classList.add("show"));
    close = () => ov.remove();
  }
  ov.querySelector(".mm-close").onclick = close;
  ov.onclick = (e) => { if (e.target === ov) close(); };
  document.addEventListener("keydown", function esc(e) {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
  });
}

// --- boot ------------------------------------------------------------------------------
(function ghosts() {
  const grid = $("grid");
  for (let i = 0; i < 260; i++) {
    const g = document.createElement("div");
    g.className = "ghost";
    grid.appendChild(g);
  }
})();

$("scan").onclick = DEMO_SCAN ? demoScan : startScan;
$("q").addEventListener("input", onFilterChange);
$("minscore").addEventListener("change", renderGrid);
$("co").addEventListener("change", renderGrid);
$("newonly").addEventListener("change", renderGrid);
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("q").value = ""; searchIds = null; renderGrid(); }
});
loadProfile().catch(() => { $("p-name").textContent = "Your resume"; });
loadState().catch((e) => {
  $("grid").classList.remove("loading");
  $("gridnote").textContent = `couldn't load jobs: ${e.message || e}`;
});
