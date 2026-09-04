# SpyDF

Local webapp to redact PDF exams: draw zones over regions to remove (names,
student IDs, ...), and export a document whose pages have been rendered flat to
an image, with those zones painted into the pixels and the rest read back by OCR
so it stays searchable. What is not visible on a page is not in the export —
there is no object left in the file to carry it. Everything runs locally and in
memory; nothing is uploaded anywhere.

![The document, with a zone drawn over a name](docs/screenshot.png)

## Install

SpyDF is run with [uv](https://docs.astral.sh/uv/), which handles the Python
version and the dependencies for you. If you do not have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # macOS, Linux
```

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

It is also in most package managers — `brew install uv`, `pipx install uv`,
`winget install astral-sh.uv`. Restart your shell afterwards so `uv` is on your
`PATH`, then:

```bash
git clone <this repo> && cd spydf
uv sync
```

`uv sync` creates `.venv` and installs everything pinned in `uv.lock`. No
`pip install`, no manual virtualenv, no system Python to match.

## Usage

```bash
uv run main.py
```

This opens `http://127.0.0.1:8765` in your browser. Drop a PDF in — or click
the drop zone to pick one — mark the zones, then export.

### Zones

Three shapes: **rectangle**, **polygon** (click the vertices, double-click to
close) and **freehand**. Drag a zone to move it, its handles to resize it.

What disappears follows the **outline you drew**, whatever its shape. The page
is a bitmap by the time a zone is applied, so the outline is cut at each row of
pixels and only the runs inside it are filled — a polygon or a freehand loop
erases its own shape, to the pixel, and nothing around it. That matters most on
a scan, where the page is one single image: filling the bounding box instead
would leave a white rectangle announcing where something was.

Each zone has a mode, switched from its right-click menu:

- **Delete** — the area is filled, and the pixels under it are gone.
- **Pixelate** — the area is replaced by a genuine downsample of itself, an
  unreadable mosaic taken before anything else is painted.

The fill of a **delete** zone is painted in the colour of the paper it was
drawn on: the average of the pixels its outline passes over — the outline, not
the inside, which is the content about to go. On a coloured or greyish scan a
white patch is itself a mark, it says where something was; matching the paper
leaves nothing to notice. The zone's right-click menu holds that colour as
three **RGB** numbers and a **pipette** that takes a colour from a click
anywhere on the page. The pipette reads the rendered page, not the screen, so
clicking on a zone samples the paper underneath rather than the fill on top.

The closing double-click of the polygon is taken before the browser gets it: it
would otherwise start a word selection on the nearest text — which a
translation or dictionary extension then picks up on a word you never meant to
select.

### Reloading the page

Reloading — `F5`, `Ctrl+F5`, a misplaced shortcut — used to throw away
everything and ask for the file again, although the document itself had never
left the server: the page simply held the only copy of the session id, of the
zones and of the page geometry.

It now picks them back up. The zones, the deleted pages and the watermark are
kept in the tab's own storage, and the document is asked back from the server by
its session id. If that session is gone — expired after
`SPYDF_SESSION_TTL`, or the server was restarted — the marking goes with it and
you land back on the drop zone, rather than drawing zones over nothing.

There is nothing to click and nothing saved to disk: it lives in
`sessionStorage`, which survives a reload and goes when the tab closes.

### Pages

The trash button on a page marks it for deletion; the page is dropped from the
exported document entirely.

### Zoom

`Ctrl` with the wheel, `Ctrl` and `+` / `−` / `0`, or the buttons in the
toolbar, from 25% to 500%. Zooming under the cursor keeps the point under it
still.

Once a page is wider than its pane, drag it with the **middle button** or hold
**Space** and drag with the left one; the arrows, `Page Up`/`Page Down`,
`Home` and `End` move around too.

Pages are re-rendered server-side at the new size once the zoom settles, so
zooming in gives you more detail rather than a bigger blur.

### What the export actually is

Every page is rendered to a bitmap, the zones are painted into that bitmap, and
a new document is built holding those bitmaps and nothing else. The original
document is never edited and never saved.

That single step is the whole guarantee. A raster has no text layer to leave a
name in, no annotation keeping its author, no form field keeping its answer, no
structure tree carrying `/Alt` or `/ActualText`, no attachment, no JavaScript,
no bookmark ("Jean Dupont's copy"), no layer name, and no image stream — so no
**Exif** either: no camera make and serial, no GPS fix, and no embedded
thumbnail, which on a phone photograph is a complete small copy of the picture
*before* anything was drawn on it. None of it is stripped, because none of it is
ever carried across.

It also settles the case this tool was built for. A white box drawn over a scan
in a reader or a phone's markup tool hides nothing: the pixels are all still
there, one object below. Here the zone is filled into the bitmap itself — the
pixels stop existing, and there is no "under" left to look at.

The zone follows the outline you drew, not its bounding box: each row of pixels
is filled only between the outline's own crossings, so a polygon or a freehand
loop erases its shape and leaves everything around it untouched. A **pixelate**
zone lays back a heavily downsampled copy of what was there, cut to the same
outline.

### Re-indexing

A page of pixels is unsearchable, so the export reads each flattened page back
with OCR and writes the recognised text over it as invisible ink: nothing
changes on screen, but the words can be selected, searched and indexed where
they are drawn.

The recognition only ever sees the bitmap **after** the zones are painted into
it. It therefore cannot report what a zone hides — those pixels were gone before
the engine looked. The status bar says how it went: every page indexed, only
some of them (a very long document stops at `SPYDF_OCR_MAX_PAGES`), or none at
all when no engine is installed, in which case the export is still produced,
just as a plain image.

Two limits worth knowing. The exported text is only as good as the recognition,
and its resolution is fixed at export time by `SPYDF_EXPORT_DPI` (200 by
default) — a page is an image now, so zooming past that shows pixels rather than
sharper letters. And the invisible layer is written in a base-14 font, which
covers Latin-1: a fragment outside it is skipped rather than written as
question marks.

### Keyboard

`Ctrl+Z` / `Ctrl+Y` undo and redo. `Tab` moves between zones, `Enter` opens
the selected zone's menu, `Delete` removes it, `Esc` cancels. `Ctrl` with
`+` / `−` / `0` zooms; arrows, `Page Up`/`Page Down`, `Home`/`End` and `Space`
held with a drag move around a zoomed page.

### Watermark

The field next to **Export** stamps a line of text diagonally across every
exported page. A preview appears on the pages as you type, so you can see where
it lands. It is stamped *after* the pages are flattened, so it is the one thing
in the exported file that is real text rather than pixels — and so it never ends
up in the bitmap the recognition reads, where it would come back as a fragment
of the document's own text.

### Signing in

With no password configured — the default, and what a run on `127.0.0.1`
wants — SpyDF opens straight onto the workspace. Set `SPYDF_AUTH_PASSWORD` and
it asks for it first, on a page of its own rather than through the browser's
Basic-auth dialog: the app's own card, the error spelled out in place, the user
name kept after a wrong password, and a **sign out** button at the end of the
header once you are in.

A login lasts `SPYDF_AUTH_TTL` seconds (12 h by default) and rides in one
cookie, `HttpOnly` and `SameSite=Lax`, `Secure` as soon as the request arrives
over HTTPS. Nothing else about you is stored, on the server or in the browser.
If the login expires while a tab is still open, the next call it makes hands
you back to the login page rather than failing in the status bar — the marking
in the tab survives, and so does the document, as long as its own session has
not expired.

Failed attempts are counted per address: `SPYDF_AUTH_MAX_TRIES` of them and
that address waits `SPYDF_AUTH_LOCKOUT` seconds before it may try again. The
log records the address and the outcome, never the submitted name or password.

## Development

```bash
uv sync          # installs dev dependencies too
uv run main.py
uv run pytest    # regression tests for the export path
uv run ruff check .    # lint
uv run ruff format .   # format
```

The tests build a PDF carrying one of each class of identifying trace — text,
annotation and its author, form field and its value, bookmark, attachment, layer
name, metadata, XMP — export it through the real routes, and assert none
survives in the raw bytes of the result. Others work in pixels: a zone's area is
really filled, a polygon does not square off into its bounding box, and what
lies outside an outline is byte-for-byte what it was.

## Project layout

- `main.py` — entry point
- `src/app.py` — FastAPI routes (open/render/export/download)
- `src/config.py` — every tunable, read from the environment and `.env`
- `src/auth.py` — the login: credentials, signed cookie, lockout
- `src/logs.py` — the audit log (connection, import, export)
- `src/flatten.py` — the export: pages rendered flat, zones painted into them
- `src/ocr.py` — the flattened page read back, its text laid over it invisibly
- `src/server.py` — server bootstrap (opens browser, runs uvicorn)
- `src/templates/index.html` — page shell
- `src/static/app.js` — pages, zones, export
- `tests/` — regression tests

## Running it

### Locally

```bash
uv sync
uv run main.py
```

Binds `127.0.0.1:8765` and opens your browser.

### Configuration

Every setting lives in `src/config.py` with a default, so SpyDF runs with no
configuration at all. To change one, set an environment variable or drop a
`.env` file next to `pyproject.toml`:

```bash
cp .env.example .env
```

`.env.example` lists every variable at its default value, so an untouched copy
changes nothing. Real environment variables win over the file, which keeps a
container's `environment:` block in charge.

**Server**

| Variable | Default | What it does |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | interface to bind; the Docker image sets `0.0.0.0` |
| `PORT` | `8765` | port to bind |

**Login** — off entirely while `SPYDF_AUTH_PASSWORD` is empty.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_AUTH_USER` | `admin` | the one account name the login page accepts |
| `SPYDF_AUTH_PASSWORD` | *(unset)* | in clear, or as `sha256:<hex>`; empty means no login page at all |
| `SPYDF_AUTH_SECRET` | *(unset)* | signs the login cookie; unset draws a fresh one per process, so a restart signs everybody out |
| `SPYDF_AUTH_TTL` | `43200` | seconds one login stays valid |
| `SPYDF_AUTH_MAX_TRIES` | `10` | failures from one address before it has to wait |
| `SPYDF_AUTH_LOCKOUT` | `300` | length of that wait, and of the window the failures are counted in |

To keep the password out of the environment in clear:

```bash
python -c "import hashlib,getpass;print('sha256:'+hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

Changing the user name or the password invalidates every cookie already issued,
since the cookie key is derived from both.

The app takes the password exactly as it arrives — spaces at either end, accents,
anything — but a `.env` file read by Docker Compose does not, and that is where an
awkward password quietly turns into a different one:

| In the `.env` | What the container receives |
| --- | --- |
| `PW=abc$def` | `abc` — `$def` is read as a variable and substituted empty |
| `PW=abc$$def` | `abc` — escaping does not survive the second pass either |
| `PW=abc #def` | `abc` — a space then `#` starts a comment |
| `PW=abc   ` | `abc` — trailing spaces go |
| `PW="a\tb"` | `a<TAB>b` — quotes are unwrapped and escapes expanded |

`@ ! % & * ( ) { } [ ] ' :` and accents all pass through untouched; `$` is the one
character with no working escape. If your password contains one, configure it as
`sha256:<hex>` instead — the digest is plain hex, so nothing in the chain can
mangle it, and you still type the real password on the login page.

To see what actually reached the app: `docker exec <container> printenv SPYDF_AUTH_PASSWORD`.
The startup log line also says `login=on` or `login=off`, which tells you whether
the variable arrived at all.

**Sessions** — documents live in memory only, never on disk.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_MAX_UPLOAD_BYTES` | `209715200` | upload cap, 200 MB |
| `SPYDF_SESSION_TTL` | `7200` | seconds a forgotten document may stay in RAM |
| `SPYDF_MAX_SESSIONS` | `32` | how many documents are held at once |

**Page rendering** — a PDF is vector art, so a resolution has to be chosen; the
app renders what the screen actually shows.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_RENDER_ZOOM` | `4.0` | zoom used when the client asks for no width |
| `SPYDF_MIN_ZOOM` | `1.5` | lower bound when it does |
| `SPYDF_MAX_ZOOM` | `8.0` | upper bound; a memory guard rail, 8x on A4 is ~128 Mpx |

**Redaction**

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_MOSAIC_BLOCKS` | `14` | width of a pixelated zone in "big pixels"; lower is coarser |

**Export** — the pages are flattened, so this is the quality of the result and
of what the recognition is given to index it.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_EXPORT_DPI` | `200` | resolution the pages are rendered at; A4 becomes about 1654 x 2339 px |
| `SPYDF_EXPORT_JPEG_QUALITY` | `80` | JPEG quality of the flattened pages; `0` keeps them lossless, five or six times heavier on a scan |

**Re-indexing (OCR)** — optional: with no engine installed the export is still
produced, without a text layer.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_OCR_MAX_SIDE` | `2400` | longest side a page bitmap is shrunk to before recognition |
| `SPYDF_OCR_MIN_SCORE` | `0.5` | below this confidence a fragment is noise, not text |
| `SPYDF_OCR_MAX_PAGES` | `100` | how many pages are read at most; beyond it the export reports itself partially indexed |
| `SPYDF_OCR_SNIPPET` | `200` | longest fragment kept; one fragment is a line, never a page |

**Watermark**

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_WATERMARK_MAX_LEN` | `80` | longest accepted watermark |
| `SPYDF_WATERMARK_MIN_SIZE` | `8` | smallest font size, in points |
| `SPYDF_WATERMARK_DIAGONAL_RATIO` | `0.78` | share of the page diagonal the text aims to span — an aim only, since it is also clamped to fit the page |
| `SPYDF_WATERMARK_FONT` | `helv` | must be a base-14 font, resolved without a font file |

**Logging** — connection, import and export always go to stderr.

| Variable | Default | What it does |
| --- | --- | --- |
| `SPYDF_LOG_LEVEL` | `INFO` | verbosity |
| `SPYDF_LOG_FILE` | *(unset)* | also write a rotating log file |
| `SPYDF_LOG_FILE_MAX_BYTES` | `1048576` | size at which that file rotates |
| `SPYDF_LOG_FILE_BACKUPS` | `3` | how many rotations are kept |
| `SPYDF_LOG_FILENAMES` | `0` | opt in to logging uploaded file names |
| `SPYDF_SID_LOG_LEN` | `8` | characters of a session id that may reach a log line |
| `SPYDF_UA_MAX_LEN` | `120` | user-agent is client-supplied, so it is bounded |

The log is written with the same care as the export. An uploaded file name is
identifying — real ones look like `copie_jean_dupont.pdf` — so it is recorded
only under `SPYDF_LOG_FILENAMES=1`. Neither the watermark text nor a word of
the document's own text is logged at any setting: only how many text fragments
were written, and whether a watermark was used.
A whole session id grants access to `/api/download/{key}`, so only its first
few characters are logged.

### Locally, in Docker

```bash
docker build -t spydf .
docker run --rm -p 8765:8765 spydf
```

The image sets `HOST=0.0.0.0` so the app is reachable from outside the
container; without it uvicorn would bind to the container's own loopback and
the published port would answer nothing.

It also installs `libxcb1`, `libgl1` and `libglib2.0-0`. The recognition engine
pulls in OpenCV, which links against those even though nothing is ever
displayed, and the slim base image does not carry them. Without them `import
cv2` raises, the engine is unavailable, and every export goes out as a plain
image with no text layer at all — a failure that looks like a broken export
rather than a missing package. The server says which it is: it logs
`event=ocr_unavailable error=...` once, the first time the engine is asked for.

### Deployed (Dokploy)

`docker-compose.yml` is the deploy descriptor, built from the same Dockerfile.
Point a Dokploy **Compose** application at this repo; it runs

```bash
docker compose -f ./docker-compose.yml up -d --build
```

The service publishes `8765:8765`, so the app answers on the host directly at
`http://<server>:8765`. It also joins `dokploy-network`, which lets you attach
a domain from Dokploy's **Domains** tab (service `spydf`, port `8765`) and go
through Traefik instead. That network is declared `external` because Dokploy's
installer creates it — on a machine without it `docker compose up` fails, so
locally use the plain `docker run` above.

The app has **no persistence** (documents live in an in-memory dict, keyed by
session id) and, until you give it a password, **no authentication** either.
Set `SPYDF_AUTH_PASSWORD` in the compose `environment:` block and the login
above covers every route — including the published `8765`, which bypasses
Traefik and so is *not* covered by auth middleware attached to the router.
Set `SPYDF_AUTH_SECRET` too, or every redeploy signs everyone out. Firewalling
the port, or switching the mapping to `127.0.0.1:8765:8765` and reaching it
over an SSH tunnel, is still worth doing on a public network.
