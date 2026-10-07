// Hosted mode. Everything personal lives in this browser: the API key, the
// resume summary, and every score. The server only lists public jobs and
// relays scoring requests to the AI provider without keeping anything.
// Loaded before app.js, which uses window.JobRadarSource instead of the
// local server.
(() => {
  const K = {
    key: "jr.key", provider: "jr.provider", model: "jr.model", profile: "jr.profile",
    details: "jr.details", scores: "jr.scores", seen: "jr.seen", tokens: "jr.tokens",
  };
  const DEFAULT_MODEL = "mistralai/mistral-large-2512";
  const BATCH = 25;

  // Storage can be missing (private windows, blocked site data); the page
  // still works, it just forgets on reload.
  const memory = {};
  const store = {
    get(k, fallback = null) {
      try {
        const v = localStorage.getItem(k);
        return v === null ? (k in memory ? memory[k] : fallback) : JSON.parse(v);
      } catch { return k in memory ? memory[k] : fallback; }
    },
    set(k, v) {
      memory[k] = v;
      try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* kept in memory */ }
    },
    clear() {
      for (const k of Object.values(K)) {
        delete memory[k];
        try { localStorage.removeItem(k); } catch { /* nothing to clear */ }
      }
    },
  };
  const ready = () => !!(store.get(K.key) && store.get(K.profile));

  // --- jobs ------------------------------------------------------------------
  let board = null;                       // last /api/jobs reply

  async function fetchJobs() {
    const r = await fetch("/api/jobs");
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `job list failed (${r.status})`);
    board = d;
    return d;
  }

  // First visit: everything already open is the baseline, not news. Later
  // visits stamp genuinely new ids, and forget ids that left every board.
  function markSeen(list, prune) {
    const seen = store.get(K.seen, {});
    const first = Object.keys(seen).length === 0;
    const now = Date.now() / 1000;
    let fresh = 0, closed = 0;
    for (const j of list) {
      if (j.id in seen) continue;
      seen[j.id] = first ? 1 : now;
      if (!first) fresh++;
    }
    if (prune) {
      const live = new Set(list.map((j) => j.id));
      const scores = store.get(K.scores, {});
      for (const id of Object.keys(seen)) {
        if (live.has(id)) continue;
        delete seen[id];
        delete scores[id];
        closed++;
      }
      store.set(K.scores, scores);
    }
    store.set(K.seen, seen);
    return { fresh, closed };
  }

  function merged(list) {
    const scores = store.get(K.scores, {});
    const seen = store.get(K.seen, {});
    return list.map((j) => {
      const s = scores[j.id];
      return {
        ...j, description: j.desc, first_seen: seen[j.id] || null,
        score: s ? s[0] : null, reason: s ? s[1] : "", breakdown: s ? s[2] : {},
      };
    });
  }

  // Scored first, then dealt round-robin across companies, as the local
  // server does, so one company never fills a block of the grid.
  function mosaic(list) {
    const sorted = [...list].sort((a, b) =>
      (a.score === null) - (b.score === null) || (b.score ?? 0) - (a.score ?? 0));
    const buckets = new Map();
    for (const j of sorted) {
      if (!buckets.has(j.company)) buckets.set(j.company, []);
      buckets.get(j.company).push(j);
    }
    const lists = [...buckets.values()];
    const out = [];
    for (let i = 0; out.length < sorted.length; i++) {
      for (const l of lists) if (i < l.length) out.push(l[i]);
    }
    return out;
  }

  function stats(list) {
    const scored = list.filter((j) => j.score !== null);
    return {
      checked: scored.length, total: list.length, threshold: 6,
      would_hire: scored.filter((j) => j.score >= 6).length,
      avg: scored.length ? scored.reduce((a, j) => a + j.score, 0) / scored.length / 10 : 0,
    };
  }

  // --- scoring ---------------------------------------------------------------
  async function scoreBatch(chunk) {
    const r = await fetch("/api/score", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Key": store.get(K.key) },
      body: JSON.stringify({
        provider: store.get(K.provider, "xkiro"),
        model: store.get(K.model, DEFAULT_MODEL),
        profile: store.get(K.profile),
        jobs: chunk.map((j) => ({ id: j.id, company: j.company, title: j.title,
          location: j.location || "", desc: j.desc || "" })),
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = new Error(d.detail || `scoring failed (${r.status})`);
      e.status = r.status;
      throw e;
    }
    return d;
  }

  function addTokens(n) {
    const today = new Date().toISOString().slice(0, 10);
    const t = store.get(K.tokens, { day: today, used: 0 });
    const used = (t.day === today ? t.used : 0) + (Number(n) || 0);
    store.set(K.tokens, { day: today, used });
    const cost = document.getElementById("s-cost");
    if (cost) cost.textContent = `${Math.round(used / 1000)}k tok`;
  }

  window.JobRadarSource = {
    async state() {
      const d = board || await fetchJobs();
      if (ready()) markSeen(d.jobs, false);   // a visitor who hasn't set up leaves no trace
      const list = mosaic(merged(d.jobs));
      return { jobs: list, stats: stats(list), scoring: true };
    },

    async profile() {
      const d = store.get(K.details);
      if (!d) {
        return { name: "", experience: [],
          error: "Add your API key and resume with the Key & resume button to start." };
      }
      return { ...d, scorer_profile: store.get(K.profile) || "", scorer_kind: "model" };
    },

    // Descriptions are already in the browser, so search never leaves it.
    async search(q) {
      const words = q.toLowerCase().split(/\s+/).filter(Boolean).slice(0, 6);
      if (!words.length) return null;
      return ((board && board.jobs) || []).filter((j) => {
        const hay = `${j.title} ${j.company} ${j.location} ${j.desc}`.toLowerCase();
        return words.every((w) => hay.includes(w));
      }).map((j) => j.id);
    },

    async scan(onEvent) {
      if (!ready()) {
        openSetup();
        onEvent({ type: "error", message: "add your API key and resume first" });
        return;
      }
      const t0 = Date.now();
      onEvent({ type: "fetch", company: "every job board", count: 0, error: "" });
      let d;
      try { d = await fetchJobs(); } catch (e) {
        onEvent({ type: "error", message: String(e.message || e) });
        return;
      }
      const { fresh, closed } = markSeen(d.jobs, true);
      onEvent({ type: "fetched", new: fresh, closed, total: d.jobs.length });

      const have = store.get(K.scores, {});
      const todo = d.jobs.filter((j) => !have[j.id]);
      onEvent({ type: "scoring", count: todo.length, backlog: todo.length });
      let scored = 0;
      for (let i = 0; i < todo.length; i += BATCH) {
        const chunk = todo.slice(i, i + BATCH);
        onEvent({ type: "looking", id: chunk[0].id, company: chunk[0].company,
          title: `scoring ${chunk.length} jobs` });
        let res;
        try { res = await scoreBatch(chunk); } catch (e) {
          if (e.status === 401) {
            onEvent({ type: "error", message: "the AI provider rejected your key; check it under Key & resume" });
            return;
          }
          if (e.status === 429) {
            onEvent({ type: "throttled", scored });
            break;
          }
          continue;                     // one bad batch: those jobs wait for the next scan
        }
        const scores = store.get(K.scores, {});
        for (const j of chunk) {
          const r = res.results[j.id];
          if (!r) continue;             // the model skipped it; the next scan retries
          scores[j.id] = [r.score, r.reason, r.breakdown];
          scored++;
          onEvent({ type: "scored", id: j.id, score: r.score, reason: r.reason,
            breakdown: r.breakdown, company: j.company, title: j.title });
        }
        store.set(K.scores, scores);   // saved per batch: closing the tab loses nothing
        addTokens(res.tokens);
      }
      onEvent({ type: "done", scored, elapsed: (Date.now() - t0) / 1000 });
    },
  };

  // --- the key & resume dialog --------------------------------------------------
  const el = (id) => document.getElementById(id);

  function openSetup() {
    const dlg = el("setup");
    if (!dlg || dlg.open) return;
    el("setup-provider").value = store.get(K.provider, "xkiro");
    el("setup-model").value = store.get(K.model, DEFAULT_MODEL);
    el("setup-key").value = store.get(K.key, "");
    el("setup-resume").value = "";
    el("setup-resume").required = !store.get(K.profile);
    el("setup-resume-hint").textContent = store.get(K.profile)
      ? "Resume already added. Choose a new file only to replace it." : "";
    el("setup-status").textContent = "";
    el("setup-forget").hidden = !ready();
    el("setup-cancel").hidden = !ready();
    dlg.showModal();
  }

  async function save(e) {
    e.preventDefault();
    const key = el("setup-key").value.trim();
    const provider = el("setup-provider").value;
    const model = el("setup-model").value.trim() || DEFAULT_MODEL;
    const file = el("setup-resume").files[0];
    const status = el("setup-status");
    if (!key) { status.textContent = "Paste your API key."; return; }
    if (!file && !store.get(K.profile)) { status.textContent = "Choose your resume."; return; }

    if (file) {
      if (file.size > 5 * 1024 * 1024) { status.textContent = "That file is over 5 MB."; return; }
      const btn = el("setup-save");
      btn.disabled = true;
      status.textContent = "Reading your resume… this takes about 20 seconds.";
      const fd = new FormData();
      fd.append("resume", file);
      fd.append("provider", provider);
      fd.append("model", model);
      try {
        const r = await fetch("/api/profile", { method: "POST", headers: { "X-Api-Key": key }, body: fd });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || `failed (${r.status})`);
        // A new resume means every old score is stale.
        if (d.profile !== store.get(K.profile)) store.set(K.scores, {});
        store.set(K.profile, d.profile);
        store.set(K.details, d.details);
        addTokens(d.tokens);
      } catch (err) {
        status.textContent = `Couldn't read it: ${err.message}`;
        btn.disabled = false;
        return;
      }
    }
    store.set(K.key, key);
    store.set(K.provider, provider);
    store.set(K.model, model);
    location.reload();
  }

  function wire() {
    if (!el("setup")) return;
    el("setup-form").addEventListener("submit", save);
    el("setup-cancel").addEventListener("click", () => el("setup").close());
    el("setup-forget").addEventListener("click", () => {
      if (!confirm("Remove your key, resume and scores from this browser?")) return;
      store.clear();
      location.reload();
    });
    el("settings").addEventListener("click", openSetup);
    const t = store.get(K.tokens);
    if (t && t.day === new Date().toISOString().slice(0, 10)) addTokens(0);
    if (!ready()) openSetup();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
