# TODO

Findings from the security + UI review. Items under "Done" are already fixed
and verified in this branch; everything below "Open" is still outstanding.

## Done — redaction leaks (all verified closed)

The review used a purpose-built PDF stuffed with identifying traces, exported
it through `/api/export`, then grepped the raw bytes *and* every decompressed
stream of the result. Before the fixes, six classes of owner data survived.

- **Vector line art crossing a zone edge survived intact.** PyMuPDF's default
  is `graphics=PDF_REDACT_LINE_ART_REMOVE_IF_COVERED`, which only drops a path
  *entirely* inside the redaction rect. A handwritten signature overflowing the
  box was left fully in the content stream, merely hidden under the white
  cover — deleting that one white rectangle restored it. Now uses
  `REMOVE_IF_TOUCHED`.
- **Annotations survived**, including the author name (`/T`) and comment body.
  `apply_redactions` does not touch annotations even when fully covered.
- **Form fields (widgets) survived**, name and value both, and stayed visible
  in the rendered output.
- **Bookmarks / TOC survived** — often literally "Copie de <student name>".
- **Embedded file attachments survived** entirely.
- **Optional-content (layer) names survived** in `/OCProperties`.

Fixes: `_purge_annots()` drops annotations and widgets intersecting any zone;
`_scrub_document()` clears metadata, XMP, TOC, layer names, attachments,
JavaScript, links and form responses; `_rename_layers()` anonymises OCG names.

Confirmed already correct, left alone: text removal, image pixel destruction,
metadata/XMP stripping, `garbage=4 + clean` dropping orphaned objects, and the
pixelate path (the mosaic is a genuine downsample — the source text was gone
from the output bytes).

## Done — verification and web layer

- **Leak check now re-opens the exported bytes** instead of inspecting the
  in-memory document, which is what `CLAUDE.md` always claimed it did. It also
  maps page numbers across deleted pages, and now reports surviving
  annotations and form fields, not just text.
- **XSS in the status bar.** `$('status').innerHTML` interpolated leak text
  taken straight from the PDF. A crafted filename or text run could execute
  script in the page, which has same-origin access to `/api/download/*` for
  every live session. Now built with `textContent`.
- Upload size cap (200 MB), password-protected PDFs rejected with a clear
  message, session TTL + cap so forgotten documents don't sit in RAM forever,
  `Content-Disposition` filename sanitised, `no-store` and `nosniff` on the
  download response.

## Done — UI

- **Non-rectangular zones delete more than they show.** Redaction runs on the
  *bounding box* of the shape while the white cover follows the exact outline.
  Drawing a triangle over a name also destroyed unrelated text that sat
  outside the triangle but inside its bounding box (verified: `"reponse
  importante"` came back as `"portante"`). Fail-safe, but unpredictable. The
  bounding box is now drawn as a dashed rectangle, while the zone is being
  traced *and* once it is placed, so the extent of the deletion is visible.
  The behaviour itself is unchanged and is now pinned by a test.
- **`redoStack` was not reset when a new file was opened**, so Ctrl+Y after
  opening a second document pasted zones from the previous one onto it.
- **No redo button** — added next to undo.
- **Export was not guarded against double-clicks**; the button now goes
  disabled for the duration of the request.
- **No error handling around the export/open fetches.** Both now report the
  failure in the status bar instead of leaving it stuck on "Traitement…".
- **No progress feedback when opening a large PDF.** Upload percentage (via
  XHR — `fetch` cannot report upload progress), then a busy state held until
  the first page has actually rendered, plus an indeterminate progress bar.
- **Touch devices could not draw at all.** All handlers are pointer events
  now. The page layer keeps `touch-action: pan-y`, so a vertical drag still
  scrolls the document and a gesture started sideways draws; `pointercancel`
  aborts a trace the browser takes back.
- **The zone context menu was keyboard-inaccessible.** Zones are focusable,
  Enter / ContextMenu / Shift+F10 opens the menu, arrows move within it,
  Escape closes it and returns focus to the zone.

## Done — other

- **Test suite** (`uv run pytest`). Builds a PDF carrying one of each trace
  class, exports it through the real routes, and asserts no marker survives in
  the raw bytes or any decompressed stream. Also covers page deletion with
  zone remapping, the pixelate path, the bounding-box behaviour, and `_verify`.
- **`README.md`** rewritten: shapes, modes, page deletion, document scrubbing,
  the verification pass, keyboard shortcuts, and how to run the tests.

## Open — UI

- [ ] **Touch drawing is discoverable only from the help popup.** `pan-y`
      means a zone has to be started with a sideways movement. An explicit
      "naviguer / dessiner" toggle, shown only for coarse pointers, would be
      less of a trick to explain.
- [ ] **Inspector panel.** Show the rendered PDF on the left and, on the
      right, a plain sheet listing everything the document carries that the
      reader cannot see in a browser: indexed text, metadata, XMP, bookmarks,
      annotations, form fields, attachments, layers, links, JavaScript. The
      point is to make the invisible payload reviewable before exporting.
