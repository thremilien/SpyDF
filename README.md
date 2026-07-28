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

What disappears follows the **outline you drew**, whatever its shape. PyMuPDF
can only redact rectangles, so a polygon or freehand zone is cut into thin
horizontal strips that hug the outline, and each strip is redacted. Strips
overshoot the outline by at most one strip height (measured: 1.25 pt, under
half a millimetre) — over-deleting a little is acceptable, leaving content
alive inside the shape is not. That matters most on a scan, where the page is
a single image: redacting the bounding box would destroy its pixels and turn
the whole box white under a cover that followed the outline.

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

### The inspector pane

Opening a PDF in a reader only shows you a picture of it. The right-hand pane
writes out, in plain text, what the file actually carries:

- the **indexed text layer** — what is selectable, copyable and searchable,
  including any **invisible text** (an OCR layer under a scan, or text hidden
  on purpose), which is highlighted because it leaks while showing nothing;
- **metadata** and **XMP** — author, title, keywords, and the scanner or
  application that produced the file;
- **bookmarks**, often literally "Copie de <student name>";
- **annotations** with their author, **form fields** with their values,
  **attachments**, **layers**, **links**, **fonts** and any **JavaScript**.

Every item is marked *effacé* or *conservé* according to the zones you have
drawn and the scrubbing checkbox, with a count of what would still leak, so
you can see the result before exporting. The pane is read-only.

Three views from the toolbar: document only, both (default), hidden content
only.

### Verification

After writing the file, the exporter re-opens **the exported bytes** and looks
for text, annotations or form fields still inside a redacted zone. A word
counts as a survivor once a redacted strip covers a real part of it, not when
it merely grazes the outline. Any survivor is reported in the status bar, so a
failed redaction is visible rather than silent.

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
- `src/app.py` — FastAPI routes (open/render/inspect/export/download)
- `src/probe.py` — read-only extraction of the document's invisible payload
- `src/server.py` — server bootstrap (opens browser, runs uvicorn)
- `src/templates/index.html` — page shell
- `src/static/app.js` — pages, zones, export
- `src/static/inspector.js` — the inspector pane
- `tests/` — regression tests

## Docker

```bash
docker build -t spydf .
docker run --rm -p 8765:8765 spydf
```
