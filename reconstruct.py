"""
Rosetta v3 - Stage 3: Reconstruct
For a given document_id and target_language:
  1. Makes a fresh copy of the extracted IDML folder (never mutates the
     original extracted folder).
  2. For every text_run row (matched by story + sequence_index), finds
     the corresponding <Content> element in that story's XML file (in
     the copy) and replaces its text with the translation from the
     `translations` table for that language.
  3. If target_language == 'ar', flips every StoryDirection attribute
     found anywhere in each story XML from LeftToRightDirection to
     RightToLeftDirection (Arabic reads right-to-left), and applies a
     local AppliedFont override (see ARABIC_FALLBACK_FONT) to every
     CharacterStyleRange whose Content was actually replaced with
     Arabic text - the document's own fonts (Bunday Sans, Myriad Pro,
     etc.) have no Arabic glyphs and render as tofu otherwise. A
     matching FontFamily entry is also added to Resources/Fonts.xml so
     the document's own font metadata stays consistent.
  4. Rewrites every LinkResourceURI (in Stories/, Spreads/ and
     MasterSpreads/ XML) from the absolute file: path baked in at
     authoring time (e.g. file:/Users/.../ProjectFolder/Links/foo.ai,
     which only resolves on the original machine) to a relative
     file:Links/foo.ai reference, so links resolve correctly on any
     machine as long as Links/ sits next to the .idml.
  5. Re-zips the modified folder into a valid .idml file, with the
     `mimetype` file written FIRST and STORED (uncompressed) - some
     IDML/EPUB-style readers require this exact layout. The Links
     folder (if present in the extracted source) is intentionally
     excluded here - it isn't part of a real IDML package, only of the
     delivery bundle.
  6. Bundles the output as a single delivery zip containing the
     translated .idml, an unchanged copy of the Links folder, and -
     for Arabic only - a "Document Fonts" folder with the actual
     ARABIC_FALLBACK_FONT font files, so rendering doesn't depend on
     that font being pre-installed on whatever machine opens the file
     (InDesign auto-detects a Document Fonts folder next to a package).

Fragments with no translation row for the requested language (e.g.
not yet translated) are left with their original English text, and a
warning is printed - this never crashes the run.
"""

import sqlite3
import shutil
import zipfile
import sys
import re
from pathlib import Path
from lxml import etree

IDML_NS = "{http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging}"

# Matches LinkResourceURI="file:<anything>/Links/<filename>" so it can be
# rewritten to a relative file:Links/<filename> reference.
LINK_RESOURCE_URI_RE = re.compile(r'LinkResourceURI="file:[^"]*?/Links/([^"/]+)"')

# The document's own fonts are Latin-only and render Arabic text as tofu.
# Noto Sans Arabic (SIL Open Font License, see fonts/OFL.txt) has full
# Arabic-block glyph coverage and is bundled in the delivery so rendering
# doesn't depend on the opening machine already having an Arabic font.
ARABIC_FALLBACK_FONT = "Noto Sans Arabic"
ARABIC_FALLBACK_FONT_FILES = {
    "Regular": "NotoSansArabic-Regular.ttf",
    "Bold": "NotoSansArabic-Bold.ttf",
}
FONTS_ASSET_DIR = Path(__file__).parent / "fonts"


def local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def find_enclosing_character_style_range(elem):
    """
    Content elements aren't always a direct child of their CharacterStyleRange
    (e.g. an inline Group/Frame anchored within a run can sit in between), so
    walk up the ancestor chain to find the nearest one.
    """
    node = elem.getparent()
    while node is not None:
        if isinstance(node.tag, str) and local_tag(node) == "CharacterStyleRange":
            return node
        node = node.getparent()
    return None


def apply_font_override(char_style_range, font_name):
    """
    Add/update a local <Properties><AppliedFont>...</AppliedFont></Properties>
    on this CharacterStyleRange. This overrides the font just for this one
    run, without touching the shared CharacterStyle resource it references
    (which is reused by other, correctly-rendering text elsewhere).
    """
    if char_style_range is None:
        return False

    props = None
    for child in char_style_range:
        if isinstance(child.tag, str) and local_tag(child) == "Properties":
            props = child
            break
    if props is None:
        props = etree.Element("Properties")
        char_style_range.insert(0, props)  # Properties must precede Content

    applied_font = None
    for child in props:
        if isinstance(child.tag, str) and local_tag(child) == "AppliedFont":
            applied_font = child
            break
    if applied_font is None:
        applied_font = etree.SubElement(props, "AppliedFont")
        applied_font.set("type", "string")
    applied_font.text = font_name
    return True


def apply_point_size_override(char_style_range, point_size):
    """
    Sets a local PointSize attribute directly on this CharacterStyleRange -
    overrides the font size just for this one run, without touching the
    shared CharacterStyle/ParagraphStyle resources reused elsewhere.
    Unlike AppliedFont, PointSize is a plain XML attribute in IDML, not a
    nested Properties child (confirmed against real story XML).
    """
    if char_style_range is None:
        return False
    char_style_range.set("PointSize", f"{point_size:g}")
    return True


def ensure_font_family_declared(fonts_xml_path: Path, family_name: str, style_names):
    """
    Adds a FontFamily entry (with one Font sub-entry per style in
    style_names) to Resources/Fonts.xml if one for family_name doesn't
    already exist - keeps the document's font metadata consistent with
    what's actually applied in the story XML.
    """
    parser = etree.XMLParser(recover=True)
    tree = etree.parse(str(fonts_xml_path), parser)
    root = tree.getroot()

    for family in root:
        if isinstance(family.tag, str) and local_tag(family) == "FontFamily" \
                and family.get("Name") == family_name:
            return  # already declared

    family_self = f"RosettaFont_{family_name.replace(' ', '')}"
    family_elem = etree.Element("FontFamily", Self=family_self, Name=family_name)
    for style_name in style_names:
        font_elem = etree.SubElement(
            family_elem, "Font",
            Self=f"{family_self}Fontn{family_name} {style_name}",
            FontFamily=family_name,
            Name=f"{family_name} {style_name}",
            PostScriptName=f"{family_name.replace(' ', '')}-{style_name}",
            Status="Installed",
            FontStyleName=style_name,
            FontType="OpenTypeTT",
            WritingScript="0",
            FullName=f"{family_name} {style_name}",
            FullNameNative=f"{family_name} {style_name}",
            FontStyleNameNative=style_name,
            PlatformName="$ID/",
            Version="$ID/",
            TypekitID="$ID/",
        )

    # Insert alongside the other FontFamily entries (before any trailing
    # CompositeFont element), rather than assuming a fixed position.
    insert_at = len(root)
    for i, child in enumerate(root):
        if isinstance(child.tag, str) and local_tag(child) != "FontFamily":
            insert_at = i
            break
    root.insert(insert_at, family_elem)

    tree.write(str(fonts_xml_path), xml_declaration=True, encoding="UTF-8", standalone=True)


def ordered_content_elements(root):
    """
    Walk the story XML in the same document order used by deconstruct.py's
    walk_story(), and return a flat list of <Content> elements in that
    order. Index i in this list corresponds to sequence_index == i.
    """
    elements = []

    def recurse(elem):
        if not isinstance(elem.tag, str):
            return
        if local_tag(elem) == "Content":
            elements.append(elem)
        for child in elem:
            if not isinstance(child.tag, str):
                continue
            recurse(child)

    recurse(root)
    return elements


def flip_story_direction(root):
    """Change every StoryDirection=LeftToRightDirection to RightToLeftDirection."""
    changed = 0
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if elem.get("StoryDirection") == "LeftToRightDirection":
            elem.set("StoryDirection", "RightToLeftDirection")
            changed += 1
    return changed


def rewrite_link_resource_uris(work_dir: Path):
    """
    LinkResourceURI attributes (in Stories/, Spreads/, MasterSpreads/ XML)
    are absolute file: paths baked in at authoring time - they only resolve
    on the machine the IDML was originally packaged on. Rewrite each to a
    relative file:Links/<filename> reference, done as a plain text
    substitution (not an XML re-parse) so every other byte of these files -
    most of which reconstruct() never otherwise touches - is left alone.
    Returns the total number of attributes rewritten.
    """
    total = 0
    for xml_path in work_dir.rglob("*.xml"):
        text = xml_path.read_text(encoding="utf-8")
        new_text, count = LINK_RESOURCE_URI_RE.subn(r'LinkResourceURI="file:Links/\1"', text)
        if count:
            xml_path.write_text(new_text, encoding="utf-8")
            total += count
    return total


def reconstruct(db_path: str, document_id: int, target_language: str,
                 idml_extracted_dir: str, work_dir: str, idml_filename: str = None,
                 progress_callback=None):
    """
    Returns dict with paths to the produced .idml and the bundled delivery zip.

    progress_callback(stories_done, stories_total), if given, is called
    after each story is rewritten - UI progress only, optional.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    doc_row = conn.execute(
        "SELECT source_idml_filename FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc_row is None:
        raise ValueError(f"No document with id={document_id}")
    idml_filename = idml_filename or doc_row["source_idml_filename"]

    src = Path(idml_extracted_dir)
    work = Path(work_dir)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(src, work)  # fresh copy - never touch the original extracted folder

    stories = conn.execute(
        "SELECT id, story_self_id, source_file FROM stories WHERE document_id = ?",
        (document_id,)
    ).fetchall()

    total_replaced = 0
    total_missing = 0
    total_rtl_flips = 0
    total_shrunk = 0
    total_retranslated = 0
    fallback_report = []  # list of dicts describing every fragment that fell back to English

    for story_index, story in enumerate(stories):
        story_path = work / story["source_file"]
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(str(story_path), parser)
        root = tree.getroot()

        content_elements = ordered_content_elements(root)

        fragments = conn.execute(
            """SELECT id, sequence_index, raw_text, paragraph_seq FROM text_runs
               WHERE story_id = ? ORDER BY sequence_index""",
            (story["id"],)
        ).fetchall()

        for frag in fragments:
            idx = frag["sequence_index"]
            if idx >= len(content_elements):
                print(f"  WARNING: story {story['story_self_id']} sequence_index {idx} "
                      f"has no matching <Content> element in the XML - skipped.")
                continue

            translation = conn.execute(
                """SELECT translated_text, status FROM translations
                   WHERE text_run_id = ? AND target_language = ?""",
                (frag["id"], target_language)
            ).fetchone()

            overflow = conn.execute(
                """SELECT retranslated_text, original_point_size, final_point_size
                   FROM overflow_resolutions
                   WHERE text_run_id = ? AND target_language = ?""",
                (frag["id"], target_language)
            ).fetchone()

            elem = content_elements[idx]
            if translation is not None and translation["status"] == "translated" \
                    and translation["translated_text"] is not None:
                # Tier 4: a real, measured overflow was retranslated for brevity -
                # use that instead of the original translation. Always also
                # flagged in the exceptions table (overflow_resolve.py) for human
                # review, since this changes content, not just layout.
                if overflow is not None and overflow["retranslated_text"]:
                    elem.text = overflow["retranslated_text"]
                    total_retranslated += 1
                else:
                    elem.text = translation["translated_text"]
                total_replaced += 1

                needs_shrink = overflow is not None and overflow["final_point_size"] is not None \
                    and abs(overflow["final_point_size"] - overflow["original_point_size"]) > 0.01
                char_style_range = None
                if target_language == "ar" or needs_shrink:
                    char_style_range = find_enclosing_character_style_range(elem)

                if target_language == "ar":
                    if not apply_font_override(char_style_range, ARABIC_FALLBACK_FONT):
                        print(f"  WARNING: story {story['story_self_id']} text_run_id "
                              f"{frag['id']} - no enclosing CharacterStyleRange found, "
                              f"could not apply {ARABIC_FALLBACK_FONT} font override.")

                if needs_shrink:
                    # Tier 3: a real, measured overflow was resolved with a
                    # controlled font-size reduction (see overflow_resolve.py).
                    if not apply_point_size_override(char_style_range, overflow["final_point_size"]):
                        print(f"  WARNING: story {story['story_self_id']} text_run_id "
                              f"{frag['id']} - no enclosing CharacterStyleRange found, "
                              f"could not apply the {overflow['final_point_size']:.2f}pt size override.")
                    else:
                        total_shrunk += 1
            else:
                # No usable translation - leave original text. Recorded in the
                # fallback report (printed as a summary at the end) instead of
                # just a scrolling console warning.
                elem.text = frag["raw_text"]
                total_missing += 1
                reason = "no translation row" if translation is None else f"status={translation['status']}"
                fallback_report.append({
                    "story_self_id": story["story_self_id"],
                    "text_run_id": frag["id"],
                    "sequence_index": idx,
                    "paragraph_seq": frag["paragraph_seq"],
                    "raw_text_preview": frag["raw_text"][:60],
                    "reason": reason,
                })

        if target_language == "ar":
            total_rtl_flips += flip_story_direction(root)

        tree.write(str(story_path), xml_declaration=True, encoding="UTF-8", standalone=True)

        if progress_callback:
            progress_callback(story_index + 1, len(stories))

    if target_language == "ar" and total_replaced > 0:
        fonts_xml_path = work / "Resources" / "Fonts.xml"
        if fonts_xml_path.exists():
            ensure_font_family_declared(
                fonts_xml_path, ARABIC_FALLBACK_FONT, list(ARABIC_FALLBACK_FONT_FILES.keys())
            )

    # Covers Stories/, Spreads/ and MasterSpreads/ in one pass - the latter
    # two are never otherwise touched by this function.
    total_link_uris_rewritten = rewrite_link_resource_uris(work)

    conn.close()

    print(f"Reconstruct: replaced {total_replaced} fragment(s), "
          f"{total_missing} left untranslated, "
          f"{total_rtl_flips} StoryDirection attribute(s) flipped to RTL, "
          f"{total_link_uris_rewritten} LinkResourceURI(s) made relative, "
          f"{total_shrunk} font-size override(s) applied (tier 3), "
          f"{total_retranslated} retranslated-for-brevity fragment(s) applied (tier 4).")

    if fallback_report:
        print()
        print(f"=== Fallback-to-English report ({len(fallback_report)} fragment(s)) ===")
        print(f"{'story':<10} {'text_run_id':<12} {'seq_idx':<8} {'para_seq':<9} {'reason':<20} preview")
        for row in fallback_report:
            print(f"{row['story_self_id']:<10} {row['text_run_id']:<12} {row['sequence_index']:<8} "
                  f"{row['paragraph_seq']:<9} {row['reason']:<20} {row['raw_text_preview']!r}")
        print("=" * 60)

    # --- Re-zip into a valid .idml, mimetype first + stored uncompressed ---
    stem = Path(idml_filename).stem
    idml_out_path = work.parent / f"{stem}_{target_language}.idml"
    if idml_out_path.exists():
        idml_out_path.unlink()

    mimetype_path = work / "mimetype"
    links_dir_in_work = work / "Links"
    all_files = sorted(
        p for p in work.rglob("*")
        if p.is_file() and links_dir_in_work not in p.parents
    )

    with zipfile.ZipFile(idml_out_path, "w") as zf:
        if mimetype_path.exists():
            # Must be first entry, stored (not deflated) - required by some readers.
            zf.write(mimetype_path, arcname="mimetype", compress_type=zipfile.ZIP_STORED)
        for file_path in all_files:
            if file_path == mimetype_path:
                continue
            arcname = str(file_path.relative_to(work))
            zf.write(file_path, arcname=arcname, compress_type=zipfile.ZIP_DEFLATED)

    # --- Bundle: translated .idml + unchanged Links/ folder into a delivery zip ---
    bundle_path = work.parent / f"{stem}_{target_language}_delivery.zip"
    if bundle_path.exists():
        bundle_path.unlink()

    links_dir = src / "Links"  # unchanged copy from the ORIGINAL extracted source
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(idml_out_path, arcname=idml_out_path.name)
        if links_dir.exists():
            for file_path in sorted(links_dir.rglob("*")):
                if file_path.is_file():
                    arcname = str(Path("Links") / file_path.relative_to(links_dir))
                    zf.write(file_path, arcname=arcname)
        if target_language == "ar" and total_replaced > 0:
            # Embed the actual Arabic font files so rendering doesn't depend
            # on that font being pre-installed on whatever machine opens the
            # file - InDesign auto-detects a "Document Fonts" folder placed
            # next to the package. See fonts/OFL.txt for the license.
            for style_name, filename in ARABIC_FALLBACK_FONT_FILES.items():
                font_file = FONTS_ASSET_DIR / filename
                if font_file.exists():
                    zf.write(font_file, arcname=str(Path("Document Fonts") / filename))
                else:
                    print(f"  WARNING: {font_file} not found - Arabic font not embedded in delivery.")
            ofl_path = FONTS_ASSET_DIR / "OFL.txt"
            if ofl_path.exists():
                zf.write(ofl_path, arcname=str(Path("Document Fonts") / "OFL.txt"))

    return {
        "idml_path": str(idml_out_path),
        "bundle_path": str(bundle_path),
        "replaced": total_replaced,
        "missing": total_missing,
        "rtl_flips": total_rtl_flips,
        "link_uris_rewritten": total_link_uris_rewritten,
        "shrunk": total_shrunk,
        "retranslated": total_retranslated,
        "fallback_report": fallback_report,
    }


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "rosetta.db"
    document_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    target_language = sys.argv[3] if len(sys.argv) > 3 else "es"
    idml_extracted_dir = sys.argv[4] if len(sys.argv) > 4 else "idml_extracted"
    work_dir = sys.argv[5] if len(sys.argv) > 5 else "idml_work_copy"

    result = reconstruct(db_path, document_id, target_language, idml_extracted_dir, work_dir)
    print(result)
