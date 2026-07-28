"""Lecture de tout ce qu'un PDF transporte sans le montrer.

Ouvrir un PDF dans un navigateur n'en donne qu'une image. Le fichier, lui,
porte aussi sa couche de texte indexable, ses metadonnees, son XMP, ses
signets, ses annotations, ses champs de formulaire, ses pieces jointes, ses
calques, ses liens et parfois du JavaScript. C'est cette charge invisible que
l'on extrait ici, pour que l'utilisateur puisse la relire avant d'exporter.

Rien n'est modifie: ce module ne fait que lire.
"""

import fitz

MAX_SPANS = 30_000  # garde-fou sur un document tres long
SNIPPET = 2000  # on ne renvoie pas un script entier

META_LABELS = [
    ("title", "Titre"),
    ("author", "Auteur"),
    ("subject", "Sujet"),
    ("keywords", "Mots-cles"),
    ("creator", "Application d'origine"),
    ("producer", "Producteur du PDF"),
    ("creationDate", "Date de creation"),
    ("modDate", "Date de modification"),
    ("format", "Format"),
    ("encryption", "Chiffrement"),
]

LINK_KINDS = {
    fitz.LINK_GOTO: "page",
    fitz.LINK_URI: "url",
    fitz.LINK_LAUNCH: "fichier",
    fitz.LINK_GOTOR: "document externe",
    fitz.LINK_NAMED: "action",
}


def _r(rect) -> list[float]:
    return [round(float(v), 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)]


def _metadata(doc) -> list[dict]:
    md = doc.metadata or {}
    return [
        {"key": k, "label": label, "value": str(md[k]).strip()}
        for k, label in META_LABELS
        if md.get(k) and str(md[k]).strip()
    ]


def _toc(doc) -> list[dict]:
    try:
        return [
            {"level": lvl, "title": title, "page": page}
            for lvl, title, page in doc.get_toc(simple=True)
        ]
    except Exception:
        return []


def _attachments(doc) -> list[dict]:
    out = []
    try:
        names = doc.embfile_names()
    except Exception:
        return out
    for name in names:
        try:
            info = doc.embfile_info(name)
        except Exception:
            info = {}
        out.append(
            {
                "name": name,
                "filename": info.get("filename") or "",
                "desc": info.get("desc") or "",
                "size": info.get("size") or 0,
            }
        )
    return out


def _layers(doc) -> list[dict]:
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:
        return []
    return [
        {"name": v.get("name") or f"calque {xref}", "on": bool(v.get("on", True))}
        for xref, v in ocgs.items()
    ]


def _javascript(doc) -> list[dict]:
    """Un PDF peut embarquer du script, declenche a l'ouverture ou sur une
    action. PyMuPDF n'expose pas d'API pour cela: on parcourt les objets.
    """
    out = []
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "S")[1] != "/JavaScript":
                continue
            kind, val = doc.xref_get_key(xref, "JS")
        except Exception:
            continue
        code = ""
        try:
            if kind == "string":
                code = val.strip("()")
            elif kind == "xref":
                code = doc.xref_stream(int(val.split()[0])).decode("utf-8", "replace")
        except Exception:
            pass
        out.append({"name": f"action {xref}", "code": code[:SNIPPET]})
    return out


def _fonts(doc) -> list[dict]:
    """Le nom d'une police sous-ensemblee ("ABCDEF+Calibri") et la liste des
    polices trahissent la machine et l'application d'origine.
    """
    seen, out = set(), []
    for page in doc:
        try:
            fonts = page.get_fonts(full=False)
        except Exception:
            continue
        for f in fonts:
            ext, ftype, basefont = f[1], f[2], f[3]
            if basefont in seen:
                continue
            seen.add(basefont)
            out.append({"name": basefont, "type": ftype, "embedded": ext != "n/a"})
    return out


def _text_blocks(page, budget: list[int]) -> list[dict]:
    """Le texte tel qu'il est reellement stocke, decoupe en blocs et lignes
    pour rester lisible, chaque fragment garde son rectangle: c'est ce qui
    permet ensuite de dire lequel tombe dans une zone.
    """
    try:
        raw = page.get_text("dict")
    except Exception:
        return []
    blocks = []
    for b in raw.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines = []
        for line in b.get("lines", []):
            spans = []
            for sp in line.get("spans", []):
                if not sp.get("text", "").strip():
                    continue
                if budget[0] <= 0:
                    return blocks
                budget[0] -= 1
                # alpha nul = texte invisible: couche OCR, ou trace volontairement
                # cachee. Il est indexe et copiable malgre tout.
                spans.append(
                    {
                        "text": sp["text"],
                        "rect": [round(v, 2) for v in sp["bbox"]],
                        "font": sp.get("font", ""),
                        "size": round(sp.get("size", 0), 1),
                        "hidden": sp.get("alpha", 255) == 0,
                    }
                )
            if spans:
                lines.append({"spans": spans})
        if lines:
            blocks.append({"rect": [round(v, 2) for v in b["bbox"]], "lines": lines})
    return blocks


def _annots(page) -> list[dict]:
    out = []
    try:
        annots = list(page.annots())
    except Exception:
        return out
    for a in annots:
        info = a.info or {}
        out.append(
            {
                "type": a.type[1] if len(a.type) > 1 else str(a.type[0]),
                "author": info.get("title") or "",
                "content": (info.get("content") or "")[:SNIPPET],
                "subject": info.get("subject") or "",
                "date": info.get("modDate") or info.get("creationDate") or "",
                "rect": _r(a.rect),
            }
        )
    return out


def _widgets(page) -> list[dict]:
    out = []
    try:
        widgets = list(page.widgets())
    except Exception:
        return out
    for w in widgets:
        out.append(
            {
                "name": w.field_name or "",
                "label": w.field_label or "",
                "value": str(w.field_value if w.field_value is not None else "")[:SNIPPET],
                "type": w.field_type_string or "",
                "rect": _r(w.rect),
            }
        )
    return out


def _links(page) -> list[dict]:
    out = []
    try:
        links = page.get_links()
    except Exception:
        return out
    for lk in links:
        target = lk.get("uri") or lk.get("file") or lk.get("name") or ""
        if not target and lk.get("kind") == fitz.LINK_GOTO:
            target = f"page {lk.get('page', 0) + 1}"
        out.append(
            {
                "kind": LINK_KINDS.get(lk.get("kind"), "autre"),
                "target": str(target)[:SNIPPET],
                "rect": _r(fitz.Rect(lk["from"])),
            }
        )
    return out


def _images(page) -> list[dict]:
    out = []
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return out
    for im in imgs:
        xref = im[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for rect in rects or [None]:
            out.append(
                {
                    "w": im[2],
                    "h": im[3],
                    "name": im[7] or f"image {xref}",
                    "rect": _r(rect) if rect is not None else None,
                }
            )
    return out


def inspect_document(data: bytes) -> dict:
    doc = fitz.open(stream=data, filetype="pdf")
    budget = [MAX_SPANS]
    try:
        info = {
            "page_count": doc.page_count,
            "encrypted": bool(doc.is_encrypted),
            "metadata": _metadata(doc),
            "xmp": (doc.get_xml_metadata() or "").strip() or None,
            "toc": _toc(doc),
            "attachments": _attachments(doc),
            "layers": _layers(doc),
            "javascript": _javascript(doc),
            "fonts": _fonts(doc),
        }
        pages = []
        for n, page in enumerate(doc):
            try:
                drawings = len(page.get_drawings())
            except Exception:
                drawings = 0
            pages.append(
                {
                    "n": n,
                    "blocks": _text_blocks(page, budget),
                    "annots": _annots(page),
                    "widgets": _widgets(page),
                    "links": _links(page),
                    "images": _images(page),
                    "drawings": drawings,
                }
            )
    finally:
        doc.close()
    return {"doc": info, "pages": pages, "truncated": budget[0] <= 0}
