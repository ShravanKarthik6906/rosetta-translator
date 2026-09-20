"""
Rosetta v3 - Stage 3b orchestration: run the full overflow-resolution
pipeline (see overflow.py for tier definitions) across every translated
fragment in a document, store results in overflow_resolutions, and record
exceptions for anything that needed retranslation (always, for human
review - tier 4 changes content, not just layout) or is still unresolved.

*** LIVE PAID API CALL WARNING *** - tier 4 (retranslate_shorter) is a
real, billed, non-deterministic call. This script only invokes it for
fragments that have genuinely failed tiers 1-3 (measured, not guessed),
and reports exactly how many calls it made. Don't re-run this file
casually - re-running re-checks fragments that already resolved cleanly
(no API cost, DB reads only) but will re-call the API for anything still
unresolved, which is by definition non-deterministic across runs.
"""

import sqlite3
import sys
from datetime import datetime, timezone

from overflow import StyleResolver, get_frame_geometry, resolve_overflow, retranslate_shorter


def run_overflow_checks(db_path, document_id, idml_extracted_dir, target_languages,
                         api_key=None, model=None, dry_run=False):
    import os
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    styles_path = f"{idml_extracted_dir}/Resources/Styles.xml"
    resolver = StyleResolver(styles_path)

    rows = conn.execute("""
        SELECT t.id, t.raw_text, t.character_style, t.paragraph_style,
               tr.target_language, tr.translated_text, sp.frame_self_id
        FROM text_runs t
        JOIN translations tr ON tr.text_run_id = t.id
        LEFT JOIN story_placements sp ON sp.story_id = t.story_id
        WHERE t.needs_translation=1 AND tr.status='translated' AND tr.engine != 'dry_run'
          AND tr.translated_text IS NOT NULL AND TRIM(tr.translated_text) <> ''
          AND tr.target_language IN ({})
    """.format(",".join("?" * len(target_languages))), target_languages).fetchall()

    client = None
    if not dry_run:
        from openai import OpenAI
        from translate import DEFAULT_MODEL, LANGUAGE_NAMES
        client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        model = model or DEFAULT_MODEL
    else:
        from translate import LANGUAGE_NAMES

    geo_cache = {}
    stats = {"fits": 0, "fits_wrapped": 0, "fits_shrunk": 0, "fits_retranslated": 0,
              "unresolved": 0, "no_geometry": 0, "retranslate_calls": 0}

    # dry_run is preview-only: no DB writes of any kind (no exceptions, no
    # overflow_resolutions rows, no clearing of prior real results).
    if not dry_run:
        conn.execute("DELETE FROM overflow_resolutions WHERE text_run_id IN "
                     "(SELECT id FROM text_runs) AND target_language IN ({})".format(
                         ",".join("?" * len(target_languages))), target_languages)

    for r in rows:
        frame_id = r["frame_self_id"]
        if not frame_id:
            stats["no_geometry"] += 1
            continue
        if frame_id not in geo_cache:
            geo_cache[frame_id] = get_frame_geometry(idml_extracted_dir, frame_id)
        geo = geo_cache[frame_id]
        if geo is None:
            stats["no_geometry"] += 1
            continue

        style = resolver.resolve(r["character_style"], r["paragraph_style"])
        result = resolve_overflow(r["translated_text"], style["family"], style["font_style"],
                                   style["point_size"], style["leading"], geo, r["target_language"])

        retranslated_text = None

        if result["resolution"] == "unresolved" and not dry_run:
            overage_pct = (result["detail"]["wrapped_height_pt"] / geo["frame_height_pt"] - 1) * 100
            overage_pct = max(overage_pct, 10)  # floor so the ask is never "0% shorter"
            stats["retranslate_calls"] += 1
            retranslated_text = retranslate_shorter(
                r["raw_text"], r["translated_text"], LANGUAGE_NAMES[r["target_language"]],
                overage_pct, client, model
            )
            retry = resolve_overflow(retranslated_text, style["family"], style["font_style"],
                                      style["point_size"], style["leading"], geo, r["target_language"])
            if retry["resolution"] != "unresolved":
                result = {**retry, "resolution": "fits_retranslated"}
            else:
                result = retry

            # Tier 4 always gets flagged for human review, regardless of outcome -
            # it changes content, not just layout, unlike tiers 1-3.
            font_note = " (measured with a substitute font, not the real licensed one)" \
                if "Substitute" in result["detail"].get("font_note", "") else ""
            _insert_exception(
                conn, document_id, r["target_language"], "overflow_retranslation_review", "MED",
                f"text_run_id={r['id']} was retranslated for brevity to resolve a real measured "
                f"layout overflow{font_note}. Original: {r['translated_text']!r}. "
                f"Retranslated: {retranslated_text!r}. Please confirm meaning was preserved.",
                [r["id"]]
            )

        stats[result["resolution"]] += 1

        if dry_run:
            continue  # preview only - no DB writes below this point

        if result["resolution"] == "unresolved":
            font_note = " (measured with a substitute font, not the real licensed one)" \
                if "Substitute" in result["detail"].get("font_note", "") else ""
            _insert_exception(
                conn, document_id, r["target_language"], "unresolved_overflow", "HIGH",
                f"text_run_id={r['id']} does not fit its frame even after a font-size reduction"
                + (" and a retranslation attempt" if retranslated_text else "")
                + f"{font_note}. Text needs {result['detail']['lines_needed']} line(s) "
                f"({result['detail']['wrapped_height_pt']:.1f}pt) but the frame is only "
                f"{geo['frame_height_pt']:.1f}pt tall. Needs human review.",
                [r["id"]]
            )

        conn.execute("""
            INSERT INTO overflow_resolutions
                (text_run_id, target_language, resolution, original_point_size, final_point_size,
                 shrink_pct, retranslated_text, lines_needed, frame_width_pt, frame_height_pt,
                 text_width_pt, font_family, font_style, font_is_substitute, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(text_run_id, target_language) DO UPDATE SET
                resolution=excluded.resolution, original_point_size=excluded.original_point_size,
                final_point_size=excluded.final_point_size, shrink_pct=excluded.shrink_pct,
                retranslated_text=excluded.retranslated_text, lines_needed=excluded.lines_needed,
                frame_width_pt=excluded.frame_width_pt, frame_height_pt=excluded.frame_height_pt,
                text_width_pt=excluded.text_width_pt, font_family=excluded.font_family,
                font_style=excluded.font_style, font_is_substitute=excluded.font_is_substitute,
                checked_at=excluded.checked_at
        """, (
            r["id"], r["target_language"], result["resolution"], style["point_size"],
            result["final_point_size"], result["shrink_pct"], retranslated_text,
            result["detail"]["lines_needed"], geo["usable_width_pt"], geo["frame_height_pt"],
            result["detail"]["width_pt"], style["family"], style["font_style"],
            1 if "Substitute" in result["detail"].get("font_note", "") else 0,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()

    conn.close()
    return stats


def _insert_exception(conn, document_id, target_language, category, severity, description, text_run_ids):
    cur = conn.execute(
        "INSERT INTO exceptions (document_id, target_language, category, severity, description, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (document_id, target_language, category, severity, description, datetime.now(timezone.utc).isoformat())
    )
    exception_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO exception_fragments (exception_id, text_run_id) VALUES (?, ?)",
        [(exception_id, tr_id) for tr_id in text_run_ids]
    )


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "rosetta.db"
    document_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    idml_extracted_dir = sys.argv[3] if len(sys.argv) > 3 else "idml_extracted"
    langs = sys.argv[4].split(",") if len(sys.argv) > 4 else ["es", "ar"]
    dry_run = "--dry-run" in sys.argv

    stats = run_overflow_checks(db_path, document_id, idml_extracted_dir, langs, dry_run=dry_run)
    print(stats)
