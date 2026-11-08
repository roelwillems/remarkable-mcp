"""
Text extraction helpers for reMarkable documents.
"""

import json
import logging
import os
import tempfile
import threading
import time
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

from remarkable_mcp import ink as _ink

logger = logging.getLogger(__name__)


def _rm_to_svg(rm_file_path: Path, output_svg_path: Path) -> bool:
    """Convert a .rm file to SVG using the built-in rmscene renderers.

    The ``rmc`` dependency has been dropped: it transitively pinned
    ``rmscene<0.7.0``, which cannot parse current-firmware ``.rm`` scene
    blocks (e.g. ``SceneInfo`` ``block_type=13``). We render via ``rmscene``
    directly using the in-repo v6/v5 renderers. v6 is tried first since it is
    the current firmware format.

    Writes the SVG content to output_svg_path and returns True on success.
    Returns False if no renderer could handle the file.

    Credit: khalid-hasan (PR #97), ljdutel (#95)
    """
    svg_content = _render_rm_v6_to_svg(rm_file_path) or _render_rm_v5_to_svg(rm_file_path)
    if svg_content is not None:
        output_svg_path.write_text(svg_content)
        return True

    return False


# reMarkable tablet screen dimensions (in pixels) - used as fallback
REMARKABLE_WIDTH = 1404
REMARKABLE_HEIGHT = 1872

# PDF points per stroke unit, keyed by the per-page SceneInfo.paper_size grid.
# The 1404x1872 value was calibrated against a device PDF export. The Paper Pro
# value follows its panel DPI and remains a best-effort calibration until a
# ground-truth export reporting that grid is available. Devices that normalize
# Paper Pro scenes to 1404x1872 continue to use the calibrated value.
_DEVICE_POINTS_PER_UNIT = {
    (1404, 1872): 0.3177,
    (1620, 2160): 0.3144,
}
REMARKABLE_PDF_POINTS_PER_UNIT = 0.3177


def _points_per_unit(paper_size: Optional[Tuple[float, float]]) -> float:
    """Return PDF points per stroke unit for a SceneInfo paper grid."""
    if paper_size:
        key = (int(round(paper_size[0])), int(round(paper_size[1])))
        if key in _DEVICE_POINTS_PER_UNIT:
            return _DEVICE_POINTS_PER_UNIT[key]
    return REMARKABLE_PDF_POINTS_PER_UNIT


def _annotation_page_viewbox(
    pdf_w_pt: float,
    pdf_h_pt: float,
    points_per_unit: float,
    *,
    centered_x: bool = True,
) -> Tuple[float, float, float, float]:
    """Return the stroke-space rectangle occupied by a PDF page."""
    view_w = pdf_w_pt / points_per_unit
    view_h = pdf_h_pt / points_per_unit
    view_x = -view_w / 2.0 if centered_x else 0.0
    return view_x, 0.0, view_w, view_h


def _rewrite_svg_root(
    svg_content: str,
    *,
    viewbox: Tuple[float, float, float, float],
    width: int,
    height: int,
) -> str:
    """Rewrite only root SVG geometry, leaving path stroke widths untouched."""
    import re

    root_match = re.search(r"<svg\b[^>]*>", svg_content, re.IGNORECASE)
    if root_match is None:
        return svg_content

    root = root_match.group(0)
    values = {
        "viewBox": " ".join(f"{value:.2f}" for value in viewbox),
        "width": str(width),
        "height": str(height),
    }
    for name, value in values.items():
        pattern = rf'(?<![-\w]){name}="[^"]*"'
        if re.search(pattern, root):
            root = re.sub(pattern, f'{name}="{value}"', root, count=1)
        else:
            root = root[:-1] + f' {name}="{value}">'

    return svg_content[: root_match.start()] + root + svg_content[root_match.end() :]


# Standard reMarkable background color (light cream/gray)
# Can be overridden via REMARKABLE_BACKGROUND_COLOR environment variable
_DEFAULT_BACKGROUND_COLOR = "#FBFBFB"


def get_background_color() -> str:
    """Get the background color, checking env var for override."""
    return os.environ.get("REMARKABLE_BACKGROUND_COLOR", _DEFAULT_BACKGROUND_COLOR)


OCR_BACKENDS = frozenset({"auto", "google", "tesseract"})


def get_ocr_backend() -> str:
    """Return a supported OCR backend, falling back to ``auto`` safely."""
    backend = os.environ.get("REMARKABLE_OCR_BACKEND", "auto").strip().lower()
    if backend not in OCR_BACKENDS:
        supported = ", ".join(sorted(OCR_BACKENDS))
        if backend == "sampling":
            logger.warning(
                "The 'sampling' OCR backend has been removed. "
                "Falling back to 'auto'. Set REMARKABLE_OCR_BACKEND to one of: %s.",
                supported,
            )
        else:
            logger.warning(
                "Unsupported REMARKABLE_OCR_BACKEND value %r. "
                "Falling back to 'auto'. Supported values are: %s.",
                backend,
                supported,
            )
        return "auto"
    return backend


# For backwards compatibility, expose as module constant (evaluated at import)
# Use get_background_color() for runtime evaluation of env var
REMARKABLE_BACKGROUND_COLOR = get_background_color()

# Margin around content when using content-based bounding box (in pixels)
CONTENT_MARGIN = 50

# Target long-edge resolution (px) for a full-page canvas render. The page is
# rasterised at this resolution preserving the page aspect; the displayed image
# is then scaled by the host, so this only sets crispness, not layout.
FULL_PAGE_TARGET_LONG_EDGE = 1872

# Cache TTL in seconds (5 minutes)
CACHE_TTL_SECONDS = 300

# Module-level cache for OCR results (full document)
# Key: doc_id
# Value: {"result": extraction_result, "include_ocr": bool, "timestamp": float}
_extraction_cache: Dict[str, Dict[str, Any]] = {}

_cache_lock = threading.RLock()
_cache_generation: Dict[str, int] = {}
_global_cache_generation = 0


def _is_cache_valid(cached: Dict[str, Any]) -> bool:
    """Check if a cached entry is still valid based on TTL."""
    if "timestamp" not in cached:
        return True  # Old cache entries without timestamp are valid
    return (time.time() - cached["timestamp"]) < CACHE_TTL_SECONDS


def clear_extraction_cache(doc_id: Optional[str] = None) -> None:
    """
    Clear the extraction cache.

    Args:
        doc_id: If provided, only clear cache for this document.
                If None, clear the entire cache.
    """
    global _global_cache_generation

    with _cache_lock:
        if doc_id:
            _cache_generation[doc_id] = _cache_generation.get(doc_id, 0) + 1
            _extraction_cache.pop(doc_id, None)
        else:
            _global_cache_generation += 1
            _extraction_cache.clear()
            _cache_generation.clear()


def _cache_token(doc_id: str) -> tuple[int, int]:
    with _cache_lock:
        return _global_cache_generation, _cache_generation.get(doc_id, 0)


def _cache_extraction_result_if_current(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool,
    token: tuple[int, int],
) -> bool:
    """Cache only if no mutation invalidated this document during extraction."""
    with _cache_lock:
        if token != (
            _global_cache_generation,
            _cache_generation.get(doc_id, 0),
        ):
            return False
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
        }
        return True


def get_cached_ocr_result(
    doc_id: str,
    include_ocr: bool = True,
    ocr_backend: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Get cached OCR result for a document if available and valid.

    Args:
        doc_id: Document ID to look up
        include_ocr: Whether OCR content is required
        ocr_backend: If specified, only return cache if it was produced by this backend.
                     Use "google" or "tesseract". None accepts any backend.

    Returns:
        Cached result dict or None if not cached/expired/wrong backend
    """
    with _cache_lock:
        if doc_id in _extraction_cache:
            cached = _extraction_cache[doc_id]
            if (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
                if ocr_backend is not None:
                    cached_backend = cached["result"].get("ocr_backend")
                    if cached_backend != ocr_backend:
                        return None
                return cached["result"]
    return None


def cache_ocr_result(
    doc_id: str,
    result: Dict[str, Any],
    include_ocr: bool = True,
) -> None:
    """
    Cache an OCR result for a document.

    Args:
        doc_id: Document ID
        result: Extraction result dict with keys: typed_text, highlights,
                handwritten_text, pages, page_ids, ocr_backend
        include_ocr: Whether this result includes OCR content
    """
    with _cache_lock:
        _extraction_cache[doc_id] = {
            "result": result,
            "include_ocr": include_ocr,
            "timestamp": time.time(),
        }


def find_similar_documents(query: str, documents: List, limit: int = 5) -> List[str]:
    """Find documents with similar names for 'did you mean' suggestions."""
    query_lower = query.lower()
    scored = []
    for doc in documents:
        name = doc.VissibleName
        # Use sequence matcher for fuzzy matching
        ratio = SequenceMatcher(None, query_lower, name.lower()).ratio()
        # Boost partial matches
        if query_lower in name.lower():
            ratio += 0.3
        scored.append((name, ratio))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [name for name, score in scored[:limit] if score > 0.3]


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file using PyMuPDF.

    Returns the full text content of the PDF.
    """
    try:
        import fitz  # PyMuPDF

        text_parts = []
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, 1):
                page_text = page.get_text()
                if page_text.strip():
                    text_parts.append(f"--- Page {page_num} ---\n{page_text.strip()}")

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_epub(epub_path: Path) -> str:
    """
    Extract text from an EPUB file.

    Returns the full text content of the EPUB.
    """
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub

        book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        text_parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                # Get text, preserving some structure
                text = soup.get_text(separator="\n", strip=True)
                if text:
                    text_parts.append(text)

        return "\n\n".join(text_parts) if text_parts else ""
    except ImportError:
        return ""
    except Exception:
        return ""


def extract_text_from_rm_file(rm_file_path: Path) -> List[str]:
    """
    Extract typed text from a .rm file using rmscene.

    This extracts text that was typed via Type Folio or on-screen keyboard.
    Does NOT require OCR - text is stored natively in v6 .rm files.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import Text
        from rmscene.text import TextDocument

        with open(rm_file_path, "rb") as f:
            blocks = list(read_blocks(f))

        # The page's typed text lives on the RootTextBlock, not among the
        # root group's children (those are the ink layers), so scan the raw
        # blocks the same way the renderer does.
        text_item = next(
            (b.value for b in blocks if isinstance(getattr(b, "value", None), Text)),
            None,
        )
        if text_item is None:
            return []

        # TextDocument resolves the CRDT character sequence into ordered,
        # styled paragraphs; iterating Text.items directly yields CRDT ids
        # and fragments in insertion order, not readable lines.
        doc = TextDocument.from_scene_item(text_item)
        return [line for line in (str(para).strip() for para in doc.contents) if line]

    except ImportError:
        return []  # rmscene not available
    except Exception:
        # Log but don't fail - file might be older format
        return []


def _parse_hex_color(hex_color: str) -> tuple:
    """Parse a hex color string to RGBA tuple.

    Supports #RRGGBB (RGB) and #RRGGBBAA (RGBA) formats.

    Args:
        hex_color: Hex color string (e.g., "#FFFFFF" or "#FFFFFF80")

    Returns:
        Tuple of (r, g, b, a) values (0-255)
    """
    if not hex_color.startswith("#"):
        return (255, 255, 255, 255)

    hex_str = hex_color.lstrip("#")
    if len(hex_str) == 6:
        r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
        return (r, g, b, 255)
    elif len(hex_str) == 8:
        r, g, b, a = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4, 6))
        return (r, g, b, a)
    else:
        return (255, 255, 255, 255)


def _get_svg_content_bounds(svg_path: Path) -> Optional[tuple]:
    """
    Parse SVG file to get the content bounding box from viewBox.

    Args:
        svg_path: Path to the SVG file

    Returns:
        Tuple of (min_x, min_y, width, height) or None if not determinable
    """
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()

        # Try to get viewBox attribute
        viewbox = root.get("viewBox")
        if viewbox:
            parts = viewbox.split()
            if len(parts) == 4:
                return (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))

        # Fallback to width/height attributes
        width = root.get("width")
        height = root.get("height")
        if width and height:
            # Remove 'px' suffix if present
            w = float(width.replace("px", ""))
            h = float(height.replace("px", ""))
            return (0, 0, w, h)

        return None
    except Exception:
        return None


CONTENT_PADDING = 20  # Padding around content bounds for SVG viewBox


def _crop_box(all_coords: list, pad: float = CONTENT_PADDING) -> Tuple[int, int, int, int]:
    """Content crop ``(min_x, min_y, w, h)`` in whole stroke units.

    ``pad`` should include half the widest stroke so wide strokes (highlighters
    reach ~110 units) are not clipped at the crop edge.
    """
    xs = [c[0] for c in all_coords]
    ys = [c[1] for c in all_coords]
    min_x = min(xs) - pad
    min_y = min(ys) - pad
    w = max(xs) - min_x + pad
    h = max(ys) - min_y + pad
    return round(min_x), round(min_y), max(1, round(w)), max(1, round(h))


def _svg_document(
    elements: list,
    viewbox: Tuple[float, float, float, float],
    width: float,
    height: float,
    root_attrs: str = "",
) -> str:
    """Wrap SVG elements in a root with the given viewBox and size."""
    vb = " ".join(_ink._fmt(v) for v in viewbox)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{vb}" width="{_ink._fmt(width)}" height="{_ink._fmt(height)}"{root_attrs}>'
        f"{''.join(elements)}</svg>"
    )


def _svg_from_paths(paths: list, all_coords: list, pad: float = CONTENT_PADDING) -> Optional[str]:
    """Build SVG string with viewBox computed from actual content bounds."""
    if not paths or not all_coords:
        return None

    min_x, min_y, w, h = _crop_box(all_coords, pad)
    return _svg_document(paths, (min_x, min_y, w, h), w, h)


def _render_rm_v5_to_svg(rm_file_path: Path) -> Optional[str]:
    """
    Render a v5 .rm file (reMarkable .lines format) to SVG.

    The v5 binary format stores layers of strokes, where each stroke
    has pen/color metadata and a sequence of (x, y, speed, tilt, width,
    pressure) segments.
    """
    import struct

    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=5" not in header:
                return None

            nlayers = struct.unpack("<I", f.read(4))[0]
            items: List[_ink.InkItem] = []

            for _ in range(nlayers):
                nstrokes = struct.unpack("<I", f.read(4))[0]

                for _ in range(nstrokes):
                    pen, color, _pad, _w, _unk, nsegments = struct.unpack("<IIIIfI", f.read(24))

                    segments = []
                    for _ in range(nsegments):
                        x, y, speed, tilt, width, pressure = struct.unpack("<ffffff", f.read(24))
                        segments.append((x, y, width, pressure, speed))

                    if not segments:
                        continue

                    # v5 highlighters were yellow only; their colour ids 0-2 are
                    # the pen colour slots, not the highlight colour.
                    if _ink.tool_name(pen) == "highlighter" and color in (0, 1, 2):
                        color = 3  # PenColor.YELLOW
                    # v5 floats are raw values: widths are already stroke units
                    # (no /4); pressure is 0-1 and speed is unscaled, so scale
                    # them the way rmscene scales the same float point format.
                    style = _ink.style_for(
                        pen,
                        color,
                        point_widths=[s[2] for s in segments],
                        pressures=[s[3] * 255 for s in segments],
                        speeds=[s[4] * 4 for s in segments],
                        width_divisor=1.0,
                    )
                    if style.is_eraser:
                        continue
                    items.append(
                        _ink.InkItem(
                            kind="stroke",
                            style=style,
                            order=len(items),
                            points=[(s[0], s[1]) for s in segments],
                        )
                    )

        highlights, ink, coords, max_half = _layer_elements(items)
        return _svg_from_paths(highlights + ink, coords, CONTENT_PADDING + max_half)
    except Exception:
        return None


def _layer_elements(items: List["_ink.InkItem"]) -> Tuple[list, list, list, float]:
    """Split items into highlight and ink SVG elements.

    Highlights (highlighter, shader, text highlights) come first: they blend
    with multiply, which on the device looks the same whatever the stacking
    order, and drawing them under the ink keeps ink and text dark in renderers
    without blend support. Returns ``(highlights, ink, coords, max_half_width)``
    with coords in the same order as the elements.
    """
    highlights: list = []
    ink: list = []
    hl_coords: list = []
    ink_coords: list = []
    max_half = 0.0
    for item in items:
        if item.style.is_highlight:
            highlights.extend(_ink.svg_elements(item))
            hl_coords.extend(item.coords())
        else:
            ink.extend(_ink.svg_elements(item))
            ink_coords.extend(item.coords())
        max_half = max(max_half, item.style.width / 2.0)
    return highlights, ink, hl_coords + ink_coords, max_half


def _v6_blocks(rm_file_path: Path) -> Optional[list]:
    """Read v6 .rm scene blocks, or None if the file is not v6 / unreadable."""
    try:
        from rmscene import read_blocks
    except ImportError:
        return None
    try:
        with open(rm_file_path, "rb") as f:
            header = f.read(43)
            if b"version=6" not in header:
                return None
            f.seek(0)
            return list(read_blocks(f))
    except Exception:
        return None


def _v6_paper_size(blocks: list) -> Tuple[float, float]:
    """Page extent (W, H) in stroke units, read from SceneInfo.paper_size.

    This is the authoritative stroke-coordinate extent for the page and the
    same value the write path (``strokes.page_geometry``) maps normalized
    coordinates into. Building a full-page render from it therefore makes the
    displayed image share ONE coordinate space with written strokes (so the
    drawing overlay and the write tool land ink in the same place). Falls back
    to the standard reMarkable page when no SceneInfo is present.
    """
    for b in blocks:
        ps = getattr(b, "paper_size", None)
        if ps and len(ps) == 2 and ps[0] and ps[1]:
            try:
                return float(ps[0]), float(ps[1])
            except (TypeError, ValueError):
                continue
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def _v6_group_offsets(blocks: list, anchor_pos: dict) -> dict:
    """Map group node ``CrdtId``s to cumulative ``(dx, dy)`` anchor offsets.

    In the v6 format a group of strokes can be anchored to a typed-text
    character (``Group.anchor_id``): its ink is stored relative to that anchor
    and the device translates it to the anchored line's Y as text reflows.
    ``anchor_pos`` comes from :func:`_v6_text_elements_with_bounds`. Offsets
    accumulate up the parent chain so groups nested under an anchored group
    inherit its translation. Returns ``{}`` when there is nothing to anchor.
    """
    if not anchor_pos:
        return {}

    parents: dict = {}
    own: dict = {}
    for block in blocks:
        # SceneTreeBlock links a node to its parent.
        tree_id = getattr(block, "tree_id", None)
        if tree_id is not None and hasattr(block, "parent_id"):
            parents[tree_id] = block.parent_id
        # TreeNodeBlock carries the group's anchor attributes.
        group = getattr(block, "group", None)
        if group is None:
            continue
        anchor_id = getattr(group, "anchor_id", None)
        if anchor_id is None:
            continue
        anchor_key = getattr(anchor_id, "value", None)
        if anchor_key not in anchor_pos:
            continue
        origin_x = getattr(group, "anchor_origin_x", None)
        dx = float(origin_x.value) if origin_x is not None else 0.0
        own[group.node_id] = (dx, float(anchor_pos[anchor_key]))

    if not own:
        return {}

    offsets: dict = {}

    def resolve(node_id) -> Tuple[float, float]:
        if node_id in offsets:
            return offsets[node_id]
        dx = dy = 0.0
        cur = node_id
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            if cur in own:
                odx, ody = own[cur]
                dx += odx
                dy += ody
            cur = parents.get(cur)
        offsets[node_id] = (dx, dy)
        return offsets[node_id]

    for node_id in set(parents) | set(own):
        resolve(node_id)
    return offsets


def _v6_paths_from_blocks(blocks: list, anchor_pos: Optional[dict] = None) -> Tuple[list, list]:
    """Build SVG ``<path>`` strings + a flat coordinate list from v6 blocks.

    ``anchor_pos`` (from :func:`_v6_text_elements_with_bounds`) enables
    text-anchored ink translation: strokes whose parent group is anchored to a
    typed-text line are shifted to that line's position, matching where the
    device draws them. Without it strokes render at their stored coordinates,
    which for anchored groups is on top of the typed text block.
    """
    highlights, ink, coords, _ = _v6_layers(blocks, anchor_pos)
    return highlights + ink, coords


def _v6_items(blocks: list, anchor_pos: Optional[dict] = None) -> List["_ink.InkItem"]:
    """Strokes and text highlights of a v6 page, with text-anchored ink moved."""
    group_offsets = _v6_group_offsets(blocks, anchor_pos) if anchor_pos else {}
    return _ink.scene_items(blocks, group_offsets)


def _v6_layers(blocks: list, anchor_pos: Optional[dict] = None) -> Tuple[list, list, list, float]:
    """``(highlight_elements, ink_elements, coords, max_half_width)`` of a page.

    Colour, width, opacity and blend come from :mod:`remarkable_mcp.ink`, the
    one style source every output format shares. Erasers are not drawn.
    """
    return _layer_elements(_v6_items(blocks, anchor_pos))


# reMarkable typed-text layout, in raw stroke units. Calibrated against
# device-rendered page thumbnails (a reMarkable Paper Pro / "Ferrari", which
# normalizes typed text into the same 1404x1872 coordinate space as the rM1/2).
# These differ from the rmc exporter's constants (github.com/ricklupton/rmc):
# rmc lays text out in a 72/226-DPI-scaled space and its plain font (7pt) and
# top offset (-88) render text noticeably smaller and higher than the device
# actually draws it. Values here are in the reference 1404x1872 space and are
# scaled by the page's real height (see ``_TEXT_REF_PAGE_HEIGHT``) so they adapt
# to other geometries (e.g. reMarkable Move) that normalize differently.
_TEXT_REF_PAGE_HEIGHT = 1872.0
_TEXT_TOP_Y = -39.0
_TEXT_DEFAULT_LINE_HEIGHT = 70.0
_TEXT_DEFAULT_FONT_SIZE = 30.0
# Continuation lines of a wrapped paragraph are spaced tighter than the gap
# between paragraphs (device uses ~44 vs ~70 units for plain text).
_TEXT_WRAP_LINE_RATIO = 44.0 / 70.0
# Average glyph advance as a fraction of font size, used only to wrap a
# paragraph to the text-box width (SVG <text> does not wrap on its own).
_TEXT_CHAR_ADVANCE = 0.5


def _wrap_text(text: str, max_width: float, char_advance: float) -> List[str]:
    """Greedily word-wrap ``text`` to ``max_width`` (stroke units).

    SVG ``<text>`` does not wrap, so paragraphs wider than the text box are
    split here the way the device wraps them. Line width is approximated as
    ``len(line) * char_advance`` -- good enough for a faithful preview without
    embedding the device font. A single word longer than the box is left on its
    own line rather than split. Returns ``[text]`` when no wrapping applies.
    """
    if max_width <= 0 or char_advance <= 0:
        return [text]
    lines: List[str] = []
    cur = ""
    for word in text.split(" "):
        trial = word if not cur else f"{cur} {word}"
        if not cur or len(trial) * char_advance <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _v6_text_svg_elements(blocks: list) -> list:
    """Build SVG ``<text>`` strings for typed text (a RootTextBlock) on a page.

    Thin wrapper around :func:`_v6_text_elements_with_bounds` for callers (e.g.
    the full-page render) that fix the viewBox to the page extent and so do not
    need the text's bounding coordinates.
    """
    return _v6_text_elements_with_bounds(blocks)[0]


# Special anchor CrdtIds the device uses for ink groups pinned to the page
# itself rather than to a typed character: (0, 281474976710654) anchors at the
# top of the text block, (0, 281474976710655) below its last line.
_ANCHOR_TEXT_TOP = (0, 281474976710654)
_ANCHOR_TEXT_BOTTOM = (0, 281474976710655)


def _v6_text_elements_with_bounds(blocks: list) -> Tuple[list, list, dict]:
    """Build SVG ``<text>`` strings, bounding coords and anchor map for typed text.

    Returns ``(elements, coords, anchor_pos)``. ``coords`` is a flat list of
    ``(x, y)`` extent points (in the page's stroke/screen units, center-origin
    X) so a content-cropped render can size its viewBox to include the text.
    ``anchor_pos`` maps text-character ``CrdtId``s (plus the special
    top/bottom-of-text ids) to the page-space Y of the line holding that
    character: ink groups anchored to a character must be translated by that Y
    (see :func:`_v6_paths_from_blocks`), which is how the device keeps
    handwriting positioned relative to text as the typed block reflows.
    Returns ``([], [], {})`` when the page has no typed text (the common case
    for handwritten notebooks) or when rmscene's text helpers are unavailable.
    """
    try:
        from rmscene.scene_items import ParagraphStyle, Text
        from rmscene.tagged_block_common import CrdtId
        from rmscene.text import TextDocument
    except ImportError:
        return [], [], {}

    text_item = next(
        (b.value for b in blocks if isinstance(getattr(b, "value", None), Text)),
        None,
    )
    if text_item is None:
        return [], [], {}

    # Blank pages we synthesize carry an empty RootTextBlock; skip them so we
    # neither emit empty <text> nodes nor trigger rmscene's empty-item warning.
    try:
        if not any(isinstance(v, str) and v.strip() for v in text_item.items.values()):
            return [], [], {}
    except Exception:
        pass

    line_heights = {
        ParagraphStyle.PLAIN: 70.0,
        ParagraphStyle.HEADING: 150.0,
        ParagraphStyle.BOLD: 70.0,
        ParagraphStyle.BULLET: 35.0,
        ParagraphStyle.BULLET2: 35.0,
        ParagraphStyle.CHECKBOX: 35.0,
        ParagraphStyle.CHECKBOX_CHECKED: 35.0,
    }
    font_sizes = {
        ParagraphStyle.HEADING: 60.0,
        ParagraphStyle.BOLD: 34.0,
    }

    try:
        doc = TextDocument.from_scene_item(text_item)
    except Exception:
        return [], [], {}

    pos_x = float(getattr(text_item, "pos_x", 0.0) or 0.0)
    pos_y = float(getattr(text_item, "pos_y", 0.0) or 0.0)
    box_width = float(getattr(text_item, "width", 0.0) or 0.0)

    # Scale the reference metrics to the page's real height so text on devices
    # that normalize to a different coordinate space (e.g. reMarkable Move)
    # stays proportional. Identity for the standard 1404x1872 page.
    _, paper_h = _v6_paper_size(blocks)
    scale = paper_h / _TEXT_REF_PAGE_HEIGHT if paper_h else 1.0

    elements: list = []
    coords: list = []
    anchor_pos: dict = {}
    y_offset = _TEXT_TOP_Y * scale
    anchor_pos[CrdtId(*_ANCHOR_TEXT_TOP)] = pos_y + y_offset
    for para in doc.contents:
        style = para.style.value if getattr(para, "style", None) is not None else None
        line_height = line_heights.get(style, _TEXT_DEFAULT_LINE_HEIGHT) * scale
        size = font_sizes.get(style, _TEXT_DEFAULT_FONT_SIZE) * scale
        text = str(para).strip()
        # Every character in this paragraph anchors ink at the paragraph's own
        # Y (the running offset before this paragraph consumes its lines), the
        # same reference point the device and the rmc exporter use.
        para_anchor_y = pos_y + y_offset
        try:
            anchor_pos[para.start_id] = para_anchor_y
            for subpara in para.contents:
                for char_id in getattr(subpara, "i", []) or []:
                    anchor_pos[char_id] = para_anchor_y
        except Exception:
            pass
        if not text:
            # A blank paragraph still consumes a line (e.g. spacing under a title).
            y_offset += line_height
            continue
        family = "serif" if style == ParagraphStyle.HEADING else "sans-serif"
        weight = (
            ' font-weight="bold"' if style in (ParagraphStyle.BOLD, ParagraphStyle.HEADING) else ""
        )
        # Wrap long paragraphs to the text box; each wrapped line takes a line,
        # with continuation lines spaced tighter than a paragraph break.
        for idx, line in enumerate(_wrap_text(text, box_width, size * _TEXT_CHAR_ADVANCE)):
            y_offset += line_height if idx == 0 else line_height * _TEXT_WRAP_LINE_RATIO
            baseline = pos_y + y_offset
            elements.append(
                f'<text x="{pos_x:.1f}" y="{baseline:.1f}" '
                f'font-family="{family}" font-size="{size:.1f}"{weight} '
                f'fill="black" xml:space="preserve">{_xml_escape(line)}</text>'
            )
            # Estimate the line's extent for crop sizing, capped at the box width.
            est_width = len(line) * size * _TEXT_CHAR_ADVANCE
            if box_width > 0:
                est_width = min(est_width, box_width)
            coords.append((pos_x, baseline - size))
            coords.append((pos_x + est_width, baseline + size * 0.3))
    anchor_pos[CrdtId(*_ANCHOR_TEXT_BOTTOM)] = pos_y + y_offset
    return elements, coords, anchor_pos


def _render_rm_v6_to_svg(rm_file_path: Path) -> Optional[str]:
    """
    Render a v6 .rm file to a content-cropped SVG (read-only viewing).

    This handles newer reMarkable firmware that uses the v6 format with
    scene tree blocks, including proper highlighter and color support. The
    viewBox is cropped to the ink bounding box; for a full-page render that
    shares the page's own coordinate space, see ``render_rm_file_full_page_png``.
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        parts = _v6_page_parts(blocks)
        # Highlights first (they multiply), then typed text, then handwriting.
        # The text extent is folded into the crop so a text-only page renders.
        return _svg_from_paths(parts.elements, parts.coords, CONTENT_PADDING + parts.max_half)
    except Exception:
        return None


class _PageParts(NamedTuple):
    highlights: list
    text: list
    ink: list
    coords: list
    max_half: float
    items: list
    paper: Tuple[float, float]

    @property
    def elements(self) -> list:
        return self.highlights + self.text + self.ink

    @property
    def foreground(self) -> list:
        """Typed text plus handwriting: everything drawn over the highlights."""
        return self.text + self.ink


def _v6_page_parts(blocks: list) -> _PageParts:
    """Everything needed to draw one v6 page, split into blend layers."""
    text_elements, text_coords, anchor_pos = _v6_text_elements_with_bounds(blocks)
    items = _v6_items(blocks, anchor_pos)
    highlights, ink, coords, max_half = _layer_elements(items)
    return _PageParts(
        highlights,
        text_elements,
        ink,
        coords + text_coords,
        max_half,
        items,
        _v6_paper_size(blocks),
    )


def render_rm_file_to_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[bytes]:
    """
    Render a .rm file to PNG image bytes.

    Uses the rmscene renderers to convert .rm to SVG, then PyMuPDF to convert to PNG.
    The output is sized based on the SVG content bounds with a margin.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", "transparent", None).
                         None means transparent. Use REMARKABLE_BACKGROUND_COLOR
                         for the standard reMarkable paper color.

    Returns:
        PNG image bytes, or None if rendering failed
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is not None:
        try:
            parts = _v6_page_parts(blocks)
            if not parts.elements or not parts.coords:
                return None
            min_x, min_y, w, h = _crop_box(parts.coords, CONTENT_PADDING + parts.max_half)
            m = CONTENT_MARGIN
            # The PNG keeps a CONTENT_MARGIN border on every side; widening the
            # viewBox by the same amount keeps 1 px per stroke unit (no stretch).
            return _render_ink_layers_png(
                parts.highlights,
                parts.foreground,
                (min_x - m, min_y - m, w + 2 * m, h + 2 * m),
                w + 2 * m,
                h + 2 * m,
                background_color=background_color,
            )
        except Exception as exc:
            logger.debug("Cropped v6 render failed: %s", exc)
            return None

    tmp_svg_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG via the rmscene renderers (v5 files)
        if not _rm_to_svg(rm_file_path, tmp_svg_path):
            return None

        # Get content bounds from SVG
        bounds = _get_svg_content_bounds(tmp_svg_path)
        if bounds:
            # Use content bounds with margin
            _, _, content_width, content_height = bounds
            output_width = int(content_width) + 2 * CONTENT_MARGIN
            output_height = int(content_height) + 2 * CONTENT_MARGIN
        else:
            # Fallback to standard reMarkable dimensions
            output_width = REMARKABLE_WIDTH
            output_height = REMARKABLE_HEIGHT

        return _svg_string_to_png(
            tmp_svg_path.read_text(),
            output_width,
            output_height,
            background_color,
        )
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _svg_full_page(paths: list, paper_w: float, paper_h: float) -> str:
    """Wrap paths in an SVG whose viewBox spans the WHOLE page in stroke units.

    The page coordinate system is center-origin in X (x in [-W/2, W/2]) and
    top-origin in Y (y in [0, H]) — the same mapping ``strokes._map_point``
    uses (``rm_x = (nx-0.5)*W``, ``rm_y = ny*H``). So a point drawn at
    normalized (nx, ny) over the rendered image lands at exactly the stroke
    coordinate the write tool will use. Empty ``paths`` yields a blank page.
    """
    min_x = -paper_w / 2.0
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{min_x:.0f} 0 {paper_w:.0f} {paper_h:.0f}" '
        f'width="{paper_w:.0f}" height="{paper_h:.0f}">'
        f"{''.join(paths)}</svg>"
    )


def _svg_string_to_image(svg: str, output_width: int, output_height: int):
    """Rasterize an SVG string to an exact-size RGBA PIL image, or None."""
    import io

    try:
        import fitz
    except ImportError:
        logger.error(
            "SVG rasterization requires PyMuPDF. Reinstall remarkable-mcp so its "
            "runtime dependencies are available."
        )
        return None

    if output_width < 1 or output_height < 1:
        logger.error(
            "SVG rasterization requires positive output dimensions, got %sx%s.",
            output_width,
            output_height,
        )
        return None

    doc = None
    try:
        doc = fitz.open(stream=svg.encode("utf-8"), filetype="svg")
        if len(doc) != 1:
            raise ValueError(f"expected one SVG page, found {len(doc)}")

        page = doc[0]
        if page.rect.width <= 0 or page.rect.height <= 0:
            raise ValueError(f"SVG has invalid dimensions: {page.rect}")

        matrix = fitz.Matrix(
            output_width / page.rect.width,
            output_height / page.rect.height,
        )
        pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=True)
        png = pixmap.tobytes("png")

        from PIL import Image as PILImage

        image = PILImage.open(io.BytesIO(png)).convert("RGBA")
        if image.size != (output_width, output_height):
            image = image.resize((output_width, output_height), PILImage.Resampling.LANCZOS)
        return image
    except Exception as exc:
        logger.error(
            "PyMuPDF could not rasterize SVG to PNG (%sx%s): %s",
            output_width,
            output_height,
            exc,
        )
        return None
    finally:
        if doc is not None:
            doc.close()


def _svg_string_to_png(
    svg: str, output_width: int, output_height: int, background_color: Optional[str]
) -> Optional[bytes]:
    """Rasterize an SVG string to exact-size PNG bytes without system Cairo."""
    import io

    image = _svg_string_to_image(svg, output_width, output_height)
    if image is None:
        return None

    from PIL import Image as PILImage

    background = _background_rgba(background_color)
    if background is not None:
        canvas = PILImage.new("RGBA", image.size, background)
        image = PILImage.alpha_composite(canvas, image)
        if background[3] == 255:
            image = image.convert("RGB")

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _background_rgba(color: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    """RGBA for a background colour; None (no background) for transparent."""
    if color is None:
        return None
    if str(color).strip().lower() in ("", "none", "transparent"):
        return None
    return _parse_hex_color(color)


def _render_ink_layers(
    highlights: list,
    foreground: list,
    viewbox: Tuple[float, float, float, float],
    width: float,
    height: float,
    *,
    background_color: Optional[str] = None,
    base_image=None,
):
    """Composite a page's highlight and ink layers into an RGBA image.

    MuPDF's SVG renderer ignores ``mix-blend-mode``, so the highlight layer is
    rasterised on its own and multiplied onto the base (paper colour or the PDF
    page): paper takes the highlight's tint while text and ink underneath stay
    dark, as on the device. Typed text and handwriting are then alpha-composited
    on top. On a transparent base there is nothing to multiply with, so the
    highlights are alpha-composited instead. Returns None if rasterising fails.
    """
    from PIL import Image as PILImage
    from PIL import ImageChops

    width = max(1, int(round(width)))
    height = max(1, int(round(height)))
    if base_image is not None:
        base = base_image.convert("RGBA")
        if base.size != (width, height):
            base = base.resize((width, height), PILImage.Resampling.LANCZOS)
    else:
        base = PILImage.new(
            "RGBA", (width, height), _background_rgba(background_color) or (0, 0, 0, 0)
        )

    if highlights:
        layer = _svg_string_to_image(
            _svg_document(highlights, viewbox, width, height), width, height
        )
        if layer is None:
            return None
        if base.getchannel("A").getextrema()[0] == 255:
            white = PILImage.new("RGBA", layer.size, (255, 255, 255, 255))
            tint = PILImage.alpha_composite(white, layer).convert("RGB")
            base = ImageChops.multiply(base.convert("RGB"), tint).convert("RGBA")
        else:
            base = PILImage.alpha_composite(base, layer)

    if foreground:
        layer = _svg_string_to_image(
            _svg_document(foreground, viewbox, width, height), width, height
        )
        if layer is None:
            return None
        base = PILImage.alpha_composite(base, layer)
    return base


def _encode_png(image) -> bytes:
    """PNG bytes; fully opaque images are stored as RGB."""
    import io

    if image.mode == "RGBA" and image.getchannel("A").getextrema()[0] == 255:
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _render_ink_layers_png(
    highlights: list,
    foreground: list,
    viewbox: Tuple[float, float, float, float],
    width: float,
    height: float,
    *,
    background_color: Optional[str] = None,
    base_image=None,
) -> Optional[bytes]:
    image = _render_ink_layers(
        highlights,
        foreground,
        viewbox,
        width,
        height,
        background_color=background_color,
        base_image=base_image,
    )
    return _encode_png(image) if image is not None else None


def render_rm_file_full_page_png(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Render a v6 .rm page to a FULL-PAGE PNG (not cropped to ink).

    Unlike ``render_rm_file_to_png`` (which crops the viewBox to the ink
    bounding box), this renders the whole page using a viewBox derived from the
    page's own ``SceneInfo.paper_size``. That makes the displayed image map
    linearly to the page's coordinate system, so the interactive drawing
    overlay places strokes exactly where the write tool will, and blank pages
    render as a blank page instead of returning ``None``.

    Returns ``(png_bytes, (paper_w, paper_h))`` or ``None`` if the file is not
    v6 or rendering dependencies are unavailable (callers should fall back).
    """
    blocks = _v6_blocks(rm_file_path)
    if blocks is None:
        return None
    try:
        parts = _v6_page_parts(blocks)
        paper_w, paper_h = parts.paper
    except Exception:
        return None

    # Highlights multiply onto the paper; typed text and then handwriting are
    # drawn on top, matching the device's compositing order.
    scale = FULL_PAGE_TARGET_LONG_EDGE / max(paper_w, paper_h)
    output_width = max(1, round(paper_w * scale))
    output_height = max(1, round(paper_h * scale))

    png = _render_ink_layers_png(
        parts.highlights,
        parts.foreground,
        (-paper_w / 2.0, 0.0, paper_w, paper_h),
        output_width,
        output_height,
        background_color=background_color,
    )
    if png is None:
        return None
    return png, (paper_w, paper_h)


def render_rm_file_to_svg(
    rm_file_path: Path, background_color: Optional[str] = None
) -> Optional[str]:
    """
    Render a .rm file to SVG string.

    Uses the rmscene renderers to convert .rm to SVG, optionally adding a background.

    Args:
        rm_file_path: Path to the .rm file
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.

    Returns:
        SVG content as string, or None if rendering failed
    """
    import subprocess
    import tempfile

    tmp_svg_path = None

    try:
        # Create temp file for SVG output
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
            tmp_svg_path = Path(tmp_svg.name)

        # Convert .rm to SVG via the rmscene renderers
        if not _rm_to_svg(rm_file_path, tmp_svg_path):
            return None

        # Read SVG content
        svg_content = tmp_svg_path.read_text()

        # Add background rectangle if color specified
        if background_color:
            svg_content = _add_svg_background(svg_content, background_color)

        return svg_content

    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        if tmp_svg_path:
            tmp_svg_path.unlink(missing_ok=True)


def _add_svg_background(svg_content: str, background_color: str) -> str:
    """Add a background rectangle to an SVG.

    Inserts a rect element as the first child of the SVG to act as background.

    Args:
        svg_content: Original SVG content
        background_color: Background color (e.g., "#FFFFFF")

    Returns:
        SVG content with background added
    """
    import re

    # Find the opening <svg> tag and its attributes
    svg_match = re.search(r"(<svg[^>]*>)", svg_content, re.IGNORECASE)
    if not svg_match:
        return svg_content

    svg_tag = svg_match.group(1)

    # Extract viewBox or width/height for the background rect dimensions
    viewbox_match = re.search(r'viewBox="([^"]*)"', svg_tag)
    if viewbox_match:
        viewbox = viewbox_match.group(1)
        parts = viewbox.split()
        if len(parts) == 4:
            x, y, width, height = parts
            bg_rect = (
                f'<rect x="{x}" y="{y}" width="{width}" '
                f'height="{height}" fill="{background_color}"/>'
            )
        else:
            # Fallback to full page
            bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'
    else:
        # No viewBox, use 100% dimensions
        bg_rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{background_color}"/>'

    # Insert background rect right after the opening svg tag
    insert_pos = svg_match.end()
    return svg_content[:insert_pos] + bg_rect + svg_content[insert_pos:]


def _get_ordered_rm_files(tmpdir_path: Path) -> List[Path]:
    """Extract and order .rm files from an extracted document directory.

    Reads the .content file to determine page order and returns .rm files
    sorted accordingly. Falls back to filesystem order if no page order found.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of .rm file paths in correct page order
    """
    # Get page order from .content file
    page_order = []
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            # New format: cPages.pages array
            if "cPages" in data and "pages" in data["cPages"]:
                page_order = [p["id"] for p in data["cPages"]["pages"]]
            # Fallback: pages array directly
            elif "pages" in data and isinstance(data["pages"], list):
                page_order = data["pages"]
        except Exception:
            # Ignore errors reading/parsing .content file; fallback to default page order
            pass
        break

    rm_files = list(tmpdir_path.glob("**/*.rm"))

    # Sort rm_files by page order if available
    if page_order:
        rm_by_id = {}
        for rm_file in rm_files:
            page_id = rm_file.stem
            rm_by_id[page_id] = rm_file

        ordered_rm_files = []
        for page_id in page_order:
            if page_id in rm_by_id:
                ordered_rm_files.append(rm_by_id[page_id])
        # Add any remaining files not in page order
        for rm_file in rm_files:
            if rm_file not in ordered_rm_files:
                ordered_rm_files.append(rm_file)
        return ordered_rm_files

    return rm_files


def _get_page_order(tmpdir_path: Path) -> List[str]:
    """Return the ordered list of page ids from the .content file.

    formatVersion 2 stores them under ``cPages.pages[].id``; formatVersion 1 as
    a flat ``pages`` list of ids. Returns an empty list if unavailable. This is
    the full page order (including pages that have no strokes), unlike
    ``_get_ordered_rm_files`` which is compacted to pages that do.
    """
    for content_file in tmpdir_path.glob("*.content"):
        try:
            data = json.loads(content_file.read_text())
            if "cPages" in data and "pages" in data["cPages"]:
                return [p.get("id") for p in data["cPages"]["pages"] if p.get("id")]
            if isinstance(data.get("pages"), list):
                return [p for p in data["pages"] if isinstance(p, str)]
        except Exception:
            pass
        break
    return []


def _select_rm_file_for_page(tmpdir_path: Path, rm_files: List[Path], page: int) -> Optional[Path]:
    """Return the .rm stroke file for a 1-based page, matched by page id.

    ``_get_ordered_rm_files`` is compacted (only pages that actually have
    strokes), so indexing it by page number pairs the wrong ink with a page
    whenever an earlier page is un-annotated. Map through the full ``.content``
    page order and match by id instead. Returns None when the page has no
    strokes (a page with a PDF underlay but no annotation), or when the page is
    out of range; callers should treat None as "no annotation layer".
    """
    page_order = _get_page_order(tmpdir_path)
    if page_order:
        if 1 <= page <= len(page_order):
            page_id = page_order[page - 1]
            return next((p for p in rm_files if p.stem == page_id), None)
        return None
    # No page order available (unusual): fall back to positional selection.
    if 1 <= page <= len(rm_files):
        return rm_files[page - 1]
    return None


def _source_pdf_path(tmpdir_path: Path) -> Optional[Path]:
    """The archive's source PDF, preferring the one named after the document."""
    pdf_files = list(tmpdir_path.glob("**/*.pdf"))
    if not pdf_files:
        return None
    content_stems = {p.stem for p in tmpdir_path.glob("*.content")}
    matching = [p for p in pdf_files if p.stem in content_stems]
    return matching[0] if matching else sorted(pdf_files)[0]


def _page_geometry(
    tmpdir_path: Path,
    page: int,
    blocks: Optional[list],
    paper_hint: Optional[Tuple[float, float]] = None,
) -> "_ink.PageGeometry":
    """Geometry of a page: its mapped PDF page when it has one, else the paper.

    Uses the same scale and centring as the merged compositor, so coordinates
    and full-page renders line up with the PDF exactly.
    """
    if blocks is not None:
        paper = _v6_paper_size(blocks)
    else:
        paper = paper_hint or (float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT))
    ppu = _points_per_unit(paper)

    pdf_path = _source_pdf_path(tmpdir_path)
    if pdf_path is not None:
        index = _resolve_pdf_page_index(tmpdir_path, page)
        if index is not None:
            try:
                import fitz

                with fitz.open(pdf_path) as doc:
                    if 0 <= index < len(doc):
                        rect = doc[index].rect
                        return _ink.PageGeometry("pdf", rect.width, rect.height, ppu, index)
            except Exception as exc:
                logger.debug("Could not read PDF page geometry: %s", exc)
    return _ink.PageGeometry("notebook", paper[0] * ppu, paper[1] * ppu, ppu, None)


def _full_page_viewbox(
    geometry: "_ink.PageGeometry", parts: Optional[_PageParts]
) -> Tuple[float, float, float, float]:
    """Full-page viewBox in stroke units: the page (the PDF page, or the
    notebook paper), grown to include any ink drawn beside or below it, so
    annotation-only output never hides margin notes or scrolled ink. The page
    rectangle itself is reported on the SVG root (``data-page-rm-viewbox``).
    """
    x, y, w, h = geometry.viewbox
    if parts is not None and parts.coords:
        cx, cy, cw, ch = _crop_box(parts.coords, CONTENT_PADDING + parts.max_half)
        x0, y0 = min(x, cx), min(y, cy)
        x1, y1 = max(x + w, cx + cw), max(y + h, cy + ch)
        return x0, y0, x1 - x0, y1 - y0
    return x, y, w, h


def _empty_parts(paper: Tuple[float, float]) -> _PageParts:
    return _PageParts([], [], [], [], 0.0, [], paper)


def _resolve_page_render(
    tmpdir_path: Path, page: int, full_page: Optional[bool]
) -> Tuple[Optional[Path], Optional[list], Optional["_ink.PageGeometry"], bool]:
    """Pick the page's .rm, its blocks, its geometry and whether to render full.

    ``full_page=None`` means "full page for PDF-backed pages, cropped to the
    ink otherwise".
    """
    rm_files = _get_ordered_rm_files(tmpdir_path)
    rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
    blocks = _v6_blocks(rm_file) if rm_file is not None else None
    if full_page is False:
        return rm_file, blocks, None, False
    geometry = _page_geometry(tmpdir_path, page, blocks, paper_hint=_document_paper_size(rm_files))
    want_full = geometry.kind == "pdf" if full_page is None else True
    return rm_file, blocks, geometry, want_full


def _geometry_root_attrs(geometry: "_ink.PageGeometry") -> str:
    attrs = (
        f' data-geometry="{geometry.kind}"'
        f' data-points-per-unit="{geometry.points_per_unit:.6g}"'
        f' data-page-width-pt="{_ink._fmt(geometry.width_pt)}"'
        f' data-page-height-pt="{_ink._fmt(geometry.height_pt)}"'
        f' data-page-rm-viewbox="{" ".join(_ink._fmt(v) for v in geometry.viewbox)}"'
    )
    if geometry.pdf_page_index is not None:
        attrs += f' data-pdf-page-index="{geometry.pdf_page_index}"'
    return attrs


def render_page_svg_from_extracted_document(
    tmpdir_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    full_page: Optional[bool] = False,
) -> Optional[str]:
    """Render one page's annotation layer to SVG.

    ``full_page=False`` crops to the ink (the historical behaviour). ``True``
    renders the whole page plus any ink beyond it (see
    :func:`_full_page_viewbox`); for a PDF-backed page ``width``/``height`` are
    that extent in PDF points (unitless numbers), and ``data-*`` attributes on
    the root give the scale and the page rectangle. ``None`` picks full page
    for PDF-backed pages and the ink crop otherwise.
    """
    rm_file, blocks, geometry, want_full = _resolve_page_render(tmpdir_path, page, full_page)
    if rm_file is None:
        return None
    if not want_full or blocks is None or geometry is None:
        # Cropped render (v5 files have no page geometry and always crop).
        return render_rm_file_to_svg(rm_file, background_color=background_color)

    parts = _v6_page_parts(blocks)
    viewbox = _full_page_viewbox(geometry, parts)
    if geometry.kind == "pdf":
        width = viewbox[2] * geometry.points_per_unit
        height = viewbox[3] * geometry.points_per_unit
    else:
        width, height = viewbox[2], viewbox[3]
    svg = _svg_document(parts.elements, viewbox, width, height, _geometry_root_attrs(geometry))
    if background_color:
        svg = _add_svg_background(svg, background_color)
    return svg


def render_page_png_from_extracted_document(
    tmpdir_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    full_page: Optional[bool] = False,
) -> Optional[bytes]:
    """Render one page's annotation layer to PNG (see the SVG twin for
    ``full_page``). Full-page PDF renders use 2 px per PDF point, like the
    merged view; full-page notebook renders use the full-page canvas scale.
    """
    rm_file, blocks, geometry, want_full = _resolve_page_render(tmpdir_path, page, full_page)
    if rm_file is None:
        return None
    if not want_full or blocks is None or geometry is None:
        return render_rm_file_to_png(rm_file, background_color=background_color)

    parts = _v6_page_parts(blocks)
    viewbox = _full_page_viewbox(geometry, parts)
    if geometry.kind == "pdf":
        scale = 2.0 * geometry.points_per_unit
    else:
        scale = FULL_PAGE_TARGET_LONG_EDGE / max(parts.paper)
    return _render_ink_layers_png(
        parts.highlights,
        parts.foreground,
        viewbox,
        viewbox[2] * scale,
        viewbox[3] * scale,
        background_color=background_color,
    )


def render_page_annotation_layer_from_extracted_document(
    tmpdir_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Full-page annotation layer for exports.

    A PDF-backed page renders on its PDF page (2 px per point, so the exported
    page keeps the PDF's size and no ink is clipped by the paper grid). Other
    pages use :func:`render_page_full_page_from_extracted_document`.
    """
    rm_files = _get_ordered_rm_files(tmpdir_path)
    rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
    blocks = _v6_blocks(rm_file) if rm_file is not None else None
    geometry = _page_geometry(tmpdir_path, page, blocks, paper_hint=_document_paper_size(rm_files))
    if geometry.kind != "pdf" or (rm_file is not None and blocks is None):
        return render_page_full_page_from_extracted_document(
            tmpdir_path, page=page, background_color=background_color
        )

    parts = _v6_page_parts(blocks) if blocks is not None else _empty_parts((0.0, 0.0))
    png = _render_ink_layers_png(
        parts.highlights,
        parts.foreground,
        geometry.viewbox,
        geometry.width_pt * 2.0,
        geometry.height_pt * 2.0,
        background_color=background_color,
    )
    if png is None:
        return None
    return png, (geometry.width_pt, geometry.height_pt)


def _extract_zip(zip_path: Path, tmpdir_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tmpdir_path)


def render_page_from_document_zip_svg(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    full_page: Optional[bool] = False,
) -> Optional[str]:
    """
    Render a specific page from a reMarkable document zip to SVG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.
        full_page: False crops to the ink, True renders the whole page, None
                   renders PDF-backed pages full page and crops the rest.

    Returns:
        SVG content as string, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _extract_zip(zip_path, tmpdir_path)
        return render_page_svg_from_extracted_document(
            tmpdir_path, page, background_color=background_color, full_page=full_page
        )


def render_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    full_page: Optional[bool] = False,
) -> Optional[bytes]:
    """
    Render a specific page from a reMarkable document zip to PNG.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color (e.g., "#FFFFFF", None for transparent).
                         Use REMARKABLE_BACKGROUND_COLOR for the standard paper color.
        full_page: False crops to the ink, True renders the whole page, None
                   renders PDF-backed pages full page and crops the rest.

    Returns:
        PNG image bytes, or None if rendering failed or page doesn't exist
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _extract_zip(zip_path, tmpdir_path)
        return render_page_png_from_extracted_document(
            tmpdir_path, page, background_color=background_color, full_page=full_page
        )


def _render_items_crop_png(
    items: List["_ink.InkItem"],
    box: Tuple[float, float, float, float],
    px_per_unit: float = 2.0,
    pad: float = CONTENT_PADDING,
) -> Optional[bytes]:
    """Render a few strokes, cropped to ``box`` (x0, y0, x1, y1), on white."""
    elements: list = []
    for item in items:
        elements.extend(_ink.svg_elements(item))
    viewbox = (box[0] - pad, box[1] - pad, box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad)
    return _render_ink_layers_png(
        [],
        elements,
        viewbox,
        viewbox[2] * px_per_unit,
        viewbox[3] * px_per_unit,
        background_color="#FFFFFF",
    )


#: OCR runs on at most this many ink clusters per call.
MAX_OCR_CLUSTERS = 40


def extract_page_ink_metadata(
    zip_path: Path,
    page: int = 1,
    *,
    include_strokes: bool = True,
    cluster_gap_pt: Optional[float] = 8.0,
    ocr=None,
    stroke_offset: int = 0,
    max_strokes: int = _ink.DEFAULT_MAX_STROKES,
) -> Dict[str, Any]:
    """Colour, tool and position of every stroke and text highlight on a page.

    Coordinates are PDF points with a top-left origin (see
    :class:`remarkable_mcp.ink.PageGeometry`) plus the raw stroke coordinates.
    The per-stroke list is paged by ``stroke_offset``/``max_strokes``.
    ``ocr``, when given, is called with PNG bytes for each ink cluster (up to
    :data:`MAX_OCR_CLUSTERS`, highlighters excluded) and its text is attached.
    Raises ``ValueError`` for a page outside the document.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        _extract_zip(zip_path, tmpdir_path)

        rm_files = _get_ordered_rm_files(tmpdir_path)
        page_order = _get_page_order(tmpdir_path)
        total = len(page_order) or len(rm_files)
        if page < 1 or page > total:
            raise ValueError(f"Page {page} out of range (document has {total} pages).")

        rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
        blocks = _v6_blocks(rm_file) if rm_file is not None else None
        geometry = _page_geometry(
            tmpdir_path, page, blocks, paper_hint=_document_paper_size(rm_files)
        )
        result: Dict[str, Any] = {
            "page": page,
            "total_pages": total,
            "page_id": page_order[page - 1] if page_order else (rm_file.stem if rm_file else None),
        }

        if rm_file is None or blocks is None:
            result.update(
                _ink.page_metadata(
                    [], geometry, include_strokes=include_strokes, cluster_gap_pt=cluster_gap_pt
                )
            )
            result["note"] = (
                "This page has no annotation layer."
                if rm_file is None
                else "This page uses the legacy v5 format; stroke metadata needs firmware 3.x."
            )
            return result

        _, _, anchor_pos = _v6_text_elements_with_bounds(blocks)
        group_offsets = _v6_group_offsets(blocks, anchor_pos) if anchor_pos else {}
        all_items = _ink.scene_items(blocks, group_offsets, include_erasers=True)
        items = [it for it in all_items if not it.style.is_eraser]
        meta = _ink.page_metadata(
            items,
            geometry,
            include_strokes=include_strokes,
            cluster_gap_pt=cluster_gap_pt,
            erasers=len(all_items) - len(items),
            stroke_offset=stroke_offset,
            max_strokes=max_strokes,
        )

        clusters = meta.get("clusters") or []
        if ocr is not None and clusters:
            stroke_items = [it for it in items if it.kind == "stroke"]
            text_clusters = [c for c in clusters if c["tool"] not in _ink.HIGHLIGHT_TOOLS]
            for cluster in text_clusters[:MAX_OCR_CLUSTERS]:
                members = [stroke_items[i] for i in cluster["stroke_indexes"]]
                box = _ink.union_box(m.bbox for m in members)
                png = _render_items_crop_png(members, box)
                try:
                    cluster["ocr_text"] = ocr(png) if png else None
                except Exception as exc:
                    logger.debug("Cluster OCR failed: %s", exc)
                    cluster["ocr_text"] = None
            if len(text_clusters) > MAX_OCR_CLUSTERS:
                meta["ocr_note"] = (
                    f"OCR ran on the first {MAX_OCR_CLUSTERS} of {len(text_clusters)} "
                    "ink clusters (reading order)."
                )
        result.update(meta)
        return result


def _document_paper_size(rm_files: List[Path]) -> Tuple[float, float]:
    """Best-effort page extent for a document by scanning its .rm files.

    Used to render a blank page (one with no .rm of its own) at the same size
    as the rest of the document. Falls back to the standard reMarkable page.
    """
    for p in rm_files:
        blocks = _v6_blocks(p)
        if blocks:
            return _v6_paper_size(blocks)
    return float(REMARKABLE_WIDTH), float(REMARKABLE_HEIGHT)


def render_page_full_page_from_document_zip(
    zip_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Full-page render of a page, addressed by cPages index.

    This is the render used by the interactive canvas. It differs from
    ``render_page_from_document_zip`` in two ways that matter for write-back:

    1. Pages are addressed by the ``.content`` cPages order — the SAME index
       the write tool (``_page_ids_from_content``) uses — so "page N" means the
       same page in the viewer and the writer even when blank pages (which may
       have no ``.rm`` file) are present.
    2. The page is rendered full-bleed using its own ``SceneInfo.paper_size``,
       so the overlay's normalized coordinates map exactly onto stroke space.

    Returns ``(png_bytes, (paper_w, paper_h))`` or ``None`` (caller falls back
    to PDF rasterization or an error).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        return render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
        )


def render_page_full_page_from_extracted_document(
    tmpdir_path: Path, page: int = 1, background_color: Optional[str] = None
) -> Optional[Tuple[bytes, Tuple[float, float]]]:
    """Render a full page from an already extracted document archive."""
    rm_files = _get_ordered_rm_files(tmpdir_path)
    page_order = _get_page_order(tmpdir_path)

    if page_order:
        if page < 1 or page > len(page_order):
            return None
        rm_file = _select_rm_file_for_page(tmpdir_path, rm_files, page)
    else:
        # No page metadata: fall back to filesystem order of .rm files.
        if page < 1 or page > len(rm_files):
            return None
        rm_file = rm_files[page - 1]

    if rm_file is not None and rm_file.exists():
        return render_rm_file_full_page_png(rm_file, background_color=background_color)

    # The page exists in cPages but has no .rm layer yet (a blank page):
    # render a blank full page at the document's paper size so the viewer
    # still shows it. (The write tool will return no_page_layer until the
    # page has a drawable layer / has been added via remarkable_add_page.)
    paper_w, paper_h = _document_paper_size(rm_files)
    svg = _svg_full_page([], paper_w, paper_h)
    scale = FULL_PAGE_TARGET_LONG_EDGE / max(paper_w, paper_h)
    png = _svg_string_to_png(
        svg, max(1, round(paper_w * scale)), max(1, round(paper_h * scale)), background_color
    )
    if png is None:
        return None
    return png, (paper_w, paper_h)


def document_zip_has_pdf_underlay(zip_path: Path) -> bool:
    """Check if a reMarkable document zip contains a PDF underlay.

    Args:
        zip_path: Path to the document zip file

    Returns:
        True if the zip contains a .pdf file
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return any(name.endswith(".pdf") for name in zf.namelist())
    except Exception:
        return False


def _read_cpages_entries(tmpdir_path: Path) -> List[Dict[str, Any]]:
    """Read cPages.pages entries from the .content metadata file.

    Args:
        tmpdir_path: Path to the extracted document directory

    Returns:
        List of cPages page entries, or empty list if not found
    """
    content_file = next(tmpdir_path.glob("*.content"), None)
    if content_file is None:
        return []
    try:
        data = json.loads(content_file.read_text())
        if "cPages" in data and "pages" in data["cPages"]:
            return data["cPages"]["pages"]
    except Exception:
        pass
    return []


def _pdf_page_index_for_cpages_entry(entry: Dict[str, Any]) -> Optional[int]:
    """Get the 0-based PDF page index from a cPages entry's redir field.

    The redir.value field in a cPages entry maps the reMarkable page to
    the original PDF page number (0-based).

    Args:
        entry: A single cPages page entry dict

    Returns:
        0-based PDF page index, or None if no redirect exists
    """
    redir = entry.get("redir", {})
    if isinstance(redir, dict) and "value" in redir:
        try:
            return int(redir["value"])
        except (ValueError, TypeError):
            return None
    return None


def _read_redirection_page_map(tmpdir_path: Path) -> List[Any]:
    """Read the legacy (formatVersion 1) redirectionPageMap from .content.

    formatVersion 1 documents have no cPages structure; instead the reMarkable
    page -> original PDF page mapping is a flat list where the index is the
    0-based reMarkable page and the value is the 0-based PDF page index (-1 for
    user-added pages). Many cloud-imported PDFs use this layout.

    Returns the list, or an empty list if not found.
    """
    content_file = next(tmpdir_path.glob("*.content"), None)
    if content_file is None:
        return []
    try:
        data = json.loads(content_file.read_text())
        rpm = data.get("redirectionPageMap")
        if isinstance(rpm, list):
            return rpm
    except Exception:
        pass
    return []


def _resolve_pdf_page_index(tmpdir_path: Path, page: int) -> Optional[int]:
    """Resolve the 0-based PDF page index for a 1-based reMarkable page.

    Handles both document layouts:
    - formatVersion 2: ``cPages.pages[i].redir.value``
    - formatVersion 1: ``redirectionPageMap[i]`` (legacy; used by many
      cloud-imported PDFs)

    Returns None only when the page genuinely has no PDF underlay (a user-added
    page). This lets render_merged composite annotations onto imported PDFs that
    use either layout, instead of falling back to an annotation-only render.

    When cPages entries exist they are authoritative: an entry without ``redir``
    is a user-added page, and we must NOT fall through to a redirectionPageMap a
    v1->v2 migrated document may still carry — its stale, order-shifted indices
    could composite the wrong PDF page under a user-added page.
    """
    # formatVersion 2 (cPages) — authoritative when present
    entries = _read_cpages_entries(tmpdir_path)
    if entries:
        if 1 <= page <= len(entries):
            return _pdf_page_index_for_cpages_entry(entries[page - 1])
        return None

    # formatVersion 1 (redirectionPageMap)
    rpm = _read_redirection_page_map(tmpdir_path)
    if rpm:
        if 1 <= page <= len(rpm):
            val = rpm[page - 1]
            if isinstance(val, int) and val >= 0:
                return val
        return None

    # Some formatVersion 1 documents (including files uploaded by this server)
    # contain a flat page-id list but omit redirectionPageMap. With no explicit
    # user-added-page markers, the source PDF order is the only authoritative
    # mapping and is therefore identity.
    page_order = _get_page_order(tmpdir_path)
    if page_order and 1 <= page <= len(page_order):
        return page - 1

    return None


def _render_pdf_page_to_png(
    pdf_bytes: bytes, page_index: int, width: int, height: int
) -> Optional[bytes]:
    """Rasterize a single PDF page to PNG bytes using PyMuPDF (fitz).

    Args:
        pdf_bytes: Raw PDF file bytes
        page_index: 0-based page index
        width: Target output width in pixels
        height: Target output height in pixels

    Returns:
        PNG image bytes, or None on failure
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if page_index < 0 or page_index >= len(doc):
                return None

            pdf_page = doc[page_index]
            # Scale to fill the target dimensions
            mat = fitz.Matrix(width / pdf_page.rect.width, height / pdf_page.rect.height)
            pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


def render_tablet_pdf_page_to_png(
    pdf_bytes: bytes, page: int = 1, target_long_edge: int = 2048
) -> Optional[bytes]:
    """Rasterize one page of the tablet's native PDF export to PNG bytes.

    This is the portable fallback used when the local stroke renderer cannot
    produce an image. The reMarkable's own firmware renders every notebook (and
    annotated PDF/EPUB) to a PDF served at ``/download/<uuid>/pdf``, so this path
    works regardless of the .rm block format, handles empty pages, and—crucially
    for portability—does not depend on a system graphics library (PyMuPDF
    bundles its own renderer). Credit: ljdutel (#95).

    Args:
        pdf_bytes: Raw bytes of the tablet-exported PDF.
        page: 1-based page number (matches notebook page ordering 1:1).
        target_long_edge: Target pixel size for the longest page edge.

    Returns:
        PNG image bytes, or None on failure / out-of-range page.
    """
    try:
        import fitz
    except ImportError:
        return None

    # The tablet's PDFs sometimes reference graphics-state resources MuPDF
    # considers malformed; it still rasterizes them correctly, so suppress the
    # noisy non-fatal error output.
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None

    try:
        index = page - 1
        if index < 0 or index >= len(doc):
            return None
        pdf_page = doc[index]
        longest_edge = max(pdf_page.rect.width, pdf_page.rect.height) or 1.0
        zoom = target_long_edge / longest_edge
        pix = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None
    finally:
        doc.close()


def render_mapped_pdf_page_from_document_zip(
    zip_path: Path, page: int = 1, target_long_edge: int = 2048
) -> Tuple[Optional[bytes], bool]:
    """Render a source-PDF underlay using the document's authoritative page map.

    Returns ``(png, has_source_pdf)``. A source PDF with ``png=None`` means the
    requested page has no mapped underlay (for example, a user-added page), so
    callers must not substitute the same ordinal from a native PDF export.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir_path)
        except Exception:
            return None, False

        pdf_files = list(tmpdir_path.glob("**/*.pdf"))
        if not pdf_files:
            return None, False

        pdf_page_index = _resolve_pdf_page_index(tmpdir_path, page)
        if pdf_page_index is None:
            return None, True

        content_stems = {path.stem for path in tmpdir_path.glob("*.content")}
        matching = [path for path in pdf_files if path.stem in content_stems]
        pdf_path = matching[0] if matching else sorted(pdf_files)[0]
        png = render_tablet_pdf_page_to_png(
            pdf_path.read_bytes(),
            page=pdf_page_index + 1,
            target_long_edge=target_long_edge,
        )
        return png, True


def render_merged_page_from_document_zip(
    zip_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    canvas_width: Optional[int] = None,
    canvas_height: Optional[int] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    """Render a page with the PDF underlay composited with the annotation layer.

    Extracts the zip, determines which PDF page corresponds to the requested
    reMarkable page, rasterizes the PDF page, renders the annotation layer,
    and alpha-composites them into a single image.

    Credit: Re-implementation inspired by PR #79 from @ColinSha.

    Args:
        zip_path: Path to the document zip file
        page: Page number (1-indexed)
        background_color: Background color for annotation layer
        canvas_width: Output canvas width (default: derived from PDF page)
        canvas_height: Output canvas height (default: derived from PDF page)

    Returns:
        Tuple of (png_bytes, note) where note is an informational message
        or None. Returns (None, error_note) on failure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir_path)
        except Exception:
            return None, "Could not extract document archive."

        return render_merged_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        )


def render_merged_page_from_extracted_document(
    tmpdir_path: Path,
    page: int = 1,
    background_color: Optional[str] = None,
    canvas_width: Optional[int] = None,
    canvas_height: Optional[int] = None,
) -> tuple[Optional[bytes], Optional[str]]:
    """Render a merged page from an already extracted document archive."""
    import io

    import fitz
    from PIL import Image as PILImage

    # Find the PDF file in the extracted directory. If multiple are present
    # (rare), prefer the one whose stem matches the .content document id so
    # selection is deterministic.
    pdf_files = list(tmpdir_path.glob("**/*.pdf"))
    if not pdf_files:
        full_page = render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
        )
        if full_page is None:
            return None, f"Page {page} has no renderable annotation layer."
        return full_page[0], "No PDF underlay found; returned full-page annotation render."

    content_stems = {p.stem for p in tmpdir_path.glob("*.content")}
    matching = [p for p in pdf_files if p.stem in content_stems]
    pdf_path = matching[0] if matching else sorted(pdf_files)[0]
    pdf_bytes = pdf_path.read_bytes()

    rm_files = _get_ordered_rm_files(tmpdir_path)
    total_pages = len(_get_page_order(tmpdir_path)) or len(rm_files)
    if total_pages == 0:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
                total_pages = len(pdf_document)
        except Exception:
            total_pages = 0

    if page < 1 or page > total_pages:
        return None, f"Page {page} out of range (document has {total_pages} pages)."

    # Select the .rm for this page by id (see _select_rm_file_for_page). A
    # page with no strokes yields None and composites to the bare PDF page.
    target_rm_file: Optional[Path] = _select_rm_file_for_page(tmpdir_path, rm_files, page)

    def annotation_only(reason: str) -> Tuple[Optional[bytes], Optional[str]]:
        """Return a complete annotation page when the PDF side cannot be used."""
        full_page = render_page_full_page_from_extracted_document(
            tmpdir_path,
            page=page,
            background_color=background_color,
        )
        if full_page is None:
            return None, f"{reason}; page has no renderable annotation layer."
        return full_page[0], f"{reason}; returned full-page annotation render."

    # Determine which PDF page this reMarkable page maps to. Handles both the
    # formatVersion 2 (cPages.redir) and formatVersion 1 (redirectionPageMap)
    # layouts; the latter is used by many cloud-imported PDFs, which would
    # otherwise be misread as user-added pages and rendered annotation-only.
    pdf_page_index: Optional[int] = _resolve_pdf_page_index(tmpdir_path, page)

    if pdf_page_index is None:
        return annotation_only("Page has no PDF underlay (user-added page)")

    # Get PDF page dimensions to set annotation viewBox correctly
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            if pdf_page_index >= len(doc):
                return annotation_only("PDF page index out of range")

            pdf_page = doc[pdf_page_index]
            pdf_w_pt = pdf_page.rect.width
            pdf_h_pt = pdf_page.rect.height
        finally:
            doc.close()
    except Exception:
        return annotation_only("Could not read PDF dimensions")

    # Determine output canvas size
    out_w = canvas_width or int(pdf_w_pt * 2)
    out_h = canvas_height or int(pdf_h_pt * 2)

    # 1. Rasterize the PDF page
    pdf_png = _render_pdf_page_to_png(pdf_bytes, pdf_page_index, out_w, out_h)
    if pdf_png is None:
        return annotation_only("PDF rasterization failed")

    try:
        pdf_img = PILImage.open(io.BytesIO(pdf_png)).convert("RGBA")
    except Exception:
        return annotation_only("Compositing failed")

    # 2. Draw the annotation layer onto the PDF page. rmscene coordinates use
    #    the device paper grid, not PDF points, so the viewBox is the calibrated
    #    page rectangle. Highlights multiply onto the page (PDF text under a
    #    highlight stays dark); handwriting is composited on top.
    composite = pdf_img
    ann_render_error: Optional[Exception] = None
    try:
        blocks = _v6_blocks(target_rm_file) if target_rm_file is not None else None
        if blocks is not None:
            parts = _v6_page_parts(blocks)
            viewbox = _annotation_page_viewbox(
                pdf_w_pt, pdf_h_pt, _points_per_unit(parts.paper), centered_x=True
            )
            layered = _render_ink_layers(
                parts.highlights, parts.foreground, viewbox, out_w, out_h, base_image=pdf_img
            )
            if layered is None:
                raise RuntimeError("PyMuPDF could not rasterize the annotation SVG")
            composite = layered
        elif target_rm_file is not None:
            composite = _composite_v5_overlay(target_rm_file, pdf_img, pdf_w_pt, pdf_h_pt)
    except Exception as exc:
        # Annotation rendering failed; return the PDF page but expose the loss.
        logger.debug("Annotation overlay rendering failed: %s", exc)
        ann_render_error = exc
        composite = pdf_img

    merged_note = (
        "Annotation overlay failed to render; returned PDF page without annotations."
        if ann_render_error is not None
        else None
    )

    # 3. Encode
    try:
        composite = composite.convert("RGB")
        buf = io.BytesIO()
        composite.save(buf, format="PNG")
        return buf.getvalue(), merged_note
    except Exception:
        return annotation_only("Compositing failed")


def _composite_v5_overlay(rm_file: Path, pdf_img, pdf_w_pt: float, pdf_h_pt: float):
    """Alpha-composite a v5 (.lines) annotation layer onto a PDF page image."""
    import io

    from PIL import Image as PILImage

    svg_content = _render_rm_v5_to_svg(rm_file)
    if svg_content is None:
        return pdf_img
    out_w, out_h = pdf_img.size
    viewbox = _annotation_page_viewbox(pdf_w_pt, pdf_h_pt, _points_per_unit(None), centered_x=False)
    svg_content = _rewrite_svg_root(svg_content, viewbox=viewbox, width=out_w, height=out_h)
    png = _svg_string_to_png(svg_content, out_w, out_h, None)
    if png is None:
        raise RuntimeError("PyMuPDF could not rasterize the annotation SVG")
    overlay = PILImage.open(io.BytesIO(png)).convert("RGBA")
    return PILImage.alpha_composite(pdf_img, overlay)


def get_document_page_count(zip_path: Path) -> int:
    """
    Get the number of pages in a reMarkable document zip.

    Uses the .content metadata file for accurate page count (includes
    user-added pages in PDFs). Falls back to counting .rm files.

    Args:
        zip_path: Path to the document zip file

    Returns:
        Number of pages (0 if unable to determine)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Try .content metadata first — it's the authoritative page list
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "cPages" in data and "pages" in data["cPages"]:
                    return len(data["cPages"]["pages"])
                if "pages" in data and isinstance(data["pages"], list):
                    return len(data["pages"])
            except Exception:
                pass
            break

        # Fallback to counting .rm files
        return len(list(tmpdir_path.glob("**/*.rm")))


def get_document_file_type(zip_path: Path) -> str:
    """
    Read the ``fileType`` from a document zip's ``.content`` file.

    Returns one of "notebook", "pdf", "epub", or "" when it cannot be
    determined. Reads only the ``.content`` entry from the zip (no full
    extraction) so it is cheap to call alongside get_document_page_count.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".content"):
                    try:
                        data = json.loads(zf.read(name).decode("utf-8"))
                    except Exception:
                        return ""
                    return str(data.get("fileType", "") or "")
    except Exception:
        pass
    return ""


def _extract_page_annotations(rm_file: Path) -> Tuple[List[str], bool]:
    """Extract a page's text highlights and whether it has pen strokes.

    reMarkable text highlights are stored as ``GlyphRange`` scene items inside the
    page's v6 ``.rm`` file (each carries the selected ``text`` and its bounding
    ``rectangles``); freehand ink is stored as ``Line`` items. The legacy
    ``.highlights`` JSON scanned elsewhere does not cover current firmware, so
    read them here.

    Returns ``(highlighted_texts_in_reading_order, has_pen_strokes)``. Returns
    ``([], False)`` if the file cannot be parsed.
    """
    try:
        import rmscene
    except Exception:
        return [], False
    try:
        with rm_file.open("rb") as fh:
            tree = rmscene.read_tree(fh)
    except Exception:
        return [], False

    highlights: list = []
    has_strokes = False
    try:
        for item in tree.walk():
            kind = type(item).__name__
            if kind == "Line":
                has_strokes = True
            elif kind == "GlyphRange":
                text = getattr(item, "text", None)
                if not text:
                    continue
                rects = getattr(item, "rectangles", None) or []
                y = rects[0].y if rects else 0.0
                x = rects[0].x if rects else 0.0
                highlights.append((y, x, text))
    except Exception:
        pass

    highlights.sort(key=lambda t: (t[0], t[1]))  # top-to-bottom, left-to-right
    return [t[2] for t in highlights], has_strokes


def extract_text_from_document_zip(
    zip_path: Path, include_ocr: bool = False, doc_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extract all text content from a reMarkable document zip.

    Args:
        zip_path: Path to the document zip file
        include_ocr: Whether to run OCR on handwritten content
        doc_id: Optional document ID for caching OCR results

    Returns:
        {
            "typed_text": [...],      # From rmscene parsing (list of strings)
            "highlights": [...],       # Highlighted text (GlyphRange + legacy JSON)
            "handwritten_text": [...], # From OCR (if enabled) - one per page, in order
            "pages": int,
            "page_ids": [...],         # Page UUIDs in order
            "annotated_pages": [       # Only pages that carry annotations
                {"page": int,          # 1-based page number
                 "page_id": str,
                 "has_handwriting": bool,   # page has pen strokes
                 "highlights": [str]}, # highlighted text on the page
            ],
            "ocr_backend": str,        # Which OCR backend was used (if any)
        }
    """
    cache_token = None
    if doc_id:
        with _cache_lock:
            cache_token = (
                _global_cache_generation,
                _cache_generation.get(doc_id, 0),
            )
            cached = _extraction_cache.get(doc_id)
            if cached and (cached["include_ocr"] or not include_ocr) and _is_cache_valid(cached):
                return cached["result"]

    result: Dict[str, Any] = {
        "typed_text": [],
        "highlights": [],
        "handwritten_text": None,
        "pages": 0,
        "page_ids": [],
        "annotated_pages": [],
        "ocr_backend": None,
        "tags": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir_path)

        # Get page order from .content file
        page_order = []
        for content_file in tmpdir_path.glob("*.content"):
            try:
                data = json.loads(content_file.read_text())
                # New format: cPages.pages array
                if "cPages" in data and "pages" in data["cPages"]:
                    page_order = [p["id"] for p in data["cPages"]["pages"]]
                # Fallback: pages array directly
                elif "pages" in data and isinstance(data["pages"], list):
                    page_order = data["pages"]
            except Exception:
                # Malformed .content file - continue without page order
                pass
            break  # Only process first .content file

        rm_files = list(tmpdir_path.glob("**/*.rm"))

        # If we have page order, sort rm_files accordingly
        if page_order:
            # Create mapping of page_id -> rm_file
            rm_by_id = {}
            for rm_file in rm_files:
                page_id = rm_file.stem  # filename without extension
                rm_by_id[page_id] = rm_file

            # Sort rm_files by page order
            ordered_rm_files = []
            for page_id in page_order:
                if page_id in rm_by_id:
                    ordered_rm_files.append(rm_by_id[page_id])
            # Add any remaining files not in page order
            for rm_file in rm_files:
                if rm_file not in ordered_rm_files:
                    ordered_rm_files.append(rm_file)
            rm_files = ordered_rm_files
            result["page_ids"] = page_order
        else:
            result["page_ids"] = [f.stem for f in rm_files]

        result["pages"] = len(page_order) or len(rm_files)

        # Extract typed text from .rm files using rmscene
        for rm_file in rm_files:
            text_lines = extract_text_from_rm_file(rm_file)
            result["typed_text"].extend(text_lines)

        # Extract per-page text highlights (GlyphRange items) and note-presence
        # from the .rm files, indexed by real page number. This powers "show only
        # the annotated pages / the highlighted text" without paging through the
        # whole document. (The legacy .highlights JSON scanned below only covers
        # older firmware exports.)
        page_pos = {pid: i for i, pid in enumerate(page_order)}
        for order_pos, rm_file in enumerate(rm_files):
            page_num = page_pos.get(rm_file.stem, order_pos) + 1
            page_highlights, has_strokes = _extract_page_annotations(rm_file)
            if page_highlights or has_strokes:
                result["annotated_pages"].append(
                    {
                        "page": page_num,
                        "page_id": rm_file.stem,
                        "has_handwriting": has_strokes,
                        "highlights": page_highlights,
                    }
                )
                result["highlights"].extend(page_highlights)
        result["annotated_pages"].sort(key=lambda a: a["page"])

        # Extract text from .txt and .md files
        for txt_file in tmpdir_path.glob("**/*.txt"):
            try:
                content = txt_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        for md_file in tmpdir_path.glob("**/*.md"):
            try:
                content = md_file.read_text(errors="ignore")
                if content.strip():
                    result["typed_text"].append(content)
            except Exception:
                # File read failed - skip this file and continue
                pass

        # Extract from .content files (metadata with text and tags)
        for content_file in tmpdir_path.glob("**/*.content"):
            try:
                data = json.loads(content_file.read_text())
                if "text" in data:
                    result["typed_text"].append(data["text"])
                if "tags" in data and data["tags"]:
                    result["tags"] = data["tags"]
            except Exception:
                # Malformed JSON or read error - skip this file
                pass

        # Extract PDF highlights
        for json_file in tmpdir_path.glob("**/*.json"):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and "highlights" in data:
                    for h in data.get("highlights", []):
                        if "text" in h and h["text"]:
                            result["highlights"].append(h["text"])
            except Exception:
                # Malformed JSON - skip this file
                pass

        # OCR for handwritten content (optional)
        if include_ocr and rm_files:
            ocr_result, ocr_backend = extract_handwriting_ocr(rm_files)
            result["handwritten_text"] = ocr_result
            result["ocr_backend"] = ocr_backend

    if doc_id and cache_token is not None:
        _cache_extraction_result_if_current(doc_id, result, include_ocr, cache_token)

    return result


def extract_handwriting_ocr(rm_files: List[Path]) -> tuple[Optional[List[str]], Optional[str]]:
    """
    Extract handwritten text using OCR.

    Supports multiple backends (set REMARKABLE_OCR_BACKEND env var):
    - "google": Google Cloud Vision - best for handwriting
    - "tesseract": pytesseract - basic OCR, rasterized locally with PyMuPDF
    - "auto" (default): Google if API key provided, else Tesseract

    Returns:
        Tuple of (ocr_results, backend_used) where backend_used is "google" or "tesseract"
    """
    backend = get_ocr_backend()

    # Auto-detect best available backend
    if backend == "auto":
        # Check for Google Vision API key first (simplest auth method)
        if os.environ.get("GOOGLE_VISION_API_KEY"):
            backend = "google"
        else:
            backend = "tesseract"

    if backend == "google":
        result = _ocr_google_vision(rm_files)
        if result:
            return (result, "google")
        result = _ocr_tesseract(rm_files)
        return (result, "tesseract")
    result = _ocr_tesseract(rm_files)
    return (result, "tesseract")


def _ocr_google_vision(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision API.
    Best quality for handwriting recognition.

    Supports two authentication methods:
    1. GOOGLE_VISION_API_KEY env var (simplest - just an API key)
    2. GOOGLE_APPLICATION_CREDENTIALS or default credentials (service account)
    """
    import os

    api_key = os.environ.get("GOOGLE_VISION_API_KEY")

    if api_key:
        # Use REST API with API key (simpler, no SDK needed)
        return _ocr_google_vision_rest(rm_files, api_key)
    else:
        # Use SDK with service account credentials
        return _ocr_google_vision_sdk(rm_files)


def _ocr_google_vision_rest(rm_files: List[Path], api_key: str) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision REST API with API key.
    """
    import base64
    import tempfile

    import requests

    ocr_results = []

    for rm_file in rm_files:
        tmp_svg_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                tmp_svg_path = Path(tmp_svg.name)

            # Convert .rm to SVG via the rmscene renderers
            if not _rm_to_svg(rm_file, tmp_svg_path):
                continue

            png = _svg_string_to_png(
                tmp_svg_path.read_text(),
                REMARKABLE_WIDTH,
                REMARKABLE_HEIGHT,
                "#FFFFFF",
            )
            if png is None:
                continue

            # Read and encode image
            image_content = base64.b64encode(png).decode("utf-8")

            # Call Google Vision REST API
            url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
            payload = {
                "requests": [
                    {
                        "image": {"content": image_content},
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            }

            response = requests.post(url, json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                if "responses" in data and data["responses"]:
                    resp = data["responses"][0]
                    if "fullTextAnnotation" in resp:
                        text = resp["fullTextAnnotation"]["text"]
                        if text.strip():
                            ocr_results.append(text.strip())
            elif response.status_code in (401, 403):
                # API key invalid or API not enabled - fall back to Tesseract
                return _ocr_tesseract(rm_files)

        except Exception:
            # API call or rendering failed - skip this page and continue
            pass
        finally:
            if tmp_svg_path:
                tmp_svg_path.unlink(missing_ok=True)

    return ocr_results if ocr_results else None


def _ocr_google_vision_sdk(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Google Cloud Vision SDK with service account credentials.
    """
    try:
        import tempfile

        from google.cloud import vision

        client = vision.ImageAnnotatorClient()
        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)

                # Convert .rm to SVG via the rmscene renderers
                if not _rm_to_svg(rm_file, tmp_svg_path):
                    continue

                png = _svg_string_to_png(
                    tmp_svg_path.read_text(),
                    REMARKABLE_WIDTH,
                    REMARKABLE_HEIGHT,
                    "#FFFFFF",
                )
                if png is None:
                    continue

                # Send to Google Vision API
                image = vision.Image(content=png)

                # Use DOCUMENT_TEXT_DETECTION for best handwriting results
                response = client.document_text_detection(image=image)

                if response.error.message:
                    continue

                if response.full_text_annotation.text:
                    ocr_results.append(response.full_text_annotation.text.strip())

            except Exception:
                # Rendering or API error - skip this page and continue
                pass
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # google-cloud-vision not installed, fall back to tesseract
        return _ocr_tesseract(rm_files)
    except Exception:
        # API error, fall back to tesseract
        return _ocr_tesseract(rm_files)


def _ocr_tesseract(rm_files: List[Path]) -> Optional[List[str]]:
    """
    OCR using Tesseract.
    Basic quality - designed for printed text, not handwriting.

    Requires: pytesseract and PyMuPDF.
    """
    try:
        import io
        import tempfile

        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        ocr_results = []

        for rm_file in rm_files:
            tmp_svg_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
                    tmp_svg_path = Path(tmp_svg.name)

                # Convert .rm to SVG via the rmscene renderers
                if not _rm_to_svg(rm_file, tmp_svg_path):
                    continue

                # Convert SVG to PNG with higher resolution for better OCR
                png = _svg_string_to_png(
                    tmp_svg_path.read_text(),
                    2106,
                    2808,
                    "#FFFFFF",
                )
                if png is None:
                    continue

                # Preprocess image for better OCR
                img = Image.open(io.BytesIO(png))

                # Convert to grayscale
                img = img.convert("L")

                # Increase contrast
                img = ImageOps.autocontrast(img, cutoff=2)

                # Slight sharpening
                img = img.filter(ImageFilter.SHARPEN)

                # Run OCR with optimized settings for sparse handwriting
                # PSM 11 = Sparse text - find as much text as possible
                # PSM 6 = Uniform block of text (alternative)
                custom_config = r"--psm 11 --oem 3"
                text = pytesseract.image_to_string(img, config=custom_config)

                if text.strip():
                    ocr_results.append(text.strip())

            except Exception:
                # Rendering or OCR error - skip this page and continue
                pass
            finally:
                if tmp_svg_path:
                    tmp_svg_path.unlink(missing_ok=True)

        return ocr_results if ocr_results else None

    except ImportError:
        # OCR dependencies not installed
        return None
