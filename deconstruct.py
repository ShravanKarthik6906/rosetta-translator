"""
Rosetta v3 - Stage 1: Deconstruct
Parses an unzipped IDML package's Stories/*.xml files and stores every
piece of live text (<Content>) into a SQLite database, preserving:
  - which story it came from
  - its order within that story (needed later for reconstruction)
  - paragraph style + character style (formatting context)
  - whether it's a table cell, and which cell
  - a simple heuristic flag for whether it looks like it needs translation
"""

import sqlite3
import re
import unicodedata
from pathlib import Path
from lxml import etree

IDML_NS = "{http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging}"


def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    schema = Path(__file__).parent / "db" / "schema.sql"
    conn.executescript(schema.read_text())
    conn.commit()
    return conn


def _is_invisible_char(ch: str) -> bool:
    """Format (Cf, e.g. zero-width space U+200B) and control (Cc) characters
    carry no visible, translatable content."""
    return unicodedata.category(ch) in ("Cf", "Cc")


# word@word.word - deliberately simple/strict (whole-string match only, via
# looks_translatable's re.fullmatch), not a full RFC 5322 validator.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# http://, https://, or www. followed by anything non-whitespace.
URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)

# US-style phone numbers: optional leading +1/1, optional parens around the
# area code, digits separated by spaces/dots/dashes - e.g. "(555) 123-4567",
# "555-123-4567", "1-800-555-1234", "+1 555 123 4567", "555.123.4567".
PHONE_RE = re.compile(r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def looks_translatable(text: str) -> bool:
    """Heuristic: skip pure numbers, whitespace, punctuation-only,
    invisible/non-printing-only strings (e.g. zero-width spaces), a
    single alphabetic character (e.g. 'g', 'm', 't') - these are almost
    always math variable names, not words - or a standalone email
    address, URL, or phone number, none of which should be translated."""
    stripped = text.strip()
    if not stripped:
        return False
    # invisible-only (zero-width spaces, other format/control chars) -> skip
    if all(_is_invisible_char(ch) for ch in stripped):
        return False
    # single letter (math variable, e.g. "g", "m", "w", "t") -> skip
    if len(stripped) == 1 and stripped.isalpha():
        return False
    # pure number (int, float, simple fraction like "1/2") -> skip
    if re.fullmatch(r"[\d\.\,/\-\$%\s]+", stripped):
        return False
    # standalone email / URL / phone number -> skip (whole-string match,
    # so a sentence that merely mentions one is still translated normally)
    if EMAIL_RE.fullmatch(stripped) or URL_RE.fullmatch(stripped) or PHONE_RE.fullmatch(stripped):
        return False
    return True


def local_tag(elem) -> str:
    """Strip namespace from a tag name, e.g. '{ns}Story' -> 'Story'."""
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def walk_story(root, story_id, conn):
    """
    Walk a parsed Story XML tree in document order, tracking the current
    paragraph style / character style / table context, and inserting
    every <Content> found (at any depth - including inside tables, and
    inside nested Groups/Frames that sit within a CharacterStyleRange).
    """
    seq = [0]        # mutable counter: order of <Content> within story
    para_counter = [-1]  # mutable counter: increments once per ParagraphStyleRange

    def recurse(elem, para_style, char_style, table_self, cell_name, para_seq):
        if not isinstance(elem.tag, str):
            return  # comments / processing instructions have no useful tag
        tag = local_tag(elem)

        if tag == "ParagraphStyleRange":
            para_style = elem.get("AppliedParagraphStyle", para_style)
            para_counter[0] += 1
            para_seq = para_counter[0]
        elif tag == "CharacterStyleRange":
            char_style = elem.get("AppliedCharacterStyle", char_style)
        elif tag == "Table":
            table_self = elem.get("Self", table_self)
        elif tag == "Cell":
            cell_name = elem.get("Name", cell_name)

        if tag == "Content":
            text = elem.text or ""
            is_table = 1 if table_self else 0
            needs_tr = 1 if looks_translatable(text) else 0
            conn.execute(
                """INSERT INTO text_runs
                   (story_id, sequence_index, paragraph_style, character_style,
                    raw_text, is_table_cell, table_self_id, cell_name,
                    needs_translation, paragraph_seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (story_id, seq[0], para_style, char_style, text,
                 is_table, table_self, cell_name, needs_tr, para_seq)
            )
            seq[0] += 1

        for child in elem:
            if not isinstance(child.tag, str):
                continue  # skip comments / processing instructions (e.g. <?ACE 7?>)
            recurse(child, para_style, char_style, table_self, cell_name, para_seq)

    recurse(root, None, None, None, None, -1)


def deconstruct(idml_extracted_dir: str, idml_filename: str, db_path: str, progress_callback=None):
    """
    progress_callback(stories_done, stories_total), if given, is called after
    each story file is parsed - purely for UI progress reporting, optional.
    """
    extracted = Path(idml_extracted_dir)
    stories_dir = extracted / "Stories"
    assert stories_dir.exists(), f"No Stories/ folder found in {extracted}"

    conn = init_db(db_path)
    cur = conn.execute(
        "INSERT INTO documents (source_idml_filename) VALUES (?)",
        (idml_filename,)
    )
    document_id = cur.lastrowid

    story_files = sorted(stories_dir.glob("Story_*.xml"))
    total_runs = 0

    for i, story_path in enumerate(story_files):
        story_self_id = story_path.stem.replace("Story_", "")
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(str(story_path), parser)
        root = tree.getroot()

        cur = conn.execute(
            "INSERT INTO stories (document_id, story_self_id, source_file) VALUES (?, ?, ?)",
            (document_id, story_self_id, f"Stories/{story_path.name}")
        )
        story_id = cur.lastrowid

        before = conn.execute("SELECT COUNT(*) FROM text_runs").fetchone()[0]
        walk_story(root, story_id, conn)
        after = conn.execute("SELECT COUNT(*) FROM text_runs").fetchone()[0]
        total_runs += (after - before)

        if progress_callback:
            progress_callback(i + 1, len(story_files))

    conn.commit()

    n_stories = len(story_files)
    n_translatable = conn.execute(
        "SELECT COUNT(*) FROM text_runs WHERE needs_translation = 1"
    ).fetchone()[0]
    n_skipped = conn.execute(
        "SELECT COUNT(*) FROM text_runs WHERE needs_translation = 0"
    ).fetchone()[0]

    print(f"Document ID: {document_id}")
    print(f"Stories parsed: {n_stories}")
    print(f"Total <Content> runs found: {total_runs}")
    print(f"  -> flagged translatable: {n_translatable}")
    print(f"  -> flagged skip (numbers/blank/etc.): {n_skipped}")

    conn.close()
    return document_id


if __name__ == "__main__":
    import sys
    idml_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/idml_work/extracted"
    idml_name = sys.argv[2] if len(sys.argv) > 2 else "RCM07_NA_SW_U01_L03.idml"
    db_path = sys.argv[3] if len(sys.argv) > 3 else "/home/claude/rosetta_v3/rosetta.db"
    deconstruct(idml_dir, idml_name, db_path)
