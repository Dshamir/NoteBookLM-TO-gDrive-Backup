/* nlm2drive UI — vanilla JS, live via Server-Sent Events */
(() => {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const state = {
    notebooks: new Map(),
    settings: {},
    counts: {},
    status: {},
    deletePhrase: "DELETE AFTER BACKUP",
    filters: { year: "all", month: "all", status: "all", topic: "all", q: "" },
    sort: { key: "created_at", dir: "desc" },
    view: localStorage.getItem("nlm2drive_view") || "table",
    flash: new Set(),
    renderQueued: false,
  };

  const COVER_GRADIENTS = [
    "linear-gradient(135deg, #4f46e5 0%, #06b6d4 100%)",
    "linear-gradient(135deg, #0e7490 0%, #10b981 100%)",
    "linear-gradient(135deg, #7c2d12 0%, #db2777 100%)",
    "linear-gradient(135deg, #1e1b4b 0%, #4338ca 100%)",
    "linear-gradient(135deg, #312e81 0%, #0d9488 100%)",
    "linear-gradient(135deg, #831843 0%, #6366f1 100%)",
    "linear-gradient(135deg, #1e293b 0%, #334155 100%)",
    "linear-gradient(135deg, #14532d 0%, #0f766e 100%)",
    "linear-gradient(135deg, #581c87 0%, #ec4899 100%)",
    "linear-gradient(135deg, #1e3a8a 0%, #38bdf8 100%)",
  ];

  function hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  }

  function getCoverGradient(id, title) {
    const idx = hashStr((id || "") + (title || "")) % COVER_GRADIENTS.length;
    return COVER_GRADIENTS[idx];
  }

  const ACTIVE = new Set(["EXPORTING", "EXPORTED", "UPLOADING", "UPLOADED", "DELETING"]);
  const LABEL = {
    NEW: "Pending", EXPORTING: "Exporting", EXPORTED: "Exported", UPLOADING: "Uploading", UPLOADED: "Verifying",
    CLOUD_VERIFIED: "In Drive", DELETING: "Deleting", DELETED: "Archived & deleted", FAILED: "Failed", SKIPPED: "Skipped",
  };

  // ------------------------------------------------------------------ helpers
  async function api(path, opts = {}) {
    const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
    if (opts.body instanceof FormData) init.body = opts.body;
    else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    if (!res.ok) throw new Error((data && (data.error || data.detail)) || `${res.status} ${res.statusText}`);
    return data;
  }

  function toast(msg, kind = "info", ms = 4200) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, ms);
  }

  function bytes(n) {
    n = Number(n || 0);
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return i === 0 ? `${n} B` : `${n.toFixed(n >= 100 ? 0 : 1)} ${u[i]}`;
  }

  function duration(sec) {
    if (sec == null) return "–";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
  }

  const fmtDate = (iso) => (iso ? iso.slice(0, 10) : "–");
  const fmtTime = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };

  function setText(sel, v) {
    const el = $(sel);
    if (!el) return;
    const s = String(v);
    if (el.textContent !== s) {
      el.textContent = s;
      if (el.classList.contains("stat-v")) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
    }
  }

  function category(r) {
    if (!r.is_owner) return "shared";
    if (r.stage === "DELETED") return "deleted";
    if (r.stage === "CLOUD_VERIFIED") return "archived";
    if (r.stage === "FAILED") return "failed";
    if (ACTIVE.has(r.stage)) return "active";
    return "pending";
  }

  function stageChip(r) {
    if (!r.is_owner) return `<span class="stage s-SHARED">Shared · never touched</span>`;
    if (r.stage === "CLOUD_VERIFIED" && !r.complete) return `<span class="stage s-KEPT" title="Export incomplete — archived but kept in NotebookLM">In Drive · kept</span>`;
    if (!r.present && r.stage !== "DELETED") return `<span class="stage s-SKIPPED">Gone from account</span>`;
    return `<span class="stage s-${esc(r.stage)}">${esc(LABEL[r.stage] || r.stage)}</span>`;
  }

  // ------------------------------------------------------------------ loading
  async function loadSummary() {
    const s = await api("/api/summary");
    state.settings = s.settings;
    state.counts = s.counts;
    state.deletePhrase = s.delete_phrase;
    renderSettingsBits();
    renderCapacity(s.limit);
    renderStatus(s.status);
    if (s.auth) renderAuth(s.auth);
    if (s.drive) renderDrive(s.drive);
  }

  async function loadNotebooks() {
    const rows = await api("/api/notebooks");
    state.notebooks = new Map(rows.map((r) => [r.id, r]));
    updateYearChips();
    updateTopicChips();
    renderTable();
  }

  async function loadEvents() {
    const evs = await api("/api/events/history?limit=200");
    $("#log").innerHTML = "";
    evs.forEach(addLog);
  }

  async function loadRuns() {
    const runs = await api("/api/runs");
    $("#history").innerHTML = runs.length ? runs.map((r) => {
      const t = r.totals || {}, o = r.options || {};
      const mode = o.e2e ? "self-test" : o.dry_run ? "dry run" : o.delete ? "archive + delete" : "archive only";
      return `<li><div class="h-top"><span>Run #${r.id} · ${esc(mode)}</span><span class="h-status ${esc(r.status)}">${esc(r.status)}</span></div>
        <div class="h-sub">${esc((r.started_at || "").replace("T", " ").slice(0, 16))} UTC${r.finished_at ? " → " + esc(r.finished_at.slice(11, 16)) : ""}</div>
        <div class="h-sub">${t.archived ?? 0} archived · ${t.deleted ?? 0} deleted · ${t.kept ?? 0} kept · ${t.failed ?? 0} failed · ${bytes(t.bytes_done)}</div></li>`;
    }).join("") : `<li class="h-sub">No runs yet.</li>`;
  }

  async function checkAuth(force = false) {
    $("#nlm-sub").textContent = "Checking session…";
    $("#nlm-dot").className = "dot warn";
    try { renderAuth(await api(`/api/auth/status${force ? "?force=true" : ""}`)); }
    catch (e) { renderAuth({ ok: false, error: e.message }); }
  }

  // ------------------------------------------------------------------ reconnect (a host helper opens Chrome)
  const reconnect = { polling: false };
  const RECONNECT_LABEL = { "#btn-reconnect": "Reconnect", "#btn-reconnect-banner": "Reconnect NotebookLM" };

  function setReconnectUI(snap, error) {
    const running = !!snap && snap.state === "running";
    const failed = !!error || (!!snap && snap.state === "failed");
    const last = running && snap.tail && snap.tail.length ? snap.tail[snap.tail.length - 1] : "";
    const msg = error || (snap && snap.state !== "idle" && snap.state !== "unavailable" ? snap.message : "");
    for (const sel of ["#reconnect-status", "#reconnect-banner-status"]) {
      const el = $(sel);
      el.textContent = msg ? (last ? `${msg} · ${last}` : msg) : "";
      el.classList.toggle("hidden", !msg);
      el.classList.toggle("err", failed);
      el.classList.toggle("busy", running);
    }
    for (const [sel, label] of Object.entries(RECONNECT_LABEL)) {
      const b = $(sel);
      b.disabled = running;
      b.textContent = running ? "Waiting for sign-in…" : label;
      b.classList.toggle("loading", running);
    }
  }

  async function pollReconnect() {
    if (reconnect.polling) return;
    reconnect.polling = true;
    try {
      for (let i = 0; i < 480; i++) {   // ~16 min; the helper enforces its own 15 min limit
        const snap = await api("/api/reconnect/status");
        setReconnectUI(snap);
        if (snap.state !== "running") {
          if (snap.state === "success") {
            toast("Signed in to NotebookLM", "ok");
            await checkAuth(true).catch(() => {});   // during a run this also nudges the paused run
          } else if (snap.state === "failed") toast(snap.message, "error", 8000);
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    } catch (e) { setReconnectUI(null, e.message); }
    finally { reconnect.polling = false; }
  }

  async function startReconnect() {
    try {
      const snap = await api("/api/reconnect", { body: {} });
      setReconnectUI(snap);
      toast(snap.already_running ? "A sign-in is already open — finish it in the browser window" : "Browser opened — sign in to Google there", "info", 6000);
      pollReconnect();
    } catch (e) {
      setReconnectUI(null, e.message);
      toast(e.message, "error", 9000);
    }
  }

  async function resumeReconnectIfRunning() {   // page reloaded in the middle of a sign-in
    try {
      const snap = await api("/api/reconnect/status");
      if (snap.state === "running") { setReconnectUI(snap); pollReconnect(); }
    } catch { /* helper status is optional */ }
  }

  async function checkDrive(force = false) {
    try { renderDrive(await api(`/api/drive/status${force ? "?force=true" : ""}`)); }
    catch (e) { renderDrive({ connected: false, error: e.message }); }
  }

  // ------------------------------------------------------------------ renderers
  function renderAuth(a) {
    const ok = !!a.ok;
    $("#nlm-dot").className = `dot ${ok ? "ok" : "bad"}`;
    $("#nlm-sub").textContent = ok
      ? (a.run_owned ? `${a.email || "Signed in"} · in use by the running archive` : `${a.email || "Signed in"} · ${a.notebooks ?? "?"} notebooks`)
      : (a.error || "Not signed in");
    $("#nlm-hint").classList.toggle("hidden", ok);
  }

  function renderDrive(d) {
    const ok = !!d.connected;
    $("#drive-dot").className = `dot ${ok ? "ok" : d.has_client ? "warn" : "bad"}`;
    $("#drive-sub").textContent = ok ? d.email : d.error ? `Error: ${d.error}` : d.has_client ? "Client uploaded — press Connect" : "Upload credentials.json first";
    $("#btn-drive-connect").classList.toggle("hidden", ok);
    $("#btn-drive-disconnect").classList.toggle("hidden", !ok);
    $("#btn-drive-connect").disabled = !d.has_client;
    $("#link-drive-folder").classList.toggle("hidden", !ok);
    $("#link-drive-folder").href = "https://drive.google.com/drive/my-drive";
  }

  function renderSettingsBits() {
    const s = state.settings;
    $("#target-path").textContent = "G:\\My Drive\\" + (s.drive_archive_path || "").replaceAll("/", "\\");
    $("#opt-delete").checked = !!s.delete_after_verify;
    $("#opt-media").checked = !!s.include_media;
    $("#confirm-phrase").textContent = state.deletePhrase;
    $("#confirm-banner").classList.toggle("hidden", !(s.delete_after_verify && !s.delete_confirmed));
    $("#delete-state-text").textContent = s.delete_confirmed ? "Deletion after verification: CONFIRMED" : "Deletion: not confirmed yet";
    $("#btn-revoke").disabled = !s.delete_confirmed;
    $("#btn-start-text").textContent = s.delete_after_verify ? "Archive & delete" : "Archive only";
  }

  // ------------------------------------------------------------------ desktop notifications
  const notif = {
    enabled: localStorage.getItem("nlm2drive_notifications") === "true",
    lastFinished: null,
    authAlerted: false,
  };

  function updateNotifButton() {
    const btn = $("#btn-toggle-notifications");
    if (!btn) return;
    const hasPerm = window.Notification && Notification.permission === "granted";
    btn.classList.toggle("active", notif.enabled && hasPerm);
    setText("#notif-label", notif.enabled && hasPerm ? "Alerts On" : "Alerts");
  }

  async function toggleNotifications() {
    if (!window.Notification) {
      toast("Desktop notifications are not supported in this browser", "warn");
      return;
    }
    if (Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") {
        toast("Notification permission denied", "warn");
        return;
      }
    }
    notif.enabled = !notif.enabled;
    localStorage.setItem("nlm2drive_notifications", notif.enabled ? "true" : "false");
    updateNotifButton();
    toast(notif.enabled ? "Desktop notifications enabled" : "Desktop notifications disabled", "info");
  }

  function notifyUser(title, body) {
    if (notif.enabled && window.Notification && Notification.permission === "granted") {
      try {
        new Notification(`nlm2drive · ${title}`, { body, icon: "/favicon.ico" });
      } catch { /* ignored */ }
    }
  }

  function renderCapacity(limit = 500) {
    const c = state.counts || {};
    const present = c.present || 0;
    const lim = limit || 500;
    const ratio = Math.min(1, present / lim);
    const fg = $("#ring-fg");
    if (fg) {
      fg.style.strokeDashoffset = String(314.16 * (1 - ratio));
      fg.classList.toggle("full", ratio >= 0.98);
    }
    setText("#cap-count", present);
    setText("#cap-limit", `/ ${lim}`);
    setText("#cap-2024", c.y2024 || 0);
    setText("#cap-2025", c.y2025 || 0);
    setText("#cap-freed", (c.by_stage || {}).DELETED || 0);
    $("#cap-sub").textContent = present ? `${Math.max(0, lim - present)} slots free · ${c.selected || 0} selected` : "Refresh the list to count";
    setText("#sel-count", `${c.selected || 0} selected`);

    // Topbar Quota Gauge
    const pct = Math.round(ratio * 100);
    setText("#quota-text", `${present} / ${lim}`);
    setText("#quota-badge", `${pct}%`);
    const qfill = $("#quota-bar-fill");
    const qbadge = $("#quota-badge");
    if (qfill && qbadge) {
      qfill.style.width = `${pct}%`;
      const level = present >= 475 ? "red" : present >= 400 ? "amber" : "green";
      qfill.className = `quota-fill ${level}`;
      qbadge.className = `quota-pct-badge ${level}`;
    }
  }

  function renderStatus(st) {
    if (!st) return;
    const prev = state.status.state;
    state.status = st;
    const running = st.state === "running" || st.state === "paused" || st.state === "stopping";
    const pill = $("#run-pill");
    $("#run-pill").className = `pill pill-${st.needs_auth ? "auth" : st.state}`;
    $("#run-pill-text").textContent = st.needs_auth ? "Sign-in needed"
      : { idle: "Idle", running: st.dry_run ? "Dry run" : "Running", paused: "Paused", stopping: "Stopping" }[st.state] || st.state;
    $("#run-panel").classList.toggle("is-running", st.state === "running");

    $("#btn-start").classList.toggle("hidden", running);
    $("#btn-dry-run").classList.toggle("hidden", running);
    $("#btn-e2e").disabled = running;
    $("#btn-pause").classList.toggle("hidden", st.state !== "running");
    $("#btn-resume").classList.toggle("hidden", st.state !== "paused");
    $("#btn-stop").classList.toggle("hidden", !running);

    const total = st.total || 0, done = st.done || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    $("#prog-fill").style.width = `${pct}%`;
    setText("#prog-pct", `${pct}%`);
    $("#prog-label").textContent = running
      ? `${st.dry_run ? "Dry run" : st.delete ? "Archiving + deleting" : "Archiving"} · ${done} of ${total}`
      : st.finished ? `Last run ${st.finished} · ${done} of ${total}` : "Ready";
    setText("#st-done", `${done} / ${total}`);
    setText("#st-archived", st.archived || 0);
    setText("#st-deleted", st.deleted || 0);
    setText("#st-kept", st.kept || 0);
    setText("#st-failed", st.failed || 0);
    setText("#st-bytes", bytes(st.bytes_done));
    setText("#st-eta", running ? duration(st.eta_sec) : "–");

    const cur = st.current;
    // Never show a 'current notebook' spinner while waiting for a sign-in (nothing is being processed).
    $("#current").classList.toggle("hidden", !cur || !running || !!st.needs_auth);
    if (cur) {
      $("#cur-title").textContent = cur.title || cur.id;
      $("#cur-detail").textContent = cur.detail || "";
      const order = ["EXPORTING", "UPLOADING", "CLOUD_VERIFIED", "DELETED"];
      const idx = { NEW: 0, EXPORTING: 0, EXPORTED: 1, UPLOADING: 1, UPLOADED: 2, CLOUD_VERIFIED: 3, DELETING: 3, DELETED: 4, FAILED: -1 }[cur.stage] ?? 0;
      $$("#stepper li").forEach((li, i) => {
        li.classList.toggle("done", i < idx);
        li.classList.toggle("active", i === idx && order[i] !== undefined);
      });
      const frac = cur.bytes_total ? Math.min(1, (cur.bytes || 0) / cur.bytes_total) : (idx >= 2 ? 1 : 0);
      $("#cur-fill").style.width = `${Math.round(frac * 100)}%`;
      if (cur.stage === "UPLOADING" && cur.bytes_total) $("#cur-detail").textContent = `${bytes(cur.bytes)} of ${bytes(cur.bytes_total)} uploaded`;
    }
    const msg = st.message;
    $("#auth-banner").classList.toggle("hidden", !st.needs_auth);
    $("#auth-banner-detail").textContent = st.needs_auth && msg && msg.includes("still not signed in") ? msg.slice(msg.indexOf("(still")) : (st.needs_auth && msg && msg.startsWith("Re-checking") ? msg : "");
    $("#run-message").classList.toggle("hidden", !msg || !!st.needs_auth);
    if (msg && !st.needs_auth) $("#run-message").textContent = msg;
    if (!!st.needs_auth !== !!state.needsAuth) {
      state.needsAuth = !!st.needs_auth;
      if (st.needs_auth) renderAuth({ ok: false, error: "Session expired — the run is waiting for a sign-in" });
      else checkAuth();
    }

    if (st.needs_auth && !notif.authAlerted) {
      notif.authAlerted = true;
      notifyUser("Sign-in Needed", "NotebookLM session expired. Press Reconnect to resume.");
    } else if (!st.needs_auth) {
      notif.authAlerted = false;
    }
    if (st.finished && notif.lastFinished !== st.finished) {
      notif.lastFinished = st.finished;
      notifyUser("Run Complete", `Archive run ${st.finished}: ${st.archived || 0} archived, ${st.deleted || 0} deleted.`);
    }

    if (prev && prev !== "idle" && st.state === "idle") { loadRuns(); loadSummary(); }
  }

  function updateYearChips() {
    const yearCounts = new Map();
    let total = 0;
    for (const r of state.notebooks.values()) {
      if (r.present || r.stage === "DELETED" || r.stage === "CLOUD_VERIFIED") {
        total++;
        const y = r.year ? String(r.year) : "other";
        yearCounts.set(y, (yearCounts.get(y) || 0) + 1);
      }
    }
    const years = Array.from(yearCounts.keys()).sort((a, b) => {
      if (a === "other") return 1;
      if (b === "other") return -1;
      return Number(b) - Number(a);
    });
    const container = $("#year-chips");
    if (!container) return;
    const activeYear = state.filters.year || "all";
    let html = `<button class="chip ${activeYear === "all" ? "active" : ""}" data-year="all">All years <small>(${total})</small></button>`;
    for (const y of years) {
      const label = y === "other" ? "Other" : y;
      html += `<button class="chip ${activeYear === y ? "active" : ""}" data-year="${esc(y)}">${esc(label)} <small>(${yearCounts.get(y)})</small></button>`;
    }
    container.innerHTML = html;
    $$("#year-chips .chip").forEach((c) => {
      c.onclick = () => {
        $$("#year-chips .chip").forEach((x) => x.classList.remove("active"));
        c.classList.add("active");
        state.filters.year = c.dataset.year;
        renderTable();
      };
    });
  }

  function updateTopicChips() {
    const counts = new Map();
    const stopWords = new Set(["and", "the", "for", "with", "from", "this", "that", "into", "about", "project", "overview", "notes", "summary", "draft", "part"]);
    for (const r of state.notebooks.values()) {
      if (!r.title) continue;
      const words = r.title.match(/[A-Za-z]{3,}/g) || [];
      for (const w of words) {
        const lw = w.toLowerCase();
        if (!stopWords.has(lw)) {
          counts.set(lw, (counts.get(lw) || 0) + 1);
        }
      }
    }
    const sorted = Array.from(counts.entries())
      .filter(([, n]) => n >= 3)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10);

    const container = $("#topic-chips");
    if (!container) return;
    const activeTopic = state.filters.topic || "all";
    let html = `<button class="chip ${activeTopic === "all" ? "active" : ""}" data-topic="all">All topics</button>`;
    for (const [w, n] of sorted) {
      const capitalized = w.charAt(0).toUpperCase() + w.slice(1);
      html += `<button class="chip ${activeTopic === w ? "active" : ""}" data-topic="${esc(w)}">${esc(capitalized)} <small>(${n})</small></button>`;
    }
    container.innerHTML = html;
    $$("#topic-chips .chip").forEach((c) => {
      c.onclick = () => {
        $$("#topic-chips .chip").forEach((x) => x.classList.remove("active"));
        c.classList.add("active");
        state.filters.topic = c.dataset.topic;
        renderTable();
      };
    });
  }

  function filtered() {
    const { year, month, status, topic, q } = state.filters;
    const qq = q.trim().toLowerCase();
    let rows = Array.from(state.notebooks.values()).filter((r) => r.present || r.stage === "DELETED" || r.stage === "CLOUD_VERIFIED");
    if (year !== "all") {
      if (year === "other") rows = rows.filter((r) => !r.year || (r.year !== 2024 && r.year !== 2025 && r.year !== 2026));
      else rows = rows.filter((r) => String(r.year) === year);
    }
    if (month && month !== "all") {
      rows = rows.filter((r) => (r.created_at || "").slice(5, 7) === month);
    }
    if (status !== "all") rows = rows.filter((r) => category(r) === status);
    if (topic && topic !== "all") {
      rows = rows.filter((r) => (r.title || "").toLowerCase().includes(topic.toLowerCase()));
    }
    if (qq) {
      rows = rows.filter((r) => (r.title || "").toLowerCase().includes(qq) || (r.id || "").toLowerCase().includes(qq));
    }
    const { key, dir } = state.sort;
    const m = dir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      const x = a[key] ?? "", y = b[key] ?? "";
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * m;
    });
    return rows;
  }

  function rowHtml(r) {
    const locked = !r.is_owner || r.stage === "DELETED" || !r.present;
    const cls = [r.selected && !locked ? "sel" : "", locked ? "locked" : "", state.flash.has(r.id) ? "flash" : ""].join(" ");
    const actions = [];
    actions.push(`<a class="icon-btn" href="https://notebooklm.google.com/notebook/${esc(r.id)}" target="_blank" rel="noopener" title="Open in NotebookLM"><svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/></svg></a>`);
    if (r.drive_folder_id) actions.push(`<a class="icon-btn" id="open-${esc(r.id)}" href="https://drive.google.com/drive/folders/${esc(r.drive_folder_id)}" target="_blank" rel="noopener" title="Open archive folder in Drive"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>`);
    actions.push(`<button class="icon-btn" data-view="${esc(r.id)}" title="Inspect details"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg></button>`);
    if (r.stage === "FAILED" && r.is_owner) actions.push(`<button class="icon-btn" data-retry="${esc(r.id)}" id="retry-${esc(r.id)}" title="Retry in next run"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg></button>`);
    const err = r.error && r.stage !== "DELETED" ? `<div class="nb-err" title="${esc(r.error)}">${esc(r.error)}</div>` : "";
    return `<tr class="${cls}" data-id="${esc(r.id)}">
      <td><input type="checkbox" data-sel="${esc(r.id)}" id="sel-${esc(r.id)}" ${r.selected && !locked ? "checked" : ""} ${locked ? "disabled" : ""} aria-label="Select ${esc(r.title)}"></td>
      <td><div class="nb-title"><span class="nb-emoji" role="button" data-view="${esc(r.id)}" style="cursor:pointer;" title="Inspect details">${esc(r.emoji || "📓")}</span><div style="min-width:0"><div class="nb-name" role="button" data-view="${esc(r.id)}" style="cursor:pointer;" title="${esc(r.title)}">${esc(r.title)}</div>${err}</div></div></td>
      <td class="mono">${fmtDate(r.created_at)}</td>
      <td class="num">${r.sources_count ?? "–"}</td>
      <td>${stageChip(r)}</td>
      <td class="num mono">${r.bytes ? bytes(r.bytes) : "–"}</td>
      <td><div class="row-actions">${actions.join("")}</div></td></tr>`;
  }

  function cardHtml(r) {
    const locked = !r.is_owner || r.stage === "DELETED" || !r.present;
    const cls = [
      "nb-card",
      r.selected && !locked ? "sel" : "",
      locked ? "locked" : "",
      state.flash.has(r.id) ? "flash" : "",
    ].filter(Boolean).join(" ");

    const grad = getCoverGradient(r.id, r.title);
    const err = r.error && r.stage !== "DELETED" ? `<div class="card-error" title="${esc(r.error)}">${esc(r.error)}</div>` : "";
    const driveLink = r.drive_folder_id ? `<a class="card-btn" href="https://drive.google.com/drive/folders/${esc(r.drive_folder_id)}" target="_blank" rel="noopener" title="Open archive folder in Google Drive"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg><span>Drive</span></a>` : "";
    const nlmLink = `<a class="card-btn" href="https://notebooklm.google.com/notebook/${esc(r.id)}" target="_blank" rel="noopener" title="Open notebook in NotebookLM"><svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/></svg><span>Live</span></a>`;
    const retryBtn = r.stage === "FAILED" && r.is_owner ? `<button class="card-btn" data-retry="${esc(r.id)}" title="Retry in next run"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg><span>Retry</span></button>` : "";

    return `<article class="${cls}" data-id="${esc(r.id)}">
      <div class="card-thumb" style="background-image: ${grad}">
        <div class="card-check">
          <input type="checkbox" data-sel="${esc(r.id)}" id="grid-sel-${esc(r.id)}" ${r.selected && !locked ? "checked" : ""} ${locked ? "disabled" : ""} aria-label="Select ${esc(r.title)}">
        </div>
        <div class="card-status-badge">${stageChip(r)}</div>
        <div class="card-emoji-hero" role="button" data-view="${esc(r.id)}" style="cursor:pointer;" title="Inspect details">${esc(r.emoji || "📓")}</div>
        <span class="card-year-pill">${r.year || fmtDate(r.created_at).slice(0, 4)}</span>
        <span class="card-sources-pill" title="${r.sources_count ?? 0} sources">📚 ${r.sources_count ?? 0}</span>
      </div>
      <div class="card-body">
        <div class="card-title" role="button" data-view="${esc(r.id)}" title="${esc(r.title)}">${esc(r.title)}</div>
        ${err}
        <div class="card-meta-row">
          <span class="mono">${fmtDate(r.created_at)}</span>
          <span class="mono">${r.bytes ? bytes(r.bytes) : (r.files ? `${r.files} files` : "–")}</span>
        </div>
      </div>
      <div class="card-footer">
        <button class="card-btn" data-view="${esc(r.id)}" title="Inspect notebook details">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
          <span>Inspect</span>
        </button>
        <div class="card-footer-actions">
          ${nlmLink}
          ${driveLink}
          ${retryBtn}
        </div>
      </div>
    </article>`;
  }

  function openNotebookModal(r) {
    if (!r) return;
    const body = $("#modal-nb-body");
    const driveLink = r.drive_folder_id
      ? `<a class="btn btn-sm btn-primary" href="https://drive.google.com/drive/folders/${esc(r.drive_folder_id)}" target="_blank" rel="noopener">Open in Google Drive ↗</a>`
      : "";
    const nlmLink = `<a class="btn btn-sm" href="https://notebooklm.google.com/notebook/${esc(r.id)}" target="_blank" rel="noopener">Open in NotebookLM ↗</a>`;
    const st = r.stats || {};
    const artCount = st.artifacts ? Object.keys(st.artifacts).length : 0;
    const errBlock = r.error ? `<div class="card-error" style="margin-top:10px;">${esc(r.error)}</div>` : "";

    const canRestore = !r.present || r.stage === "DELETED";
    const restoreBtn = canRestore
      ? `<button class="btn btn-sm btn-restore" id="btn-nb-restore" title="Reconstitute this notebook into NotebookLM with backed-up sources">🔄 Restore to NotebookLM</button>`
      : "";

    body.innerHTML = `
      <div class="nb-detail-head">
        <div class="nb-detail-emoji">${esc(r.emoji || "📓")}</div>
        <div class="nb-detail-info">
          <div class="nb-detail-title">${esc(r.title)}</div>
          <div class="nb-detail-id">
            <span>ID: ${esc(r.id)}</span>
            <button class="icon-btn" style="width:20px;height:20px;" id="btn-copy-id" title="Copy ID"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
          </div>
          <div class="nb-detail-actions">
            ${nlmLink}
            ${driveLink}
            ${restoreBtn}
          </div>
        </div>
      </div>

      ${errBlock}

      <div class="nb-detail-grid">
        <div class="nb-detail-section">
          <h4>Overview</h4>
          <div class="detail-row"><span class="detail-k">Status</span><span class="detail-v">${stageChip(r)}</span></div>
          <div class="detail-row"><span class="detail-k">Created</span><span class="detail-v mono">${r.created_at ? esc(r.created_at.slice(0, 16).replace("T", " ")) + " UTC" : "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Last Seen</span><span class="detail-v mono">${r.last_seen_at ? esc(r.last_seen_at.slice(0, 16).replace("T", " ")) + " UTC" : "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Ownership</span><span class="detail-v">${r.is_owner ? "Owner" : "Shared"}</span></div>
          <div class="detail-row"><span class="detail-k">Sources Count</span><span class="detail-v">${r.sources_count ?? "–"}</span></div>
        </div>

        <div class="nb-detail-section">
          <h4>Archive &amp; Drive Storage</h4>
          <div class="detail-row"><span class="detail-k">Drive Verified</span><span class="detail-v">${r.complete ? "Yes (MD5 verified)" : (r.drive_folder_id ? "Pending check" : "Not archived")}</span></div>
          <div class="detail-row"><span class="detail-k">Total Size</span><span class="detail-v mono">${r.bytes ? bytes(r.bytes) : "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Files Archived</span><span class="detail-v mono">${r.files || "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Folder Name</span><span class="detail-v mono" style="font-size:11px;">${r.folder ? esc(r.folder) : "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Attempts</span><span class="detail-v">${r.attempts || 0}</span></div>
        </div>

        <div class="nb-detail-section" style="grid-column: span 2;">
          <h4>Content &amp; Studio Artifacts</h4>
          <div class="detail-row"><span class="detail-k">Sources Exported</span><span class="detail-v">${st.sources ?? "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Notes Exported</span><span class="detail-v">${st.notes ?? "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Mind Maps</span><span class="detail-v">${st.mind_maps ?? "–"}</span></div>
          <div class="detail-row"><span class="detail-k">Studio Artifacts</span><span class="detail-v">${artCount} items</span></div>
          <div class="detail-row"><span class="detail-k">Chat Turns</span><span class="detail-v">${st.chat_turns ?? "–"}</span></div>
        </div>

        <div id="nb-content-preview-box" style="grid-column: span 2; display: none;"></div>
      </div>

      <div class="modal-actions">
        <button type="button" class="btn btn-primary" id="btn-nb-detail-close">Close</button>
      </div>
    `;

    $("#btn-nb-detail-close").onclick = () => $("#modal-nb-detail").close();
    const copyBtn = $("#btn-copy-id");
    if (copyBtn) {
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(r.id);
        toast("Notebook ID copied", "ok", 2000);
      };
    }

    const restBtn = $("#btn-nb-restore");
    if (restBtn) {
      restBtn.onclick = () => withBusy(restBtn, async () => {
        toast("Restoring notebook to NotebookLM…", "info");
        const res = await api(`/api/notebooks/${r.id}/restore`, { body: {} });
        if (res.already_present) {
          toast("Notebook is already present in NotebookLM", "info");
        } else {
          toast(`Restored as "${res.title}" with ${res.sources_restored || 0} sources`, "ok", 6000);
          await loadNotebooks();
          $("#modal-nb-detail").close();
        }
      });
    }

    // Async load content preview (sources, audio)
    api(`/api/notebooks/${r.id}/content`).then((data) => {
      const box = $("#nb-content-preview-box");
      if (!box) return;
      let inner = "";
      if (data.audio_file) {
        inner += `
          <div class="nb-detail-section audio-section">
            <h4>🎧 Audio Overview / Podcast</h4>
            <audio controls class="audio-player" src="/api/notebooks/${r.id}/audio" preload="metadata"></audio>
          </div>
        `;
      }
      if (data.sources && data.sources.length) {
        inner += `
          <div class="nb-detail-section">
            <h4>Backed-up Sources (${data.sources.length})</h4>
            <ul class="content-item-list">
              ${data.sources.map((s) => `<li class="content-item"><span>${esc(s.name)}</span><span class="mono">${bytes(s.size)}</span></li>`).join("")}
            </ul>
          </div>
        `;
      }
      if (inner) {
        box.innerHTML = inner;
        box.style.display = "grid";
        box.style.gap = "12px";
      }
    }).catch(() => {});

    $("#modal-nb-detail").showModal();
  }

  function renderTable() {
    state.renderQueued = false;
    const rows = filtered();
    const isGrid = state.view === "grid";

    $("#nb-table").classList.toggle("hidden", isGrid);
    $("#nb-grid").classList.toggle("hidden", !isGrid);
    $("#btn-view-table").classList.toggle("active", !isGrid);
    $("#btn-view-grid").classList.toggle("active", isGrid);

    if (isGrid) {
      $("#nb-grid").innerHTML = rows.map(cardHtml).join("");
    } else {
      $("#nb-body").innerHTML = rows.map(rowHtml).join("");
    }

    $("#nb-count").textContent = rows.length;
    $("#nb-empty").classList.toggle("hidden", rows.length > 0);
    const selectable = rows.filter((r) => r.is_owner && r.present && r.stage !== "DELETED");
    const all = $("#chk-all");
    all.checked = selectable.length > 0 && selectable.every((r) => r.selected);
    all.indeterminate = !all.checked && selectable.some((r) => r.selected);
    state.flash.clear();
    recount();
  }

  function queueRender() {
    if (state.renderQueued) return;
    state.renderQueued = true;
    requestAnimationFrame(renderTable);
  }

  function recount() {
    let sel = 0, present = 0, y24 = 0, y25 = 0, y26 = 0, del = 0;
    for (const r of state.notebooks.values()) {
      if (r.present) {
        present++;
        if (r.selected) sel++;
        if (r.year === 2024) y24++;
        if (r.year === 2025) y25++;
        if (r.year === 2026) y26++;
      }
      if (r.stage === "DELETED") del++;
    }
    state.counts = { ...state.counts, present, selected: sel, y2024: y24, y2025: y25, y2026: y26, by_stage: { ...(state.counts.by_stage || {}), DELETED: del } };
    renderCapacity(state.settings.notebook_limit || 500);
  }

  function addLog(ev) {
    const li = document.createElement("li");
    li.className = `l-${ev.level}`;
    const nb = ev.notebook_id ? state.notebooks.get(ev.notebook_id) : null;
    const ref = nb ? `<span class="nbref">${esc((nb.title || "").slice(0, 48))}</span> · ` : "";
    li.innerHTML = `<time>${fmtTime(ev.ts)}</time><span class="msg">${ref}${esc(ev.message)}</span>`;
    const log = $("#log");
    log.appendChild(li);
    while (log.children.length > 400) log.firstChild.remove();
    if ($("#opt-autoscroll").checked) log.scrollTop = log.scrollHeight;
  }

  // ------------------------------------------------------------------ live events
  function connectSSE() {
    const es = new EventSource("/api/events");
    es.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      if (ev.type === "status" && ev.status) renderStatus(ev.status);
      else if (ev.type === "event") addLog(ev.event);
      else if (ev.type === "notebook" && ev.row) { state.notebooks.set(ev.row.id, ev.row); state.flash.add(ev.row.id); queueRender(); }
      else if (ev.type === "refresh") loadNotebooks();
      else if (ev.type === "run_finished") { loadRuns(); loadSummary(); loadNotebooks(); toast(`Run ${ev.status}`, ev.status === "completed" ? "ok" : "warn"); }
    };
    es.onerror = () => { es.close(); setTimeout(connectSSE, 2500); };
  }

  // ------------------------------------------------------------------ actions
  async function withBusy(btn, fn) {
    btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, "error", 6500); } finally { btn.disabled = false; }
  }

  async function select(ids, selected) {
    ids.forEach((id) => { const r = state.notebooks.get(id); if (r && r.is_owner) r.selected = selected ? 1 : 0; });
    queueRender();
    try { await api("/api/notebooks/select", { body: { ids, selected } }); }
    catch (e) { toast(e.message, "error"); loadNotebooks(); }
  }

  async function saveSetting(patch) {
    state.settings = await api("/api/settings", { body: patch });
    renderSettingsBits();
  }

  function startRun(dry) {
    const s = state.settings;
    const del = !dry && $("#opt-delete").checked;
    if (del && !s.delete_confirmed) {
      $("#confirm-banner").classList.remove("hidden");
      $("#input-confirm").focus();
      toast(`Type "${state.deletePhrase}" to allow automatic deletion — or switch deletion off for an archive-only run.`, "warn", 7000);
      return;
    }
    const limit = parseInt($("#opt-limit").value, 10);
    return withBusy(dry ? $("#btn-dry-run") : $("#btn-start"), async () => {
      const r = await api("/api/run", { body: { dry_run: dry, delete: del, limit: limit > 0 ? limit : null } });
      toast(`Run #${r.run_id} started${dry ? " (dry run)" : ""}`, "ok");
    });
  }

  function bind() {
    $("#btn-refresh").onclick = (e) => withBusy(e.currentTarget, async () => {
      const r = await api("/api/refresh", { body: {} });
      toast(`Found ${r.count} notebooks`, "ok");
      await Promise.all([loadNotebooks(), loadSummary()]);
    });
    $("#btn-start").onclick = () => startRun(false);
    $("#btn-dry-run").onclick = () => startRun(true);
    $("#btn-pause").onclick = () => api("/api/pause", { body: {} }).then(renderStatus);
    $("#btn-resume").onclick = () => api("/api/resume", { body: {} }).then(renderStatus);
    $("#btn-auth-recheck").onclick = (e) => withBusy(e.currentTarget, async () => { renderStatus(await api("/api/resume", { body: {} })); toast("Re-checking the NotebookLM session…", "info"); });
    $("#btn-stop").onclick = () => { if (confirm("Stop after the current step? Partially processed notebooks restart next time.")) api("/api/stop", { body: {} }).then(renderStatus); };
    $("#btn-e2e").onclick = (e) => {
      if (!confirm("Self-test: create a throwaway notebook, archive it to Drive, verify, then delete it. Continue?")) return;
      withBusy(e.currentTarget, async () => { await api("/api/test-e2e", { body: {} }); toast("Self-test started — watch the activity log", "ok"); });
    };

    $("#opt-delete").onchange = (e) => saveSetting({ delete_after_verify: e.target.checked }).catch((er) => toast(er.message, "error"));
    $("#opt-media").onchange = (e) => saveSetting({ include_media: e.target.checked }).catch((er) => toast(er.message, "error"));

    $("#btn-confirm").onclick = (e) => withBusy(e.currentTarget, async () => {
      state.settings = await api("/api/confirm-delete", { body: { phrase: $("#input-confirm").value } });
      $("#input-confirm").value = "";
      renderSettingsBits();
      toast("Deletion after verification confirmed", "ok");
    });
    $("#input-confirm").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btn-confirm").click(); });

    $("#btn-nlm-check").onclick = (e) => withBusy(e.currentTarget, () => checkAuth(true));
    $("#btn-reconnect").onclick = startReconnect;
    $("#btn-reconnect-banner").onclick = startReconnect;
    $("#input-cookie-import").onchange = async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try { renderAuth(await api("/api/auth/import", { body: fd })); toast("Cookies imported", "ok"); }
      catch (er) { toast(er.message, "error"); }
      e.target.value = "";
    };
    $("#input-cred-upload").onchange = async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try { renderDrive(await api("/api/drive/credentials", { body: fd })); toast("credentials.json saved — now press Connect", "ok"); }
      catch (er) { toast(er.message, "error"); }
      e.target.value = "";
    };
    $("#btn-drive-connect").onclick = (e) => withBusy(e.currentTarget, async () => { window.location.href = (await api("/api/drive/connect")).url; });
    $("#btn-drive-disconnect").onclick = (e) => withBusy(e.currentTarget, async () => { renderDrive(await api("/api/drive/disconnect", { body: {} })); });
    $("#link-drive-guide").onclick = (e) => { e.preventDefault(); $("#modal-guide").showModal(); };
    $("#btn-guide-close").onclick = () => $("#modal-guide").close();

    // view layout toggle
    $("#btn-view-table").onclick = () => {
      state.view = "table";
      localStorage.setItem("nlm2drive_view", "table");
      renderTable();
    };
    $("#btn-view-grid").onclick = () => {
      state.view = "grid";
      localStorage.setItem("nlm2drive_view", "grid");
      renderTable();
    };

    // month selector
    $("#sel-month").onchange = (e) => {
      state.filters.month = e.target.value;
      renderTable();
    };

    // date cutoff selector
    $("#btn-sel-cutoff").onclick = async () => {
      const val = $("#input-cutoff").value;
      if (!val) { toast("Choose a cutoff date first", "warn"); return; }
      try {
        await api("/api/notebooks/select-cutoff", { body: { before: val, only: true } });
        await loadNotebooks();
        toast(`Selected notebooks created on or before ${val}`, "ok");
      } catch (e) { toast(e.message, "error"); }
    };

    // table & grid events
    const handleViewClick = (e) => {
      const el = e.target.closest("[data-view]");
      if (el) {
        const nb = state.notebooks.get(el.dataset.view);
        if (nb) openNotebookModal(nb);
      }
    };
    $("#nb-body").addEventListener("click", handleViewClick);
    $("#nb-grid").addEventListener("click", handleViewClick);

    $("#nb-body").addEventListener("change", (e) => { const id = e.target.dataset.sel; if (id) select([id], e.target.checked); });
    $("#nb-body").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-retry]");
      if (!b) return;
      try { await api(`/api/notebooks/${encodeURIComponent(b.dataset.retry)}/retry`, { body: {} }); await loadNotebooks(); toast("Queued for the next run", "ok"); }
      catch (er) { toast(er.message, "error"); }
    });

    $("#nb-grid").addEventListener("change", (e) => { const id = e.target.dataset.sel; if (id) select([id], e.target.checked); });
    $("#nb-grid").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-retry]");
      if (!b) return;
      try { await api(`/api/notebooks/${encodeURIComponent(b.dataset.retry)}/retry`, { body: {} }); await loadNotebooks(); toast("Queued for the next run", "ok"); }
      catch (er) { toast(er.message, "error"); }
    });

    $("#btn-sel-visible").onclick = () => select(filtered().filter((r) => r.is_owner && r.present && r.stage !== "DELETED").map((r) => r.id), true);
    $("#btn-unsel-visible").onclick = () => select(filtered().map((r) => r.id), false);
    const selFiltBtn = $("#btn-sel-filtered");
    if (selFiltBtn) {
      selFiltBtn.onclick = async () => {
        const rows = filtered().filter((r) => r.is_owner && r.present && r.stage !== "DELETED");
        if (!rows.length) { toast("No selectable notebooks in current filter", "warn"); return; }
        await select(rows.map((r) => r.id), true);
        toast(`Selected ${rows.length} filtered notebooks`, "ok");
      };
    }
    const notifBtn = $("#btn-toggle-notifications");
    if (notifBtn) notifBtn.onclick = toggleNotifications;
    const auditBtn = $("#btn-audit-archive");
    if (auditBtn) {
      auditBtn.onclick = (e) => withBusy(e.currentTarget, async () => {
        toast("Auditing Drive archive integrity…", "info");
        const res = await api("/api/audit", { body: {} });
        toast(`Audit complete: ${res.verified}/${res.total} intact in Drive (${res.missing} missing)`, res.missing ? "warn" : "ok", 7000);
      });
    }
    $$("#preset-chips .chip-preset").forEach((btn) => {
      btn.onclick = async () => {
        const preset = btn.dataset.preset;
        try {
          const res = await api("/api/notebooks/select-preset", { body: { preset, only: true } });
          await loadNotebooks();
          toast(`Selected ${res.count} notebooks for preset "${btn.textContent.trim()}"`, "ok");
        } catch (e) { toast(e.message, "error"); }
      };
    });
    $("#btn-sel-years").onclick = async () => { await api("/api/notebooks/select-years", { body: { years: state.settings.years || [2024, 2025], only: true } }); await loadNotebooks(); toast("Selected all owned 2024 + 2025 notebooks", "ok"); };
    $$("#year-chips .chip").forEach((c) => c.onclick = () => { $$("#year-chips .chip").forEach((x) => x.classList.remove("active")); c.classList.add("active"); state.filters.year = c.dataset.year; renderTable(); });
    $$("#status-chips .chip").forEach((c) => c.onclick = () => { $$("#status-chips .chip").forEach((x) => x.classList.remove("active")); c.classList.add("active"); state.filters.status = c.dataset.status; renderTable(); });
    let t; $("#input-search").oninput = (e) => { clearTimeout(t); t = setTimeout(() => { state.filters.q = e.target.value; renderTable(); }, 120); };
    $$(".nb-table th[data-sort]").forEach((th) => th.onclick = () => {
      const key = th.dataset.sort;
      state.sort = { key, dir: state.sort.key === key && state.sort.dir === "desc" ? "asc" : "desc" };
      $$(".nb-table th").forEach((x) => x.classList.remove("sorted", "asc", "desc"));
      th.classList.add("sorted", state.sort.dir);
      renderTable();
    });

    // settings modal
    $("#btn-settings").onclick = () => {
      const s = state.settings;
      $("#set-path").value = s.drive_archive_path || "";
      $("#set-years").value = (s.years || []).join(", ");
      $("#set-conc").value = s.upload_concurrency;
      $("#set-delay").value = s.inter_notebook_delay_sec;
      $("#set-chat").checked = !!s.include_chat;
      $("#set-keep").checked = !!s.keep_staging;
      $("#modal-settings").showModal();
    };
    $("#btn-settings-cancel").onclick = () => $("#modal-settings").close();
    $("#btn-settings-save").onclick = (e) => withBusy(e.currentTarget, async () => {
      const years = $("#set-years").value.split(/[,\s]+/).map((x) => parseInt(x, 10)).filter((x) => x > 2000);
      await saveSetting({
        drive_archive_path: $("#set-path").value, years,
        upload_concurrency: parseInt($("#set-conc").value, 10) || 4,
        inter_notebook_delay_sec: parseFloat($("#set-delay").value) || 0,
        include_chat: $("#set-chat").checked, keep_staging: $("#set-keep").checked,
      });
      $("#modal-settings").close();
      toast("Settings saved", "ok");
    });
    $("#btn-revoke").onclick = (e) => withBusy(e.currentTarget, async () => {
      state.settings = await api("/api/revoke-delete", { body: {} });
      renderSettingsBits();
      toast("Deletion confirmation revoked", "warn");
    });
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    bind();
    const qs = new URLSearchParams(location.search);
    if (qs.get("drive") === "connected") toast("Google Drive connected", "ok");
    if (qs.get("drive_error")) toast(`Drive connection failed: ${qs.get("drive_error")}`, "error", 8000);
    if (qs.toString()) history.replaceState(null, "", "/");
    try {
      await loadSummary();
      await Promise.all([loadNotebooks(), loadRuns()]);
      await loadEvents();
    } catch (e) { toast(`Backend unreachable: ${e.message}`, "error", 8000); }
    connectSSE();
    checkAuth();
    resumeReconnectIfRunning();
    checkDrive();
    updateNotifButton();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
