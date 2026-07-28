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

There is no test suite and no linter configured yet.

## Structure

- `main.py` — thin entry point, delegates to `src.server.main`
- `src/app.py` — the FastAPI app and all `/api/*` routes
- `src/server.py` — uvicorn bootstrap; reads `HOST`/`PORT` env vars
- `src/templates/index.html` + `src/static/` — the UI (served directly, no templating engine)

## Conventions

- UI-facing strings and comments in the app are in French; keep new
  user-visible text consistent with that unless told otherwise.
- Redaction correctness matters more than convenience here: `/api/export`
  re-opens the exported PDF and checks for leftover text words intersecting
  redacted rects (`leaks`), returned to the client as a warning. Don't remove
  this check when touching the export path.
- Documents are held in an in-memory `DOCS` dict keyed by a generated session
  id — there is no persistence and no auth; this is meant to run locally only.
