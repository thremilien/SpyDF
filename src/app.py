"""FastAPI app: routes for opening, rendering, redacting and downloading PDFs."""

import mimetypes
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

import fitz
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from src.probe import inspect_document

PACKAGE_DIR = Path(__file__).parent
RENDER_ZOOM = 4.0  # zoom de repli si le client ne demande pas de largeur
MIN_ZOOM = 1.5
MAX_ZOOM = 8.0     # garde-fou memoire: 8x sur A4 = ~128 Mpx
MOSAIC_BLOCKS = 14  # largeur en "gros pixels" d'une zone repixelisee
STRIP_HEIGHT = 2.0  # hauteur d'une bande de redaction, en points PDF
MAX_STRIPS = 200    # garde-fou: une zone tres haute ne genere pas mille rects
MASK_MAX_PX = 240   # resolution du masque qui decoupe une mosaique au contour

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
SESSION_TTL = 2 * 3600   # un document oublie ne doit pas rester en RAM
MAX_SESSIONS = 32

# sur certains systemes .js est devine comme application/javascript, qui ne
# recoit pas de charset: les accents du JS arrivent alors casses dans l'UI.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

app = FastAPI()
app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

DOCS: dict[str, dict] = {}  # sid -> {"bytes": ..., "name": ..., "ts": ...}


def _sweep():
    """Rien n'est persiste, mais un PDF garde en RAM tout ce qu'on vient d'en
    retirer: on ne laisse pas trainer les sessions oubliees."""
    now = time.time()
    for k in [k for k, v in DOCS.items() if now - v["ts"] > SESSION_TTL]:
        DOCS.pop(k, None)
    while len(DOCS) > MAX_SESSIONS:
        DOCS.pop(min(DOCS, key=lambda k: DOCS[k]["ts"]), None)


def _put(name: str, data: bytes) -> str:
    _sweep()
    key = uuid.uuid4().hex
    DOCS[key] = {"bytes": data, "name": name, "ts": time.time()}
    return key


def _get(key: str) -> dict:
    _sweep()
    entry = DOCS.get(key)
    if not entry:
        raise HTTPException(404, "session inconnue ou expiree")
    return entry


def _safe_filename(name: str) -> str:
    """Le nom vient du fichier depose: il ne doit ni casser l'en-tete
    Content-Disposition ni ramener un chemin."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name)).strip(" .")
    return name[:100] or "document.pdf"


@app.post("/api/open")
async def api_open(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "fichier vide")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "fichier trop volumineux (200 Mo maximum)")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        # un PDF chiffre s'ouvre, mais ses pages sont illisibles: sans mot de
        # passe on ne pourrait ni rendre ni rediger quoi que ce soit.
        if doc.needs_pass:
            doc.close()
            raise HTTPException(400, "PDF protege par mot de passe")
        pages = [{"w": p.rect.width, "h": p.rect.height,
                  "x0": p.rect.x0, "y0": p.rect.y0} for p in doc]
        doc.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"PDF illisible: {e}")

    name = _safe_filename(file.filename or "document.pdf")
    sid = _put(name, data)
    return {"sid": sid, "name": name, "pages": pages}


@app.get("/api/page/{sid}/{n}")
def api_page(sid: str, n: int, w: int = 0):
    """`w` = largeur voulue en pixels ecran reels (CSS x devicePixelRatio).
    Un PDF est vectoriel: il n'y a pas de "qualite native", on choisit une
    resolution. On rend donc exactement ce que l'ecran affiche, plutot qu'un
    zoom fixe qui serait soit flou, soit du gaspillage."""
    entry = _get(sid)
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


@app.get("/api/inspect/{sid}")
def api_inspect(sid: str):
    """Tout ce que le document transporte sans l'afficher: couche de texte,
    metadonnees, signets, annotations, champs, pieces jointes, calques, liens,
    JavaScript. Lecture seule; c'est l'export qui decide de ce qui disparait."""
    entry = _get(sid)
    return JSONResponse(inspect_document(entry["bytes"]))


def _is_box(points, rect) -> bool:
    """Une zone rectangulaire n'a rien a decouper: son contour *est* son
    rectangle englobant."""
    return len(points) == 4 and all(
        (abs(p.x - rect.x0) < 0.01 or abs(p.x - rect.x1) < 0.01)
        and (abs(p.y - rect.y0) < 0.01 or abs(p.y - rect.y1) < 0.01)
        for p in points)


def _spans(points, y):
    """Intervalles horizontaux interieurs au contour a l'ordonnee y.

    Regle non-nulle (et non pair-impair): c'est celle qu'appliquent deja le
    cache blanc pose par PyMuPDF et l'apercu SVG du navigateur. Un trace libre
    qui se recoupe est donc plein ici comme il l'est a l'ecran."""
    xs = []
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        if a.y == b.y:
            continue
        top, bot = (a, b) if a.y < b.y else (b, a)
        if not top.y <= y < bot.y:
            continue
        xs.append((a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y), 1 if b.y > a.y else -1))
    xs.sort()
    out, wind, start = [], 0, 0.0
    for x, direction in xs:
        if wind == 0:
            start = x
        wind += direction
        if wind == 0 and x > start:
            out.append((start, x))
    return out


def _merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _zone_rects(points, rect):
    """Les rectangles a rediger pour une zone donnee.

    PyMuPDF ne sait rediger que des rectangles. Rediger le rectangle englobant
    d'un polygone ne se contente pas de supprimer trop de texte: sur un examen
    scanne la page est une image, et PDF_REDACT_IMAGE_PIXELS en detruit alors
    tous les pixels, si bien que le rectangle entier vire au blanc sous un
    cache qui, lui, suivait le contour. On decoupe donc la zone en bandes
    horizontales qui epousent le trace.

    Les bandes debordent legerement le contour (union des trois lignes de
    balayage de chaque bande): sur-effacer un peu est acceptable, laisser
    survivre du contenu a l'interieur du trace ne l'est pas."""
    if _is_box(points, rect):
        return [rect]
    n = max(1, min(MAX_STRIPS, int(rect.height / STRIP_HEIGHT) + 1))
    h = rect.height / n
    out = []
    for i in range(n):
        y0 = rect.y0 + i * h
        y1 = y0 + h
        sampled = [s for y in (y0, y0 + h / 2, min(y1, rect.y1 - 1e-6))
                   for s in _spans(points, y)]
        for a, b in _merge(sampled):
            if b - a > 0.05:
                out.append(fitz.Rect(a, y0, b, y1))
    # contour degenere (aire nulle): mieux vaut son rectangle que rien du tout
    return out or [rect]


def _shape_mask(points, rect):
    """Masque alpha du contour, a la taille du rectangle englobant: repose sur
    la mosaique, il l'empeche de deborder du trace."""
    if rect.width <= 0 or rect.height <= 0:
        return None
    s = min(MASK_MAX_PX / max(rect.width, rect.height), 4.0)
    w = max(1, min(MASK_MAX_PX, round(rect.width * s)))
    h = max(1, min(MASK_MAX_PX, round(rect.height * s)))
    buf = bytearray(w * h)
    for j in range(h):
        y = rect.y0 + (j + 0.5) * rect.height / h
        for a, b in _spans(points, y):
            i0 = max(0, int((a - rect.x0) / rect.width * w))
            i1 = min(w, int((b - rect.x0) / rect.width * w) + 1)
            if i1 > i0:
                buf[j * w + i0:j * w + i1] = b"\xff" * (i1 - i0)
    return fitz.Pixmap(fitz.csGRAY, w, h, bytes(buf), False)


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


LEAK_COVERAGE = 0.15   # part d'un mot dans la zone a partir de laquelle il fuit


def _covered_fraction(rect, rects) -> float:
    """Part de `rect` couverte par les bandes redigees. Les bandes ne se
    chevauchent pas (une par ligne de balayage), leurs aires s'additionnent."""
    area = rect.get_area()
    if area <= 0:
        return 0.0
    return sum((rect & r).get_area() for r in rects) / area


def _verify(out: bytes, zones_by_page, page_map):
    """Relecture du PDF reellement produit (et non du document en memoire):
    reste-t-il du texte, une annotation ou un champ dans les zones ?

    Un mot n'est signale que si la zone mordait vraiment dessus. Depuis que la
    redaction suit le contour dessine et non le rectangle englobant, un mot
    qui frole le trace de quelques dixiemes de point est monnaie courante: le
    signaler noierait les vraies fuites sous des avertissements sans objet."""
    leaks = []
    chk = fitz.open(stream=out, filetype="pdf")
    try:
        for pno, zs in zones_by_page.items():
            new_no = page_map.get(pno)
            if new_no is None or not 0 <= new_no < chk.page_count:
                continue
            page = chk[new_no]
            # les memes bandes que celles redigees: confronter le texte au
            # rectangle englobant signalerait comme fuite ce qui est reste
            # volontairement intact hors du trace.
            rects = [r for z in zs for r in z["rects"]]
            for w in page.get_text("words"):
                if _covered_fraction(fitz.Rect(w[:4]), rects) >= LEAK_COVERAGE:
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
    entry = _get(payload.get("sid") or "")

    # {"3": [{"points": [[x,y], ...], "mode": "delete"|"pixelate"}, ...]} en coordonnees PDF.
    # "points" est le contour de la zone (rectangle = 4 coins, mais aussi
    # polygone ou trace libre). Tout suit ce contour: la redaction via les
    # bandes de _zone_rects, le cache blanc, et la mosaique via son masque.
    # Rien n'est jamais efface ni recouvert hors du trace.
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
                "box": _is_box(points, rect),
                "rects": _zone_rects(points, rect),
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
                mosaics.append((z, pm))

        # 2. redaction reelle de TOUTES les zones: le texte est supprime, les
        # pixels des images couvertes sont detruits (pas seulement masques) et
        # les traces vectorielles qui touchent une zone sont retirees.
        # LINE_ART_REMOVE_IF_TOUCHED est indispensable: par defaut PyMuPDF ne
        # retire qu'un trace *entierement* contenu dans la zone, si bien qu'une
        # signature qui deborde survivait intacte sous le cache blanc.
        rects = [r for z in zs for r in z["rects"]]
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
        # keep_proportion=False: la mosaique fait quelques pixels de cote, ses
        # proportions arrondies ne sont pas exactement celles de la zone, et
        # une image centree y laisserait deux bandes de contenu d'origine.
        for z, pm in mosaics:
            rect = z["rect"]
            if z["box"]:
                page.insert_image(rect, pixmap=pm, keep_proportion=False)
                continue
            # zone non rectangulaire: la mosaique est decoupee au contour,
            # sinon elle recouvrirait tout le rectangle englobant.
            mask = _shape_mask(z["points"], rect)
            if mask is None:
                continue
            page.insert_image(rect, stream=pm.tobytes("png"),
                              mask=mask.tobytes("png"), keep_proportion=False)

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

    base = os.path.splitext(entry["name"])[0] or "document"
    key = _put(f"{base}_redacted.pdf", out)
    return JSONResponse({
        "download": f"/api/download/{key}",
        "filename": f"{base}_redacted.pdf",
        "leaks": leaks[:20],
        "leak_count": len(leaks),
    })


@app.get("/api/download/{key}")
def api_download(key: str):
    entry = _get(key)
    return Response(
        entry["bytes"],
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{entry["name"]}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return (PACKAGE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
