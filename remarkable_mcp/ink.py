"""Ink colour, tool, width and page geometry for reMarkable scenes.

Every renderer (SVG, the cropped / full-page / merged PNGs, the PDF export) and
the stroke-metadata tool resolve a stroke's appearance through
:func:`style_for`, so all output paths agree on colour, width, opacity and
blending. Colours come from :data:`PEN_PALETTE` or from the ``color_rgba`` value
newer firmware stores per stroke; anything else renders in the visible
:data:`FALLBACK_HEX` and is logged, never silently as black.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def _tolerate_unknown_enum_values() -> None:
    """Keep strokes whose tool or colour id is newer than rmscene's enums.

    rmscene converts ids with ``Pen(...)`` / ``PenColor(...)``; an unknown id
    raises, and rmscene turns the whole Line or GlyphRange into an
    ``UnreadableBlock`` that every renderer then skips without a trace. A
    ``_missing_`` hook returns a pseudo-member that keeps the raw int, so the
    stroke survives and renders in :data:`FALLBACK_HEX`. The write path
    (``strokes.resolve_tool`` / ``resolve_color``) checks enum membership
    explicitly, so it still rejects unknown ids.
    """
    try:
        from rmscene.scene_items import Pen, PenColor
    except ImportError:  # pragma: no cover - rmscene is a hard dependency
        return

    def _missing_(cls, value):
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        member = int.__new__(cls, value)
        member._name_ = f"UNKNOWN_{value}"
        member._value_ = value
        return member

    for enum_cls in (Pen, PenColor):
        if not getattr(enum_cls, "_remarkable_mcp_tolerant", False):
            enum_cls._missing_ = classmethod(_missing_)
            enum_cls._remarkable_mcp_tolerant = True


_tolerate_unknown_enum_values()


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

#: Visible colour for any colour value the palette cannot resolve.
FALLBACK_HEX = "#FF00FF"
FALLBACK_NAME = "unknown"

#: ``PenColor`` id -> (name, hex). Values are rmc's ``RM_PALETTE``
#: (github.com/ricklupton/rmc, ``exporters/writing_tools.py``). rmc leaves out
#: ``HIGHLIGHT`` (9) on purpose: firmware 3.14+ stores the real colour of those
#: strokes and text highlights in ``color_rgba``.
PEN_PALETTE: Dict[int, Tuple[str, str]] = {
    0: ("black", "#000000"),
    1: ("gray", "#909090"),
    2: ("white", "#FFFFFF"),
    3: ("yellow", "#FBF719"),
    4: ("green", "#00FF00"),
    5: ("pink", "#FFC0CB"),
    6: ("blue", "#4E69C9"),
    7: ("red", "#B33E39"),
    8: ("gray_overlap", "#7D7D7D"),
    10: ("green_2", "#A1D87D"),
    11: ("cyan", "#8BD0E5"),
    12: ("magenta", "#B782CD"),
    13: ("yellow_2", "#F7E851"),
}

#: Names for the colours firmware writes into ``color_rgba``, as found in device
#: .rm files (highlighter at alpha 255, shader at alpha 64-128). Names follow
#: rmscene PR #44.
RGBA_NAMES: Dict[str, str] = {
    "#FFED75": "yellow",
    "#ACFF85": "green",
    "#BEEAFE": "blue",
    "#F29EFF": "pink",
    "#FFC38C": "orange",
    "#C7C7C6": "gray",
    "#211E1C": "gray",
    "#FEB200": "orange",
    "#C07FD2": "magenta",
    "#304AE0": "blue",
    "#C23132": "red",
    "#91DA71": "green",
    "#FAE719": "yellow",
    "#74D2E8": "cyan",
}

#: Text highlights from older firmware carry an enum colour plus this
#: ``color_rgba``; it is a placeholder, not the colour.
PLACEHOLDER_RGBA = (0, 0, 0, 255)

#: ``Pen`` id -> tool family name.
TOOL_NAMES: Dict[int, str] = {
    0: "paintbrush",
    12: "paintbrush",
    1: "pencil",
    14: "pencil",
    2: "ballpoint",
    15: "ballpoint",
    3: "marker",
    16: "marker",
    4: "fineliner",
    17: "fineliner",
    5: "highlighter",
    18: "highlighter",
    6: "eraser",
    8: "eraser_area",
    7: "mechanical_pencil",
    13: "mechanical_pencil",
    21: "calligraphy",
    23: "shader",
}
HIGHLIGHT_TOOLS = frozenset({"highlighter", "shader"})
ERASER_TOOLS = frozenset({"eraser", "eraser_area"})
#: Tool name used for text highlights (GlyphRange items).
TEXT_HIGHLIGHT = "text_highlight"

#: Opacity of a highlighter, shader or text highlight drawn in a saturated
#: palette colour (rmc uses 0.3). The pastel colours firmware 3.14+ writes into
#: ``color_rgba`` are already the on-screen colour and use their own alpha.
#: Highlights always blend with multiply, so ink and text underneath stay dark.
HIGHLIGHT_PALETTE_OPACITY = 0.35
#: rmc's mechanical-pencil opacity.
MECHANICAL_PENCIL_OPACITY = 0.7
#: Pencil opacity never drops below this, so light strokes stay visible.
MIN_PENCIL_OPACITY = 0.25

#: ``Point.width`` is stored in quarter units and already includes
#: ``thickness_scale`` (a fineliner is 8x its thickness): rendered width in
#: stroke units is ``Point.width / 4``.
POINT_WIDTH_DIVISOR = 4.0
#: ``Point.width`` per unit of ``thickness_scale`` for fixed-width tools,
#: measured on device files. Only used when a stroke has no point widths.
_WIDTH_PER_THICKNESS: Dict[str, float] = {
    "fineliner": 8.0,
    "ballpoint": 6.0,
    "highlighter": 120.0,
    "mechanical_pencil": 12.0,
    "shader": 42.0,
    "eraser": 48.0,
    "eraser_area": 12.0,
}
_DEFAULT_WIDTH_PER_THICKNESS = 8.0
MIN_STROKE_WIDTH = 0.5

_warned_colors: set = set()


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    raw = getattr(value, "value", value)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def tool_name(tool_raw: int) -> str:
    """Family name of a ``Pen`` id, e.g. ``fineliner`` for 4 and 17."""
    return TOOL_NAMES.get(tool_raw, f"unknown_{tool_raw}")


def _hex(rgb: Sequence[int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(c))) for c in rgb[:3]))


def _nearest_name(rgb: Sequence[int]) -> str:
    """Name of the closest known colour, for rgba values without a name."""
    candidates = [(name, hx) for name, hx in PEN_PALETTE.values()]
    candidates += [(name, hx) for hx, name in RGBA_NAMES.items()]

    def distance(hx: str) -> int:
        other = (int(hx[1:3], 16), int(hx[3:5], 16), int(hx[5:7], 16))
        return sum((a - b) ** 2 for a, b in zip(rgb[:3], other))

    return min(candidates, key=lambda c: distance(c[1]))[0]


@dataclass(frozen=True)
class ResolvedColor:
    name: str
    hex: str
    raw: int
    rgba: Optional[Tuple[int, int, int, int]]
    source: str  # "palette", "rgba" or "fallback"


def resolve_color(color_raw: int, color_rgba: Any = None) -> ResolvedColor:
    """Resolve a colour id plus optional ``color_rgba`` to a name and hex.

    ``color_rgba`` wins when present (it is how firmware 3.14+ stores
    highlighter, shader and text-highlight colours), except for the
    ``(0, 0, 0, 255)`` placeholder older text highlights carry next to a real
    enum colour. Otherwise the id is looked up in :data:`PEN_PALETTE`. A value
    that resolves to neither renders as :data:`FALLBACK_HEX` and is logged once.
    """
    rgba: Optional[Tuple[int, int, int, int]] = None
    if color_rgba is not None:
        try:
            values = tuple(int(c) for c in color_rgba)
            if len(values) == 4:
                rgba = values  # type: ignore[assignment]
        except (TypeError, ValueError):
            rgba = None

    if rgba is not None and not (rgba == PLACEHOLDER_RGBA and color_raw != 0):
        hx = _hex(rgba)
        return ResolvedColor(RGBA_NAMES.get(hx) or _nearest_name(rgba), hx, color_raw, rgba, "rgba")
    if color_raw in PEN_PALETTE:
        name, hx = PEN_PALETTE[color_raw]
        return ResolvedColor(name, hx, color_raw, rgba, "palette")

    key = (color_raw, rgba)
    if key not in _warned_colors:
        _warned_colors.add(key)
        logger.warning(
            "No palette entry for reMarkable colour id %s (color_rgba=%s); rendering it as %s.",
            color_raw,
            rgba,
            FALLBACK_HEX,
        )
    return ResolvedColor(FALLBACK_NAME, FALLBACK_HEX, color_raw, rgba, "fallback")


# --------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InkStyle:
    """How one stroke or text highlight is drawn, in every output format."""

    tool: str
    tool_raw: int
    color: ResolvedColor
    width: float  # stroke units (1/226 inch at 100% zoom)
    opacity: float
    blend: str  # "normal" or "multiply"
    linecap: str

    @property
    def is_highlight(self) -> bool:
        return self.blend == "multiply"

    @property
    def is_eraser(self) -> bool:
        return self.tool in ERASER_TOOLS


def _highlight_opacity(color: ResolvedColor) -> float:
    if color.source == "rgba" and color.rgba is not None:
        return max(0.05, min(1.0, color.rgba[3] / 255.0))
    return HIGHLIGHT_PALETTE_OPACITY


def _pencil_opacity(pressures: Sequence[int], speeds: Sequence[int]) -> float:
    """rmc's pencil opacity, averaged over the stroke."""
    if not pressures:
        return 1.0
    values = []
    for i, pressure in enumerate(pressures):
        speed = speeds[i] if i < len(speeds) else 0
        raw = (0.1 * -((speed / 4) / 35)) + (pressure / 255)
        values.append(max(0.0, min(1.0, raw)) - 0.1)
    return max(MIN_PENCIL_OPACITY, min(1.0, sum(values) / len(values)))


def style_for(
    tool_raw: int,
    color_raw: int,
    color_rgba: Any = None,
    *,
    point_widths: Sequence[float] = (),
    thickness_scale: Optional[float] = None,
    pressures: Sequence[int] = (),
    speeds: Sequence[int] = (),
    width_divisor: float = POINT_WIDTH_DIVISOR,
) -> InkStyle:
    """Resolve the style of a stroke from its raw fields.

    Width is the median ``Point.width / width_divisor`` (``Point.width`` already
    includes ``thickness_scale``); without point widths it falls back to the
    tool's measured width per thickness unit. Highlighters and shaders blend
    with multiply; the mechanical pencil and pencil are translucent as in rmc.
    """
    name = tool_name(tool_raw)
    color = resolve_color(color_raw, color_rgba)

    widths = [float(w) for w in point_widths if w and float(w) > 0]
    if widths:
        width = statistics.median(widths) / width_divisor
    else:
        scale = thickness_scale if thickness_scale and thickness_scale > 0 else 1.0
        per_thickness = _WIDTH_PER_THICKNESS.get(name, _DEFAULT_WIDTH_PER_THICKNESS)
        width = scale * per_thickness / POINT_WIDTH_DIVISOR
    width = max(MIN_STROKE_WIDTH, width)

    if name in HIGHLIGHT_TOOLS:
        opacity, blend = _highlight_opacity(color), "multiply"
    elif name == "mechanical_pencil":
        opacity, blend = MECHANICAL_PENCIL_OPACITY, "normal"
    elif name == "pencil":
        opacity, blend = _pencil_opacity(pressures, speeds), "normal"
    else:
        opacity, blend = 1.0, "normal"

    linecap = "square" if name in ("highlighter", "eraser", "eraser_area") else "round"
    return InkStyle(name, tool_raw, color, width, opacity, blend, linecap)


def stroke_style(line: Any) -> InkStyle:
    """Style of an rmscene ``Line``."""
    points = getattr(line, "points", None) or []
    return style_for(
        _as_int(getattr(line, "tool", None), -1),
        _as_int(getattr(line, "color", None), 0),
        getattr(line, "color_rgba", None),
        point_widths=[getattr(p, "width", 0) for p in points],
        thickness_scale=getattr(line, "thickness_scale", None),
        pressures=[getattr(p, "pressure", 0) for p in points],
        speeds=[getattr(p, "speed", 0) for p in points],
    )


def glyph_style(glyph: Any) -> InkStyle:
    """Style of an rmscene ``GlyphRange`` (a highlight on PDF text)."""
    color = resolve_color(
        _as_int(getattr(glyph, "color", None), 9), getattr(glyph, "color_rgba", None)
    )
    return InkStyle(TEXT_HIGHLIGHT, -1, color, 0.0, _highlight_opacity(color), "multiply", "butt")


# --------------------------------------------------------------------------
# Scene items
# --------------------------------------------------------------------------

_ROOT_IDS = ((0, 0), (0, 1))


def _crdt_key(value: Any) -> Any:
    """Hashable key for a CrdtId (or anything else)."""
    if value is None:
        return None
    part1 = getattr(value, "part1", None)
    part2 = getattr(value, "part2", None)
    if part1 is not None and part2 is not None:
        return (int(part1), int(part2))
    return value


@dataclass
class LayerInfo:
    key: Any
    label: str
    visible: bool


@dataclass
class InkItem:
    """One stroke or text highlight, in page stroke coordinates."""

    kind: str  # "stroke" or "glyph"
    style: InkStyle
    order: int
    layer: Optional[LayerInfo] = None
    points: List[Tuple[float, float]] = field(default_factory=list)
    rects: List[Tuple[float, float, float, float]] = field(default_factory=list)
    thickness_scale: Optional[float] = None
    text: str = ""
    start: Optional[int] = None
    length: Optional[int] = None

    @property
    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Geometry bounds ``(x0, y0, x1, y1)``, ignoring stroke width."""
        if self.kind == "glyph":
            if not self.rects:
                return None
            return (
                min(r[0] for r in self.rects),
                min(r[1] for r in self.rects),
                max(r[0] + r[2] for r in self.rects),
                max(r[1] + r[3] for r in self.rects),
            )
        if not self.points:
            return None
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def ink_bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Bounds including half the stroke width."""
        box = self.bbox
        if box is None:
            return None
        half = self.style.width / 2.0
        return box[0] - half, box[1] - half, box[2] + half, box[3] + half

    def coords(self) -> List[Tuple[float, float]]:
        """Extent coordinates in the format the SVG crop expects."""
        if self.kind == "glyph":
            out: List[Tuple[float, float]] = []
            for x, y, w, h in self.rects:
                out.append((x, y))
                out.append((x + w, y + h))
            return out
        return list(self.points)


def _layer_map(blocks: Iterable[Any]) -> Dict[Any, LayerInfo]:
    """Map every group node to the top-level layer it belongs to."""
    parents: Dict[Any, Any] = {}
    labels: Dict[Any, str] = {}
    visible: Dict[Any, bool] = {}
    seen_order: List[Any] = []  # nodes in block order, so layer numbering is stable
    for block in blocks:
        tree_id = getattr(block, "tree_id", None)
        if tree_id is not None and hasattr(block, "parent_id"):
            node = _crdt_key(tree_id)
            parents[node] = _crdt_key(block.parent_id)
            seen_order.append(node)
        group = getattr(block, "group", None)
        if group is not None:
            key = _crdt_key(getattr(group, "node_id", None))
            label = getattr(getattr(group, "label", None), "value", "") or ""
            labels[key] = str(label)
            vis = getattr(getattr(group, "visible", None), "value", True)
            visible[key] = bool(vis) if vis is not None else True
            seen_order.append(key)

    layers: Dict[Any, LayerInfo] = {}
    numbered: Dict[Any, int] = {}

    def top_layer(node: Any) -> Any:
        seen = set()
        current = node
        while current is not None and current not in seen:
            seen.add(current)
            parent = parents.get(current)
            if parent is None or parent in _ROOT_IDS:
                return current
            current = parent
        return node

    for node in dict.fromkeys(seen_order):
        top = top_layer(node)
        if top not in numbered:
            numbered[top] = len(numbered) + 1
        hidden = False
        cursor, seen = node, set()
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            if visible.get(cursor) is False:
                hidden = True
            cursor = parents.get(cursor)
        layers[node] = LayerInfo(
            key=top,
            label=labels.get(top) or f"Layer {numbered[top]}",
            visible=not hidden,
        )
    return layers


def scene_items(
    blocks: Sequence[Any],
    group_offsets: Optional[Dict[Any, Tuple[float, float]]] = None,
    *,
    include_erasers: bool = False,
) -> List[InkItem]:
    """Strokes and text highlights of a v6 page, in file order.

    ``group_offsets`` (keyed by the block's ``parent_id``) translates ink that is
    anchored to typed text, exactly as the renderer does.
    """
    offsets = group_offsets or {}
    layers = _layer_map(blocks)
    items: List[InkItem] = []
    for order, block in enumerate(blocks):
        item = getattr(block, "item", None)
        value = getattr(item, "value", None)
        if value is None:
            continue
        parent = getattr(block, "parent_id", None)
        dx, dy = offsets.get(parent, (0.0, 0.0))
        layer = layers.get(_crdt_key(parent))

        rects = getattr(value, "rectangles", None)
        if rects:
            items.append(
                InkItem(
                    kind="glyph",
                    style=glyph_style(value),
                    order=order,
                    layer=layer,
                    rects=[(r.x + dx, r.y + dy, r.w, r.h) for r in rects],
                    text=str(getattr(value, "text", "") or ""),
                    start=getattr(value, "start", None),
                    length=getattr(value, "length", None),
                )
            )
            continue

        points = getattr(value, "points", None)
        if not points:
            continue
        style = stroke_style(value)
        if style.is_eraser and not include_erasers:
            continue
        items.append(
            InkItem(
                kind="stroke",
                style=style,
                order=order,
                layer=layer,
                points=[(p.x + dx, p.y + dy) for p in points],
                thickness_scale=getattr(value, "thickness_scale", None),
            )
        )
    return items


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def highlight_overlay(style: InkStyle) -> Tuple[str, float]:
    """Paint and opacity that draw a multiply highlight with normal blending.

    On white paper a highlight of colour C at opacity a multiplies to
    ``T = 255 - a * (255 - C)`` per channel. This returns the most transparent
    colour that lands exactly on T over white: ``alpha = 1 - min(T) / 255`` and
    ``C' = (T - (1 - alpha) * 255) / alpha``. Renderers that honour
    ``mix-blend-mode`` still multiply it; renderers that do not (PyMuPDF, or a
    PNG with a transparent background laid over a page) show the device colour
    on paper while text and ink underneath stay visible instead of covered.
    """
    rgb = [int(style.color.hex[i : i + 2], 16) for i in (1, 3, 5)]
    target = [255.0 - style.opacity * (255.0 - c) for c in rgb]
    alpha = 1.0 - min(target) / 255.0
    if alpha <= 0.001:
        return style.color.hex, 0.0
    paint = [(t - (1.0 - alpha) * 255.0) / alpha for t in target]
    return _hex([round(v) for v in paint]), alpha


def svg_elements(item: InkItem) -> List[str]:
    """SVG elements for one item: one ``<path>`` per stroke, one ``<rect>`` per
    highlight rectangle.

    Paint and opacity are presentation attributes (the PyMuPDF rasteriser
    ignores them inside ``style``). Highlights use :func:`highlight_overlay` plus
    a ``mix-blend-mode:multiply`` style for browsers; PNG output multiplies them
    explicitly. ``data-tool``, ``data-color`` and ``data-color-hex`` carry the
    tool and the colour as the tablet stored it.
    """
    style = item.style
    if style.is_highlight:
        paint, opacity_value = highlight_overlay(style)
        blend = ' style="mix-blend-mode:multiply"'
    else:
        paint, opacity_value = style.color.hex, style.opacity
        blend = ""
    opacity = f' opacity="{_fmt(opacity_value)}"' if opacity_value < 1.0 else ""
    data = (
        f' data-tool="{style.tool}" data-color="{style.color.name}"'
        f' data-color-hex="{style.color.hex}"'
    )
    if item.kind == "glyph":
        return [
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{paint}"{opacity} stroke="none"{blend}{data}/>'
            for x, y, w, h in item.rects
        ]
    pts = item.points
    d = f"M {pts[0][0]:.1f} {pts[0][1]:.1f}"
    tail = pts[1:] or pts[:1]  # a single-point dot still needs a segment to draw
    d += "".join(f" L {x:.1f} {y:.1f}" for x, y in tail)
    return [
        f'<path d="{d}" stroke="{paint}" stroke-width="{_fmt(style.width)}" '
        f'fill="none" stroke-linecap="{style.linecap}" stroke-linejoin="round"'
        f"{opacity}{blend}{data}/>"
    ]


# --------------------------------------------------------------------------
# Page geometry and coordinate mapping
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PageGeometry:
    """Where a page sits in stroke space and in PDF points.

    Stroke coordinates are centre-origin in X and top-origin in Y. PDF points
    are top-left origin: ``pdf_x = x * ppu + width_pt / 2`` and
    ``pdf_y = y * ppu``. For a PDF-backed page ``width_pt``/``height_pt`` are
    the PDF page (cropbox, rotation applied); for a notebook they are the
    paper grid at the device's export scale.
    """

    kind: str  # "pdf" or "notebook"
    width_pt: float
    height_pt: float
    points_per_unit: float
    pdf_page_index: Optional[int] = None

    @property
    def viewbox(self) -> Tuple[float, float, float, float]:
        width = self.width_pt / self.points_per_unit
        height = self.height_pt / self.points_per_unit
        return -width / 2.0, 0.0, width, height

    def to_pdf(self, x: float, y: float) -> Tuple[float, float]:
        return (
            x * self.points_per_unit + self.width_pt / 2.0,
            y * self.points_per_unit,
        )

    def box_to_pdf(self, box: Tuple[float, float, float, float]) -> Tuple[float, ...]:
        x0, y0 = self.to_pdf(box[0], box[1])
        x1, y1 = self.to_pdf(box[2], box[3])
        return x0, y0, x1, y1

    def contains(self, box_pdf: Tuple[float, ...]) -> bool:
        return (
            box_pdf[0] >= -0.5
            and box_pdf[1] >= -0.5
            and box_pdf[2] <= self.width_pt + 0.5
            and box_pdf[3] <= self.height_pt + 0.5
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "width_pt": round(self.width_pt, 2),
            "height_pt": round(self.height_pt, 2),
            "points_per_unit": self.points_per_unit,
            "pdf_page_index": self.pdf_page_index,
            "rm_viewbox": [round(v, 2) for v in self.viewbox],
            "coordinate_note": (
                "PDF points, origin top-left. pdf_x = rm_x * points_per_unit + "
                "width_pt / 2; pdf_y = rm_y * points_per_unit."
            ),
        }


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def _round_box(box: Sequence[float]) -> List[float]:
    return [round(float(v), 2) for v in box]


#: Default page size of the per-stroke list, and the cap on clusters, so one
#: dense page stays a few tens of KB of JSON.
DEFAULT_MAX_STROKES = 200
MAX_CLUSTERS = 150


def _color_fields(color: ResolvedColor) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "color_name": color.name,
        "color_hex": color.hex,
        "color_raw": color.raw,
    }
    if color.rgba is not None:
        fields["color_rgba"] = list(color.rgba)
    return fields


def _layer_fields(item: InkItem) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"layer": item.layer.label if item.layer else None}
    if item.layer is not None and not item.layer.visible:
        fields["layer_hidden"] = True
    return fields


def stroke_record(index: int, item: InkItem, geometry: PageGeometry) -> Dict[str, Any]:
    box = item.bbox or (0.0, 0.0, 0.0, 0.0)
    box_pdf = geometry.box_to_pdf(box)
    record: Dict[str, Any] = {
        "index": index,
        **_layer_fields(item),
        "tool": item.style.tool,
        **_color_fields(item.style.color),
        "thickness_scale": (
            round(float(item.thickness_scale), 4) if item.thickness_scale is not None else None
        ),
        "width_pt": round(item.style.width * geometry.points_per_unit, 2),
        "point_count": len(item.points),
        "bbox_pdf": _round_box(box_pdf),
        "bbox_rm": _round_box(box),
    }
    if not geometry.contains(box_pdf):
        record["outside_page"] = True
    return record


def glyph_record(index: int, item: InkItem, geometry: PageGeometry) -> Dict[str, Any]:
    corners = [(x, y, x + w, y + h) for x, y, w, h in item.rects]
    return {
        "index": index,
        **_layer_fields(item),
        **_color_fields(item.style.color),
        "text": item.text,
        "rects_pdf": [_round_box(geometry.box_to_pdf(c)) for c in corners],
        "rects_rm": [_round_box(c) for c in corners],
    }


def cluster_strokes(
    items: Sequence[InkItem], gap_rm: float
) -> List[Tuple[Tuple[str, str], List[int]]]:
    """Group strokes that share tool and colour and lie within ``gap_rm``.

    Returns ``[((tool, color_hex), [item indexes])]`` in reading order. Two
    strokes join when their ink boxes, each grown by ``gap_rm / 2``, touch.
    """
    by_key: Dict[Tuple[str, str], List[int]] = {}
    for idx, item in enumerate(items):
        if item.kind != "stroke" or item.ink_bbox is None:
            continue
        by_key.setdefault((item.style.tool, item.style.color.hex), []).append(idx)

    clusters: List[Tuple[Tuple[str, str], List[int]]] = []
    half = gap_rm / 2.0
    for key, members in by_key.items():
        boxes = {}
        for i in members:
            b = items[i].ink_bbox
            boxes[i] = (b[0] - half, b[1] - half, b[2] + half, b[3] + half)
        parent = {i: i for i in members}

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        ordered = sorted(members, key=lambda i: boxes[i][0])
        for pos, i in enumerate(ordered):
            bi = boxes[i]
            for j in ordered[pos + 1 :]:
                bj = boxes[j]
                if bj[0] > bi[2]:
                    break
                if bj[1] <= bi[3] and bi[1] <= bj[3]:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
        groups: Dict[int, List[int]] = {}
        for i in members:
            groups.setdefault(find(i), []).append(i)
        clusters.extend((key, sorted(g)) for g in groups.values())

    def reading_order(entry: Tuple[Tuple[str, str], List[int]]) -> Tuple[float, float]:
        box = union_box(items[i].ink_bbox for i in entry[1])
        return (box[1], box[0])

    clusters.sort(key=reading_order)
    return clusters


def union_box(boxes: Iterable[Optional[Tuple[float, float, float, float]]]) -> Tuple[float, ...]:
    real = [b for b in boxes if b is not None]
    return (
        min(b[0] for b in real),
        min(b[1] for b in real),
        max(b[2] for b in real),
        max(b[3] for b in real),
    )


def page_metadata(
    items: Sequence[InkItem],
    geometry: PageGeometry,
    *,
    include_strokes: bool = True,
    cluster_gap_pt: Optional[float] = 8.0,
    erasers: int = 0,
    stroke_offset: int = 0,
    max_strokes: int = DEFAULT_MAX_STROKES,
) -> Dict[str, Any]:
    """Structured ink metadata for one page (see ``remarkable_strokes``).

    The per-stroke list is paged (``stroke_offset``/``max_strokes``); stroke
    ``index`` values and cluster ``stroke_indexes`` always refer to the whole
    page. At most :data:`MAX_CLUSTERS` clusters are returned, in reading order.
    """
    stroke_items = [it for it in items if it.kind == "stroke"]
    glyph_items = [it for it in items if it.kind == "glyph"]

    counts: Dict[Tuple[str, str, str], int] = {}
    for it in stroke_items:
        key = (it.style.tool, it.style.color.name, it.style.color.hex)
        counts[key] = counts.get(key, 0) + 1
    glyph_counts: Dict[Tuple[str, str], int] = {}
    for it in glyph_items:
        key2 = (it.style.color.name, it.style.color.hex)
        glyph_counts[key2] = glyph_counts.get(key2, 0) + 1

    result: Dict[str, Any] = {
        "geometry": geometry.as_dict(),
        "summary": {
            "stroke_count": len(stroke_items),
            "glyph_highlight_count": len(glyph_items),
            "eraser_count": erasers,
            "strokes_by_tool_color": [
                {"tool": t, "color_name": n, "color_hex": h, "count": c}
                for (t, n, h), c in sorted(counts.items(), key=lambda kv: -kv[1])
            ],
            "glyph_highlights_by_color": [
                {"color_name": n, "color_hex": h, "count": c}
                for (n, h), c in sorted(glyph_counts.items(), key=lambda kv: -kv[1])
            ],
            "fallback_colors": sorted(
                {it.style.color.raw for it in items if it.style.color.source == "fallback"}
            ),
            "outside_page_count": 0,
        },
    }

    result["summary"]["outside_page_count"] = sum(
        1
        for it in stroke_items
        if it.bbox is not None and not geometry.contains(geometry.box_to_pdf(it.bbox))
    )
    hidden = sum(1 for it in stroke_items if it.layer is not None and not it.layer.visible)
    if hidden:
        result["summary"]["hidden_layer_stroke_count"] = hidden

    if include_strokes:
        start = max(0, int(stroke_offset))
        end = min(len(stroke_items), start + max(0, int(max_strokes)))
        result["strokes"] = [stroke_record(i, stroke_items[i], geometry) for i in range(start, end)]
        result["stroke_page"] = {
            "offset": start,
            "returned": max(0, end - start),
            "total": len(stroke_items),
            "next_offset": end if end < len(stroke_items) else None,
        }
    result["glyph_highlights"] = [glyph_record(i, it, geometry) for i, it in enumerate(glyph_items)]

    if cluster_gap_pt is not None and cluster_gap_pt >= 0 and stroke_items:
        gap_rm = cluster_gap_pt / geometry.points_per_unit
        grouped = cluster_strokes(stroke_items, gap_rm)
        clusters = []
        for cid, ((tool, _hex), members) in enumerate(grouped[:MAX_CLUSTERS]):
            box = union_box(stroke_items[i].ink_bbox for i in members)
            color = stroke_items[members[0]].style.color
            clusters.append(
                {
                    "cluster": cid,
                    "tool": tool,
                    "color_name": color.name,
                    "color_hex": color.hex,
                    "stroke_count": len(members),
                    "stroke_indexes": members,
                    "bbox_pdf": _round_box(geometry.box_to_pdf(box)),
                    "bbox_rm": _round_box(box),
                }
            )
        result["clusters"] = clusters
        result["summary"]["cluster_count"] = len(grouped)
        if len(grouped) > MAX_CLUSTERS:
            result["summary"]["clusters_truncated"] = True
    return result
