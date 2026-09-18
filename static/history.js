"use strict";
/* QuackQuery history page: GET /api/query (newest first, limit 50).
   Row click expands full SQL + answer; "Run again" deep-links to the demo
   page (/demo?ds=…&q=…), which auto-fills and re-asks. */
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  return sameDay ? t : d.toLocaleDateString() + " " + t;
}

function attemptsBadge(n) {
  n = Number(n) || 0;
  const cls = n <= 1 ? "st-ok" : (n <= 3 ? "st-warn" : "st-err");
  return `<span class="st ${cls}">${n} attempt${n === 1 ? "" : "s"}</span>`;
}

function fmtMs(ms) {
  ms = Number(ms) || 0;
  return ms >= 1000 ? (ms / 1000).toFixed(1) + " s" : ms + " ms";
}

function sqlSummary(sql) {
  const s = String(sql || "").replace(/\s+/g, " ").trim();
  return s.length > 90 ? s.slice(0, 88) + "…" : s;
}

function render(items) {
  if (!items.length) {
    $("list").innerHTML = `
      <div class="empty">
        <h3>No queries yet</h3>
        <p>Ask your first question on the demo page — everything you ask
        shows up here with its SQL, attempts and timing.</p>
        <a class="btn" href="/demo">Open the demo</a>
      </div>`;
    return;
  }
  $("list").innerHTML = items.map((it, i) => `
    <div class="hrow" id="hrow${i}">
      <div class="hrow-head" data-i="${i}">
        <svg class="caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>
        <span class="hrow-q" title="${esc(it.question)}">${esc(it.question)}</span>
        <span class="hrow-meta">
          <span class="st st-info">${esc(it.dataset || it.dataset_id || "?")}</span>
          ${attemptsBadge(it.attempts)}
          <span class="mono">${fmtMs(it.timing_ms)}</span>
          <span class="mono">${Number(it.row_count || 0).toLocaleString()} rows</span>
          <span>${esc(fmtTs(it.ts))}</span>
        </span>
      </div>
      <div class="hdet">
        <p class="small muted" style="margin:10px 0 2px;"><b>SQL</b> <span class="mono">(qid ${esc(it.qid)})</span></p>
        <pre>${esc(it.sql || "")}</pre>
        <p class="small muted" style="margin:8px 0 2px;"><b>Answer</b></p>
        <p style="margin:0 0 12px;">${esc(it.answer || "(no answer text)")}</p>
        <a class="btn btn-sm" href="/demo?ds=${encodeURIComponent(it.dataset_id || "")}&q=${encodeURIComponent(it.question || "")}">▶ Run again</a>
        <button class="btn btn-ghost btn-sm" data-copy="${esc(it.sql || "")}">Copy SQL</button>
      </div>
    </div>`).join("");

  for (const head of document.querySelectorAll(".hrow-head")) {
    head.onclick = () => head.parentElement.classList.toggle("open");
  }
  for (const b of document.querySelectorAll("[data-copy]")) {
    b.onclick = e => {
      e.stopPropagation();
      const txt = b.getAttribute("data-copy");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(txt).then(
          () => { b.textContent = "Copied ✓"; setTimeout(() => b.textContent = "Copy SQL", 1200); },
          () => {});
      }
    };
  }
}

async function load() {
  $("list").innerHTML = '<p class="muted">loading history…</p>';
  try {
    const r = await fetch("/api/query");
    if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 200));
    render(await r.json());
  } catch (e) {
    $("list").innerHTML = '<div class="empty"><h3>Could not load history</h3><p>' +
      esc(e.message) + "</p></div>";
  }
}

$("refresh").onclick = load;
load();
