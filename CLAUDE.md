# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SpyDF — a local FastAPI webapp for redacting PDFs (e.g. exam scans): the user
draws rectangles, polygons or freehand outlines over regions in the browser, and
the export **renders every page flat into a bitmap, paints those zones into the
pixels, and builds a new document holding nothing but those bitmaps**, re-read by
OCR so the result stays searchable. Nothing of the original file survives except
what is visible on its pages. Everything runs in-memory, single process, no
external network calls.

## Commands

```bash
uv sync          # install/update deps into .venv
uv run main.py   # run the app (opens http://127.0.0.1:8765)
```

```bash
uv run pytest    # regression tests for the export path (tests/)
```

```bash
uv run ruff check .          # lint
uv run ruff check . --fix    # lint, fixing what can be fixed
uv run ruff format .         # format
```

Ruff is configured in `pyproject.toml`. Docstrings follow the Google
convention; `D1` (missing docstring) is off, since a trivial helper is
allowed a one-line comment instead of a docstring.

## Structure

- `main.py` — thin entry point, delegates to `src.server.main`
- `src/app.py` — the FastAPI app and all `/api/*` routes
- `src/flatten.py` — the export: pages rendered flat, zones painted into the
  pixels, a new document built from them
- `src/background.py` — each page's paper colour, the default fill of a
  delete zone
- `src/ocr.py` — the recognition that reads a flattened page back and lays its
  text over the page as invisible ink
- `src/server.py` — uvicorn bootstrap
- `src/config.py` — every tunable, read from the environment and from `.env`;
  `.env.example` is the committed reference, `.env` itself is ignored
- `src/auth.py` — the login: the credentials, the signed cookie, the lockout
- `src/templates/index.html` + `src/templates/login.html` + `src/static/` — the
  UI (served directly, no templating engine)
- `src/logs.py` — the `"spydf"` logger: stderr always, optional rotating
  file; `log_event()` is the only thing that should write to it

## Conventions

- Everything is in English: UI strings, comments, docstrings, log fields.
- Comments stay short — one or two lines. Anything longer belongs in a
  Google-style docstring (summary line, blank line, `Args:`/`Returns:`/
  `Raises:`). A trivial helper takes a single `#` line above it instead of a
  docstring. Every file opens with a one-line docstring saying what it is for.
- **Flattening is the redaction, and it is not negotiable.** The export never
  edits the source document and never saves it: `src/flatten.py` renders each
  page to a bitmap and builds a *new* document from those bitmaps. That is what
  makes the guarantee cheap and total — a raster carries no text layer, no
  annotation, no form field, no structure tree (`/Alt`, `/ActualText`, `/E`), no
  attachment, no JavaScript, no bookmark, no layer name, no image stream of the
  original and so no Exif, and nothing painted under anything. There is no list
  of carriers to keep up to date, and no "strip this too" option, because there
  is nothing left to strip. Anything that reintroduces objects from the source
  document into the export breaks this and is a bug, not an optimisation.
- The price is paid knowingly: the result is an image, its resolution fixed at
  export time by `EXPORT_DPI`, and its text is only as good as the recognition
  that reads it back. `EXPORT_DPI` is therefore both the quality of the exported
  document and the quality of its index — do not lower it to save bytes without
  saying so.
- A zone is an outline, not a box. `_spans` cuts the outline at each pixel row
  and fills only the runs inside it, under the **non-zero winding rule** — the
  same rule the browser's SVG preview applies, so a self-crossing freehand
  stroke is solid on screen and solid in the export. Over-deleting slightly is
  fine; erasing or covering anything *outside* the outline is not — on a scan
  that shows up as a white bounding box. Both modes follow the same outline: the
  fill and the mosaic's blocks alike.
- The mosaic of a pixelate zone is rendered from the page *before* any zone is
  painted (`_page_image`), so a pixelate zone overlapping a delete zone shows
  the original blurred rather than the cover that is about to land on it.
- The OCR only ever sees the bitmap **after** the zones are painted into it.
  This is the whole reason the text layer is safe: it cannot describe what a
  zone hides, because those pixels were gone before the engine saw them. Never
  move the recognition earlier, and never read the source page for it.
- The invisible layer is written with base-14 Helvetica, which only encodes
  Latin-1. A fragment outside it is skipped rather than written as substitution
  glyphs — a line of question marks in the index is worse than a gap in it.
- The engine is optional. A missing or broken install degrades the export to a
  plain image and says so in the response (`ocr: "unavailable"`); it never fails
  the export. `OCR_MAX_PAGES` bounds a long document, and reports `"partial"`.
- Rendering and recognition are both CPU-bound and both slow — seconds a page.
  `/api/export` calls `flatten` through `run_in_threadpool`, or one export
  freezes every other session.
- A delete zone carries a colour (`color: [r, g, b]`, 0-255) and the fill uses
  it. The default is the page's paper colour, read once per page at
  `/api/open` (`src/background.py`) and handed to the client as `pages[i].bg` —
  a white fill on coloured paper advertises the redaction. It is the *mode* of
  the page's colours, never a mean, and never sampled around the zone: either of
  those turns grey as soon as text is near. Anything unusable falls back to
  white server-side, so an older client keeps working.
- The optional watermark is stamped *after* flattening, not before: it is the
  one thing in the exported file that is real text rather than pixels, and
  stamping it earlier would put it in the bitmap the recognition is given, where
  it would come back as a fragment of the document's own text.
- There is one pane. `--page-w` is still set from JS (`syncPageWidth` /
  `setZoom` in `src/static/app.js`) rather than as a CSS percentage, which would
  resolve against a container that is itself sized by its pages once zoomed in.
- A reload must not cost the work: `saveState`/`restoreState` in
  `src/static/app.js` keep the marking (zones, deleted pages, watermark) in
  `sessionStorage` — it survives Ctrl+F5 and goes when the tab does, where
  localStorage would outlive the sitting for no reason. The document is not
  stored client-side: only its session id is, and `GET /api/session/{sid}` hands
  back the name and the page geometry, or 404s. A 404 clears the marking rather
  than drawing it over nothing. `saveState` is called from `updateStatus`, which
  every zone and page mutation already ends in — keep it that way rather than
  sprinkling calls at each mutation site.
- Documents are held in an in-memory `DOCS` dict keyed by a generated session
  id — there is no persistence; this is meant to run locally, or behind the
  login below.
- The login is off until `SPYDF_AUTH_PASSWORD` is set, and then it is the app's
  own page (`/login`), not the browser's Basic-auth dialog — that dialog cannot
  be styled, cannot say *why* it refused, and cannot be signed out of. One
  middleware in `src/app.py` gates everything: a page request is redirected to
  `/login`, anything else gets a 401, because answering a redirect to a fetch
  hands it a login page where it expected JSON. The client side of that is
  `signedOut()` in `src/static/app.js`, which every call that can meet a 401
  goes through. The cookie is `HttpOnly`/`SameSite=Lax` and its signing key is
  derived from the user name and the password (`src/auth.py`), so changing
  either one invalidates the cookies already out there without a revocation
  list. Nothing about a login attempt but the address and the outcome is
  logged — a password typed into the name field would otherwise land in the log
  in clear.
- The credentials are compared as UTF-8 bytes (`_same` in `src/auth.py`), never
  as `str`: `hmac.compare_digest` raises on a string holding non-ASCII, so an
  accented password would blow up in the check rather than be refused. For the
  same reason `auth_password()` reads the environment raw instead of through
  `env_str`, which strips — a password may begin or end with a space.
- Both HTML templates are served by `src/app.py` with a `{{placeholder}}`
  replaced by markup the server picks from a fixed set (`{{signout}}`,
  `{{error}}`) — there is still no templating engine, and the one value that
  comes from the user (the submitted name, put back in the field) goes through
  `html.escape`.
- The connection/import/export log lines (`src/logs.py`, wired in
  `src/app.py`) never carry identifying content: no uploaded filename unless
  `SPYDF_LOG_FILENAMES=1`, no watermark text (`watermark=true|false` only), no
  word of the text layer (a `fragments` count only), and only an 8-char session
  id prefix, never the full uuid — it is a capability granting
  `/api/download/{key}` access. `SPYDF_LOG_LEVEL` and `SPYDF_LOG_FILE` control
  the rest.
- No magic numbers in `src/app.py` or `src/flatten.py`: a new tunable goes in
  `src/config.py` with a default, and gets a documented line in `.env.example`.
