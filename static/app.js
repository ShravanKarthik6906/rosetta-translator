// Polls /api/job/<id> every 1.2s and updates the page. No frameworks, no
// build step - this is a small local tool, kept as plain JS on purpose.

const script = document.currentScript;
const jobId = script.dataset.jobId;

const LANGUAGE_LABELS = { es: "Spanish", ar: "Arabic" };

function fmtStage(stage) {
  const known = {
    starting: "Starting...",
    deconstruct: "Deconstructing IDML (extracting text)",
    story_placements: "Mapping stories to pages/frames",
    translate: "Translating (live API calls)",
    "exception checks": "Running exception checks",
    done: "Done",
  };
  return known[stage] || stage;
}

function render(job) {
  const badge = document.getElementById("status-badge");
  badge.textContent = job.status;
  badge.className = "status-badge status-" + job.status;

  document.getElementById("stage-name").textContent = fmtStage(job.stage);

  const fill = document.getElementById("progress-fill");
  const text = document.getElementById("progress-text");
  if (job.stage_progress) {
    const [done, total] = job.stage_progress;
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    fill.style.width = pct + "%";
    text.textContent = `${done} / ${total}`;
  } else {
    fill.style.width = job.status === "done" ? "100%" : "0%";
    text.textContent = "";
  }

  document.getElementById("log").textContent = job.log.join("\n");
  const logEl = document.getElementById("log");
  logEl.scrollTop = logEl.scrollHeight;

  const errorBox = document.getElementById("error-box");
  if (job.status === "error") {
    errorBox.innerHTML = `<div class="error-box"><strong>Something went wrong:</strong> ${escapeHtml(job.error || "unknown error")}</div>`;
  } else {
    errorBox.innerHTML = "";
  }

  const resultsEl = document.getElementById("results");
  if (job.status === "done") {
    let html = "";
    for (const lang of job.langs) {
      const r = job.results[lang];
      if (!r) continue;
      const label = LANGUAGE_LABELS[lang] || lang;
      const counts = r.exception_counts || {};
      html += `
        <div class="lang-result">
          <h3>${label}</h3>
          <div>${r.replaced} fragment(s) translated, ${r.missing} left untranslated${r.rtl_flips ? `, ${r.rtl_flips} RTL flip(s)` : ""}.</div>
          <div class="downloads">
            <a href="/download/${jobId}/${lang}/delivery">Download delivery .zip</a>
            <a href="/download/${jobId}/${lang}/exceptions">Download exception report</a>
            <a href="/download/${jobId}/${lang}/exceptions_json" class="secondary">Raw JSON</a>
          </div>
          <div class="exc-counts">Exceptions: ${counts.translation_error || 0} translation_error, ${counts.missing_translation || 0} missing_translation, ${counts.layout_risk_heuristic || 0} layout_risk_heuristic</div>
        </div>`;
    }

    if (job.usage) {
      const u = job.usage;
      const cost = job.cost || {};
      html += `<div class="usage-box">
        <strong>Usage this run</strong><br>
        API calls: ${u.calls} &nbsp; | &nbsp; Prompt tokens: ${u.prompt_tokens.toLocaleString()} &nbsp; | &nbsp; Completion tokens: ${u.completion_tokens.toLocaleString()} &nbsp; | &nbsp; Total tokens: ${u.total_tokens.toLocaleString()}<br>
        Model: ${u.model || "n/a"}<br>`;
      if (cost.available) {
        html += `Estimated cost: $${cost.usd.toFixed(4)}`;
      } else {
        html += `<span class="note">${escapeHtml(cost.note || "Cost unavailable.")}</span>`;
      }
      html += `</div>`;
    }

    resultsEl.innerHTML = html;
  } else {
    resultsEl.innerHTML = "";
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function poll() {
  try {
    const res = await fetch(`/api/job/${jobId}`);
    if (!res.ok) return;
    const job = await res.json();
    render(job);
    if (job.status === "running") {
      setTimeout(poll, 1200);
    }
  } catch (e) {
    setTimeout(poll, 2000);
  }
}

poll();
