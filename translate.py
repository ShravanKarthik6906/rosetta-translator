"""
Rosetta v3 - Stage 2: Translate

*** LIVE PAID API CALL WARNING ***
translate_document() and call_openai_translate() below hit the real OpenAI
API and cost real money per call - they are NOT idempotent-safe to blindly
re-run. In particular, do NOT re-run this whole script (or a scratch
script importing it) just to extract a value from earlier output - each
run is a fresh, non-deterministic call. If you need to inspect or reuse a
prior result, copy the ALREADY-PRINTED output verbatim rather than
re-invoking the API - see the rosetta_v3 session history for an example
of this going wrong (a scratch script was appended to and re-run "to add
a print statement," silently firing a second real translation call).
When testing changes to the prompt/parsing logic, use --dry-run (no API
calls) or scope tightly with --story-id/--paragraph-seq/--limit first.

For each paragraph (group of text_run fragments sharing the same
paragraph_seq), rebuilds the full sentence with numbered tags around
each translatable fragment, e.g.:

    Here are some examples of <t713>proportional relationships</t713> that you may be familiar with.

...sends the WHOLE sentence to the translation model at once (so it has
full grammatical context), and asks it to translate only the tagged
portions, allowed to reorder tags as needed for correct target-language
grammar. The result is parsed back into one translated string per
original fragment and stored in the `translations` table.

Non-translatable fragments (numbers, blank separators, etc.) are NOT
wrapped in tags - they're left as fixed anchor text so the model sees
real surrounding context but never touches them. Their "translation" is
just the original text, copied through unchanged.
"""

import sqlite3
import re
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load secrets (OPENAI_API_KEY, ROSETTA_OPENAI_MODEL) from .env.
# This is intentionally only done here in translate.py - the API key is
# scoped to the translation step only and must never be read/used by
# any other script.
load_dotenv()

DEFAULT_MODEL = os.environ.get("ROSETTA_OPENAI_MODEL", "gpt-4o")

TAG_RE = re.compile(r"<t(\d+)>(.*?)</t\1>", re.DOTALL)

LANGUAGE_NAMES = {
    "es": "Spanish",
    "ar": "Arabic",
}


def build_tagged_paragraph(fragments):
    """
    fragments: list of sqlite3.Row with (id, raw_text, needs_translation),
    already ordered by sequence_index.
    Returns (tagged_string, edge_whitespace):
      - tagged_string: translatable fragments wrapped as <tN>...</tN>, with
        each fragment's leading/trailing whitespace stripped before tagging
        (models don't reliably preserve incidental whitespace at tag edges).
      - edge_whitespace: {fragment_id: (leading_ws, trailing_ws)} so the
        original whitespace can be glued back onto the model's output.
    """
    parts = []
    edge_whitespace = {}
    for f in fragments:
        if f["needs_translation"]:
            text = f["raw_text"]
            lstripped = text.lstrip()
            leading_ws = text[:len(text) - len(lstripped)]
            core = lstripped.rstrip()
            trailing_ws = lstripped[len(core):]
            edge_whitespace[f["id"]] = (leading_ws, trailing_ws)
            parts.append(f'<t{f["id"]}>{core}</t{f["id"]}>')
        else:
            parts.append(f["raw_text"])
    return "".join(parts), edge_whitespace


def parse_tagged_result(translated_text: str) -> dict:
    """Extract {fragment_id: translated_fragment_text} from model output."""
    result = {}
    for match in TAG_RE.finditer(translated_text):
        frag_id = int(match.group(1))
        result[frag_id] = match.group(2)
    return result


def call_openai_translate(tagged_paragraph: str, target_lang_name: str, context_note: str, client, model=None, usage_callback=None):
    """
    Sends the tagged paragraph to OpenAI chat completions.
    Returns the raw translated string (still containing <tN> tags).

    *** LIVE PAID API CALL - not deterministic, not free to re-run. ***
    Every invocation is billed and can return different wording than a
    prior call to the same arguments. Never call this just to "regenerate"
    output you already have printed somewhere - reuse the earlier output.

    usage_callback(model, prompt_tokens, completion_tokens), if given, is
    called with the real token counts from this call - for UI/cost
    reporting only, optional.
    """
    model = model or DEFAULT_MODEL
    system_prompt = (
        f"You are translating a K-12 math worksheet from English into {target_lang_name}. "
        "The text contains numbered tags like <t123>...</t123> wrapping specific phrases. "
        "Translate the ENTIRE sentence naturally and grammatically into the target language, "
        "but you MUST keep every <tN> ... </tN> tag in your output, wrapping the translated "
        "version of exactly the words that were originally inside that tag. "
        "You MAY reorder the tags relative to each other and relative to the surrounding fixed "
        "text if the target language's grammar requires it, to keep the sentence natural. "
        "Do NOT translate or alter any text that is outside of a tag - copy it through exactly "
        "as-is, since it may be a number, a name, or punctuation. "
        "Preserve the mathematical and educational meaning precisely - do not simplify or "
        "paraphrase beyond what is needed for a natural, correct translation. "
        "Output ONLY the translated sentence with tags intact - no explanations, no extra text."
    )
    user_prompt = tagged_paragraph
    if context_note:
        user_prompt = f"[Context: {context_note}]\n{tagged_paragraph}"

    # Some newer models (e.g. gpt-5-mini) only support the default
    # temperature (1) and reject any explicit non-default value with a
    # 400 error. Only send temperature for models known to support
    # overriding it.
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    if not model.startswith("gpt-5"):
        kwargs["temperature"] = 0.2

    # --- money spent here: one real, billed, non-deterministic API call ---
    response = client.chat.completions.create(**kwargs)
    if usage_callback and response.usage:
        usage_callback(model, response.usage.prompt_tokens, response.usage.completion_tokens)
    return response.choices[0].message.content.strip()


def translate_document(db_path: str, document_id: int, target_langs, api_key=None, model=None, dry_run=False, paragraph_limit=None, only_paragraph_seq=None, only_story_id=None, progress_callback=None, usage_callback=None):
    """
    progress_callback(paragraphs_done, paragraphs_total), if given, is called
    after each paragraph (across all target_langs) finishes - UI progress
    only, optional. usage_callback is forwarded to call_openai_translate for
    real token-usage reporting, optional.
    """
    model = model or DEFAULT_MODEL
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    client = None
    if not dry_run:
        from openai import OpenAI
        client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    if only_paragraph_seq is not None and only_story_id is not None:
        paragraphs = conn.execute("""
            SELECT DISTINCT tr.story_id, tr.paragraph_seq
            FROM text_runs tr
            JOIN stories s ON tr.story_id = s.id
            WHERE s.document_id = ? AND tr.paragraph_seq = ? AND tr.story_id = ?
            ORDER BY tr.story_id, tr.paragraph_seq
        """, (document_id, only_paragraph_seq, only_story_id)).fetchall()
    elif only_paragraph_seq is not None:
        paragraphs = conn.execute("""
            SELECT DISTINCT tr.story_id, tr.paragraph_seq
            FROM text_runs tr
            JOIN stories s ON tr.story_id = s.id
            WHERE s.document_id = ? AND tr.paragraph_seq = ?
            ORDER BY tr.story_id, tr.paragraph_seq
        """, (document_id, only_paragraph_seq)).fetchall()
    else:
        paragraphs = conn.execute("""
            SELECT DISTINCT tr.story_id, tr.paragraph_seq
            FROM text_runs tr
            JOIN stories s ON tr.story_id = s.id
            WHERE s.document_id = ?
            ORDER BY tr.story_id, tr.paragraph_seq
        """, (document_id,)).fetchall()

    if paragraph_limit is not None:
        paragraphs = paragraphs[:paragraph_limit]

    stats = {lang: {"translated": 0, "skipped_no_content": 0, "errors": 0} for lang in target_langs}

    for para_index, p in enumerate(paragraphs):
        fragments = conn.execute("""
            SELECT id, raw_text, needs_translation, context_note
            FROM text_runs
            WHERE story_id = ? AND paragraph_seq = ?
            ORDER BY sequence_index
        """, (p["story_id"], p["paragraph_seq"])).fetchall()

        if not any(f["needs_translation"] for f in fragments):
            for lang in target_langs:
                stats[lang]["skipped_no_content"] += 1
            if progress_callback:
                progress_callback(para_index + 1, len(paragraphs))
            continue

        tagged, edge_whitespace = build_tagged_paragraph(fragments)
        context_notes = [f["context_note"] for f in fragments if f["context_note"]]
        context_note = "; ".join(context_notes) if context_notes else None

        for lang in target_langs:
            lang_name = LANGUAGE_NAMES[lang]
            try:
                if dry_run:
                    translated_raw = TAG_RE.sub(
                        lambda m: f'<t{m.group(1)}>[{lang}]{m.group(2)}</t{m.group(1)}>',
                        tagged
                    )
                else:
                    translated_raw = call_openai_translate(tagged, lang_name, context_note, client, model=model, usage_callback=usage_callback)

                per_fragment = parse_tagged_result(translated_raw)

                for f in fragments:
                    if f["needs_translation"]:
                        translated_text = per_fragment.get(f["id"])
                        if translated_text is None:
                            status = "error"
                            error_msg = "tag missing from model output"
                        elif not translated_text.strip():
                            status = "error"
                            error_msg = "model returned empty/blank text for tag"
                            translated_text = None
                        else:
                            leading_ws, trailing_ws = edge_whitespace.get(f["id"], ("", ""))
                            translated_text = leading_ws + translated_text.strip() + trailing_ws
                            status = "translated"
                            error_msg = None
                    else:
                        translated_text = f["raw_text"]
                        status = "skipped"
                        error_msg = None

                    conn.execute("""
                        INSERT INTO translations
                            (text_run_id, target_language, translated_text, engine, status, error_message, translated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(text_run_id, target_language) DO UPDATE SET
                            translated_text=excluded.translated_text,
                            engine=excluded.engine,
                            status=excluded.status,
                            error_message=excluded.error_message,
                            translated_at=excluded.translated_at
                    """, (f["id"], lang, translated_text,
                          "dry_run" if dry_run else model, status, error_msg,
                          datetime.now(timezone.utc).isoformat()))

                    if status == "translated":
                        stats[lang]["translated"] += 1
                    elif status == "error":
                        stats[lang]["errors"] += 1

            except Exception as e:
                for f in fragments:
                    if f["needs_translation"]:
                        conn.execute("""
                            INSERT INTO translations
                                (text_run_id, target_language, translated_text, engine, status, error_message, translated_at)
                            VALUES (?, ?, NULL, ?, 'error', ?, ?)
                            ON CONFLICT(text_run_id, target_language) DO UPDATE SET
                                status='error', error_message=excluded.error_message, translated_at=excluded.translated_at
                        """, (f["id"], lang, "dry_run" if dry_run else model, str(e),
                              datetime.now(timezone.utc).isoformat()))
                        stats[lang]["errors"] += 1

        conn.commit()
        if progress_callback:
            progress_callback(para_index + 1, len(paragraphs))

    print("Translation run complete.")
    for lang in target_langs:
        s = stats[lang]
        print(f"  [{lang}] translated={s['translated']}  errors={s['errors']}  paragraphs_with_no_content={s['skipped_no_content']}")

    conn.close()
    return stats


if __name__ == "__main__":
    # *** Running this file makes LIVE, BILLED API calls unless --dry-run is
    # passed. Don't invoke it just to "check something" - use --dry-run or
    # scope with --story-id/--paragraph-seq/--limit first. ***
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/rosetta_v3/rosetta.db"
    document_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    dry_run = "--dry-run" in sys.argv

    # --langs es,ar  -> restrict which target languages are used (default: es,ar)
    langs = ["es", "ar"]
    for arg in sys.argv:
        if arg.startswith("--langs="):
            langs = [x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip()]

    # --limit N -> restrict to the first N paragraphs found (for small controlled tests)
    limit = None
    for arg in sys.argv:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    # --paragraph-seq N -> restrict to exactly one paragraph_seq value (precise targeting)
    only_seq = None
    for arg in sys.argv:
        if arg.startswith("--paragraph-seq="):
            only_seq = int(arg.split("=", 1)[1])

    # --story-id N -> combined with --paragraph-seq, pins down one exact paragraph
    # instance instead of matching that paragraph_seq across every story
    only_story = None
    for arg in sys.argv:
        if arg.startswith("--story-id="):
            only_story = int(arg.split("=", 1)[1])

    translate_document(db_path, document_id, target_langs=langs, dry_run=dry_run, paragraph_limit=limit, only_paragraph_seq=only_seq, only_story_id=only_story)
