"""Ink colour, tool, width and page geometry across every render and read path.

Fixtures are real v6 .rm pages written with rmscene (see ink_fixtures.py), so
these tests exercise the same parse -> style -> SVG/PNG/PDF path as device files.
"""

import json
import logging
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import fitz
import pytest
from PIL import Image
from rmscene import read_blocks
from rmscene import scene_items as si

import ink_fixtures as fx
import remarkable_mcp.tools  # noqa: F401  (registers the MCP tools)
from remarkable_mcp import ink, strokes
from remarkable_mcp.exporters import ExportMetadata, write_archive_pdf_export
from remarkable_mcp.extract import (
    _v6_blocks,
    _v6_paths_from_blocks,
    extract_page_ink_metadata,
    render_merged_page_from_document_zip,
    render_page_annotation_layer_from_extracted_document,
    render_page_from_document_zip,
    render_page_from_document_zip_svg,
    render_rm_file_to_png,
)
from remarkable_mcp.server import mcp

A4 = (595.276, 841.89)
PPU = fx.POINTS_PER_UNIT

#: rmc's RM_PALETTE, copied here independently so changing the palette has to
#: be a deliberate decision.
RMC_PALETTE = {
    0: "#000000",
    1: "#909090",
    2: "#FFFFFF",
    3: "#FBF719",
    4: "#00FF00",
    5: "#FFC0CB",
    6: "#4E69C9",
    7: "#B33E39",
    8: "#7D7D7D",
    10: "#A1D87D",
    11: "#8BD0E5",
    12: "#B782CD",
    13: "#F7E851",
}

FINELINER = int(si.Pen.FINELINER_2)
BALLPOINT = int(si.Pen.BALLPOINT_2)
HIGHLIGHTER = int(si.Pen.HIGHLIGHTER_2)
SHADER = int(si.Pen.SHADER)
ERASER = int(si.Pen.ERASER)
RED, BLUE, BLACK, HIGHLIGHT = 7, 6, 0, 9
GREEN_RGBA = fx.HIGHLIGHTER_RGBA["green"]


def _rgb(hex_color: str):
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _close(pixel, expected, tolerance=14):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], expected))


def _pdf(width=A4[0], height=A4[1], black_rects=()):
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    for rect in black_rects:
        page.draw_rect(fitz.Rect(*rect), color=(0, 0, 0), fill=(0, 0, 0))
    data = doc.tobytes()
    doc.close()
    return data


def _write(tmp_path: Path, data: bytes, name="doc.zip") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _stroke_pt(tool, color, x0_pt, x1_pt, y_pt, *, width=16, rgba=None, ts=2.0, page_w=A4[0]):
    """A straight stroke given in PDF points on a PDF-backed page."""
    a = fx.to_rm(x0_pt, y_pt, page_w)
    b = fx.to_rm(x1_pt, y_pt, page_w)
    return fx.StrokeSpec(tool, color, [a, b], width=width, thickness_scale=ts, color_rgba=rgba)


def _page_blocks(page_bytes: bytes, tmp_path: Path):
    path = tmp_path / "page.rm"
    path.write_bytes(page_bytes)
    return _v6_blocks(path), path


# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------


class TestPalette:
    def test_palette_is_rmc_palette(self):
        for color_id, hex_color in RMC_PALETTE.items():
            resolved = ink.resolve_color(color_id)
            assert (resolved.hex, resolved.source) == (hex_color, "palette"), color_id

    def test_every_pen_colour_except_highlight_has_a_palette_entry(self):
        missing = {int(c) for c in si.PenColor} - set(ink.PEN_PALETTE)
        assert missing == {int(si.PenColor.HIGHLIGHT)}

    def test_color_rgba_wins_for_highlight(self):
        resolved = ink.resolve_color(HIGHLIGHT, (255, 237, 117, 255))
        assert (resolved.hex, resolved.name, resolved.source) == ("#FFED75", "yellow", "rgba")
        assert resolved.rgba == (255, 237, 117, 255)

    def test_unnamed_rgba_gets_nearest_known_name(self):
        resolved = ink.resolve_color(3, (254, 253, 96, 255))
        assert resolved.hex == "#FEFD60"
        assert resolved.name.startswith("yellow")

    def test_placeholder_rgba_next_to_enum_colour_is_ignored(self):
        assert ink.resolve_color(4, (0, 0, 0, 255)).hex == "#00FF00"
        assert ink.resolve_color(4, (0, 0, 0, 255)).source == "palette"
        assert ink.resolve_color(BLACK, (0, 0, 0, 255)).hex == "#000000"

    def test_unknown_colour_renders_magenta_and_is_logged(self, caplog, monkeypatch):
        monkeypatch.setattr(ink, "_warned_colors", set())
        with caplog.at_level(logging.WARNING, logger="remarkable_mcp.ink"):
            resolved = ink.resolve_color(42)
        assert (resolved.hex, resolved.source) == (ink.FALLBACK_HEX, "fallback")
        assert "42" in caplog.text

    def test_highlight_without_rgba_is_a_visible_fallback(self, monkeypatch):
        monkeypatch.setattr(ink, "_warned_colors", set())
        assert ink.resolve_color(HIGHLIGHT).hex == ink.FALLBACK_HEX


# --------------------------------------------------------------------------
# Style: width, opacity, blend
# --------------------------------------------------------------------------


class TestStyle:
    def test_width_is_point_width_over_four(self):
        assert ink.style_for(FINELINER, RED, point_widths=[16] * 5).width == 4.0
        assert ink.style_for(HIGHLIGHTER, 3, point_widths=[120] * 5).width == 30.0

    def test_width_falls_back_to_tool_and_thickness(self):
        assert ink.style_for(FINELINER, RED, thickness_scale=2.0).width == 4.0
        assert ink.style_for(HIGHLIGHTER, 3, thickness_scale=1.0).width == 30.0
        assert ink.style_for(FINELINER, RED, point_widths=[1]).width == ink.MIN_STROKE_WIDTH

    def test_highlighter_with_device_colour_is_full_strength_multiply(self):
        style = ink.style_for(HIGHLIGHTER, HIGHLIGHT, GREEN_RGBA, point_widths=[120])
        assert (style.opacity, style.blend, style.color.hex) == (1.0, "multiply", "#ACFF85")

    def test_highlighter_with_palette_colour_is_translucent_multiply(self):
        style = ink.style_for(HIGHLIGHTER, 3, point_widths=[120])
        assert (style.opacity, style.blend) == (ink.HIGHLIGHT_PALETTE_OPACITY, "multiply")

    def test_shader_uses_its_alpha(self):
        style = ink.style_for(SHADER, HIGHLIGHT, (33, 30, 28, 64), point_widths=[84])
        assert style.blend == "multiply"
        assert style.opacity == pytest.approx(64 / 255)

    def test_pencils_are_translucent_pens(self):
        mech = ink.style_for(int(si.Pen.MECHANICAL_PENCIL_2), RED, point_widths=[24])
        assert (mech.opacity, mech.blend) == (ink.MECHANICAL_PENCIL_OPACITY, "normal")
        light = ink.style_for(int(si.Pen.PENCIL_2), RED, point_widths=[20], pressures=[0])
        heavy = ink.style_for(int(si.Pen.PENCIL_2), RED, point_widths=[20], pressures=[255])
        assert light.opacity == ink.MIN_PENCIL_OPACITY
        assert heavy.opacity == pytest.approx(0.9)

    def test_pens_are_opaque(self):
        style = ink.style_for(FINELINER, BLUE, point_widths=[16])
        assert (style.opacity, style.blend, style.tool) == (1.0, "normal", "fineliner")

    def test_tool_names(self):
        assert ink.tool_name(4) == ink.tool_name(17) == "fineliner"
        assert ink.tool_name(23) == "shader"
        assert ink.tool_name(99) == "unknown_99"


# --------------------------------------------------------------------------
# Unknown enum values
# --------------------------------------------------------------------------


class TestUnknownIds:
    def test_unknown_colour_and_tool_survive_parsing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ink, "_warned_colors", set())
        page = fx.build_page([fx.StrokeSpec(99, 20, [(0, 100), (50, 100)])])
        with BytesIO(page) as fh:
            kinds = [type(b).__name__ for b in read_blocks(fh)]
        assert "SceneLineItemBlock" in kinds
        assert "UnreadableBlock" not in kinds

        blocks, _ = _page_blocks(page, tmp_path)
        paths, _ = _v6_paths_from_blocks(blocks)
        assert len(paths) == 1
        assert f'stroke="{ink.FALLBACK_HEX}"' in paths[0]
        assert 'data-tool="unknown_99"' in paths[0]

    def test_write_path_still_rejects_unknown_ids(self):
        with pytest.raises(ValueError):
            strokes.resolve_color(20)
        with pytest.raises(ValueError):
            strokes.resolve_tool(99)
        assert strokes.resolve_color(7) == 7
        assert strokes.resolve_tool(17) == 17


# --------------------------------------------------------------------------
# SVG writer
# --------------------------------------------------------------------------


class TestSvg:
    def test_stroke_keeps_colour_width_and_tool(self, tmp_path):
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(0, 100), (80, 120)], width=16)])
        blocks, _ = _page_blocks(page, tmp_path)
        (path,), coords = _v6_paths_from_blocks(blocks)
        assert 'stroke="#B33E39"' in path
        assert 'stroke-width="4"' in path
        assert 'data-tool="fineliner" data-color="red"' in path
        assert "opacity" not in path
        assert path.startswith('<path d="M 0.0 100.0 L 80.0 120.0"')
        assert coords == [(0.0, 100.0), (80.0, 120.0)]

    def test_highlights_come_first_and_multiply(self, tmp_path):
        page = fx.build_page(
            [
                fx.StrokeSpec(FINELINER, BLACK, [(0, 100), (80, 100)]),
                fx.StrokeSpec(HIGHLIGHTER, HIGHLIGHT, [(0, 100), (80, 100)], 120, 1.0, GREEN_RGBA),
            ]
        )
        blocks, _ = _page_blocks(page, tmp_path)
        paths, _ = _v6_paths_from_blocks(blocks)
        assert 'data-tool="highlighter"' in paths[0]
        assert 'data-color-hex="#ACFF85"' in paths[0]
        # Paint is the translucent equivalent of multiplying #ACFF85 onto paper.
        assert 'stroke="#52FF00"' in paths[0] and 'opacity="0.478"' in paths[0]
        assert "mix-blend-mode:multiply" in paths[0]
        assert 'stroke-linecap="square"' in paths[0]
        assert 'data-tool="fineliner"' in paths[1]

    def test_glyph_highlight_rect(self, tmp_path):
        page = fx.build_page(
            glyphs=[fx.GlyphSpec(HIGHLIGHT, [(10, 20, 30, 8)], "word", GREEN_RGBA)]
        )
        blocks, _ = _page_blocks(page, tmp_path)
        (rect,), _ = _v6_paths_from_blocks(blocks)
        assert rect.startswith('<rect x="10.0" y="20.0" width="30.0" height="8.0" fill="#52FF00"')
        assert 'data-color-hex="#ACFF85"' in rect
        assert "mix-blend-mode:multiply" in rect

    @pytest.mark.parametrize(
        "rgba,palette_id",
        [
            (GREEN_RGBA, 9),
            (fx.HIGHLIGHTER_RGBA["yellow"], 9),
            (fx.SHADER_RGBA["blue"], 9),
            (None, 3),
        ],
    )
    def test_highlight_overlay_reproduces_multiply_on_white(self, rgba, palette_id):
        tool = SHADER if rgba == fx.SHADER_RGBA["blue"] else HIGHLIGHTER
        style = ink.style_for(tool, palette_id, rgba, point_widths=[120])
        paint, alpha = ink.highlight_overlay(style)
        on_white = [alpha * c + (1 - alpha) * 255 for c in _rgb(paint)]
        multiplied = [255 - style.opacity * (255 - c) for c in _rgb(style.color.hex)]
        assert on_white == pytest.approx(multiplied, abs=1.0)
        assert alpha < 1.0  # never an opaque block

    def test_cropped_svg_keeps_wide_strokes_inside(self, tmp_path):
        page = fx.build_page(
            [fx.StrokeSpec(HIGHLIGHTER, 3, [(0, 500), (300, 500)], width=120, thickness_scale=1.0)]
        )
        zip_path = _write(tmp_path, fx.build_document_zip([page]))
        svg = render_page_from_document_zip_svg(zip_path, 1)
        x, y, w, h = (float(v) for v in svg.split('viewBox="')[1].split('"')[0].split())
        half = 30 / 2
        assert y <= 500 - half and y + h >= 500 + half
        assert x <= 0 - half and x + w >= 300 + half


# --------------------------------------------------------------------------
# Geometry and coordinate mapping
# --------------------------------------------------------------------------


class TestGeometry:
    def test_pdf_mapping(self):
        g = ink.PageGeometry("pdf", A4[0], A4[1], PPU, 0)
        assert g.to_pdf(0, 0) == pytest.approx((A4[0] / 2, 0))
        assert g.to_pdf(-A4[0] / 2 / PPU, A4[1] / PPU) == pytest.approx((0, A4[1]))
        x, y, w, h = g.viewbox
        assert (x, y) == pytest.approx((-A4[0] / 2 / PPU, 0))
        assert (w, h) == pytest.approx((A4[0] / PPU, A4[1] / PPU))
        assert g.contains((0, 0, A4[0], A4[1]))
        assert not g.contains((-10, 0, 20, 20))

    def test_notebook_mapping_matches_device_export_size(self):
        g = ink.PageGeometry("notebook", 1404 * PPU, 1872 * PPU, PPU)
        assert g.to_pdf(-702, 0) == pytest.approx((0, 0))
        assert g.to_pdf(702, 1872) == pytest.approx((446.05, 594.73), abs=0.01)


# --------------------------------------------------------------------------
# Full page vs cropped renders
# --------------------------------------------------------------------------


class TestFullPage:
    def _pdf_doc(self, tmp_path, strokes_=(), glyphs=(), black_rects=()):
        page = fx.build_page(strokes_, glyphs)
        return _write(
            tmp_path, fx.build_document_zip([page], pdf_bytes=_pdf(black_rects=black_rects))
        )

    def test_pdf_backed_svg_is_full_page_by_default(self, tmp_path):
        zip_path = self._pdf_doc(tmp_path, [_stroke_pt(FINELINER, RED, 100, 200, 300)])
        svg = render_page_from_document_zip_svg(zip_path, 1, full_page=None)
        root = svg.split(">", 2)[1]
        assert 'data-geometry="pdf"' in root
        assert 'data-points-per-unit="0.3177"' in root
        assert 'width="595.276"' in root and 'height="841.89"' in root
        vb = [float(v) for v in root.split('viewBox="')[1].split('"')[0].split()]
        assert vb == pytest.approx([-A4[0] / 2 / PPU, 0, A4[0] / PPU, A4[1] / PPU], abs=0.01)

    def test_cropped_when_asked(self, tmp_path):
        zip_path = self._pdf_doc(tmp_path, [_stroke_pt(FINELINER, RED, 100, 200, 300)])
        svg = render_page_from_document_zip_svg(zip_path, 1, full_page=False)
        assert "data-geometry" not in svg

    def test_notebook_stays_cropped_by_default_and_can_be_full(self, tmp_path):
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(0, 100), (80, 100)])])
        zip_path = _write(tmp_path, fx.build_document_zip([page]))
        assert "data-geometry" not in render_page_from_document_zip_svg(zip_path, 1, full_page=None)
        full = render_page_from_document_zip_svg(zip_path, 1, full_page=True)
        assert 'data-geometry="notebook"' in full
        assert 'viewBox="-702 0 1404 1872"' in full

    def test_notebook_full_page_grows_for_ink_below_the_paper(self, tmp_path):
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(0, 2500), (80, 2500)])])
        zip_path = _write(tmp_path, fx.build_document_zip([page]))
        full = render_page_from_document_zip_svg(zip_path, 1, full_page=True)
        x, y, w, h = (float(v) for v in full.split('viewBox="')[1].split('"')[0].split())
        assert y + h >= 2500

    def test_annotation_only_png_of_pdf_page_has_pdf_size(self, tmp_path):
        zip_path = self._pdf_doc(tmp_path, [_stroke_pt(FINELINER, RED, 100, 200, 300)])
        png = render_page_from_document_zip(zip_path, 1, "#FFFFFF", full_page=None)
        assert Image.open(BytesIO(png)).size == (round(A4[0] * 2), round(A4[1] * 2))

    def test_export_annotation_layer_keeps_pdf_page(self, tmp_path):
        zip_path = self._pdf_doc(tmp_path, [_stroke_pt(FINELINER, RED, 100, 200, 300)])
        extracted = tmp_path / "x"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
        png, size = render_page_annotation_layer_from_extracted_document(extracted, 1, "#FFFFFF")
        assert size == pytest.approx(A4)
        image = Image.open(BytesIO(png)).convert("RGB")
        assert _close(image.getpixel((300, 600)), _rgb("#B33E39"))

    def test_annotations_pdf_export_has_pdf_page_size_and_colour(self, tmp_path):
        zip_path = self._pdf_doc(tmp_path, [_stroke_pt(FINELINER, RED, 100, 200, 300, width=40)])
        out = tmp_path / "out.pdf"
        meta = ExportMetadata("doc", "Doc", "/Doc", "pdf", 1)
        write_archive_pdf_export(zip_path, out, meta, pdf_mode="annotations")
        with fitz.open(out) as doc:
            page = doc[0]
            assert (page.rect.width, page.rect.height) == pytest.approx(A4, abs=0.6)
            pix = page.get_pixmap(clip=fitz.Rect(140, 297, 160, 303))
            assert _close(pix.pixel(10, 3), _rgb("#B33E39"), tolerance=20)


# --------------------------------------------------------------------------
# Raster colours
# --------------------------------------------------------------------------


class TestRaster:
    def test_full_page_png_draws_the_pen_colour(self, tmp_path):
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(-300, 900), (300, 900)], width=40)])
        zip_path = _write(tmp_path, fx.build_document_zip([page]))
        png = render_page_from_document_zip(zip_path, 1, "#FFFFFF", full_page=True)
        image = Image.open(BytesIO(png)).convert("RGB")
        assert image.size == (1404, 1872)
        assert _close(image.getpixel((702, 900)), _rgb("#B33E39"))

    def test_merged_highlight_multiplies_and_ink_stays_on_top(self, tmp_path):
        # Black box on the PDF at x 100-150 pt; a green highlight over x 80-200 pt;
        # a blue pen line across the highlight at y 330.
        rect_rm = (*fx.to_rm(80, 290, A4[0]), 120 / PPU, 60 / PPU)
        page = fx.build_page(
            [_stroke_pt(BALLPOINT, BLUE, 60, 220, 330, width=40)],
            [fx.GlyphSpec(HIGHLIGHT, [rect_rm], "box", GREEN_RGBA)],
        )
        pdf = _pdf(black_rects=[(100, 300, 150, 320)])
        zip_path = _write(tmp_path, fx.build_document_zip([page], pdf_bytes=pdf))
        png, note = render_merged_page_from_document_zip(zip_path, 1)
        assert note is None
        image = Image.open(BytesIO(png)).convert("RGB")
        assert all(c < 40 for c in image.getpixel((250, 620)))  # PDF black under highlight
        assert _close(image.getpixel((360, 600)), _rgb("#ACFF85"))  # paper takes the tint
        assert _close(image.getpixel((330, 660)), _rgb("#4E69C9"))  # pen over highlight
        assert _close(image.getpixel((500, 900)), (255, 255, 255))  # untouched paper

    def test_cropped_png_is_one_pixel_per_unit_and_not_stretched(self, tmp_path):
        # A 400-unit horizontal fineliner (4 units wide). The crop pads by
        # 20 + 2 units; the PNG adds a 50 px margin on every side.
        page = fx.build_page([fx.StrokeSpec(FINELINER, BLACK, [(0, 0), (400, 0)], width=16)])
        rm_path = tmp_path / "p.rm"
        rm_path.write_bytes(page)
        image = Image.open(BytesIO(render_rm_file_to_png(rm_path, "#FFFFFF"))).convert("L")
        assert image.size == (444 + 100, 44 + 100)
        column = [image.getpixel((272, y)) for y in range(image.size[1])]
        assert 3 <= sum(1 for v in column if v < 128) <= 5  # ~4 px thick, not stretched

    def test_device_highlight_stays_translucent_on_transparent_background(self, tmp_path):
        page = fx.build_page(
            [fx.StrokeSpec(HIGHLIGHTER, HIGHLIGHT, [(0, 0), (300, 0)], 120, 1.0, GREEN_RGBA)]
        )
        rm_path = tmp_path / "p.rm"
        rm_path.write_bytes(page)
        image = Image.open(BytesIO(render_rm_file_to_png(rm_path, "#00000000")))
        r, g, b, a = image.getpixel((image.size[0] // 2, image.size[1] // 2))
        assert 0 < a < 255
        on_white = [(c * a + 255 * (255 - a)) / 255 for c in (r, g, b)]
        assert _close(on_white, _rgb("#ACFF85"), tolerance=4)

    def test_transparent_background_stays_transparent(self, tmp_path):
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(0, 0), (100, 0)])])
        rm_path = tmp_path / "p.rm"
        rm_path.write_bytes(page)
        image = Image.open(BytesIO(render_rm_file_to_png(rm_path, "transparent")))
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0


# --------------------------------------------------------------------------
# Stroke metadata
# --------------------------------------------------------------------------


class TestMetadata:
    def _doc(self, tmp_path, strokes_=(), glyphs=(), pdf=True):
        page = fx.build_page(strokes_, glyphs)
        data = fx.build_document_zip([page], pdf_bytes=_pdf() if pdf else None)
        return _write(tmp_path, data)

    def test_strokes_highlights_and_summary_in_pdf_points(self, tmp_path):
        rect_rm = (*fx.to_rm(50, 100, A4[0]), 100 / PPU, 12 / PPU)
        zip_path = self._doc(
            tmp_path,
            [
                _stroke_pt(FINELINER, RED, 100, 140, 200),
                _stroke_pt(FINELINER, RED, 143, 170, 200),
                _stroke_pt(BALLPOINT, BLUE, 400, 450, 600),
                _stroke_pt(HIGHLIGHTER, HIGHLIGHT, 50, 150, 106, width=120, rgba=GREEN_RGBA, ts=1),
            ],
            [fx.GlyphSpec(HIGHLIGHT, [rect_rm], "Standup", GREEN_RGBA)],
        )
        meta = extract_page_ink_metadata(zip_path, 1)

        assert meta["geometry"]["kind"] == "pdf"
        assert meta["geometry"]["width_pt"] == pytest.approx(A4[0], abs=0.01)
        summary = meta["summary"]
        assert summary["stroke_count"] == 4
        assert summary["glyph_highlight_count"] == 1
        counts = {
            (e["tool"], e["color_name"]): e["count"] for e in summary["strokes_by_tool_color"]
        }
        assert counts == {
            ("fineliner", "red"): 2,
            ("ballpoint", "blue"): 1,
            ("highlighter", "green"): 1,
        }

        first = meta["strokes"][0]
        assert first["tool"] == "fineliner" and first["color_hex"] == "#B33E39"
        assert first["color_raw"] == RED and "color_rgba" not in first
        assert first["layer"] == "Layer 1"
        assert first["bbox_pdf"] == pytest.approx([100, 200, 140, 200], abs=0.05)
        assert first["width_pt"] == pytest.approx(4 * PPU, abs=0.01)
        assert "outside_page" not in first

        hl = meta["strokes"][3]
        assert hl["color_rgba"] == list(GREEN_RGBA)

        (glyph,) = meta["glyph_highlights"]
        assert (glyph["color_name"], glyph["color_hex"], glyph["text"]) == (
            "green",
            "#ACFF85",
            "Standup",
        )
        assert glyph["rects_pdf"][0] == pytest.approx([50, 100, 150, 112], abs=0.05)
        # rects_rm uses the same [x0, y0, x1, y1] corners as every other box.
        x0, y0 = fx.to_rm(50, 100, A4[0])
        x1, y1 = fx.to_rm(150, 112, A4[0])
        assert glyph["rects_rm"][0] == pytest.approx([x0, y0, x1, y1], abs=0.05)

    def test_clusters_group_nearby_same_colour_ink(self, tmp_path):
        zip_path = self._doc(
            tmp_path,
            [
                _stroke_pt(FINELINER, RED, 100, 140, 200),
                _stroke_pt(FINELINER, RED, 143, 170, 200),
                _stroke_pt(FINELINER, BLACK, 100, 140, 230),
                _stroke_pt(FINELINER, RED, 400, 450, 600),
            ],
        )
        clusters = extract_page_ink_metadata(zip_path, 1)["clusters"]
        red = [c for c in clusters if c["color_name"] == "red"]
        assert sorted(c["stroke_count"] for c in red) == [1, 2]
        assert [c["color_name"] for c in clusters][0] == "red"  # reading order: top first

    def test_ocr_runs_per_text_cluster(self, tmp_path):
        zip_path = self._doc(
            tmp_path,
            [
                _stroke_pt(FINELINER, RED, 100, 140, 200),
                _stroke_pt(HIGHLIGHTER, 3, 100, 140, 400, width=120, ts=1),
            ],
        )
        seen = []

        def fake_ocr(png):
            seen.append(Image.open(BytesIO(png)).size)
            return "Call Anna"

        clusters = extract_page_ink_metadata(zip_path, 1, ocr=fake_ocr)["clusters"]
        by_tool = {c["tool"]: c for c in clusters}
        assert by_tool["fineliner"]["ocr_text"] == "Call Anna"
        assert "ocr_text" not in by_tool["highlighter"]
        assert len(seen) == 1

    def test_options_and_off_page_ink(self, tmp_path):
        zip_path = self._doc(tmp_path, [_stroke_pt(FINELINER, RED, -40, -10, 100)])
        meta = extract_page_ink_metadata(zip_path, 1, include_strokes=False, cluster_gap_pt=None)
        assert "strokes" not in meta and "clusters" not in meta
        assert meta["summary"]["outside_page_count"] == 1
        full = extract_page_ink_metadata(zip_path, 1)
        assert full["strokes"][0]["outside_page"] is True

    def test_erasers_are_counted_not_listed(self, tmp_path):
        zip_path = self._doc(
            tmp_path,
            [
                _stroke_pt(FINELINER, BLACK, 100, 140, 200),
                fx.StrokeSpec(ERASER, 2, [(0, 0), (50, 0)], width=96),
            ],
            pdf=False,
        )
        meta = extract_page_ink_metadata(zip_path, 1)
        assert meta["summary"]["eraser_count"] == 1
        assert [s["tool"] for s in meta["strokes"]] == ["fineliner"]
        assert meta["geometry"]["kind"] == "notebook"

    def test_page_without_layer_and_out_of_range(self, tmp_path):
        data = fx.build_document_zip([None, fx.build_page()], pdf_bytes=None)
        zip_path = _write(tmp_path, data)
        meta = extract_page_ink_metadata(zip_path, 1)
        assert meta["summary"]["stroke_count"] == 0
        assert "no annotation layer" in meta["note"]
        with pytest.raises(ValueError):
            extract_page_ink_metadata(zip_path, 3)


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------


@pytest.fixture
def day_page_client(tmp_path):
    document = Mock()
    document.VissibleName = "Day page"
    document.ID = "doc-1"
    document.Parent = ""
    document.is_folder = False
    client = Mock()
    client.get_meta_items.return_value = [document]
    page = fx.build_page([_stroke_pt(FINELINER, RED, 100, 140, 200)])
    client.download.return_value = fx.build_document_zip([page], pdf_bytes=_pdf())
    return client


class TestTools:
    @pytest.mark.asyncio
    async def test_remarkable_strokes_returns_metadata(self, day_page_client):
        with patch("remarkable_mcp.tools.get_rmapi", return_value=day_page_client):
            result = await mcp.call_tool("remarkable_strokes", {"document": "Day page"})
        data = json.loads(result.content[0].text)
        assert data["document"] == "Day page"
        assert data["summary"]["stroke_count"] == 1
        assert data["strokes"][0]["color_name"] == "red"
        assert "fineliner red" in data["_hint"]

    @pytest.mark.asyncio
    async def test_remarkable_strokes_page_out_of_range(self, day_page_client):
        with patch("remarkable_mcp.tools.get_rmapi", return_value=day_page_client):
            result = await mcp.call_tool("remarkable_strokes", {"document": "Day page", "page": 5})
        data = json.loads(result.content[0].text)
        assert data["_error"]["type"] == "page_out_of_range"

    @pytest.mark.asyncio
    async def test_remarkable_image_svg_is_full_pdf_page_with_colour(self, day_page_client):
        with patch("remarkable_mcp.tools.get_rmapi", return_value=day_page_client):
            result = await mcp.call_tool(
                "remarkable_image",
                {"document": "Day page", "output_format": "svg", "compatibility": True},
            )
        svg = json.loads(result.content[0].text)["svg"]
        assert 'data-geometry="pdf"' in svg
        assert 'stroke="#B33E39"' in svg

    @pytest.mark.asyncio
    async def test_new_parameters_are_in_the_schemas(self):
        tools = {t.name: t for t in await mcp.list_tools()}
        assert "full_page" in tools["remarkable_image"].input_schema["properties"]
        strokes_props = tools["remarkable_strokes"].input_schema["properties"]
        assert {"document", "page", "include_strokes", "cluster_gap", "include_ocr"} <= set(
            strokes_props
        )
        assert tools["remarkable_strokes"].annotations.read_only_hint is True


def test_palette_chart_fixture_round_trips(tmp_path):
    """The labelled chart used for the visual check parses and renders."""
    zip_path = _write(tmp_path, fx.palette_chart_zip())
    meta = extract_page_ink_metadata(zip_path, 1, include_strokes=False)
    pens = len(fx.PEN_TOOLS) * len(fx.PEN_COLORS)
    expected = (
        pens + len(fx.HIGHLIGHTER_RGBA) + len(fx.LEGACY_HIGHLIGHT_COLORS) + len(fx.SHADER_RGBA)
    )
    assert meta["summary"]["stroke_count"] == expected
    assert meta["summary"]["glyph_highlight_count"] == len(fx.HIGHLIGHTER_RGBA) + len(
        fx.LEGACY_HIGHLIGHT_COLORS
    )
    assert meta["summary"]["fallback_colors"] == []
    with tempfile.TemporaryDirectory() as d:
        png, note = render_merged_page_from_document_zip(zip_path, 1)
        assert png and note is None
        assert Path(d).exists()


# --------------------------------------------------------------------------
# Review regressions
# --------------------------------------------------------------------------


def _multi_page_pdf(sizes):
    doc = fitz.open()
    for width, height in sizes:
        doc.new_page(width=width, height=height)
    data = doc.tobytes()
    doc.close()
    return data


class TestReviewRegressions:
    def test_full_page_default_keeps_margin_ink(self, tmp_path):
        # One stroke on the page, one in the left margin (off the PDF page).
        page = fx.build_page(
            [
                _stroke_pt(FINELINER, RED, 100, 200, 300),
                _stroke_pt(BALLPOINT, BLUE, -40, -10, 300),
            ]
        )
        zip_path = _write(tmp_path, fx.build_document_zip([page], pdf_bytes=_pdf()))
        svg = render_page_from_document_zip_svg(zip_path, 1, full_page=None)
        root = svg.split(">", 2)[1]
        x, y, w, h = (float(v) for v in root.split('viewBox="')[1].split('"')[0].split())
        page_left = -A4[0] / 2 / PPU
        assert x < fx.to_rm(-40, 0, A4[0])[0] < page_left  # margin ink inside the viewBox
        assert y == 0 and x + w == pytest.approx(-page_left, abs=0.01)
        assert 'data-page-rm-viewbox="' in root
        assert float(root.split('width="')[1].split('"')[0]) == pytest.approx(w * PPU, abs=0.01)
        assert svg.count('data-tool="ballpoint"') == 1

        png = render_page_from_document_zip(zip_path, 1, "#FFFFFF", full_page=None)
        image = Image.open(BytesIO(png)).convert("RGB")
        left_pt = x * PPU + A4[0] / 2  # where the image starts, in PDF points
        px = round((-25 - left_pt) * 2)
        assert _close(image.getpixel((px, 600)), _rgb("#4E69C9"))

    def test_v5_highlighter_is_yellow_and_pencil_speed_is_scaled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ink, "_warned_colors", set())
        rm_path = tmp_path / "v5.rm"
        rm_path.write_bytes(
            fx.build_v5_page(
                [
                    (5, 0, [(0, 0, 0, 0, 30.0, 0.5), (200, 0, 0, 0, 30.0, 0.5)]),
                    (7, 0, [(0, 50, 0, 0, 2.0, 0.5), (200, 50, 0, 0, 2.0, 0.5)]),
                    (14, 0, [(0, 90, 40.0, 0, 4.0, 0.6), (200, 90, 40.0, 0, 4.0, 0.6)]),
                ]
            )
        )
        from remarkable_mcp.extract import _render_rm_v5_to_svg

        svg = _render_rm_v5_to_svg(rm_path)
        highlighter, mech, pencil = svg.split("<path")[1:]
        assert 'data-color-hex="#FBF719"' in highlighter  # palette yellow, not black
        assert 'data-tool="mechanical_pencil"' in mech  # id 7 is a pen, not an eraser
        expected = (0.1 * -(40 / 35) + 0.6) - 0.1  # rmc pencil opacity on raw values
        assert f'opacity="{ink._fmt(expected)}"' in pencil

    @pytest.mark.asyncio
    async def test_strokes_response_is_bounded_and_paged(self):
        strokes_ = [
            fx.StrokeSpec(FINELINER, BLACK, [(-650 + (i % 50) * 26, 40 + (i // 50) * 60)] * 2)
            for i in range(1000)
        ]
        client = Mock()
        document = Mock(VissibleName="Busy", ID="busy", Parent="", is_folder=False)
        client.get_meta_items.return_value = [document]
        client.download.return_value = fx.build_document_zip([fx.build_page(strokes_)])
        with patch("remarkable_mcp.tools.get_rmapi", return_value=client):
            first = await mcp.call_tool("remarkable_strokes", {"document": "Busy"})
            second = await mcp.call_tool(
                "remarkable_strokes", {"document": "Busy", "stroke_offset": 200}
            )
        text = first.content[0].text
        assert len(text) < 100_000
        data = json.loads(text)
        assert data["stroke_page"] == {
            "offset": 0,
            "returned": 200,
            "total": 1000,
            "next_offset": 200,
        }
        assert len(data["clusters"]) <= ink.MAX_CLUSTERS
        assert "stroke_offset=200" in data["_hint"]
        assert json.loads(second.content[0].text)["strokes"][0]["index"] == 200

    def test_geometry_follows_redirection_page_map(self, tmp_path):
        # formatVersion 1: page 2 is user-added (-1); page 3 shows PDF page 2.
        content = {
            "fileType": "pdf",
            "formatVersion": 1,
            "pages": ["p1", "p2", "p3"],
            "redirectionPageMap": [0, -1, 1],
        }
        page = fx.build_page([fx.StrokeSpec(FINELINER, RED, [(0, 100), (50, 100)])])
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("doc.content", json.dumps(content))
            zf.writestr("doc.pdf", _multi_page_pdf([(300, 400), (612, 792)]))
            for pid in ("p1", "p2", "p3"):
                zf.writestr(f"doc/{pid}.rm", page)
        zip_path = _write(tmp_path, buf.getvalue())

        geometries = [extract_page_ink_metadata(zip_path, p)["geometry"] for p in (1, 2, 3)]
        assert [(g["kind"], g["pdf_page_index"]) for g in geometries] == [
            ("pdf", 0),
            ("notebook", None),
            ("pdf", 1),
        ]
        assert geometries[0]["width_pt"] == 300 and geometries[2]["width_pt"] == 612
        svg = render_page_from_document_zip_svg(zip_path, 3, full_page=None)
        assert 'data-page-width-pt="612"' in svg

    def test_paper_pro_grid_uses_its_own_scale(self, tmp_path):
        page = fx.build_page(
            [fx.StrokeSpec(FINELINER, RED, [(0, 100), (50, 100)])], paper_size=(1620, 2160)
        )
        zip_path = _write(tmp_path, fx.build_document_zip([page]))
        geometry = extract_page_ink_metadata(zip_path, 1)["geometry"]
        assert geometry["points_per_unit"] == 0.3144
        assert geometry["width_pt"] == pytest.approx(1620 * 0.3144, abs=0.01)
        png = render_page_from_document_zip(zip_path, 1, "#FFFFFF", full_page=True)
        assert Image.open(BytesIO(png)).size == (round(1620 * 1872 / 2160), 1872)

    @pytest.mark.asyncio
    async def test_remarkable_image_annotation_png_defaults_to_pdf_page(self, day_page_client):
        with patch("remarkable_mcp.tools.get_rmapi", return_value=day_page_client):
            full = await mcp.call_tool(
                "remarkable_image",
                {"document": "Day page", "render_merged": False, "compatibility": True},
            )
            cropped = await mcp.call_tool(
                "remarkable_image",
                {
                    "document": "Day page",
                    "render_merged": False,
                    "full_page": False,
                    "compatibility": True,
                },
            )
        import base64

        def size(result):
            data = json.loads(result.content[0].text)
            return Image.open(BytesIO(base64.b64decode(data["image_base64"]))).size

        assert size(full) == (round(A4[0] * 2), round(A4[1] * 2))
        assert size(cropped)[0] < 400
