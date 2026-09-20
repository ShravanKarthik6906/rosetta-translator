"""
Rosetta v3 - Stage 4a: Story placements
Parses Spreads/*.xml to work out which page and frame each story actually
appears on, and stores the result in story_placements.

IDML spread geometry, briefly:
  - A <Spread> contains one or more <Page> elements side by side, and a set
    of page-item elements (TextFrame, Rectangle, Group, ...) positioned in
    the spread's own local coordinate space.
  - Every positioned element carries an ItemTransform="a b c d tx ty" (a 2D
    affine matrix) that maps ITS OWN local coordinate space into its
    parent's. Elements can be nested (a TextFrame inside a Group), so a
    point in a frame's local space must have every ancestor's transform
    applied, innermost first, to land in spread space.
  - A <Page>'s own GeometricBounds="Y1 X1 Y2 X2" (local) transformed by its
    own ItemTransform gives that page's bounding box in spread space.
  - A text frame's shape is a <PathGeometry>/<PathPointArray> of anchor
    points in the frame's own local space; a story is bound to whichever
    frame(s) carry ParentStory="<story_self_id>".

To place a story on a page: take the centroid of a frame's anchor points,
apply the frame's own transform composed with every ancestor transform to
get a point in spread space, then find which page's transformed bounding
box contains that point (nearest-page fallback if none exactly contains
it - e.g. a frame that slightly overhangs a page's margin).
"""

import sqlite3
import sys
from pathlib import Path
from lxml import etree

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def parse_transform(s):
    if not s:
        return IDENTITY
    a, b, c, d, tx, ty = (float(x) for x in s.split())
    return (a, b, c, d, tx, ty)


def apply_transform(m, x, y):
    a, b, c, d, tx, ty = m
    return (a * x + c * y + tx, b * x + d * y + ty)


def compose(inner, outer):
    """Returns a transform equal to applying `inner` first, then `outer`."""
    a1, b1, c1, d1, tx1, ty1 = inner
    a2, b2, c2, d2, tx2, ty2 = outer
    a = a1 * a2 + b1 * c2
    b = a1 * b2 + b1 * d2
    c = c1 * a2 + d1 * c2
    d = c1 * b2 + d1 * d2
    tx = tx1 * a2 + ty1 * c2 + tx2
    ty = tx1 * b2 + ty1 * d2 + ty2
    return (a, b, c, d, tx, ty)


def page_bbox_in_spread_space(page_elem):
    """Returns (xmin, ymin, xmax, ymax) for a <Page> in spread coordinates."""
    y1, x1, y2, x2 = (float(v) for v in page_elem.get("GeometricBounds").split())
    transform = parse_transform(page_elem.get("ItemTransform"))
    corners = [
        apply_transform(transform, x1, y1),
        apply_transform(transform, x1, y2),
        apply_transform(transform, x2, y1),
        apply_transform(transform, x2, y2),
    ]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def frame_centroid_local(elem):
    """Centroid of a frame's own PathPointArray anchors, in its own local space."""
    anchors = elem.findall(".//{*}PathPointType")
    points = []
    for pt in anchors:
        anchor = pt.get("Anchor")
        if anchor:
            x, y = (float(v) for v in anchor.split())
            points.append((x, y))
    if not points:
        return (0.0, 0.0)
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def nearest_page(point, pages):
    """pages: list of (page_self_id, page_name, bbox). Point-in-box test, else nearest center."""
    px, py = point
    for page_self, page_name, (xmin, ymin, xmax, ymax) in pages:
        if xmin <= px <= xmax and ymin <= py <= ymax:
            return page_self, page_name
    best = None
    best_dist = None
    for page_self, page_name, (xmin, ymin, xmax, ymax) in pages:
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        dist = (cx - px) ** 2 + (cy - py) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = (page_self, page_name)
    return best


FRAME_TAGS = {"TextFrame", "Rectangle", "Group", "Polygon", "Oval", "GraphicLine"}


def find_frame_placements(spread_root):
    """
    Walks a <Spread>'s element tree, composing ItemTransforms down through
    nested Groups, and returns [(story_self_id, frame_self_id, point_in_spread_space)]
    for every element carrying a ParentStory.
    """
    pages = []
    for child in spread_root:
        if local_tag(child) == "Page":
            pages.append((child.get("Self"), child.get("Name"), page_bbox_in_spread_space(child)))

    placements = []

    def walk(elem, accumulated_transform):
        tag = local_tag(elem)
        own_transform = parse_transform(elem.get("ItemTransform"))
        combined = compose(own_transform, accumulated_transform)

        parent_story = elem.get("ParentStory")
        if parent_story and parent_story != "n" and tag in FRAME_TAGS:
            local_center = frame_centroid_local(elem)
            spread_point = apply_transform(combined, *local_center)
            placements.append((parent_story, elem.get("Self"), spread_point))

        for child in elem:
            if isinstance(child.tag, str) and local_tag(child) in FRAME_TAGS:
                walk(child, combined)

    for child in spread_root:
        if local_tag(child) in FRAME_TAGS:
            walk(child, IDENTITY)

    return pages, placements


def populate_story_placements(db_path: str, document_id: int, idml_extracted_dir: str, progress_callback=None):
    """
    progress_callback(spreads_done, spreads_total), if given, is called
    after each Spread XML file is parsed - UI progress only, optional.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    story_by_self_id = {
        row["story_self_id"]: row["id"]
        for row in conn.execute(
            "SELECT id, story_self_id FROM stories WHERE document_id = ?", (document_id,)
        ).fetchall()
    }

    conn.execute(
        "DELETE FROM story_placements WHERE story_id IN "
        "(SELECT id FROM stories WHERE document_id = ?)",
        (document_id,),
    )

    spreads_dir = Path(idml_extracted_dir) / "Spreads"
    total_placements = 0
    unmatched_stories = set()

    spread_files = sorted(spreads_dir.glob("*.xml"))
    for spread_index, spread_file in enumerate(spread_files):
        parser = etree.XMLParser(recover=True)
        tree = etree.parse(str(spread_file), parser)
        root = tree.getroot()
        spread_root = root[0] if len(root) else root  # unwrap idPkg:Spread wrapper

        pages, frame_placements = find_frame_placements(spread_root)
        if not pages:
            if progress_callback:
                progress_callback(spread_index + 1, len(spread_files))
            continue

        for story_self_id, frame_self_id, point in frame_placements:
            story_id = story_by_self_id.get(story_self_id)
            if story_id is None:
                unmatched_stories.add(story_self_id)
                continue
            _, page_name = nearest_page(point, pages)
            conn.execute(
                "INSERT INTO story_placements (story_id, page_name, frame_self_id) VALUES (?, ?, ?)",
                (story_id, page_name, frame_self_id),
            )
            total_placements += 1

        if progress_callback:
            progress_callback(spread_index + 1, len(spread_files))

    conn.commit()

    total_stories = conn.execute(
        "SELECT COUNT(*) FROM stories WHERE document_id = ?", (document_id,)
    ).fetchone()[0]
    stories_placed = conn.execute(
        "SELECT COUNT(DISTINCT story_id) FROM story_placements WHERE story_id IN "
        "(SELECT id FROM stories WHERE document_id = ?)", (document_id,)
    ).fetchone()[0]
    conn.close()

    print(f"story_placements: inserted {total_placements} placement(s) across "
          f"{len(spread_files)} spread file(s). "
          f"{stories_placed}/{total_stories} stories placed.")
    if stories_placed < total_stories:
        print(f"  ({total_stories - stories_placed} story/stories have no frame anywhere in Spreads/*.xml - "
              f"most are likely master-page content (running headers/footers, page numbers) that "
              f"repeats per-page via a master rather than living on one fixed spread page.)")
    if unmatched_stories:
        print(f"  ({len(unmatched_stories)} ParentStory reference(s) had no matching row in `stories` "
              f"- likely master-page-only or out-of-scope content, skipped.)")

    return total_placements


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "rosetta.db"
    document_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    idml_extracted_dir = sys.argv[3] if len(sys.argv) > 3 else "idml_extracted"
    populate_story_placements(db_path, document_id, idml_extracted_dir)
