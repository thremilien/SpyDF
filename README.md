# SpyDF

Local webapp to redact PDF exams: draw zones over regions to remove (names,
student IDs, ...), and export a PDF where the underlying text, image and
vector objects are actually deleted — not just covered by a drawn box.
Everything runs locally and in memory; nothing is uploaded anywhere.

## Usage

```bash
uv run main.py
```

This opens `http://127.0.0.1:8765` in your browser. Drop a PDF in, mark the
zones, then export.

### Zones

Three shapes: **rectangle**, **polygon** (click the vertices, double-click to
close) and **freehand**. Drag a zone to move it, its handles to resize it.

Redaction can only operate on rectangles, so a polygon or freehand zone
destroys everything inside its **bounding box**, which the UI draws as a
dashed rectangle around the shape — that dashed box, not the outline, is what
disappears from the file.

Each zone has a mode, switched from its right-click menu:

- **Supprimer** — the content is removed and the area covered in white.
- **Repixeliser** — the area is replaced by a genuine downsample of itself,
  an unreadable mosaic; the source objects are removed just the same.

### Pages

The trash button on a page marks it for deletion; the page is dropped from the
exported document entirely.

### Beyond the visible page

"Effacer les traces du document" (on by default) also clears what redaction
leaves untouched because it does not live in the page content: metadata, XMP,
bookmarks (often the student's name), attachments, JavaScript, links, form
responses and optional-content layer names. Annotations and form fields
intersecting a zone are deleted explicitly.

### Verification

After writing the file, the exporter re-opens **the exported bytes** and looks
for text, annotations or form fields still intersecting a redacted zone. Any
survivor is reported in the status bar, so a failed redaction is visible
rather than silent.

### Keyboard

`Ctrl+Z` / `Ctrl+Y` undo and redo. `Tab` moves between zones, `Enter` opens
the selected zone's menu, `Suppr` deletes it, `Échap` cancels.

Set `PORT` to change the listening port (default `8765`).

## Development

```bash
uv sync          # installs dev dependencies too
uv run main.py
uv run pytest    # regression tests for the redaction path
```

The tests build a PDF carrying one of each class of identifying trace, export
it through the real routes, and assert none survives in the raw bytes or in
any decompressed stream of the result.

## Project layout

- `main.py` — entry point
- `src/app.py` — FastAPI routes (open/render/export/download)
- `src/server.py` — server bootstrap (opens browser, runs uvicorn)
- `src/templates/index.html` — page shell
- `src/static/` — CSS and client-side JS

## Docker

```bash
docker build -t spydf .
docker run --rm -p 8765:8765 spydf
```
