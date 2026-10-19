// PitchForce Feedback – small browser helpers (no framework).
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function api(method, url, body, headers) {
    const h = Object.assign({ "X-CSRF-Token": csrf }, headers || {});
    let b = body;
    if (body && !(body instanceof Blob) && typeof body === "object") { h["Content-Type"] = "application/json"; b = JSON.stringify(body); }
    const r = await fetch(url, { method, headers: h, body: b, credentials: "same-origin" });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) { const err = new Error(data.error || data.detail || ("HTTP " + r.status)); err.status = r.status; err.data = data; throw err; }
    return data;
  }

  // ---------------- Resumable chunked uploads ----------------
  function fmtMB(n) { return (n / 1048576).toFixed(1) + " MB"; }

  async function uploadFile(eventId, kind, file, row) {
    const bar = row.querySelector(".progress > span"), status = row.querySelector(".status");
    const init = await api("POST", `/api/events/${eventId}/uploads`, { kind, filename: file.name, size: file.size });
    let offset = init.received || 0;
    const chunk = init.chunk_size;
    if (offset > 0) status.textContent = "Resuming at " + fmtMB(offset);
    while (offset < file.size) {
      const end = Math.min(offset + chunk, file.size);
      let attempt = 0;
      for (;;) {
        try {
          const res = await api("PUT", `/api/uploads/${init.upload_id}?offset=${offset}`, file.slice(offset, end),
                                { "Content-Type": "application/octet-stream" });
          offset = res.received;
          break;
        } catch (e) {
          if (e.status === 409 && e.data && typeof e.data.received === "number") { offset = e.data.received; break; }
          if (e.status && e.status < 500 && e.status !== 409) throw e;
          if (++attempt > 5) throw e;
          status.textContent = "Connection problem, retrying…";
          await new Promise(r => setTimeout(r, 1500 * attempt));
        }
      }
      bar.style.width = (100 * offset / file.size).toFixed(1) + "%";
      status.textContent = fmtMB(offset) + " of " + fmtMB(file.size);
    }
    status.textContent = "Verifying…";
    const done = await api("POST", `/api/uploads/${init.upload_id}/complete`);
    status.textContent = done.duplicate ? "Already uploaded (identical file)" : "Uploaded – reading file…";
    row.classList.add("done");
  }

  document.querySelectorAll("[data-uploader]").forEach(box => {
    const eventId = box.dataset.event, input = box.querySelector("input[type=file]"), kindSel = box.querySelector("select[name=kind]");
    const list = box.querySelector(".uploads"), zone = box.querySelector(".dropzone");
    async function handle(files) {
      const kind = kindSel.value;
      if (!kind) { alert("Choose what kind of file this is first."); return; }
      let ok = true;
      for (const f of files) {
        const row = document.createElement("div");
        row.className = "item";
        row.innerHTML = `<div><strong></strong><div class="small muted"></div></div><div class="progress"><span style="width:0"></span></div><div class="status small">Starting…</div><div></div>`;
        row.querySelector("strong").textContent = f.name;
        row.querySelector(".small.muted").textContent = kindSel.options[kindSel.selectedIndex].text + " · " + fmtMB(f.size);
        list.appendChild(row);
        try { await uploadFile(eventId, kind, f, row); }
        catch (e) { ok = false; row.querySelector(".status").textContent = "Failed: " + e.message; row.querySelector(".status").classList.add("b-err"); }
      }
      if (ok) setTimeout(() => location.reload(), 1200);
    }
    input.addEventListener("change", () => handle(Array.from(input.files)));
    if (zone) {
      ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
      ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove("drag"); }));
      zone.addEventListener("drop", e => handle(Array.from(e.dataTransfer.files)));
    }
  });

  // ---------------- Live job progress ----------------
  document.querySelectorAll("[data-jobs]").forEach(box => {
    const eventId = box.dataset.event;
    let wasActive = box.dataset.active === "1";
    async function tick() {
      try {
        const data = await api("GET", `/api/events/${eventId}/jobs`);
        const rows = data.jobs.slice(0, Number(box.dataset.limit || 12)).map(j => {
          const pct = Math.round(j.progress * 100);
          const cls = { succeeded: "b-ok", failed: "b-err", running: "b-info", queued: "", cancelled: "b-warn" }[j.status] || "";
          const err = j.error ? `<div class="small" style="color:var(--err)">${escapeHtml(j.error).slice(0, 300)}</div>` : "";
          return `<tr><td>${escapeHtml(j.label)}${err}</td><td><span class="badge ${cls}">${j.status}</span></td>
                  <td>${escapeHtml(j.stage)}</td><td><div class="progress"><span style="width:${pct}%"></span></div></td>
                  <td class="small">${j.attempts}/${j.max_attempts}</td></tr>`;
        });
        box.querySelector("tbody").innerHTML = rows.join("") || `<tr><td colspan="5" class="muted">No background work yet.</td></tr>`;
        if (wasActive && !data.active && box.dataset.reload === "1") { location.reload(); return; }
        wasActive = data.active;
        setTimeout(tick, data.active ? 2000 : 8000);
      } catch (e) { setTimeout(tick, 10000); }
    }
    tick();
  });

  function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

  // ---------------- Clip editor ----------------
  function hms(t) {
    if (t == null || isNaN(t)) return "";
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = (t % 60);
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
  }
  document.querySelectorAll("[data-clip-editor]").forEach(ed => {
    const video = ed.querySelector("video"), start = ed.querySelector("input[name=start]"), end = ed.querySelector("input[name=end]");
    const dur = ed.querySelector("[data-duration]");
    function parse(v) { if (!v) return NaN; if (v.includes(":")) { const p = v.split(":").map(Number); return p.reduce((a, x) => a * 60 + x, 0); } return Number(v); }
    function upd() { const d = parse(end.value) - parse(start.value); if (dur) dur.textContent = isNaN(d) ? "" : (d > 0 ? `${Math.floor(d / 60)}m ${Math.round(d % 60)}s` : "end must be after start"); }
    ed.querySelectorAll("[data-set]").forEach(b => b.addEventListener("click", e => {
      e.preventDefault(); (b.dataset.set === "start" ? start : end).value = hms(video.currentTime); upd();
    }));
    ed.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", e => {
      e.preventDefault(); const t = parse((b.dataset.go === "start" ? start : end).value); if (!isNaN(t)) { video.currentTime = Math.max(0, t - (b.dataset.go === "end" ? 5 : 0)); video.play(); }
    }));
    ed.querySelectorAll("[data-nudge]").forEach(b => b.addEventListener("click", e => {
      e.preventDefault(); const [which, delta] = b.dataset.nudge.split(":"); const inp = which === "start" ? start : end;
      const t = parse(inp.value) + Number(delta); if (!isNaN(t)) { inp.value = hms(Math.max(0, t)); video.currentTime = Math.max(0, t); upd(); }
    }));
    ed.querySelectorAll(".cue").forEach(c => c.addEventListener("click", () => { video.currentTime = Number(c.dataset.t); video.play(); }));
    [start, end].forEach(i => i && i.addEventListener("input", upd));
    upd();
  });

  // Confirm dangerous actions
  document.querySelectorAll("form[data-confirm]").forEach(f => f.addEventListener("submit", e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));
  // Select-all checkboxes in transcript tab
  document.querySelectorAll("[data-check-all]").forEach(cb => cb.addEventListener("change", () => {
    document.querySelectorAll(cb.dataset.checkAll).forEach(x => { x.checked = cb.checked; });
  }));
})();
