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
    filters: { year: "all", status: "all", q: "" },
    sort: { key: "created_at", dir: "desc" },
    flash: new Set(),
    renderQueued: false,
  };

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

  async function checkDrive(force = false) {
    try { renderDrive(await api(`/api/drive/status${force ? "?force=true" : ""}`)); }
    catch (e) { renderDrive({ connected: false, error: e.message }); }
  }

  // ------------------------------------------------------------------ renderers
  function renderAuth(a) {
    const ok = !!a.ok;
    $("#nlm-dot").className = `dot ${ok ? "ok" : "bad"}`;
    $("#nlm-sub").textContent = ok ? `${a.email || "Signed in"} · ${a.notebooks ?? "?"} notebooks` : (a.error || "Not signed in");
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

  function renderCapacity(limit = 500) {
    const c = state.counts || {};
    const present = c.present || 0;
    const ratio = Math.min(1, present / (limit || 500));
    const fg = $("#ring-fg");
    fg.style.strokeDashoffset = String(314.16 * (1 - ratio));
    fg.classList.toggle("full", ratio >= 0.98);
    setText("#cap-count", present);
    setText("#cap-limit", `/ ${limit}`);
    setText("#cap-2024", c.y2024 || 0);
    setText("#cap-2025", c.y2025 || 0);
    setText("#cap-freed", (c.by_stage || {}).DELETED || 0);
    $("#cap-sub").textContent = present ? `${Math.max(0, limit - present)} slots free · ${c.selected || 0} selected` : "Refresh the list to count";
    setText("#sel-count", `${c.selected || 0} selected`);
  }

  function renderStatus(st) {
    if (!st) return;
    const prev = state.status.state;
    state.status = st;
    const running = st.state === "running" || st.state === "paused" || st.state === "stopping";
    const pill = $("#run-pill");
    pill.className = `pill pill-${st.state}`;
    $("#run-pill-text").textContent = { idle: "Idle", running: st.dry_run ? "Dry run" : "Running", paused: "Paused", stopping: "Stopping" }[st.state] || st.state;
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
    $("#current").classList.toggle("hidden", !cur || !running);
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
    $("#run-message").classList.toggle("hidden", !msg);
    if (msg) $("#run-message").textContent = msg;

    if (prev && prev !== "idle" && st.state === "idle") { loadRuns(); loadSummary(); }
  }

  function filtered() {
    const { year, status, q } = state.filters;
    const qq = q.trim().toLowerCase();
    let rows = Array.from(state.notebooks.values()).filter((r) => r.present || r.stage === "DELETED" || r.stage === "CLOUD_VERIFIED");
    if (year === "2024" || year === "2025") rows = rows.filter((r) => String(r.year) === year);
    else if (year === "other") rows = rows.filter((r) => r.year !== 2024 && r.year !== 2025);
    if (status !== "all") rows = rows.filter((r) => category(r) === status);
    if (qq) rows = rows.filter((r) => (r.title || "").toLowerCase().includes(qq));
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
    if (r.drive_folder_id) actions.push(`<a class="icon-btn" id="open-${esc(r.id)}" href="https://drive.google.com/drive/folders/${esc(r.drive_folder_id)}" target="_blank" rel="noopener" title="Open archive folder in Drive"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg></a>`);
    if (r.stage === "FAILED" && r.is_owner) actions.push(`<button class="icon-btn" data-retry="${esc(r.id)}" id="retry-${esc(r.id)}" title="Retry in next run"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg></button>`);
    const err = r.error && r.stage !== "DELETED" ? `<div class="nb-err" title="${esc(r.error)}">${esc(r.error)}</div>` : "";
    return `<tr class="${cls}" data-id="${esc(r.id)}">
      <td><input type="checkbox" data-sel="${esc(r.id)}" id="sel-${esc(r.id)}" ${r.selected && !locked ? "checked" : ""} ${locked ? "disabled" : ""} aria-label="Select ${esc(r.title)}"></td>
      <td><div class="nb-title"><span class="nb-emoji">${esc(r.emoji || "📓")}</span><div style="min-width:0"><div class="nb-name" title="${esc(r.title)}">${esc(r.title)}</div>${err}</div></div></td>
      <td class="mono">${fmtDate(r.created_at)}</td>
      <td class="num">${r.sources_count ?? "–"}</td>
      <td>${stageChip(r)}</td>
      <td class="num mono">${r.bytes ? bytes(r.bytes) : "–"}</td>
      <td><div class="row-actions">${actions.join("")}</div></td></tr>`;
  }

  function renderTable() {
    state.renderQueued = false;
    const rows = filtered();
    $("#nb-body").innerHTML = rows.map(rowHtml).join("");
    $("#nb-count").textContent = rows.length;
    $("#nb-empty").classList.toggle("hidden", state.notebooks.size > 0);
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
    let sel = 0, present = 0, y24 = 0, y25 = 0, del = 0;
    for (const r of state.notebooks.values()) {
      if (r.present) { present++; if (r.selected) sel++; if (r.year === 2024) y24++; if (r.year === 2025) y25++; }
      if (r.stage === "DELETED") del++;
    }
    state.counts = { ...state.counts, present, selected: sel, y2024: y24, y2025: y25, by_stage: { ...(state.counts.by_stage || {}), DELETED: del } };
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

    // table
    $("#nb-body").addEventListener("change", (e) => { const id = e.target.dataset.sel; if (id) select([id], e.target.checked); });
    $("#nb-body").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-retry]");
      if (!b) return;
      try { await api(`/api/notebooks/${encodeURIComponent(b.dataset.retry)}/retry`, { body: {} }); await loadNotebooks(); toast("Queued for the next run", "ok"); }
      catch (er) { toast(er.message, "error"); }
    });
    $("#chk-all").onchange = (e) => select(filtered().filter((r) => r.is_owner && r.present && r.stage !== "DELETED").map((r) => r.id), e.target.checked);
    $("#btn-sel-visible").onclick = () => select(filtered().filter((r) => r.is_owner && r.present && r.stage !== "DELETED").map((r) => r.id), true);
    $("#btn-unsel-visible").onclick = () => select(filtered().map((r) => r.id), false);
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
    checkDrive();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
