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
MOSAIC_BLOCKS = 14  # largeur en "gros pixels" d'une zone repixelisee

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


def _mosaic_pixmap(page, rect):
    """Rend la zone en tout petit: en la reposant a sa taille d'origine on
    obtient une mosaique illisible du contenu initial."""
    w, h = max(rect.width, 1.0), max(rect.height, 1.0)
    s = MOSAIC_BLOCKS / max(w, h)
    try:
        pm = page.get_pixmap(matrix=fitz.Matrix(s, s), clip=rect, alpha=False)
    except Exception:
        return None
    return pm if pm.width and pm.height else None


def _purge_annots(page, rects):
    """apply_redactions ne touche ni aux annotations ni aux champs de
    formulaire: une note de correction garde le nom de son auteur et un champ
    garde sa valeur, meme entierement recouverts par une zone. On les supprime
    donc explicitement des qu'ils touchent une zone."""
    for a in list(page.annots()):
        if a.type[0] == fitz.PDF_ANNOT_REDACT:
            continue
        if any(a.rect.intersects(r) for r in rects):
            page.delete_annot(a)
    for w in list(page.widgets()):
        if any(w.rect.intersects(r) for r in rects):
            page.delete_widget(w)


def _rename_layers(doc):
    """Le nom d'un calque ("Copie de Jean Dupont") survit dans /OCProperties
    meme quand son contenu a ete redige."""
    for i, xref in enumerate(doc.get_ocgs() or {}, 1):
        doc.xref_set_key(xref, "Name", fitz.get_pdf_str(f"calque {i}"))


def _scrub_document(doc):
    """Traces d'identite qui ne vivent pas dans le contenu des pages et que la
    redaction laisse donc intactes: metadonnees, XMP, signets (souvent le nom
    de l'eleve), pieces jointes, JavaScript, liens, reponses de formulaire."""
    doc.set_metadata({})
    for step in (
        lambda: doc.del_xml_metadata(),
        lambda: doc.set_toc([]),
        lambda: _rename_layers(doc),
        lambda: doc.scrub(redactions=False, clean_pages=False),
    ):
        try:
            step()
        except Exception:
            pass


def _verify(out: bytes, zones_by_page, page_map):
    """Relecture du PDF reellement produit (et non du document en memoire):
    reste-t-il du texte, une annotation ou un champ dans les zones ?"""
    leaks = []
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for pno, zs in zones_by_page.items():
            new_no = page_map.get(pno)
            if new_no is None or not 0 <= new_no < chk.page_count:
                continue
            page, rects = chk[new_no], [z["rect"] for z in zs]
            for w in page.get_text("words"):
                if any(fitz.Rect(w[:4]).intersects(r) for r in rects):
                    leaks.append({"page": new_no + 1, "kind": "texte", "text": w[4]})
            for a in page.annots():
                if any(a.rect.intersects(r) for r in rects):
                    leaks.append({"page": new_no + 1, "kind": "annotation",
                                  "text": a.info.get("title") or a.type[1]})
            for w in page.widgets():
                if any(w.rect.intersects(r) for r in rects):
                    leaks.append({"page": new_no + 1, "kind": "champ",
                                  "text": w.field_name or "?"})
    finally:
        chk.close()
    return leaks


@app.post("/api/export")
async def api_export(payload: dict):
    sid = payload.get("sid")
    entry = DOCS.get(sid)
    if not entry:
        raise HTTPException(404, "session inconnue")

    # {"3": [{"points": [[x,y], ...], "mode": "delete"|"pixelate"}, ...]} en coordonnees PDF.
    # "points" est le contour de la zone (rectangle = 4 coins, mais aussi
    # polygone ou trace libre) ; la suppression de texte se fait sur le
    # rectangle englobant, le cache visuel blanc suit le contour exact.
    raw = payload.get("zones") or {}
    zones_by_page: dict[int, list[dict]] = {}
    for k, v in raw.items():
        if not v:
            continue
        parsed = []
        for z in v:
            pts = z.get("points") or []
            if len(pts) < 2:
                continue
            points = [fitz.Point(x, y) for x, y in pts]
            rect = fitz.Rect(points[0], points[0])
            for p in points:
                rect.include_point(p)
            parsed.append({
                "points": points,
                "rect": rect,
                "mode": "pixelate" if z.get("mode") == "pixelate" else "delete",
            })
        if parsed:
            zones_by_page[int(k)] = parsed

    deleted_pages = {int(p) for p in (payload.get("deleted_pages") or [])}
    # pas besoin de rediger une page qui va disparaitre entierement
    zones_by_page = {p: zs for p, zs in zones_by_page.items() if p not in deleted_pages}

    if not zones_by_page and not deleted_pages:
        raise HTTPException(400, "aucune zone ni page supprimee")

    strip_meta = bool(payload.get("strip_meta", True))

    doc = fitz.open(stream=entry["bytes"], filetype="pdf")

    if len(deleted_pages) >= doc.page_count:
        doc.close()
        raise HTTPException(400, "impossible de supprimer toutes les pages")

    for pno, zs in zones_by_page.items():
        page = doc[pno]
        delete_zs = [z for z in zs if z["mode"] == "delete"]
        pixel_zs = [z for z in zs if z["mode"] == "pixelate"]

        # 1. pour les zones "repixeliser", on capture d'abord une version
        # fortement sous-echantillonnee du contenu d'origine (illisible), avant
        # toute redaction. C'est cette mosaique qui sera reposee ensuite.
        mosaics = []
        for z in pixel_zs:
            pm = _mosaic_pixmap(page, z["rect"])
            if pm:
                mosaics.append((z["rect"], pm))

        # 2. redaction reelle de TOUTES les zones: le texte est supprime, les
        # pixels des images couvertes sont detruits (pas seulement masques) et
        # les traces vectorielles qui touchent une zone sont retirees.
        # LINE_ART_REMOVE_IF_TOUCHED est indispensable: par defaut PyMuPDF ne
        # retire qu'un trace *entierement* contenu dans la zone, si bien qu'une
        # signature qui deborde survivait intacte sous le cache blanc.
        rects = [z["rect"] for z in zs]
        _purge_annots(page, rects)
        for r in rects:
            page.add_redact_annot(r)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)

        # 3. zones "supprimer": cache blanc suivant le contour exact.
        if delete_zs:
            shape = page.new_shape()
            for z in delete_zs:
                shape.draw_polyline(z["points"])
                shape.finish(fill=(1, 1, 1), color=(1, 1, 1), closePath=True)
            shape.commit()

        # 4. zones "repixeliser": on repose la mosaique par-dessus le vide.
        for rect, pm in mosaics:
            page.insert_image(rect, pixmap=pm)

    # numero de page d'origine -> numero dans le document exporte
    page_map = {p: p - sum(1 for d in deleted_pages if d < p)
                for p in zones_by_page}

    if deleted_pages:
        doc.delete_pages(sorted(deleted_pages))

    if strip_meta:
        _scrub_document(doc)

    # garbage=4 + clean: les objets devenus orphelins (anciennes images, flux de
    # contenu remplaces) sont reellement retires du fichier, pas seulement
    # dereferences comme le ferait une sauvegarde incrementale.
    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()

    leaks = _verify(out, zones_by_page, page_map)

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
