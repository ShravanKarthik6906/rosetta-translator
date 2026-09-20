"""
Rosetta v3 - Stage 4b: Exception report
Runs three data-quality checks against translated content for a given
document + target_language and records findings in `exceptions` /
`exception_fragments`:

  - translation_error: the same English text (exact raw_text match)
    appears in multiple text_runs but was translated inconsistently.
  - missing_translation: a text_run that needs translation has no usable
    translation (no row, status='error', or blank translated_text).
  - layout_risk_heuristic: translated text is meaningfully longer/shorter
    than the original by character count. This is a cheap proxy for
    InDesign frame overflow risk, NOT a measurement of actual overflow -
    InDesign doesn't expose text-frame overset state outside the running
    application, so a real check would require InDesign automation (a
    separate future task). For raw text >= SHORT_TEXT_THRESHOLD chars,
    flagged by percentage difference (MED at >40-70%, HIGH beyond 70%).
    Below that, percentage is noise (a single letter growing by one
    character is a 100%+ "difference" that means nothing for overflow
    risk), so flagged by absolute character-count difference instead
    (MED at >3 chars, HIGH at >6 chars).

Each run first clears prior exceptions for that (document_id, target_language,
category) so re-running is idempotent, then re-inserts fresh findings.
"""

import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

LENGTH_DIFF_MED_THRESHOLD = 40   # >40% triggers MED (raw text >= SHORT_TEXT_THRESHOLD chars)
LENGTH_DIFF_HIGH_THRESHOLD = 70  # >70% escalates to HIGH
SHORT_TEXT_THRESHOLD = 4         # below this many chars, percentage is noise - use absolute diff instead
SHORT_TEXT_ABS_MED_THRESHOLD = 3   # >3 chars absolute difference triggers MED
SHORT_TEXT_ABS_HIGH_THRESHOLD = 6  # >6 chars absolute difference escalates to HIGH
VARIANT_COUNT_HIGH_THRESHOLD = 3  # 3+ distinct translations of the same phrase escalates to HIGH


def _now():
    return datetime.now(timezone.utc).isoformat()


def _page_refs_for_stories(conn, story_ids, limit=3):
    """Returns a human-readable 'Page N, Frame F' summary for a set of story_ids."""
    if not story_ids:
        return "Page unknown"
    placeholders = ",".join("?" * len(story_ids))
    rows = conn.execute(f"""
        SELECT DISTINCT page_name, frame_self_id
        FROM story_placements
        WHERE story_id IN ({placeholders})
        ORDER BY CAST(page_name AS INTEGER), frame_self_id
    """, list(story_ids)).fetchall()
    if not rows:
        return "Page unknown (no frame placement found for this story)"
    refs = [f"Page {r[0]}, Frame {r[1]}" for r in rows]
    if len(refs) <= limit:
        return "; ".join(refs)
    return "; ".join(refs[:limit]) + f"; +{len(refs) - limit} more location(s)"


def _clear_category(conn, document_id, target_language, category):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM exceptions WHERE document_id=? AND target_language=? AND category=?",
        (document_id, target_language, category)
    ).fetchall()]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM exception_fragments WHERE exception_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM exceptions WHERE id IN ({placeholders})", ids)


def _insert_exception(conn, document_id, target_language, category, severity, description, text_run_ids):
    cur = conn.execute(
        "INSERT INTO exceptions (document_id, target_language, category, severity, description, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (document_id, target_language, category, severity, description, _now())
    )
    exception_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO exception_fragments (exception_id, text_run_id) VALUES (?, ?)",
        [(exception_id, tr_id) for tr_id in text_run_ids]
    )
    return exception_id


def check_translation_error(conn, document_id, target_language):
    """Same English text translated inconsistently across occurrences."""
    _clear_category(conn, document_id, target_language, "translation_error")

    rows = conn.execute("""
        SELECT t.id AS text_run_id, t.story_id, t.raw_text, tr.translated_text
        FROM text_runs t
        JOIN stories s ON s.id = t.story_id
        JOIN translations tr ON tr.text_run_id = t.id AND tr.target_language = ?
        WHERE s.document_id = ? AND t.needs_translation = 1 AND tr.status = 'translated'
          AND tr.translated_text IS NOT NULL AND TRIM(tr.translated_text) <> ''
    """, (target_language, document_id)).fetchall()

    groups = defaultdict(list)  # raw_text -> [(text_run_id, story_id, translated_text)]
    for text_run_id, story_id, raw_text, translated_text in rows:
        groups[raw_text].append((text_run_id, story_id, translated_text))

    count = 0
    for raw_text, items in groups.items():
        if len(items) < 2:
            continue
        variants = Counter(t for _, _, t in items)
        if len(variants) < 2:
            continue  # all occurrences agree - not an exception

        severity = "HIGH" if len(variants) >= VARIANT_COUNT_HIGH_THRESHOLD else "MED"
        variant_summary = ", ".join(f"{repr(v)} ({c}x)" for v, c in variants.most_common())
        story_ids = {story_id for _, story_id, _ in items}
        page_refs = _page_refs_for_stories(conn, story_ids)
        description = (
            f"English text {raw_text!r} was translated {len(variants)} different ways across "
            f"{len(items)} occurrences: {variant_summary}. {page_refs}."
        )
        text_run_ids = [tr_id for tr_id, _, _ in items]
        _insert_exception(conn, document_id, target_language, "translation_error", severity, description, text_run_ids)
        count += 1

    return count


def check_missing_translation(conn, document_id, target_language):
    """A text_run needing translation has no usable translation for this language."""
    _clear_category(conn, document_id, target_language, "missing_translation")

    rows = conn.execute("""
        SELECT t.id AS text_run_id, t.story_id, t.raw_text, tr.status
        FROM text_runs t
        JOIN stories s ON s.id = t.story_id
        LEFT JOIN translations tr ON tr.text_run_id = t.id AND tr.target_language = ?
        WHERE s.document_id = ? AND t.needs_translation = 1
          AND (tr.id IS NULL OR tr.status = 'error'
               OR tr.translated_text IS NULL OR TRIM(tr.translated_text) = '')
    """, (target_language, document_id)).fetchall()

    count = 0
    for text_run_id, story_id, raw_text, status in rows:
        reason = "no translation row exists" if status is None else \
                 ("translation status='error'" if status == "error" else "translated_text is blank")
        page_refs = _page_refs_for_stories(conn, {story_id})
        preview = raw_text[:60]
        description = (
            f"text_run_id={text_run_id} ({preview!r}) has no usable {target_language} translation: "
            f"{reason}. {page_refs}."
        )
        _insert_exception(conn, document_id, target_language, "missing_translation", "HIGH", description, [text_run_id])
        count += 1

    return count


def check_layout_risk_heuristic(conn, document_id, target_language):
    """Translated text length differs substantially from the original - possible overflow risk."""
    _clear_category(conn, document_id, target_language, "layout_risk_heuristic")

    rows = conn.execute("""
        SELECT t.id AS text_run_id, t.story_id, t.raw_text, tr.translated_text
        FROM text_runs t
        JOIN stories s ON s.id = t.story_id
        JOIN translations tr ON tr.text_run_id = t.id AND tr.target_language = ?
        WHERE s.document_id = ? AND t.needs_translation = 1 AND tr.status = 'translated'
          AND tr.translated_text IS NOT NULL AND TRIM(tr.translated_text) <> ''
    """, (target_language, document_id)).fetchall()

    count = 0
    for text_run_id, story_id, raw_text, translated_text in rows:
        raw_len = len(raw_text)
        if raw_len == 0:
            continue
        trans_len = len(translated_text)
        abs_diff = abs(trans_len - raw_len)
        direction = "longer" if trans_len > raw_len else "shorter"

        if len(raw_text.strip()) < SHORT_TEXT_THRESHOLD:
            # Percentage is noise on very short strings (a single letter growing by
            # one character is a 100%+ "difference"), so use an absolute char-count
            # difference instead.
            if abs_diff <= SHORT_TEXT_ABS_MED_THRESHOLD:
                continue
            severity = "HIGH" if abs_diff > SHORT_TEXT_ABS_HIGH_THRESHOLD else "MED"
            metric_note = f"{abs_diff} character(s) {direction}"
        else:
            pct_diff = abs_diff / raw_len * 100
            if pct_diff <= LENGTH_DIFF_MED_THRESHOLD:
                continue
            severity = "HIGH" if pct_diff > LENGTH_DIFF_HIGH_THRESHOLD else "MED"
            metric_note = f"{pct_diff:.0f}% {direction}"

        page_refs = _page_refs_for_stories(conn, {story_id})
        description = (
            f"Translated text is {metric_note} than the original "
            f"({raw_len} -> {trans_len} chars). This is a length-based estimate only, NOT a "
            f"confirmed InDesign overflow measurement - real overflow checking would require "
            f"InDesign automation, which is a separate future task. {page_refs}."
        )
        _insert_exception(conn, document_id, target_language, "layout_risk_heuristic", severity, description, [text_run_id])
        count += 1

    return count


def run_all_checks(db_path, document_id, target_languages, progress_callback=None):
    """
    progress_callback(checks_done, checks_total), if given, is called after
    each (language, check) pair finishes - UI progress only, optional.
    """
    conn = sqlite3.connect(db_path)
    results = {}
    checks_total = len(target_languages) * 3
    checks_done = 0
    for lang in target_languages:
        results[lang] = {}
        for name, fn in (
            ("translation_error", check_translation_error),
            ("missing_translation", check_missing_translation),
            ("layout_risk_heuristic", check_layout_risk_heuristic),
        ):
            results[lang][name] = fn(conn, document_id, lang)
            checks_done += 1
            if progress_callback:
                progress_callback(checks_done, checks_total)
        conn.commit()
    conn.close()
    return results


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "rosetta.db"
    document_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    langs = sys.argv[3].split(",") if len(sys.argv) > 3 else ["es", "ar"]

    results = run_all_checks(db_path, document_id, langs)
    for lang, counts in results.items():
        print(f"[{lang}] {counts}")
