"""FastAPI app: routes for opening, rendering, redacting and downloading PDFs."""

import mimetypes
import os
import uuid
from pathlib import Path

import fitz
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

PACKAGE_DIR = Path(__file__).parent
RENDER_ZOOM = 4.0  # zoom de repli si le client ne demande pas de largeur
MIN_ZOOM = 1.5
MAX_ZOOM = 8.0     # garde-fou memoire: 8x sur A4 = ~128 Mpx

# sur certains systemes .js est devine comme application/javascript, qui ne
# recoit pas de charset: les accents du JS arrivent alors casses dans l'UI.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

app = FastAPI()
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

DOCS: dict[str, dict] = {}  # sid -> {"bytes": ..., "name": ...}


@app.post("/api/open")
async def api_open(file: UploadFile = File(...)):
    data = await file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = [{"w": p.rect.width, "h": p.rect.height,
                  "x0": p.rect.x0, "y0": p.rect.y0} for p in doc]
        doc.close()
    except Exception as e:
        raise HTTPException(400, f"PDF illisible: {e}")

    sid = uuid.uuid4().hex
    DOCS[sid] = {"bytes": data, "name": file.filename or "document.pdf"}
    return {"sid": sid, "name": DOCS[sid]["name"], "pages": pages}


@app.get("/api/page/{sid}/{n}")
def api_page(sid: str, n: int, w: int = 0):
    """`w` = largeur voulue en pixels ecran reels (CSS x devicePixelRatio).
    Un PDF est vectoriel: il n'y a pas de "qualite native", on choisit une
    resolution. On rend donc exactement ce que l'ecran affiche, plutot qu'un
    zoom fixe qui serait soit flou, soit du gaspillage."""
    entry = DOCS.get(sid)
    if not entry:
        raise HTTPException(404, "session inconnue")
    doc = fitz.open(stream=entry["bytes"], filetype="pdf")
    if not 0 <= n < len(doc):
        doc.close()
        raise HTTPException(404, "page hors limites")
    page = doc[n]
    if w > 0 and page.rect.width:
        zoom = min(max(w / page.rect.width, MIN_ZOOM), MAX_ZOOM)
    else:
        zoom = RENDER_ZOOM
    pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pm.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/export")
async def api_export(payload: dict):
    sid = payload.get("sid")
    entry = DOCS.get(sid)
    if not entry:
        raise HTTPException(404, "session inconnue")

    # {"3": [[x0,y0,x1,y1], ...]} en coordonnees PDF
    raw = payload.get("rects") or {}
    rects = {int(k): [fitz.Rect(*r) for r in v] for k, v in raw.items() if v}
    if not rects:
        raise HTTPException(400, "aucune zone selectionnee")

    pixelate = bool(payload.get("pixelate", True))
    strip_meta = bool(payload.get("strip_meta", True))
    mode = fitz.PDF_REDACT_IMAGE_PIXELS if pixelate else fitz.PDF_REDACT_IMAGE_NONE

    doc = fitz.open(stream=entry["bytes"], filetype="pdf")
    for pno, rs in rects.items():
        page = doc[pno]
        for r in rs:
            page.add_redact_annot(r, fill=(1, 1, 1))
        page.apply_redactions(images=mode)

    if strip_meta:
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass

    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()

    # verification: reste-t-il du texte dans les zones ?
    leaks = []
    chk = fitz.open(stream=out, filetype="pdf")
    for pno, rs in rects.items():
        for w in chk[pno].get_text("words"):
            wr = fitz.Rect(w[:4])
            if any(wr.intersects(r) for r in rs):
                leaks.append({"page": pno + 1, "text": w[4]})
    chk.close()

    base = os.path.splitext(entry["name"])[0]
    key = uuid.uuid4().hex
    DOCS[key] = {"bytes": out, "name": f"{base}_redacted.pdf"}
    return JSONResponse({
        "download": f"/api/download/{key}",
        "filename": f"{base}_redacted.pdf",
        "leaks": leaks[:20],
        "leak_count": len(leaks),
    })


@app.get("/api/download/{key}")
def api_download(key: str):
    entry = DOCS.get(key)
    if not entry:
        raise HTTPException(404, "expire")
    return Response(
        entry["bytes"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{entry["name"]}"'},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return (PACKAGE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
