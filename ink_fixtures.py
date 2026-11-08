"""Synthetic reMarkable pages for the ink colour tests and the visual check.

``build_page`` writes a real v6 ``.rm`` file with rmscene: one layer holding
any mix of strokes (tool, colour, optional ``color_rgba``, point width,
thickness) and text highlights (GlyphRange). ``build_document_zip`` wraps pages
into a document archive, optionally with a source PDF. ``palette_chart_zip``
builds a labelled chart: a landscape PDF with one row per tool and one column
per colour, and the matching annotation layer, so a merged render can be
checked by eye.

Run ``uv run python ink_fixtures.py OUT_DIR`` to write the chart archive and
its renders.
"""

from __future__ import annotations

import io
import json
import math
import sys
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGlyphItemBlock,
    SceneGroupItemBlock,
    SceneInfo,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

ROOT_ID = CrdtId(0, 1)
LAYER_ID = CrdtId(0, 11)
POINTS_PER_UNIT = 0.3177  # stroke units -> PDF points on the 1404x1872 grid

#: Device-written highlighter colours (alpha 255) and shader colours.
HIGHLIGHTER_RGBA = {
    "yellow": (255, 237, 117, 255),
    "green": (172, 255, 133, 255),
    "blue": (190, 234, 254, 255),
    "pink": (242, 158, 255, 255),
    "orange": (255, 195, 140, 255),
    "gray": (199, 199, 198, 255),
}
SHADER_RGBA = {
    "gray": (33, 30, 28, 64),
    "orange": (254, 178, 0, 115),
    "magenta": (192, 127, 210, 128),
    "blue": (48, 74, 224, 77),
    "red": (194, 49, 50, 102),
    "green": (145, 218, 113, 128),
    "yellow": (250, 231, 25, 115),
    "cyan": (116, 210, 232, 102),
}
#: Pen tools (v2 ids) with a device-like point width and thickness_scale.
PEN_TOOLS = [
    ("ballpoint", si.Pen.BALLPOINT_2, 12, 2.0),
    ("fineliner", si.Pen.FINELINER_2, 16, 2.0),
    ("marker", si.Pen.MARKER_2, 24, 2.0),
    ("pencil", si.Pen.PENCIL_2, 20, 2.0),
    ("mechanical pencil", si.Pen.MECHANICAL_PENCIL_2, 24, 2.0),
    ("paintbrush", si.Pen.PAINTBRUSH_2, 30, 2.0),
    ("calligraphy", si.Pen.CALIGRAPHY, 18, 2.0),
]
#: Every palette colour a pen can carry (HIGHLIGHT, 9, is rgba-only).
PEN_COLORS = [c for c in si.PenColor if c != si.PenColor.HIGHLIGHT]
#: Older text-highlight colours: an enum colour plus a placeholder rgba.
LEGACY_HIGHLIGHT_COLORS = [
    si.PenColor.YELLOW,
    si.PenColor.GREEN,
    si.PenColor.PINK,
    si.PenColor.GRAY_OVERLAP,
]


@dataclass
class StrokeSpec:
    tool: int
    color: int
    points: Sequence[Tuple[float, float]]
    width: int = 16
    thickness_scale: float = 2.0
    color_rgba: Optional[Tuple[int, int, int, int]] = None
    pressure: int = 160


@dataclass
class GlyphSpec:
    color: int
    rects: Sequence[Tuple[float, float, float, float]]
    text: str = "highlighted text"
    color_rgba: Optional[Tuple[int, int, int, int]] = None
    start: int = 0


def wave(x0: float, y: float, length: float = 70.0, steps: int = 12) -> List[Tuple[float, float]]:
    """A short wavy horizontal stroke, like a scribble."""
    return [
        (x0 + length * i / steps, y + 8.0 * math.sin(i * math.pi / 3)) for i in range(steps + 1)
    ]


def build_page(
    strokes: Sequence[StrokeSpec] = (),
    glyphs: Sequence[GlyphSpec] = (),
    paper_size: Optional[Tuple[int, int]] = (1404, 1872),
    layer_label: str = "Layer 1",
) -> bytes:
    """A v6 ``.rm`` page with one layer holding the given strokes and highlights."""
    blocks: list = [
        AuthorIdsBlock(author_uuids={1: uuid.UUID(int=1)}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
    ]
    if paper_size is not None:
        blocks.append(
            SceneInfo(
                current_layer=LwwValue(CrdtId(0, 0), LAYER_ID),
                background_visible=LwwValue(CrdtId(0, 0), True),
                root_document_visible=LwwValue(CrdtId(0, 0), True),
                paper_size=paper_size,
            )
        )
    blocks += [
        SceneTreeBlock(tree_id=LAYER_ID, node_id=CrdtId(0, 0), is_update=True, parent_id=ROOT_ID),
        TreeNodeBlock(si.Group(node_id=ROOT_ID)),
        TreeNodeBlock(
            si.Group(node_id=LAYER_ID, label=LwwValue(timestamp=CrdtId(0, 12), value=layer_label))
        ),
        SceneGroupItemBlock(
            parent_id=ROOT_ID,
            item=CrdtSequenceItem(
                item_id=CrdtId(0, 13),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=LAYER_ID,
            ),
        ),
    ]

    counter = 100
    for glyph in glyphs:
        counter += 1
        blocks.append(
            SceneGlyphItemBlock(
                parent_id=LAYER_ID,
                item=CrdtSequenceItem(
                    item_id=CrdtId(1, counter),
                    left_id=CrdtId(0, 0),
                    right_id=CrdtId(0, 0),
                    deleted_length=0,
                    value=si.GlyphRange(
                        start=glyph.start,
                        length=len(glyph.text),
                        text=glyph.text,
                        color=si.PenColor(glyph.color),
                        rectangles=[si.Rectangle(*r) for r in glyph.rects],
                        color_rgba=glyph.color_rgba,
                    ),
                ),
            )
        )
    for stroke in strokes:
        counter += 1
        points = [
            si.Point(
                x=float(x),
                y=float(y),
                speed=0,
                direction=0,
                width=stroke.width,
                pressure=stroke.pressure,
            )
            for x, y in stroke.points
        ]
        blocks.append(
            SceneLineItemBlock(
                parent_id=LAYER_ID,
                item=CrdtSequenceItem(
                    item_id=CrdtId(1, counter),
                    left_id=CrdtId(0, 0),
                    right_id=CrdtId(0, 0),
                    deleted_length=0,
                    value=si.Line(
                        color=si.PenColor(stroke.color),
                        tool=si.Pen(stroke.tool),
                        points=points,
                        thickness_scale=stroke.thickness_scale,
                        starting_length=0.0,
                        color_rgba=stroke.color_rgba,
                    ),
                ),
            )
        )

    buf = io.BytesIO()
    write_blocks(buf, blocks, options={"version": "3.1"})
    return buf.getvalue()


def build_v5_page(
    strokes: Sequence[Tuple[int, int, Sequence[Tuple[float, float, float, float, float, float]]]],
) -> bytes:
    """A legacy v5 (.lines) page: one layer of ``(pen, color, segments)`` where
    each segment is ``(x, y, speed, tilt, width, pressure)`` as stored on disk."""
    import struct

    buf = bytearray(b"reMarkable .lines file, version=5          "[:43])
    buf += struct.pack("<I", 1)
    buf += struct.pack("<I", len(strokes))
    for pen, color, segments in strokes:
        buf += struct.pack("<IIIIfI", pen, color, 0, 0, 2.0, len(segments))
        for segment in segments:
            buf += struct.pack("<ffffff", *segment)
    return bytes(buf)


def build_document_zip(
    pages: Sequence[Optional[bytes]],
    *,
    pdf_bytes: Optional[bytes] = None,
    doc_id: str = "doc",
) -> bytes:
    """A document archive: ``.content`` (cPages), one ``.rm`` per page, optional PDF.

    With ``pdf_bytes`` every page maps to the PDF page of the same index.
    """
    page_ids = [f"p{i + 1}" for i in range(len(pages))]
    entries = []
    for i, pid in enumerate(page_ids):
        entry: dict = {"id": pid}
        if pdf_bytes is not None:
            entry["redir"] = {"value": i}
        entries.append(entry)
    content = {
        "fileType": "pdf" if pdf_bytes is not None else "notebook",
        "formatVersion": 2,
        "cPages": {"pages": entries},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{doc_id}.content", json.dumps(content))
        zf.writestr(f"{doc_id}.metadata", json.dumps({"visibleName": "Ink fixture"}))
        if pdf_bytes is not None:
            zf.writestr(f"{doc_id}.pdf", pdf_bytes)
        for pid, page in zip(page_ids, pages):
            if page is not None:
                zf.writestr(f"{doc_id}/{pid}.rm", page)
    return buf.getvalue()


def to_rm(x_pt: float, y_pt: float, page_width_pt: float) -> Tuple[float, float]:
    """PDF point (top-left origin) -> stroke coordinates on a PDF-backed page."""
    return (x_pt - page_width_pt / 2.0) / POINTS_PER_UNIT, y_pt / POINTS_PER_UNIT


# --------------------------------------------------------------------------
# Labelled palette chart (landscape A4)
# --------------------------------------------------------------------------

CHART_W, CHART_H = 842.0, 595.0
_LABEL_W = 96.0
_COL_W = 56.0
_ROW_H = 34.0
_TOP = 70.0


def _chart_rows() -> List[Tuple[str, List[Tuple[str, StrokeSpec]]]]:
    rows = []
    for label, tool, width, thickness in PEN_TOOLS:
        cells = [
            (
                color.name.lower(),
                StrokeSpec(int(tool), int(color), [], width=width, thickness_scale=thickness),
            )
            for color in PEN_COLORS
        ]
        rows.append((label, cells))
    rows.append(
        (
            "highlighter",
            [
                (f"{name} (rgba)", StrokeSpec(18, 9, [], 120, 1.0, rgba))
                for name, rgba in HIGHLIGHTER_RGBA.items()
            ]
            + [
                (f"{c.name.lower()} (enum)", StrokeSpec(18, int(c), [], 120, 1.0))
                for c in LEGACY_HIGHLIGHT_COLORS
            ],
        )
    )
    rows.append(
        (
            "shader",
            [
                (f"{name} (rgba)", StrokeSpec(23, 9, [], 84, 2.0, rgba))
                for name, rgba in SHADER_RGBA.items()
            ],
        )
    )
    return rows


def palette_chart_pdf() -> bytes:
    """The chart's paper: row labels, column headers and a line of text per
    highlight colour (to show highlights over printed text)."""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=CHART_W, height=CHART_H)
    page.insert_text((20, 30), "Ink fixture: one stroke per tool x colour", fontsize=14)
    page.insert_text(
        (20, 46),
        "Column header = colour written; row = tool. Highlights below are over black text.",
        fontsize=8,
    )
    rows = _chart_rows()
    headers = [c.name.lower() for c in PEN_COLORS]
    for col, name in enumerate(headers):
        page.insert_text((_LABEL_W + col * _COL_W + 2, _TOP - 6), name, fontsize=6.5)
    for r, (label, cells) in enumerate(rows):
        y = _TOP + r * _ROW_H
        page.insert_text((20, y + _ROW_H / 2 + 3), label, fontsize=8)
        if label in ("highlighter", "shader"):
            for col, (name, _) in enumerate(cells):
                page.insert_text((_LABEL_W + col * _COL_W + 2, y + 8), name, fontsize=5.5)
        page.draw_line((20, y), (CHART_W - 20, y), color=(0.85, 0.85, 0.85), width=0.5)

    text_top = _TOP + len(rows) * _ROW_H + 18
    for i, (name, _) in enumerate(_glyph_lines()):
        page.insert_text(
            (20, text_top + i * 14),
            f"Text highlighted in {name}: the quick brown fox jumps over the lazy dog",
            fontsize=10,
        )
    out = doc.tobytes()
    doc.close()
    return out


def _glyph_lines() -> List[Tuple[str, Tuple[int, Optional[Tuple[int, int, int, int]]]]]:
    lines = [(f"{n} (rgba)", (9, rgba)) for n, rgba in HIGHLIGHTER_RGBA.items()]
    lines += [
        (f"{c.name.lower()} (enum)", (int(c), (0, 0, 0, 255))) for c in LEGACY_HIGHLIGHT_COLORS
    ]
    return lines


def palette_chart_page() -> bytes:
    """The chart's annotation layer, aligned with :func:`palette_chart_pdf`."""
    strokes: List[StrokeSpec] = []
    for r, (_, cells) in enumerate(_chart_rows()):
        y_pt = _TOP + r * _ROW_H + _ROW_H / 2 + 4
        for col, (_, spec) in enumerate(cells):
            x0_pt = _LABEL_W + col * _COL_W + 6
            x0, y = to_rm(x0_pt, y_pt, CHART_W)
            spec.points = wave(x0, y, length=38 / POINTS_PER_UNIT)
            strokes.append(spec)

    glyphs = []
    text_top = _TOP + len(_chart_rows()) * _ROW_H + 18
    for i, (name, (color, rgba)) in enumerate(_glyph_lines()):
        baseline = text_top + i * 14
        x, y = to_rm(20 + 98, baseline - 9, CHART_W)
        glyphs.append(
            GlyphSpec(
                color=color,
                rects=[(x, y, 330 / POINTS_PER_UNIT, 12 / POINTS_PER_UNIT)],
                text="the quick brown fox jumps over the lazy dog",
                color_rgba=rgba,
            )
        )
    return build_page(strokes, glyphs)


def palette_chart_zip() -> bytes:
    return build_document_zip([palette_chart_page()], pdf_bytes=palette_chart_pdf())


def main(out_dir: str) -> None:
    """Write the chart archive plus merged / annotation-only / SVG renders."""
    import tempfile

    from remarkable_mcp.extract import (
        render_merged_page_from_document_zip,
        render_page_from_document_zip,
        render_page_from_document_zip_svg,
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    archive = palette_chart_zip()
    (out / "ink-chart.zip").write_bytes(archive)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(archive)
        path = Path(tmp.name)
    try:
        merged, note = render_merged_page_from_document_zip(path, 1)
        (out / "ink-chart-merged.png").write_bytes(merged)
        layer = render_page_from_document_zip(path, 1, background_color="#FFFFFF", full_page=None)
        (out / "ink-chart-annotations.png").write_bytes(layer)
        svg = render_page_from_document_zip_svg(path, 1, full_page=None)
        (out / "ink-chart.svg").write_text(svg)
    finally:
        path.unlink(missing_ok=True)
    print(f"wrote chart renders to {out} (merged note: {note})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ink-chart")
