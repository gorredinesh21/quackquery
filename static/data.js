"use strict";
/* QuackQuery data-profile page: dataset picker -> GET /api/datasets/{id}/profile
   -> stats + schema table (type, nulls, distinct, top values). */
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function err(msg) {
  $("err").innerHTML = '<div class="card" style="border-color:#fecaca;background:var(--red-bg);">' +
    '<b style="color:var(--red);">Could not load profile</b><p class="small" style="color:var(--red);margin:4px 0 0;">' +
    esc(msg) + "</p></div>";
}

async function refreshList(pickFirst) {
  $("err").innerHTML = "";
  try {
    const r = await fetch("/api/datasets");
    if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 200));
    const list = await r.json();
    if (!list.length) {
      $("empty").hidden = false;
      $("profile").innerHTML = "";
      $("dsel").innerHTML = '<option value="">(no datasets)</option>';
      return;
    }
    $("empty").hidden = true;
    const prev = $("dsel").value;
    $("dsel").innerHTML = list.map(d =>
      `<option value="${esc(d.dataset_id)}">${esc(d.name)} — ${d.row_count} rows (${esc(d.source)})</option>`).join("");
    const stillThere = list.some(d => d.dataset_id === prev);
    $("dsel").value = (prev && stillThere && !pickFirst) ? prev : list[0].dataset_id;
    loadProfile();
  } catch (e) { err(e.message); }
}

function topValuesHTML(col) {
  if (!col.top_values) {
    return '<span class="muted">—</span>';
  }
  return col.top_values.map(tv =>
    `<span class="tv">${esc(tv.value === null ? "NULL" : tv.value)}` +
    `<span class="n">×${Number(tv.count)}</span></span>`).join("");
}

function nullsCell(n, rowCount) {
  if (!n) return '<span class="st st-ok">0 nulls</span>';
  const pct = rowCount ? (100 * n / rowCount).toFixed(1) : "?";
  return `<span class="st st-warn">${n} nulls · ${pct}%</span>`;
}

function renderProfile(d) {
  const totalNulls = d.columns.reduce((a, c) => a + (c.nulls || 0), 0);
  const withTop = d.columns.filter(c => c.top_values).length;

  let h = `
    <div class="statrow">
      <div class="stat"><b>${d.row_count.toLocaleString()}</b><span>rows</span></div>
      <div class="stat"><b>${d.columns.length}</b><span>columns</span></div>
      <div class="stat"><b>${totalNulls.toLocaleString()}</b><span>null cells</span></div>
      <div class="stat"><b>${withTop}</b><span>low-cardinality cols</span></div>
    </div>
    <div class="card">
      <div class="tblwrap"><table class="tbl">
        <tr><th>Column</th><th>Type</th><th>Nulls</th><th>Distinct</th><th>Top values (max 8)</th></tr>`;
  for (const c of d.columns) {
    const uniq = d.row_count && c.distinct === d.row_count;
    h += `<tr>
      <td class="mono"><b>${esc(c.name)}</b></td>
      <td><code>${esc(c.type)}</code></td>
      <td>${nullsCell(c.nulls, d.row_count)}</td>
      <td class="mono">${Number(c.distinct).toLocaleString()}${uniq ? ' <span class="muted small">(all unique)</span>' : ""}</td>
      <td>${topValuesHTML(c)}</td>
    </tr>`;
  }
  h += "</table></div></div>";
  $("profile").innerHTML = h;
}

async function loadProfile() {
  const id = $("dsel").value;
  if (!id) return;
  $("err").innerHTML = "";
  $("profile").innerHTML = '<p class="muted">profiling…</p>';
  try {
    const r = await fetch("/api/datasets/" + encodeURIComponent(id) + "/profile");
    if (!r.ok) {
      const t = await r.text();
      let msg = t;
      try { const d = JSON.parse(t).detail; msg = typeof d === "string" ? d : JSON.stringify(d); } catch (_) {}
      throw new Error("HTTP " + r.status + " — " + String(msg).slice(0, 200));
    }
    renderProfile(await r.json());
  } catch (e) {
    $("profile").innerHTML = "";
    err(e.message);
  }
}

$("dsel").onchange = loadProfile;
$("reload").onclick = () => refreshList(false);

$("loaddemos").onclick = async () => {
  const btn = $("loaddemos");
  btn.disabled = true; btn.textContent = "Loading…";
  try {
    const r = await fetch("/api/datasets/demo");
    if (!r.ok) throw new Error("HTTP " + r.status);
    await refreshList(true);
  } catch (e) { err(e.message); }
  btn.disabled = false; btn.textContent = "Load demo datasets";
};

refreshList(true);
