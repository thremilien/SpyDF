# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

SpyDF — a local FastAPI webapp for redacting PDFs (e.g. exam scans): the user draws
rectangles over regions in the browser, and export uses PyMuPDF's
`apply_redactions` to actually delete the underlying text/image objects in
those zones — not just draw a box over them. Everything runs in-memory,
single process, no external network calls.

## Commands

```bash
uv sync          # install/update deps into .venv
uv run main.py   # run the app (opens http://127.0.0.1:8765)
```

```bash
uv run pytest    # regression tests for the redaction path (tests/)
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
- `src/server.py` — uvicorn bootstrap; reads `HOST`/`PORT` env vars
- `src/templates/index.html` + `src/static/` — the UI (served directly, no templating engine)
- `src/logs.py` — the `"spydf"` logger: stderr always, optional rotating
  file via `SPYDF_LOG_FILE`, level via `SPYDF_LOG_LEVEL`; `log_event()` is
  the only thing that should write to it

## Conventions

- UI-facing strings and comments in the app are in French; keep new
  user-visible text consistent with that unless told otherwise.
- Redaction correctness matters more than convenience here: `/api/export`
  re-opens the exported PDF and checks for leftover text words inside the
  redacted rects (`leaks`), returned to the client as a warning. Don't remove
  this check when touching the export path.
- A zone is an outline, not a box. PyMuPDF only redacts rectangles, so
  `_zone_rects` cuts a non-rectangular zone into horizontal strips that follow
  the drawn shape. Over-deleting slightly is fine; erasing or covering
  anything *outside* the outline is not — on a scan that shows up as a white
  bounding box. The white cover, the mosaic (masked by `_shape_mask`) and the
  inspector's status all follow the same outline.
- Documents are held in an in-memory `DOCS` dict keyed by a generated session
  id — there is no persistence and no auth; this is meant to run locally only.
- `src/probe.py` reads the document once, when it is opened, and the inspector
  renders that snapshot. Nothing mutates the session document today, so it
  stays accurate; anything that does (reloading the file, undoing a redaction
  after export) has to re-probe or the pane goes stale silently.
- The connection/import/export log lines (`src/logs.py`, wired in
  `src/app.py`) never carry identifying content: no uploaded filename unless
  `SPYDF_LOG_FILENAMES=1`, no leak text (leak COUNT only), no watermark text
  (`watermark=true|false` only), and only an 8-char session id prefix, never
  the full uuid — it is a capability granting `/api/download/{key}` access.
  `SPYDF_LOG_LEVEL` and `SPYDF_LOG_FILE` control the rest.
- The optional watermark is stamped *after* `_verify` runs, not before: a
  diagonal watermark crosses redacted zones by design, so verifying against
  the watermarked bytes would report it as a leak on every page — and
  filtering leaks that merely match the watermark text would risk hiding a
  real leak that happens to say the same thing.
