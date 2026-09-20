"""
Rosetta v3 - Stage 3b: Overflow handling

For each translated fragment, checks whether the translated text actually
fits in its real InDesign frame, using REAL font metrics (via HarfBuzz
text shaping + fontTools, not a character-count heuristic) and REAL frame
geometry (parsed from the IDML Spreads XML - TextColumnFixedWidth, frame
height, insets). Tiered resolution:
  1. Measure: does the shaped text fit on one line at the original size?
  2. If not: does the frame have vertical room to wrap onto another line?
  3. If not: try a controlled font-size reduction (up to SHRINK_MAX_PCT).
  4. If still not: re-translate that one fragment asking for a shorter
     phrasing that preserves the same meaning (a real, billed API call -
     see translate.py's live-API-call warning; only invoked when tiers
     1-3 have genuinely failed, never speculatively).
  5. If still not: flag 'unresolved_overflow' in the exceptions table so
     a human can look at it - never silently left broken.

Text shaping, not just glyph-width summation, matters most for Arabic: it
is a cursive script where a letter's glyph shape changes by position
(initial/medial/final/isolated, via GSUB substitution) - summing
default/isolated-form widths per codepoint would misrepresent the real
rendered width. HarfBuzz performs the same category of OpenType shaping
InDesign's own composer does.

Font availability note: this document's actual licensed fonts (Myriad Pro,
Bunday Sans, Museo Sans) are not installed on this machine (checked
system fonts, project assets, and Adobe Fonts sync - nothing found, see
FONT_SUBSTITUTES for the resolution). Per explicit direction, widely
available system fonts with matching weights are used as the default,
primary measurement path (not a stopgap) - see FONT_SUBSTITUTES below.
Real licensed font files can be dropped in later for exact fidelity; nothing
here blocks on that. Arabic needs no substitution - translated Arabic runs
are already rendered in the real, bundled Noto Sans Arabic font (see
reconstruct.py's ARABIC_FALLBACK_FONT), so Arabic measurement is exact.
"""

import re
import sqlite3
from pathlib import Path

from fontTools.ttLib import TTFont, TTCollection
import uharfbuzz as hb
from lxml import etree

FONTS_ASSET_DIR = Path(__file__).parent / "fonts"

# (font_family, font_style) -> (file_path, font_number | None for a plain
# .ttf/.otf, or the face index within a .ttc collection). Covers every font
# actually resolved onto translatable text in the real document (see
# investigation: Myriad Pro, Museo Sans, Bunday Sans - Arial/Zapf/Wingdings
# etc. never apply to translatable runs).
FONT_SUBSTITUTES = {
    ("Myriad Pro", "Regular"): ("/System/Library/Fonts/Supplemental/Arial.ttf", None),
    ("Myriad Pro", "Bold"): ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", None),
    ("Myriad Pro", "Italic"): ("/System/Library/Fonts/Supplemental/Arial Italic.ttf", None),
    ("Myriad Pro", "Black"): ("/System/Library/Fonts/Supplemental/Arial Black.ttf", None),
    ("Myriad Pro", "Semibold"): ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", None),  # Arial has no Semibold; nearest available
    ("Myriad Pro", "SemiCondensed"): ("/System/Library/Fonts/Supplemental/Arial Narrow.ttf", None),  # nearest condensed available
    ("Museo Sans", "300"): ("/System/Library/Fonts/Avenir Next.ttc", 7),   # Avenir Next Regular
    ("Museo Sans", "500"): ("/System/Library/Fonts/Avenir Next.ttc", 5),   # Avenir Next Medium
    ("Museo Sans", "700"): ("/System/Library/Fonts/Avenir Next.ttc", 0),   # Avenir Next Bold
    ("Museo Sans", "900"): ("/System/Library/Fonts/Avenir Next.ttc", 8),   # Avenir Next Heavy
    ("Bunday Sans", "Heavy"): ("/System/Library/Fonts/HelveticaNeue.ttc", 1),  # Helvetica Neue Bold; no Heavy/Black face available
    ("Bunday Sans", "ExtraBold"): ("/System/Library/Fonts/HelveticaNeue.ttc", 1),  # Helvetica Neue Bold; no ExtraBold face available
}
# Approximate flags: substitutes with no exact weight match in the target family.
FONT_SUBSTITUTES_APPROXIMATE = {
    ("Myriad Pro", "Semibold"), ("Myriad Pro", "SemiCondensed"),
    ("Bunday Sans", "Heavy"), ("Bunday Sans", "ExtraBold"),
}

ARABIC_FONT_REGULAR = (str(FONTS_ASSET_DIR / "NotoSansArabic-Regular.ttf"), None)
ARABIC_FONT_BOLD = (str(FONTS_ASSET_DIR / "NotoSansArabic-Bold.ttf"), None)

DEFAULT_POINT_SIZE = 10.0  # used only if no size can be resolved anywhere in the cascade
DEFAULT_LEADING_RATIO = 1.2  # InDesign's "Auto" leading default when no explicit Leading is set

SHRINK_STEPS_PCT = [10, 15]  # tier 3: try 10% smaller, then 15% smaller


# ------------------------------------------------------------------ style resolution

def local_tag(elem):
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


class StyleResolver:
    """Resolves the real, cascaded font family/style/point-size/leading for
    a (character_style, paragraph_style) pair, by walking Resources/Styles.xml's
    BasedOn inheritance chains - the same cascade InDesign itself applies."""

    def __init__(self, styles_xml_path):
        tree = etree.parse(str(styles_xml_path))
        root = tree.getroot()
        self.styles = {}
        for elem in root.iter():
            if local_tag(elem) in ("CharacterStyle", "ParagraphStyle"):
                info = {"AppliedFont": None, "BasedOn": None, "FontStyle": elem.get("FontStyle"),
                        "PointSize": elem.get("PointSize"), "Leading": None}
                for props in elem:
                    if local_tag(props) != "Properties":
                        continue
                    for child in props:
                        if local_tag(child) == "AppliedFont":
                            info["AppliedFont"] = child.text
                        elif local_tag(child) == "BasedOn":
                            info["BasedOn"] = child.text
                        elif local_tag(child) == "Leading" and child.get("type") == "unit":
                            info["Leading"] = float(child.text)
                self.styles[elem.get("Self")] = info

    def _resolve_field(self, style_id, field, depth=0):
        if style_id not in self.styles or depth > 15:
            return None
        info = self.styles[style_id]
        if info.get(field):
            return info[field]
        based_on = info.get("BasedOn")
        if based_on and based_on != style_id:
            prefix = style_id.split("/")[0] + "/"
            candidate = based_on if "/" in based_on else prefix + based_on
            return self._resolve_field(candidate, field, depth + 1)
        return None

    def resolve(self, character_style, paragraph_style):
        """Returns dict: family, font_style, point_size, leading - each
        resolved from character_style first, falling back to paragraph_style."""
        family = self._resolve_field(character_style, "AppliedFont") or self._resolve_field(paragraph_style, "AppliedFont")
        font_style = self._resolve_field(character_style, "FontStyle") or self._resolve_field(paragraph_style, "FontStyle") or "Regular"
        point_size = self._resolve_field(character_style, "PointSize") or self._resolve_field(paragraph_style, "PointSize")
        leading = self._resolve_field(character_style, "Leading") or self._resolve_field(paragraph_style, "Leading")

        point_size = float(point_size) if point_size else DEFAULT_POINT_SIZE
        leading = leading if leading else point_size * DEFAULT_LEADING_RATIO
        return {"family": family, "font_style": font_style, "point_size": point_size, "leading": leading}


# ------------------------------------------------------------------ font measurement

_face_cache = {}


def _get_hb_font(file_path, font_number):
    key = (file_path, font_number)
    if key in _face_cache:
        return _face_cache[key]
    with open(file_path, "rb") as f:
        data = f.read()
    face = hb.Face(data, font_number) if font_number is not None else hb.Face(data)
    font = hb.Font(face)
    upem = face.upem
    font.scale = (upem, upem)
    _face_cache[key] = (font, upem)
    return _face_cache[key]


def resolve_font_file(family, font_style, target_language):
    """
    Returns (file_path, font_number, is_approximate, note).
    Arabic translated runs always render in the real bundled Noto Sans
    Arabic (see reconstruct.py) - exact, not a substitute. Everything else
    goes through FONT_SUBSTITUTES.
    """
    if target_language == "ar":
        bold = font_style and ("bold" in font_style.lower() or font_style in ("700", "900", "Heavy"))
        path, num = ARABIC_FONT_BOLD if bold else ARABIC_FONT_REGULAR
        return path, num, False, "Real font (Noto Sans Arabic) - this is what actually renders in the delivered document."

    key = (family, font_style)
    if key in FONT_SUBSTITUTES:
        path, num = FONT_SUBSTITUTES[key]
        approx = key in FONT_SUBSTITUTES_APPROXIMATE
        note = (f"Substitute font: real '{family} {font_style}' not available on this machine; "
                f"measured with {Path(path).stem}" + (" (no exact weight match, nearest used)" if approx else "") + ".")
        return path, num, True, note

    # Unknown family/style combo not seen in our investigation - fall back to
    # Arial Regular rather than crashing, but say so loudly.
    return ("/System/Library/Fonts/Supplemental/Arial.ttf", None, True,
            f"No substitute configured for '{family} {font_style}' - fell back to Arial Regular as a last resort.")


def measure_text_width_pt(text, family, font_style, point_size, target_language):
    """
    Returns (width_pt, font_note). Uses real HarfBuzz text shaping (handles
    Arabic contextual joining/ligatures and Latin kerning) over the real
    glyph metrics of the resolved font file - not a character-count guess.
    """
    if not text:
        return 0.0, ""
    file_path, font_number, is_approx, note = resolve_font_file(family, font_style, target_language)
    font, upem = _get_hb_font(file_path, font_number)

    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    if target_language == "ar":
        buf.direction = "rtl"
        buf.script = "Arab"
    hb.shape(font, buf)

    total_units = sum(pos.x_advance for pos in buf.glyph_positions)
    width_pt = total_units / upem * point_size
    return width_pt, note


# ------------------------------------------------------------------ frame geometry
# Reuses the same affine-transform math as story_placements.py.

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def parse_transform(s):
    if not s:
        return IDENTITY
    a, b, c, d, tx, ty = (float(x) for x in s.split())
    return (a, b, c, d, tx, ty)


def apply_transform(m, x, y):
    a, b, c, d, tx, ty = m
    return (a * x + c * y + tx, b * x + d * y + ty)


def compose(inner, outer):
    a1, b1, c1, d1, tx1, ty1 = inner
    a2, b2, c2, d2, tx2, ty2 = outer
    a = a1 * a2 + b1 * c2
    b = a1 * b2 + b1 * d2
    c = c1 * a2 + d1 * c2
    d = c1 * b2 + d1 * d2
    tx = tx1 * a2 + ty1 * c2 + tx2
    ty = tx1 * b2 + ty1 * d2 + ty2
    return (a, b, c, d, tx, ty)


def frame_local_bbox(elem):
    """(min_x, min_y, max_x, max_y) of a frame's own PathPointArray anchors,
    in the frame's own local (pre-transform) coordinate space."""
    anchors = elem.findall(".//{*}PathPointType")
    points = []
    for pt in anchors:
        anchor = pt.get("Anchor")
        if anchor:
            x, y = (float(v) for v in anchor.split())
            points.append((x, y))
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def get_frame_geometry(idml_extracted_dir, frame_self_id):
    """
    Finds frame_self_id in Spreads/*.xml and returns a dict with real,
    IDML-derived geometry: usable_width_pt (InDesign's own
    TextColumnFixedWidth - already net of insets/columns), frame_height_pt
    (bounding-box height minus top/bottom insets), inset_top/bottom_pt.
    Returns None if the frame can't be found or has no measurable geometry.
    """
    spreads_dir = Path(idml_extracted_dir) / "Spreads"
    for spread_file in sorted(spreads_dir.glob("*.xml")):
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(str(spread_file), parser)
        root = tree.getroot()
        spread_root = root[0] if len(root) else root

        for elem in spread_root.iter():
            if not isinstance(elem.tag, str) or elem.get("Self") != frame_self_id:
                continue
            bbox = frame_local_bbox(elem)
            if bbox is None:
                return None
            frame_height_pt = bbox[3] - bbox[1]

            usable_width_pt = None
            inset_top = inset_bottom = 0.0
            for child in elem:
                if local_tag(child) != "TextFramePreference":
                    continue
                w = child.get("TextColumnFixedWidth")
                if w:
                    usable_width_pt = float(w)
                for props in child:
                    if local_tag(props) != "Properties":
                        continue
                    for prop_child in props:
                        if local_tag(prop_child) == "InsetSpacing":
                            items = [li.text for li in prop_child if local_tag(li) == "ListItem"]
                            if len(items) == 4:
                                # InDesign order: Top, Left, Bottom, Right
                                inset_top = float(items[0])
                                inset_bottom = float(items[2])

            if usable_width_pt is None:
                return None
            return {
                "usable_width_pt": usable_width_pt,
                "frame_height_pt": frame_height_pt - inset_top - inset_bottom,
                "inset_top_pt": inset_top,
                "inset_bottom_pt": inset_bottom,
            }
    return None


# ------------------------------------------------------------------ tiered resolution

def simulate_wrapped_lines(text, family, font_style, point_size, usable_width_pt, target_language):
    """
    Greedy word-wrap using REAL measured word widths (HarfBuzz-shaped, same
    as measure_text_width_pt) - not a character-count estimate. Splits on
    whitespace (words don't break mid-word; no hyphenation, which InDesign
    could additionally use to fit more per line - this is therefore a
    conservative/upper-bound estimate of lines needed, never an
    under-estimate). Returns the number of lines the text would wrap into.
    """
    words = text.split()
    if not words:
        return 0
    space_width, _ = measure_text_width_pt(" ", family, font_style, point_size, target_language)

    lines = 1
    current_width = 0.0
    for i, word in enumerate(words):
        word_width, _ = measure_text_width_pt(word, family, font_style, point_size, target_language)
        add_width = word_width if current_width == 0 else space_width + word_width
        if current_width + add_width > usable_width_pt and current_width > 0:
            lines += 1
            current_width = word_width
        else:
            current_width += add_width
    return lines


def check_fit(text, family, font_style, point_size, leading, geo, target_language):
    """
    Runs tiers 1-2 at a given point size: does it fit on one line, or
    wrapped within the frame's real height? Returns dict with fits (bool),
    lines_needed, width_pt, wrapped_height_pt.
    """
    width_pt, font_note = measure_text_width_pt(text, family, font_style, point_size, target_language)
    if width_pt <= geo["usable_width_pt"]:
        return {"fits": True, "tier": 1, "lines_needed": 1, "width_pt": width_pt,
                "wrapped_height_pt": leading, "font_note": font_note}

    lines_needed = simulate_wrapped_lines(text, family, font_style, point_size, geo["usable_width_pt"], target_language)
    wrapped_height_pt = lines_needed * leading
    fits_wrapped = wrapped_height_pt <= geo["frame_height_pt"]
    return {"fits": fits_wrapped, "tier": 2, "lines_needed": lines_needed, "width_pt": width_pt,
            "wrapped_height_pt": wrapped_height_pt, "font_note": font_note}


def resolve_overflow(text, family, font_style, point_size, leading, geo, target_language,
                      shrink_steps_pct=SHRINK_STEPS_PCT):
    """
    Tiers 1-3 (measurement only, no API calls / no DB writes - that's the
    caller's job for tier 4/5). Returns a dict describing the outcome:
      resolution: 'fits' | 'fits_wrapped' | 'fits_shrunk' | 'unresolved'
      tier: 1, 2, or 3
      final_point_size: the point size that made it fit (may be reduced)
      shrink_pct: 0 if no shrink was needed
      detail: dict from check_fit at the winning size
    """
    result = check_fit(text, family, font_style, point_size, leading, geo, target_language)
    if result["fits"]:
        resolution = "fits" if result["tier"] == 1 else "fits_wrapped"
        return {"resolution": resolution, "tier": result["tier"], "final_point_size": point_size,
                "shrink_pct": 0, "detail": result}

    for pct in shrink_steps_pct:
        shrunk_size = point_size * (1 - pct / 100)
        shrunk_leading = leading * (1 - pct / 100)
        result = check_fit(text, family, font_style, shrunk_size, shrunk_leading, geo, target_language)
        if result["fits"]:
            return {"resolution": "fits_shrunk", "tier": 3, "final_point_size": shrunk_size,
                    "shrink_pct": pct, "detail": result}

    return {"resolution": "unresolved", "tier": 5, "final_point_size": point_size,
            "shrink_pct": shrink_steps_pct[-1] if shrink_steps_pct else 0, "detail": result}


def retranslate_shorter(raw_text, current_translation, target_lang_name, overage_pct, client, model):
    """
    Tier 4: one real, billed API call asking for a shorter phrasing of the
    SAME fragment that preserves meaning. Only call this after tiers 1-3
    have genuinely failed (checked by the caller) - never speculatively.
    Returns the raw shortened translation string.
    """
    system_prompt = (
        f"You are adjusting a {target_lang_name} translation of a K-12 math worksheet fragment "
        f"so it fits a fixed print layout. The current translation is too wide even after a font-size "
        f"reduction. Provide a SHORTER {target_lang_name} phrasing of the exact same meaning - compress "
        f"grammar and wording only (shorter synonyms, fewer words, tighter sentence structure). "
        f"Every content word in the original English must have a corresponding word or phrase in your "
        f"output - this includes qualifying/hedging/precision words (e.g. 'possible', 'sample', "
        f"'approximately', 'at least', 'may', 'about') AND the noun, verb, or phrase each of them "
        f"modifies. Do not delete a word just because deleting it happens to be shorter than translating "
        f"it - e.g. if the English is 'possible answers', a valid shortening keeps both the "
        f"'possible'-equivalent and the 'answers'-equivalent (shorter synonyms or reordering are fine; "
        f"dropping either word is not). The result must be a complete, grammatically correct phrase or "
        f"sentence in {target_lang_name} - never a sentence fragment missing its subject/object/noun. "
        f"It needs to be roughly {overage_pct:.0f}% shorter by character count than the current "
        f"translation, achieved without dropping any content word. Output ONLY the shortened translated "
        f"phrase - no tags, no explanation, no quotes."
    )
    user_prompt = f"Original English: {raw_text}\nCurrent {target_lang_name} translation (too wide): {current_translation}"
    # --- money spent here: one real, billed, non-deterministic API call ---
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content.strip()
