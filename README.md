# ExamAnonymizer

Local webapp to redact PDF exams: draw rectangles over regions to remove
(names, student IDs, ...), and export a PDF where the underlying text/image
objects are actually deleted — not just covered by a drawn box. Everything
runs locally and in memory; nothing is uploaded anywhere.

## Usage

```bash
uv run main.py
```

This opens `http://127.0.0.1:8765` in your browser. Draw rectangles over the
zones to redact, then export. The exporter also reports any leftover text
fragments still intersecting a redacted zone, so you can double check nothing
leaked.

Set `PORT` to change the listening port (default `8765`).

## Development

```bash
uv sync
uv run main.py
```

## Project layout

- `main.py` — entry point
- `src/app.py` — FastAPI routes (open/render/export/download)
- `src/server.py` — server bootstrap (opens browser, runs uvicorn)
- `src/templates/index.html` — page shell
- `src/static/` — CSS and client-side JS

## Docker

```bash
docker build -t examanonymizer .
docker run --rm -p 8765:8765 examanonymizer
```
