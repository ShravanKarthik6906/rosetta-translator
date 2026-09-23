"""
Rosetta v3 - web UI
Thin Flask wrapper around the existing pipeline scripts (deconstruct.py,
story_placements.py, translate.py, reconstruct.py, exception_checks.py).
This file does NOT reimplement any pipeline logic - it just: validates an
upload, runs the existing functions in a background thread while reporting
progress via their optional progress_callback/usage_callback hooks, and
serves the resulting files.

Local dev:
    .venv/bin/python app.py
then open http://127.0.0.1:5050 (reads PORT from the environment if set,
defaults to 5050; binds 0.0.0.0 so this also works unchanged when a host
platform like Render runs it behind gunicorn).

Optional access gate: set SITE_PASSWORD in the environment to require HTTP
Basic Auth (any username, that password) on every request - this app makes
real, billed OpenAI calls per upload, so a public deployment SHOULD set
this. Leave it unset for local-only use.
"""

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from html import escape
import zipfile
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, abort, Response

import deconstruct
import story_placements
import translate
import overflow_resolve
import reconstruct
import exception_checks

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500MB - real IDML+Links bundles can be large

SITE_PASSWORD = os.environ.get("SITE_PASSWORD")  # optional - unset means no gate (local dev default)


@app.before_request
def require_site_password():
    if not SITE_PASSWORD:
        return
    auth = request.authorization
    if not auth or auth.password != SITE_PASSWORD:
        return Response(
            "Authentication required.", 401,
            {"WWW-Authenticate": 'Basic realm="Rosetta"'}
        )


JOBS_ROOT = Path(__file__).parent / "webapp_jobs"
JOBS_ROOT.mkdir(exist_ok=True)

LANGUAGE_LABELS = {"es": "Spanish", "ar": "Arabic"}

# Prices are USD per 1,000,000 tokens. VERIFY against https://openai.com/api/pricing/
# before trusting a number here - only as accurate as when last checked, and
# intentionally does NOT include an entry for every model (see compute_cost).
PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},  # verified vs OpenAI pricing page, early Sep 2026
}

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------- job state

def _new_job(job_id, langs):
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "running",  # running | done | error
            "stage": "starting",
            "stage_progress": None,  # [done, total] or None
            "langs": langs,
            "log": [],
            "error": None,
            "results": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0, "model": None},
            "created_at": time.time(),
        }


def _update_job(job_id, **kwargs):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kwargs)


def _log_job(job_id, message):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["log"].append(message)
            JOBS[job_id]["log"] = JOBS[job_id]["log"][-300:]


def _get_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return json.loads(json.dumps(job)) if job is not None else None  # cheap deep copy


def _add_usage(job_id, model, prompt_tokens, completion_tokens):
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        u = JOBS[job_id]["usage"]
        u["prompt_tokens"] += prompt_tokens
        u["completion_tokens"] += completion_tokens
        u["total_tokens"] += prompt_tokens + completion_tokens
        u["calls"] += 1
        u["model"] = model


def compute_cost(usage):
    model = usage.get("model")
    pricing = PRICING.get(model)
    if not pricing:
        return {
            "available": False,
            "model": model,
            "note": (
                f"No verified pricing configured for model '{model}'. Token counts above are "
                f"accurate (from the real API response); add pricing to PRICING in app.py to "
                f"compute a dollar figure - check https://openai.com/api/pricing/ first."
            ),
        }
    usd = (usage["prompt_tokens"] / 1e6) * pricing["input"] + (usage["completion_tokens"] / 1e6) * pricing["output"]
    return {"available": True, "model": model, "usd": round(usd, 4)}


# ---------------------------------------------------------------- upload validation

def _is_macos_junk_path(name: str) -> bool:
    """
    True for macOS Finder's AppleDouble junk: a top-level __MACOSX/ folder
    (mirroring the real structure with resource-fork files) and any
    individual ._filename entry it drops alongside real files. Finder's
    "Compress" always adds these to a zip - they must be ignored when
    looking for the real .idml/Links contents, or a mirrored
    __MACOSX/Links/._foo.idml can look like a second real .idml.
    """
    parts = name.split("/")
    return "__MACOSX" in parts or any(part.startswith("._") for part in parts)


def validate_upload_zip(path: Path):
    """
    Returns (ok: bool, message: str, idml_member: str | None).
    Checks the zip contains exactly one .idml file and a 'Links' folder,
    without extracting or running anything. Ignores macOS AppleDouble junk
    (__MACOSX/, ._*) that Finder's "Compress" adds to zips.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return False, "That file isn't a valid zip archive.", None

    try:
        names = [n for n in zf.namelist() if not _is_macos_junk_path(n)]
    except Exception:
        return False, "Couldn't read the contents of that zip archive.", None

    idml_members = [n for n in names if n.lower().endswith(".idml") and not n.endswith("/")]
    has_links_folder = any(part == "Links" for n in names for part in n.split("/"))

    if not idml_members:
        return False, "No .idml file found in the uploaded zip. Expected one .idml file plus a Links folder.", None
    if len(idml_members) > 1:
        return False, f"Found {len(idml_members)} .idml files in the zip - expected exactly one ({', '.join(idml_members)}).", None
    if not has_links_folder:
        return False, "No 'Links' folder found in the uploaded zip. Expected one .idml file plus a Links folder alongside it.", None

    return True, "OK", idml_members[0]


def find_subdir(base: Path, name: str):
    direct = base / name
    if direct.is_dir():
        return direct
    for p in base.rglob(name):
        if p.is_dir() and "__MACOSX" not in p.parts:
            return p
    return None


# ---------------------------------------------------------------- pipeline runner

def run_pipeline(job_id, job_dir: Path, idml_extracted_dir: Path, idml_filename: str, langs: list):
    try:
        db_path = str(job_dir / "rosetta.db")

        # --- Stage 1: deconstruct ---
        _update_job(job_id, stage="deconstruct", stage_progress=None)
        _log_job(job_id, "Deconstructing IDML: extracting every text run...")
        document_id = deconstruct.deconstruct(
            str(idml_extracted_dir), idml_filename, db_path,
            progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
        )
        _log_job(job_id, f"Deconstruct complete (document_id={document_id}).")

        # --- Stage 2: story placements (page/frame mapping) ---
        _update_job(job_id, stage="story_placements", stage_progress=None)
        _log_job(job_id, "Mapping stories to pages/frames...")
        story_placements.populate_story_placements(
            db_path, document_id, str(idml_extracted_dir),
            progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
        )
        _log_job(job_id, "Story placement mapping complete.")

        # --- Stage 3: translate (all selected languages, real API calls) ---
        _update_job(job_id, stage="translate", stage_progress=None)
        _log_job(job_id, f"Translating into {', '.join(LANGUAGE_LABELS[l] for l in langs)}...")
        stats = translate.translate_document(
            db_path, document_id, target_langs=langs,
            progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
            usage_callback=lambda model, p, c: _add_usage(job_id, model, p, c),
        )
        _log_job(job_id, f"Translation complete: {stats}")

        # --- Stage 3b: overflow check (real font-metric + frame-geometry fit,
        # controlled shrink, retranslate-for-brevity on genuine failures) ---
        _update_job(job_id, stage="overflow check", stage_progress=None)
        _log_job(job_id, "Checking real layout fit (font metrics + frame geometry) and fixing overflow...")
        overflow_stats = overflow_resolve.run_overflow_checks(
            db_path, document_id, str(idml_extracted_dir), langs,
            progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
            usage_callback=lambda model, p, c: _add_usage(job_id, model, p, c),
        )
        _log_job(job_id, f"Overflow check complete: {overflow_stats}")

        # --- Stage 4: reconstruct delivery package per language ---
        results = {}
        for lang in langs:
            _update_job(job_id, stage=f"reconstruct ({LANGUAGE_LABELS[lang]})", stage_progress=None)
            _log_job(job_id, f"Rebuilding the {LANGUAGE_LABELS[lang]} .idml + delivery package...")
            work_dir = job_dir / f"work_{lang}"
            recon_result = reconstruct.reconstruct(
                db_path, document_id, lang, str(idml_extracted_dir), str(work_dir),
                idml_filename=idml_filename,
                progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
            )
            results[lang] = {
                "delivery_zip_path": recon_result["bundle_path"],
                "replaced": recon_result["replaced"],
                "missing": recon_result["missing"],
                "rtl_flips": recon_result["rtl_flips"],
            }
            _log_job(job_id, f"{LANGUAGE_LABELS[lang]} delivery package ready "
                              f"({recon_result['replaced']} fragments translated).")

        # --- Stage 5: exception checks + per-language report ---
        _update_job(job_id, stage="exception checks", stage_progress=None)
        _log_job(job_id, "Running exception checks (translation consistency, missing translations, layout risk)...")
        exc_results = exception_checks.run_all_checks(
            db_path, document_id, langs,
            progress_callback=lambda done, total: _update_job(job_id, stage_progress=[done, total]),
        )
        for lang in langs:
            exceptions = _fetch_exceptions(db_path, document_id, lang)
            html_path = job_dir / f"exceptions_{lang}.html"
            json_path = job_dir / f"exceptions_{lang}.json"
            _write_exception_report_html(exceptions, document_id, lang, html_path)
            _write_exception_report_json(exceptions, document_id, lang, json_path)
            results[lang]["exception_report_html_path"] = str(html_path)
            results[lang]["exception_report_json_path"] = str(json_path)
            results[lang]["exception_counts"] = exc_results[lang]
        _log_job(job_id, "Exception checks complete.")

        cost = compute_cost(_get_job(job_id)["usage"])
        _update_job(job_id, status="done", stage="done", stage_progress=None, results=results, cost=cost)
        _log_job(job_id, "All done.")

    except Exception as e:
        _log_job(job_id, f"ERROR: {e}")
        _update_job(job_id, status="error", error=str(e))


def _fetch_exceptions(db_path, document_id, lang):
    """Returns a list of dicts: id, category, severity, description, status,
    created_at, text_run_ids - the full data behind both report formats."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    exceptions = conn.execute("""
        SELECT id, category, severity, description, status, created_at
        FROM exceptions WHERE document_id=? AND target_language=?
        ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MED' THEN 1 ELSE 2 END, category, id
    """, (document_id, lang)).fetchall()

    report = []
    for exc in exceptions:
        frag_ids = [r[0] for r in conn.execute(
            "SELECT text_run_id FROM exception_fragments WHERE exception_id=?", (exc["id"],)
        ).fetchall()]
        row = dict(exc)
        row["text_run_ids"] = frag_ids
        report.append(row)
    conn.close()
    return report


def _write_exception_report_json(exceptions, document_id, lang, out_path: Path):
    out_path.write_text(json.dumps({
        "document_id": document_id,
        "target_language": lang,
        "total_exceptions": len(exceptions),
        "exceptions": exceptions,
    }, indent=2, ensure_ascii=False))


CATEGORY_LABELS = {
    "translation_error": "Inconsistent translation",
    "missing_translation": "Missing translation",
    "layout_risk_heuristic": "Possible layout risk",
}

SEVERITY_COLORS = {"HIGH": "#f8d7da", "MED": "#fff3cd", "LOW": "#e2e3e5"}
SEVERITY_TEXT_COLORS = {"HIGH": "#721c24", "MED": "#856404", "LOW": "#383d41"}


def _write_exception_report_html(exceptions, document_id, lang, out_path: Path):
    """
    A plain-English, single-file HTML report - the primary/default download.
    Each row: category, severity, description (already includes a
    "Page N, Frame F" location, produced by exception_checks.py), grouped
    by severity (HIGH first) then category, matching the order they're
    already stored in from _fetch_exceptions.
    """
    lang_label = LANGUAGE_LABELS.get(lang, lang)

    if not exceptions:
        body = '<p class="empty">No exceptions found for this language. 🎉</p>'
    else:
        rows = []
        current_severity = None
        for exc in exceptions:
            if exc["severity"] != current_severity:
                current_severity = exc["severity"]
                rows.append(f'<h2 class="severity-heading">{current_severity} severity</h2>')
            bg = SEVERITY_COLORS.get(exc["severity"], "#e2e3e5")
            fg = SEVERITY_TEXT_COLORS.get(exc["severity"], "#383d41")
            category_label = CATEGORY_LABELS.get(exc["category"], exc["category"])
            rows.append(f'''
            <div class="exception-card">
              <div class="exception-header">
                <span class="badge" style="background:{bg};color:{fg}">{exc["severity"]}</span>
                <span class="category">{category_label}</span>
              </div>
              <p class="description">{escape(exc["description"])}</p>
            </div>''')
        body = "\n".join(rows)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Exception report - {lang_label}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 760px; margin: 40px auto; color: #1a1a1a; padding: 0 20px; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .subtitle {{ color: #666; margin-bottom: 28px; }}
  .severity-heading {{ font-size: 15px; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin: 28px 0 10px 0; border-bottom: 1px solid #eee; padding-bottom: 4px; }}
  .severity-heading:first-of-type {{ margin-top: 0; }}
  .exception-card {{ border: 1px solid #ddd; border-radius: 8px; padding: 14px 18px; margin-bottom: 10px; }}
  .exception-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .badge {{ font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 10px; letter-spacing: 0.03em; }}
  .category {{ font-weight: 600; font-size: 14px; }}
  .description {{ margin: 0; color: #333; line-height: 1.5; font-size: 14px; }}
  .empty {{ color: #666; font-size: 15px; }}
  .footer {{ margin-top: 32px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
  <h1>Exception report - {lang_label}</h1>
  <p class="subtitle">{len(exceptions)} exception(s) found for document #{document_id}.</p>
  {body}
  <p class="footer">Generated by Rosetta. A machine-readable version of this same data is available as a JSON download alongside this report.</p>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html", languages=LANGUAGE_LABELS)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("idml_zip")
    langs = request.form.getlist("langs")

    if not file or file.filename == "":
        return render_template("index.html", languages=LANGUAGE_LABELS, error="Please choose a .zip file to upload."), 400
    if not langs:
        return render_template("index.html", languages=LANGUAGE_LABELS, error="Select at least one target language."), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True)

    upload_path = job_dir / "upload.zip"
    file.save(upload_path)

    ok, message, idml_member = validate_upload_zip(upload_path)
    if not ok:
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_template("index.html", languages=LANGUAGE_LABELS, error=message), 400

    try:
        extract_dir = job_dir / "uploaded_contents"
        with zipfile.ZipFile(upload_path) as zf:
            zf.extractall(extract_dir)

        idml_source_path = extract_dir / idml_member
        idml_filename = Path(idml_member).name

        links_dir = find_subdir(extract_dir, "Links")
        if links_dir is None:
            raise RuntimeError("Links folder disappeared after extraction (unexpected).")

        idml_extracted_dir = job_dir / "idml_extracted"
        with zipfile.ZipFile(idml_source_path) as zf:
            zf.extractall(idml_extracted_dir)
        shutil.copytree(links_dir, idml_extracted_dir / "Links")
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_template(
            "index.html", languages=LANGUAGE_LABELS,
            error=f"Couldn't unpack the uploaded file: {e}"
        ), 400

    _new_job(job_id, langs)
    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id, job_dir, idml_extracted_dir, idml_filename, langs),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("job_page", job_id=job_id))


@app.route("/job/<job_id>")
def job_page(job_id):
    if _get_job(job_id) is None:
        abort(404)
    return render_template("job.html", job_id=job_id, languages=LANGUAGE_LABELS)


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>/<lang>/<kind>")
def download(job_id, lang, kind):
    job = _get_job(job_id)
    if job is None or job["status"] != "done":
        abort(404)
    result = job["results"].get(lang)
    if not result:
        abort(404)
    key = {
        "delivery": "delivery_zip_path",
        "exceptions": "exception_report_html_path",       # default: human-readable
        "exceptions_json": "exception_report_json_path",   # secondary: machine-readable
    }.get(kind)
    if key is None or key not in result:
        abort(404)
    path = Path(result[key])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print("OpenAI API key stays server-side only - never sent to the browser.")
    print(f"Open http://127.0.0.1:{port} in your browser.")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
