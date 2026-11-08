# MCP Tools Reference

This document describes the read and render tools. Write tools are listed in the
[README](../README.md#write-tools-by-transport).

## Overview

| Tool | Purpose |
|------|---------|
| [`remarkable_read`](#remarkable_read) | Read and search document content |
| [`remarkable_browse`](#remarkable_browse) | Navigate folders and find documents |
| [`remarkable_search`](#remarkable_search) | Search across multiple documents |
| [`remarkable_recent`](#remarkable_recent) | Get recently modified documents |
| [`remarkable_status`](#remarkable_status) | Check connection status |
| [`remarkable_image`](#remarkable_image) | Get page images (PNG or SVG) |
| [`remarkable_strokes`](#remarkable_strokes) | Ink colour, tool and position as JSON |
| [`remarkable_export`](#remarkable_export) | Export one document as PDF or Markdown |

These tools never modify tablet content and return structured next-step hints.
`remarkable_export` additionally creates a bounded temporary file on the MCP server
host, so its MCP annotation is `readOnlyHint=false` even under tablet read-only mode.

## Root Path Filtering

All tools respect the `REMARKABLE_ROOT_PATH` environment variable. When configured, operations are scoped to that folder:

```json
{
  "env": {
    "REMARKABLE_ROOT_PATH": "/Work"
  }
}
```

With this configuration:
- Paths in responses are relative to the root (e.g., `/Work/Project` appears as `/Project`)
- Documents outside the root are not accessible
- `remarkable_status()` shows the configured root and document count within that folder

---

## remarkable_read

**Read and extract text from a document.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `content_type` | string | `"text"` | What content to extract |
| `page` | int | `1` | Extracted-content page number for bounded text pagination |
| `grep` | string | `None` | Search for keywords in content |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |

### Content Types

- **`"text"`**: source text plus annotations, highlights, and typed text (default)
- **`"raw"`**: original PDF/EPUB text without annotations
- **`"annotations"`**: highlights, typed notebook text, and OCR content

### Examples

```python
# Read first page of a document
remarkable_read("Meeting Notes")

# Read the third extracted-content chunk
remarkable_read("Research Paper.pdf", page=3)

# Search for keywords
remarkable_read("Project Plan", grep="deadline")

# Get only annotations and highlights
remarkable_read("Book.pdf", content_type="annotations")

# Enable OCR for handwritten notes
remarkable_read("Journal", include_ocr=True)

# Read by full path
remarkable_read("/Work/Projects/Q4 Planning")
```

### Response Format

```json
{
  "document": "Meeting Notes",
  "path": "/Work/Meeting Notes",
  "file_type": "notebook",
  "content_type": "text",
  "content": "Extracted text content...",
  "page": 1,
  "total_pages": 5,
  "total_pages_known": true,
  "content_pages": 2,
  "total_chars": 12500,
  "more": true,
  "next_page": 2,
  "modified": "2025-11-28T10:30:00Z",
  "_hint": "Content page 1/2; document has 5 physical pages. Next: remarkable_read('Meeting Notes', page=2)."
}
```

### Smart Features

- **Auto-OCR**: If a notebook has no typed text and `include_ocr=False`, OCR is automatically enabled and you're notified via `_ocr_auto_enabled: true`
- **Fuzzy matching**: If the exact document isn't found, similar names are suggested
- **Path resolution**: Works with document names or full paths

### Pagination

- `total_pages` reports physical document pages, matching `remarkable_image`,
  and `total_pages_known` says whether that count was available.
- `content_pages` reports extracted-text pagination units. PDF/EPUB source text
  uses ~8000-character chunks; notebook OCR may produce fewer content pages than
  physical pages when blank pages have no text.
- `page`, `more`, and `next_page` refer to those content pages. This preserves
  bounded text responses without overloading the physical page count.
- Raw PDF reads count pages from the same source bytes used for text extraction
  and do not fetch the full document archive. A raw EPUB may need that archive
  for its physical page count; if the archive is unavailable, the extracted
  content still succeeds with `total_pages: null`, `total_pages_known: false`,
  and a `page_count_note`.
- On older USB firmware that returns a native PDF instead of an rmdoc archive,
  `content_type="text"` returns source text without annotations, while
  `content_type="annotations"` returns `annotations_not_available`.

When `more: true`, use the `page` parameter to continue reading.

---

## remarkable_browse

**Navigate your document library.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `path` | string | `"/"` | Folder path to browse |
| `query` | string | `None` | Search documents by name |
| `tags` | list[string] | `None` | Require all listed tags (case-insensitive) |

### Examples

```python
# List root folder
remarkable_browse("/")

# Browse a specific folder
remarkable_browse("/Work/Projects")

# Search for documents by name
remarkable_browse(query="meeting")

# Combine path and search
remarkable_browse("/Work", query="report")

# Filter by tag
remarkable_browse("/Work", tags=["project", "active"])
```

### Response Format

```json
{
  "path": "/Work",
  "folders": [
    {"name": "Projects", "path": "/Work/Projects"},
    {"name": "Archive", "path": "/Work/Archive"}
  ],
  "documents": [
    {
      "name": "Weekly Report",
      "path": "/Work/Weekly Report",
      "type": "pdf",
      "modified": "2025-11-28T10:30:00Z"
    }
  ],
  "_hint": "Found 2 folders, 1 document. To read: remarkable_read('Weekly Report')."
}
```

### Smart Features

- **Auto-redirect**: If `path` points to a document instead of a folder, automatically returns the document content (like calling `remarkable_read`)
- **Case-insensitive**: Paths and searches are case-insensitive

---

## remarkable_search

**Search across multiple documents.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | *required* | Search term for document names |
| `grep` | string | `None` | Pattern to search within content |
| `limit` | int | `5` | Maximum documents to search (max: 5) |
| `include_ocr` | bool | `False` | Enable OCR for handwritten content |
| `tags` | list[string] | `None` | Require all listed tags (case-insensitive) |

### Examples

```python
# Find documents with "meeting" in the name
remarkable_search("meeting")

# Find "action items" inside meeting documents
remarkable_search("meeting", grep="action items")

# Search journals for a specific topic
remarkable_search("journal", grep="project idea", include_ocr=True)

# Search only tagged documents
remarkable_search("project", tags=["work"])
```

### Response Format

```json
{
  "query": "meeting",
  "grep": "action items",
  "count": 3,
  "documents": [
    {
      "name": "Team Meeting Nov",
      "path": "/Work/Team Meeting Nov",
      "modified": "2025-11-28T10:30:00Z",
      "content": "...context around matches...",
      "total_pages": 2,
      "content_pages": 1,
      "grep_matches": 5,
      "truncated": true
    }
  ],
  "_hint": "Found 3 document(s) with 12 grep match(es). To read more: remarkable_read('/Work/Team Meeting Nov')."
}
```

### Limits

- Maximum 5 documents per search
- Content is truncated to ~2000 characters per document
- Designed for quick discovery, use `remarkable_read` for full content

---

## remarkable_recent

**Get recently modified documents.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `10` | Maximum documents to return |
| `include_preview` | bool | `False` | Include text preview for each document |

### Examples

```python
# Get last 10 documents
remarkable_recent()

# Get last 5 with previews
remarkable_recent(limit=5, include_preview=True)
```

### Response Format

```json
{
  "count": 5,
  "documents": [
    {
      "name": "Meeting Notes",
      "path": "/Work/Meeting Notes",
      "modified": "2025-11-28T10:30:00Z",
      "preview": "First 200 characters of content..."
    }
  ],
  "_hint": "Showing 5 recent documents. To read one: remarkable_read('Meeting Notes')."
}
```

### Notes

- With `include_preview=True`, limit is capped at 10 (performance)
- Notebooks skip preview (require OCR), showing `preview_skipped` instead
- PDFs and EPUBs have fast text extraction for previews

---

## remarkable_status

**Check connection and authentication status.**

### Parameters

None.

### Examples

```python
remarkable_status()
```

### Response Format

```json
{
  "authenticated": true,
  "transport": "ssh",
  "connection": "SSH to root@10.11.99.1:22",
  "status": "connected",
  "document_count": 142,
  "root_path": "/Work",
  "ocr_backend": "google",
  "_hint": "Connection healthy. Filtered to root: /Work. Use remarkable_browse('/') to explore your library."
}
```

### Fields

| Field | Description |
|-------|-------------|
| `authenticated` | Whether authentication succeeded |
| `transport` | `"local-dir"`, `"cloud"`, `"usb-web"`, or `"ssh"` |
| `connection` | Connection details |
| `document_count` | Total documents in library (filtered by root if configured) |
| `write_enabled` | Whether write tools are enabled (the default; `false` only with `--read-only`) |
| `capabilities` | Effective capabilities for the active transport (read/render/export/upload/mkdir/move/rename/delete) |
| `capabilities_by_transport` | The full per-transport capability matrix |
| `root_path` | Configured root path filter (only present if set) |
| `ocr_backend` | Which OCR backend is configured |

---

## remarkable_image

**Get a PNG or SVG image of a specific page.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `page` | int | `1` | Page number (1-indexed) |
| `background` | string | `"#FBFBFB"` | Background color (hex RGB or RGBA) |
| `output_format` | string | `"png"` | Output format: `"png"` or `"svg"` |
| `include_ocr` | bool | `False` | Enable OCR on the image |
| `compatibility` | bool | `False` | Return resource URI instead of embedded resource |
| `render_merged` | bool \| null | `None` (auto) | PNG compositing mode: auto-merge PDF-backed pages, `True` to request merging explicitly, or `False` for annotations only |
| `full_page` | bool \| null | `None` (auto) | Extent of annotation-only output: `None` renders PDF-backed pages as the whole PDF page (grown to include margin ink) and crops notebook pages to the ink; `True` always renders the whole page; `False` always crops to the ink |

### Ink colours and tools

Every render (SVG, PNG, merged PNG and PDF export) keeps the colour and tool of
each stroke:

- Pen colours come from rmc's reMarkable palette (black `#000000`, gray
  `#909090`, white, yellow `#FBF719`, green `#00FF00`, pink `#FFC0CB`, blue
  `#4E69C9`, red `#B33E39`, ...).
- Highlighter, shader and text-highlight colours stored by firmware 3.14+ in
  `color_rgba` are used as stored (for example yellow `#FFED75`, green `#ACFF85`).
- A colour the server does not know renders magenta `#FF00FF` and is logged.
- Width follows the stored point width (which includes the pen thickness).
- Highlights blend with multiply: in PNGs, text and ink under a highlight stay
  dark. In SVG, highlights carry `style="mix-blend-mode:multiply"` and a
  translucent paint that gives the same colour on white paper; every element has
  `data-tool`, `data-color` and `data-color-hex` (the stored colour).
- A full-page SVG of a PDF-backed page carries `data-geometry`,
  `data-points-per-unit`, `data-page-width-pt`, `data-page-height-pt` and
  `data-page-rm-viewbox` on its root, so strokes can be mapped onto the PDF page.

### Background Colors

- **`"#FBFBFB"`**: default reMarkable paper color
- **`"#FFFFFF"`**: white
- **`"#00000000"`**: fully transparent
- **`"#80008080"`**: semi-transparent purple

**Tip:** Set `REMARKABLE_BACKGROUND_COLOR` environment variable to change the default for all image operations.

### Examples

```python
# Get first page with default paper background
remarkable_image("UI Mockup")

# Get specific page
remarkable_image("Meeting Notes", page=2)

# White background
remarkable_image("Diagram", background="#FFFFFF")

# Transparent background for compositing
remarkable_image("Logo Sketch", background="#00000000")

# SVG format for editing in design tools
remarkable_image("Wireframe", output_format="svg")

# SVG with custom background
remarkable_image("Sketch", output_format="svg", background="#F0F0F0")

# Enable OCR
remarkable_image("Handwritten Notes", include_ocr=True)

# Compatibility mode: return resource URI instead of embedded resource
remarkable_image("Diagram", compatibility=True)

# PDF-backed PNG pages merge the source page and annotations automatically
remarkable_image("Research Paper", page=3)

# Return only the reMarkable annotation layer (whole PDF page by default)
remarkable_image("Research Paper", page=3, render_merged=False)

# Annotation layer cropped to the ink instead
remarkable_image("Research Paper", page=3, render_merged=False, full_page=False)

# Whole notebook page as SVG
remarkable_image("Notes", output_format="svg", full_page=True)
```

### Response Format

For PNG format, returns an embedded image resource that can be displayed inline.

For SVG format, returns an embedded text resource with the SVG content.

When `compatibility=True`, returns a JSON object with the resource URI:

```json
{
  "resource_uri": "remarkableimg:///path/doc.page-1.merged.png",
  "page": 1,
  "total_pages": 5,
  "merged": true,
  "_hint": "Page 1/5. Use resource URI to access the image."
}
```

When `include_ocr=True`, OCR text is included in the response. Google Vision is
used when configured; otherwise OCR runs locally with Tesseract.

### Notes

- Notebook pages are cropped to the ink unless `full_page=True`; blank notebook
  pages render as before.
- The returned resource URI always serves the default (`full_page=None`) extent.
- PDF-backed PNG pages merge the source page and annotations by default. Explicit
  `render_merged=False` preserves annotation-only access, including user-added
  PDF pages that have no underlay.
- SVG remains annotation-only; explicit `render_merged=True` with SVG returns
  SVG plus an explanatory note.
- Compatibility responses preserve the same merge choice and expose `merged`.
- If older USB firmware returns only a native PDF export, PNG still renders via
  that export. SVG and explicit `render_merged=False` require the unavailable
  rmdoc annotation layer and return clear errors.
- RGBA colors (8-digit hex) allow transparency control

---

## remarkable_strokes

**Get the colour, tool and page position of every stroke and text highlight on
one page, as JSON.** Read-only; it never changes the tablet.

Use it when colour or position matters: which time-grid row or to-do line has
red ink, which text was highlighted in green, which notes were written with a
highlighter rather than a pen.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `page` | int | `1` | Page number (1-indexed) |
| `include_strokes` | bool | `True` | Include the per-stroke list |
| `cluster_gap` | float \| null | `8` | Maximum gap in PDF points between strokes of one cluster; `null` skips clustering (and OCR) |
| `include_ocr` | bool | `False` | Recognise the handwriting in each cluster (one request per cluster, at most 40, highlighters skipped) |
| `stroke_offset` | int | `0` | First stroke to return |
| `max_strokes` | int | `200` | Strokes per response |

### Coordinates

Positions are PDF points with the origin at the top-left of the page:
`pdf_x = rm_x * points_per_unit + width_pt / 2` and
`pdf_y = rm_y * points_per_unit` (`points_per_unit` is 0.3177 on the
1404x1872 grid and 0.3144 on the Paper Pro 1620x2160 grid). For an imported PDF
the page is the PDF page itself (cropbox, rotation applied), so positions line
up with the PDF's text. For a notebook page it is the paper at the device's
export scale (about 446 x 595 pt on a reMarkable 2). Raw tablet coordinates are
included as `bbox_rm` / `rects_rm`. All boxes are `[x0, y0, x1, y1]`.

### Response Format

Compact JSON:

```json
{
  "document": "Day-2026-11-05",
  "page": 1,
  "total_pages": 2,
  "geometry": {"kind": "pdf", "width_pt": 595.28, "height_pt": 841.89,
               "points_per_unit": 0.3177, "pdf_page_index": 0, "rm_viewbox": [...]},
  "summary": {
    "stroke_count": 3, "glyph_highlight_count": 1, "eraser_count": 0,
    "strokes_by_tool_color": [{"tool": "fineliner", "color_name": "red",
                               "color_hex": "#B33E39", "count": 2}],
    "glyph_highlights_by_color": [{"color_name": "green", "color_hex": "#ACFF85", "count": 1}],
    "fallback_colors": [], "outside_page_count": 0, "cluster_count": 2
  },
  "strokes": [{"index": 0, "layer": "Layer 1", "tool": "fineliner",
               "color_name": "red", "color_hex": "#B33E39", "color_raw": 7,
               "thickness_scale": 2.0, "width_pt": 1.27, "point_count": 24,
               "bbox_pdf": [100.0, 196.4, 140.2, 203.1],
               "bbox_rm": [-622.1, 618.2, -495.6, 639.3]}],
  "stroke_page": {"offset": 0, "returned": 3, "total": 3, "next_offset": null},
  "glyph_highlights": [{"index": 0, "layer": "Layer 1", "color_name": "green",
                        "color_hex": "#ACFF85", "color_raw": 9,
                        "color_rgba": [172, 255, 133, 255], "text": "Standup",
                        "rects_pdf": [[50.0, 100.0, 150.0, 112.0]], "rects_rm": [...]}],
  "clusters": [{"cluster": 0, "tool": "fineliner", "color_name": "red",
                "color_hex": "#B33E39", "stroke_count": 2, "stroke_indexes": [0, 1],
                "bbox_pdf": [99.4, 195.8, 170.6, 203.7], "bbox_rm": [...]}],
  "_hint": "Page 1/2: 3 strokes, 1 text highlights. Ink: ballpoint blue, fineliner red."
}
```

- `color_rgba` appears only when the tablet stored an explicit colour.
- `outside_page` marks ink drawn off the page; `layer_hidden` marks ink on a
  hidden layer.
- `color_name` `"unknown"` (magenta in images) means an unrecognised colour id;
  such ids are listed in `summary.fallback_colors`.
- Clusters group strokes of the same tool and colour that lie within
  `cluster_gap` of each other, in reading order, at most 150 per response.
  `stroke_indexes` always refer to the whole page.
- A busy page can hold more than 1,000 strokes. Page through them with
  `stroke_offset`, or set `include_strokes=False` for just the summary,
  highlights and clusters.

### Examples

```python
remarkable_strokes("Day-2026-11-05")  # ink on page 1
remarkable_strokes("Day-2026-11-05", page=2, include_ocr=True)
remarkable_strokes("Research Paper", page=3, include_strokes=False)
remarkable_strokes("Notes", stroke_offset=200)  # next 200 strokes
```

---

## remarkable_export

**Export one document as a reusable PDF or Markdown resource.**

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `document` | string | *required* | Document name or full path |
| `output_format` | `"pdf"` \| `"markdown"` | `"pdf"` | Export format |
| `pdf_mode` | `"merged"` \| `"annotations"` | `"merged"` | PDF-only rendering mode |
| `include_ocr` | bool | `False` | Run the existing OCR path for Markdown |

### Delivery and lifecycle

The tool returns a small JSON summary plus an MCP `ResourceLink`. It does not
embed PDF/Markdown bytes in the tool response and does not accept or invent a host
output path. The server writes the generated file beneath a private temporary
directory:

- links expire 15 minutes after creation;
- at most eight exports are retained, with least-recently-used eviction;
- expired and evicted links stop resolving;
- all managed exports are removed at server shutdown.

This local temporary write is why the tool declares `readOnlyHint=false`.
The reMarkable tablet and library are never modified, and the tool remains
available when the server runs with `--read-only`.

### PDF behavior

- Physical page order comes from reMarkable `.content` metadata.
- `pdf_mode="merged"` combines mapped PDF underlays and annotations by default.
- Native notebooks use complete full-page ink/blank renders.
- `pdf_mode="annotations"` returns complete annotation layers only.
- A flattened native PDF (older USB firmware) remains exportable in merged mode,
  but cannot be separated into annotation-only layers.
- A page render failure is represented by a labeled placeholder at the same page
  ordinal and reported with `status: "partial"`; pages are never silently skipped.
- EPUB source pages use a transport-native PDF when one is available. Otherwise
  available annotation pages are exported with an explicit partial warning.

### Markdown behavior

Markdown starts with basic front matter containing the stable reMarkable document
UUID, title, path, source type, physical page count, modified time, tags, and
completeness. It then uses fixed sections:

1. Source text
2. Typed text
3. Annotations
4. Highlights
5. OCR
6. Export notes (only for partial exports)

Extracted text is emitted verbatim. The exporter does not claim to detect headings,
lists, paragraphs, links, or physical-page attribution that the existing extraction
result cannot prove. OCR is opt-in and uses the same configured backend and cache as
`remarkable_read`.

### Examples

```python
# Complete PDF (source underlay + annotations where available)
remarkable_export("Meeting Notes")

# Full-page annotation layers only
remarkable_export("Research Paper", pdf_mode="annotations")

# Markdown with handwriting OCR
remarkable_export("Journal", output_format="markdown", include_ocr=True)
```

### Response

```json
{
  "document": "Meeting Notes",
  "document_id": "11111111-2222-3333-4444-555555555555",
  "path": "/Work/Meeting Notes",
  "format": "pdf",
  "filename": "Meeting Notes.pdf",
  "size": 481203,
  "pages": 7,
  "status": "complete",
  "failed_pages": [],
  "warnings": [],
  "resource_uri": "remarkableexport:///pdf/opaque-resource-id",
  "expires_at": "2026-08-14T22:15:00+00:00",
  "temporary_local_file": true,
  "_hint": "Temporary PDF export ready as an MCP resource..."
}
```

Fetch and save the accompanying `ResourceLink` before it expires when durable
storage is required.

---

## Error Handling

All tools return structured errors with suggestions:

```json
{
  "_error": {
    "type": "document_not_found",
    "message": "Document 'Meting Notes' not found",
    "suggestion": "Did you mean: 'Meeting Notes', 'Meeting Notes 2'?",
    "did_you_mean": ["Meeting Notes", "Meeting Notes 2"]
  }
}
```

Common error types:
- `document_not_found`: document does not exist (includes suggestions)
- `authentication_failed`: token invalid or SSH connection failed
- `connection_error`: network or SSH connection issue
