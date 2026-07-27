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

## Open — UI

- [ ] **Non-rectangular zones delete more than they show.** Redaction runs on
      the *bounding box* of the shape while the white cover follows the exact
      outline. Drawing a triangle over a name also destroyed unrelated text
      that sat outside the triangle but inside its bounding box (verified:
      `"reponse importante"` came back as `"portante"`). This is fail-safe —
      it over-deletes, never under-deletes — but it is unpredictable for the
      user. Fix by drawing the bounding rectangle while a polygon/freehand
      zone is being drawn, so what you see is what gets removed.
- [ ] **`redoStack` is not reset when a new file is opened.** `openFile()`
      clears `zones` and `history` but not `redoStack`, so Ctrl+Y after
      opening a second document pastes zones from the previous one onto it.
- [ ] **No redo button.** Redo exists only via Ctrl+Y / Ctrl+Shift+Z; the
      toolbar has undo but no counterpart.
- [ ] **Export is not guarded against double-clicks.** The button stays
      enabled during the request, so a second click exports twice.
- [ ] **No error handling around the export/open fetches.** If the server is
      unreachable the promise rejects unhandled and the status bar stays stuck
      on "Traitement…".
- [ ] **No progress feedback when opening a large PDF.** The upload and first
      render can take seconds with no indication anything is happening.
- [ ] **Touch devices cannot draw at all.** Every handler is `mousedown` /
      `mousemove` / `mouseup`; there are no pointer or touch events.
- [ ] **The zone context menu is keyboard-inaccessible.** It only opens on
      right-click, so mode switching and zone deletion are mouse-only.

## Open — other

- [ ] No test suite. The review scripts that found the leaks above are worth
      keeping as regression tests: build a PDF containing each trace type,
      export it, assert none of the markers survive in the output bytes.
- [ ] `README.md` still says the tool draws *rectangles* only, and does not
      mention pixelate mode or page deletion.
