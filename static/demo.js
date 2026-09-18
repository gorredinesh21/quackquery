"use strict";
/* QuackQuery demo page — dataset picker + CSV upload + SSE pipeline view.
   The SSE parser below handles \r\n\r\n, \n\n and \r\r frame separators
   (sse-starlette writes CRLF); do not simplify it. */
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// Example questions — each chip also selects the dataset it belongs to.
const CHIPS = [
  {q: "which team won the most matches",                  ds: "ipl_matches"},
  {q: "how many orders per payment method",               ds: "ecommerce_orders"},
  {q: "average revenue by category",                      ds: "ecommerce_orders"},
  {q: "top 5 sectors by total funding amount",            ds: "indian_startup_funding"},
  {q: "which city has the highest total funding",         ds: "indian_startup_funding"},
];
const dsNameToId = {};

function ev(type, html) {
  const d = document.createElement("div");
  d.className = "ev " + type;
  d.innerHTML = html;
  $("log").appendChild(d);
  return d;
}
function showError(msg) {
  ev("error", '<span class="tag">error</span>' + esc(msg));
  $("pipecard").scrollIntoView({behavior: "smooth", block: "nearest"});
}
function tableHTML(columns, rows) {
  if (!rows || !rows.length) return '<span class="muted">(0 rows)</span>';
  let h = "<table><tr>" + columns.map(c => "<th>" + esc(c) + "</th>").join("") + "</tr>";
  for (const r of rows.slice(0, 15))
    h += "<tr>" + r.map(v => "<td>" + esc(v === null ? "NULL" : v) + "</td>").join("") + "</tr>";
  h += "</table>";
  if (rows.length > 15) h += '<span class="muted">' + rows.length + " rows (15 shown)</span>";
  return h;
}

// Surface unexpected JS errors / unhandled rejections in the pipeline box.
window.addEventListener("error", e => {
  showError("Unexpected JS error: " + (e.message || e.type) +
            (e.filename ? " (" + e.filename + ":" + e.lineno + ")" : ""));
});
window.addEventListener("unhandledrejection", e => {
  const r = e.reason;
  showError("Unhandled promise rejection: " + (r && r.message ? r.message : String(r)));
});

function setLoading(on) {
  document.body.classList.toggle("busy", on);
  $("go").disabled = on;
  $("go").textContent = on ? "Asking…" : "Ask";
}

function hasOption(id) {
  return !!document.querySelector('#ds option[value="' + String(id).replace(/"/g, '\\"') + '"]');
}

async function ensureDataset(id) {
  /* For /demo?ds=… links (e.g. "Run again" from History): demos are loaded
     on page open; any other still-registered dataset is fetched and added. */
  if (hasOption(id)) return true;
  try {
    const r = await fetch("/api/datasets/" + encodeURIComponent(id));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    dsNameToId[d.name] = d.dataset_id;
    const opt = document.createElement("option");
    opt.value = d.dataset_id;
    opt.textContent = d.name + " (" + d.row_count + " rows)";
    $("ds").appendChild(opt);
    return true;
  } catch (e) {
    showError("dataset " + id + " is no longer available: " + e.message);
    return false;
  }
}

async function loadDatasets() {
  try {
    const r = await fetch("/api/datasets/demo");
    if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0, 200));
    const list = await r.json();
    $("ds").innerHTML = list.map(d =>
      `<option value="${esc(d.dataset_id)}">${esc(d.name)} (${d.row_count} rows)</option>`).join("");
    for (const d of list) dsNameToId[d.name] = d.dataset_id;
    renderChips();
    ev("stage", '<span class="tag">stage</span>▸ ' + list.length +
       " demo datasets ready — click an example question below the ask box");
  } catch (e) { showError("could not load demo datasets: " + e.message); }
}

function renderChips() {
  $("chips").innerHTML = "";
  for (const c of CHIPS) {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = c.q;
    b.onclick = () => {
      if (document.body.classList.contains("busy")) return;
      if (!dsNameToId[c.ds]) { showError("dataset not loaded: " + c.ds); return; }
      $("ds").value = dsNameToId[c.ds];
      $("q").value = c.q;
      ask();
    };
    $("chips").appendChild(b);
  }
}

// ---- the query stream ----------------------------------------------------
async function ask() {
  if (document.body.classList.contains("busy")) return;
  const dataset_id = $("ds").value, question = $("q").value.trim();
  if (!dataset_id) { showError("pick a dataset first (or upload a CSV)."); return; }
  if (!question)   { showError("type a question first — or click an example question."); return; }

  $("log").innerHTML = "";
  setLoading(true);
  $("pipecard").scrollIntoView({behavior: "smooth", block: "start"});
  ev("stage", '<span class="tag">request</span>POST /api/query/stream {dataset: ' +
     esc(dataset_id) + ', question: "' + esc(question) + '"}');

  let sawAnswer = false, sawTerminal = false, qid = null, cached = false;
  try {
    const resp = await fetch("/api/query/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({dataset_id, question})
    });
    if (!resp.ok) {
      const t = await resp.text();
      let msg = t;
      try { const d = JSON.parse(t).detail; msg = typeof d === "string" ? d : JSON.stringify(d); } catch (_) {}
      throw new Error("HTTP " + resp.status + " " + resp.statusText + " — " +
                      String(msg).slice(0, 300));
    }
    if (!resp.body) throw new Error("response has no stream body");

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    // SSE frames are separated by a BLANK LINE. sse-starlette writes CRLF
    // line endings ("\r\n\r\n"); some servers use "\n\n" or "\r\r" — accept all.
    const SEP = /\r\n\r\n|\n\n|\r\r/;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let m;
      while ((m = SEP.exec(buf)) !== null) {
        const frame = buf.slice(0, m.index);
        buf = buf.slice(m.index + m[0].length);
        // A frame: "event: <name>" lines plus one or more "data: <json>" lines.
        const dataLines = frame.split(/\r\n|\n|\r/)
          .filter(l => l.startsWith("data:"))
          .map(l => l.slice(5).trim());
        if (!dataLines.length) continue;
        const payload = dataLines.join("\n");
        let msg;
        try { msg = JSON.parse(payload); }
        catch (e) {
          showError("bad SSE data line (not JSON): " + payload.slice(0, 200));
          continue;
        }
        try {
          handle(msg);
          if (msg.type === "answer") sawAnswer = true;
          if (msg.type === "done") { sawTerminal = true; qid = msg.qid || null; cached = !!msg.cached; }
          if (msg.type === "error") sawTerminal = true;
        } catch (e) { showError("error while rendering event '" + msg.type + "': " + e.message); }
      }
    }
    // Server contract: stream always ends with done or error. If it just
    // stopped, that's a contract breach — say so, never stay silent.
    if (!sawTerminal)
      showError("stream ended without a done or error event (server or connection dropped)");
    else if (cached && !sawAnswer) await replayCached(qid);
  } catch (e) {
    showError("query failed: " + (e && e.message ? e.message : String(e)));
  } finally {
    setLoading(false);
  }
}

async function replayCached(qid) {
  // A cache hit streams only a `done` event; fetch the full result.
  try {
    const r = await fetch("/api/query/" + encodeURIComponent(qid));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const res = await r.json();
    handle({type: "sql", sql: res.sql, attempt: 1});
    handle({type: "rows", columns: res.columns, rows: res.rows, row_count: res.row_count});
    handle({type: "chart", spec: res.chart});
    handle({type: "answer", text: res.answer});
    ev("done", '<span class="tag">done</span>served from cache (qid ' + esc(qid) + ")");
  } catch (e) {
    showError("cached result could not be replayed: " + e.message);
  }
}

// Server event types (app/pipeline.py): stage | sql | guard_error | exec_error |
// retry | rows | chart | answer | done | error — each `data:` line is JSON
// with a "type" field.
function handle(m) {
  switch (m.type) {
    case "stage":
      ev("stage", '<span class="tag">stage</span>▸ ' + esc(m.name) +
         (m.dataset ? " · " + esc(m.dataset) + " (" + m.row_count + " rows)" : ""));
      break;
    case "sql":
      ev("sql", '<span class="tag">sql · attempt ' + Number(m.attempt) +
         '</span>' + esc(m.sql));
      break;
    case "guard_error":
      ev("error", '<span class="tag">guard rejected</span>' + esc(m.error));
      break;
    case "exec_error":
      ev("error", '<span class="tag">exec error</span>' + esc(m.error));
      break;
    case "retry":
      ev("retry", '<span class="tag">retry</span>↻ self-correcting (attempt ' +
         Number(m.attempt) + ")…");
      break;
    case "rows":
      ev("rows", '<span class="tag">rows</span>' + Number(m.row_count) +
        " row(s) returned" + tableHTML(m.columns, m.rows));
      break;
    case "chart":
      ev("chart", '<span class="tag">chart</span>' +
        (m.spec ? esc(m.spec.mark) + " chart — x: " + esc(m.spec.x) +
                 ", y: " + esc(m.spec.y)
                : "no chart for this result"));
      break;
    case "answer":
      ev("answer", '<span class="tag">answer</span><span class="answertext">' +
        esc(m.text || "(no answer text)") + "</span>");
      break;
    case "done":
      ev("done", '<span class="tag">done</span>✓ done in ' + Number(m.timing_ms) +
        "ms · attempts=" + Number(m.attempts) + (m.cached ? " · cached" : "") +
        (m.qid ? " · qid " + esc(m.qid) : ""));
      break;
    case "error": {
      let h = '<span class="tag">fatal</span>' + esc(m.error || "unknown error") +
              (m.status ? " (HTTP " + Number(m.status) + ")" : "");
      if (m.sql_attempts && m.sql_attempts.length) {
        h += "\nSQL attempts:\n" + m.sql_attempts.map((s, i) =>
               "  #" + (i + 1) + ": " + esc(s)).join("\n");
      }
      ev("error", h);
      break;
    }
    default:
      ev("stage", '<span class="tag">' + esc(m.type || "event") + "</span>" +
        esc(JSON.stringify(m).slice(0, 300)));
  }
}

// ---- CSV upload ----------------------------------------------------------
$("up").onchange = async () => {
  const f = $("up").files[0];
  if (!f) return;
  $("upstatus").textContent = 'uploading "' + f.name + '"…';
  try {
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch("/api/datasets", {method: "POST", body: fd});
    if (!r.ok) {
      const t = await r.text();
      let msg = t;
      try { const d = JSON.parse(t).detail; msg = typeof d === "string" ? d : JSON.stringify(d); } catch (_) {}
      throw new Error("HTTP " + r.status + " — " + String(msg).slice(0, 200));
    }
    const d = await r.json();
    dsNameToId[d.name] = d.dataset_id;
    const opt = document.createElement("option");
    opt.value = d.dataset_id;
    opt.textContent = d.name + " (" + d.row_count + " rows)";
    $("ds").appendChild(opt);
    $("ds").value = d.dataset_id;
    $("upstatus").textContent = 'uploaded "' + d.name + '" — ' + d.row_count +
                                " rows, now ask away.";
    ev("stage", '<span class="tag">upload</span>▸ dataset "' + esc(d.name) +
       '" ready (' + d.row_count + " rows)");
    $("q").focus();
  } catch (e) {
    $("upstatus").textContent = "";
    showError("CSV upload failed: " + e.message);
  } finally {
    $("up").value = "";
  }
};

$("go").onclick = ask;
$("q").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });

// ---- boot: load demos, then honor /demo?ds=<id>&q=<question> links --------
(async () => {
  await loadDatasets();
  const params = new URLSearchParams(location.search);
  const wantDs = params.get("ds"), wantQ = params.get("q");
  if (!wantDs && !wantQ) return;
  if (wantDs) {
    const ok = await ensureDataset(wantDs);
    if (!ok) return;
    $("ds").value = wantDs;
  }
  if (wantQ) $("q").value = wantQ;
  if (wantDs && wantQ) ask();
})();
