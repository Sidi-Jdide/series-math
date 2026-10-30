"""Mode « garder la mise en page » : on nettoie le PDF d'origine puis on y pose le nouvel en-tête.

Étapes pour chaque page :
1. Neutraliser les en-têtes et pieds de page Word (/Artifact Header|Footer) : c'est là que Word range les filigranes
   (nom en diagonale, grande lettre) et souvent le bandeau de l'école.
2. Supprimer le texte en diagonale (filigranes texte).
3. Effacer toutes les occurrences des anciens noms / anciennes écoles (liste fournie par Claude).
4. Supprimer les images (logos, bandeaux) du haut de la page 1, ou répétées en haut de plusieurs pages.
5. Recadrer sur le contenu restant et le replacer sous un nouvel en-tête, avec un nouveau pied de page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf as fitz

HEADER_H = 50      # hauteur du cadre d'en-tête (pt)
TOP = 18           # marge au-dessus du cadre
FOOTER_Y = 18      # distance du pied de page au bas de la page
GAP = 8            # espace entre l'en-tête et le contenu
WATERMARK_PT = 18  # texte incliné plus petit = libellé de figure (axe, côté), pas un filigrane


@dataclass
class Identity:
    ecole: str = "ECOLE COLLEG1"
    prof: str = "TACHEFINE"
    annee: str = "2026.2027"
    classes: str = "4AS1 - 4AS2"
    matiere: str = "Mathématiques"
    titre: str = "SERIE"


# ---------------------------------------------------------------- nettoyage

_PAINT = re.compile(rb"(?<=\s)(f\*|f|F|B\*|B|b\*|b|S|s)(?=[\s/\[(<])")
_HEADER_BDC = re.compile(rb"/Artifact\s*<<[^>]*/Subtype\s*/(?:Header|Footer)[^>]*>>\s*BDC")
_MARK = re.compile(rb"\b(BDC|BMC|EMC)\b")


def _neutralize_header_artifacts(doc: fitz.Document, page: fitz.Page) -> int:
    """Rend invisibles les dessins et textes placés dans les en-têtes Word. Retourne le nb de blocs traités."""
    page.clean_contents()
    xrefs = page.get_contents()
    if not xrefs:
        return 0
    xref = xrefs[0]
    c = doc.xref_stream(xref)
    out, pos, n = [], 0, 0
    for m in _HEADER_BDC.finditer(c):
        if m.start() < pos:
            continue
        i = m.end()
        depth, j = 1, len(c)
        for t in _MARK.finditer(c, i):
            depth += -1 if t.group(1) == b"EMC" else 1
            if depth == 0:
                j = t.start()
                break
        seg = c[i:j]
        seg = _PAINT.sub(b"n", b" " + seg)[1:]          # chemins : peints -> non peints
        seg = re.sub(rb"\bBT\b", b"BT 3 Tr", seg)        # texte : mode invisible
        seg = re.sub(rb"/[\w.]+\s+Do\b", b"", seg)       # images / XObjects
        # q … Q : l'état graphique (dont le mode texte « 3 Tr ») ne déborde pas sur la suite de la page
        out += [c[pos:i], b" q ", seg, b" Q "]
        pos, n = j, n + 1
    if n:
        out.append(c[pos:])
        doc.update_stream(xref, b"".join(out))
    return n


def _variants(s: str, min_len: int = 3) -> set[str]:
    s = " ".join(s.split())
    v = {s, s.upper(), s.lower(), s.title()}
    return {x for x in v if len(x) >= min_len}


def _in_margin(page: fitz.Page, r: fitz.Rect) -> bool:
    """Haut de la page 1 (ancien en-tête), ou bandes du haut et du bas de chaque page."""
    H = page.rect.height
    return r.y1 < (0.18 if page.number == 0 else 0.10) * H or r.y0 > 0.92 * H


def _top_images(page: fitz.Page) -> list[tuple[int, fitz.Rect]]:
    H = page.rect.height
    return [(i["xref"], fitz.Rect(i["bbox"])) for i in page.get_image_info(xrefs=True)
            if i["bbox"][3] < 0.2 * H and i["bbox"][2] - i["bbox"][0] > 40]


def repeated_top_images(doc: fitz.Document) -> set[int]:
    """Images présentes en haut de plusieurs pages : logos ou bandeaux, pas des figures."""
    seen: dict[int, int] = {}
    for p in doc:
        for x in {x for x, _ in _top_images(p) if x}:
            seen[x] = seen.get(x, 0) + 1
    return {x for x, n in seen.items() if n >= 2}


def clean_page(doc: fitz.Document, page: fitz.Page, old_texts: list[str], logos: set[int] = frozenset(),
               margin_texts: list[str] = ()) -> dict:
    stats = {"header_blocks": _neutralize_header_artifacts(doc, page), "rotated": 0, "names": 0, "images": 0}

    # grand texte en diagonale / vertical = filigrane
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            dx, dy = line["dir"]
            big = max((s["size"] for s in line["spans"]), default=0) >= WATERMARK_PT
            if abs(dy) > 0.15 and big and "".join(s["text"] for s in line["spans"]).strip():
                page.add_redact_annot(fitz.Rect(line["bbox"]))
                stats["rotated"] += 1

    # anciens noms / écoles / téléphones… Si la ligne qui contient le nom est courte
    # (« Prof : X     Bonne chance », « Lycée X  2025-2026 »), on efface toute la ligne.
    lines = [(fitz.Rect(l["bbox"]), " ".join("".join(sp["text"] for sp in l["spans"]).split()))
             for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])]
    touched = []   # lignes où l'on a effacé quelque chose
    # mots génériques de l'ancien en-tête (« 4AS », « Math »…) : seulement dans les marges, jamais dans un exercice
    found = [(r, v) for s in old_texts for v in _variants(s) for r in page.search_for(v)]
    found += [(r, v) for s in margin_texts for v in _variants(s, 2) for r in page.search_for(v) if _in_margin(page, r)]
    for r, v in found:
        target = r
        touched += [lr for lr, _ in lines if lr.intersects(r) and abs(lr.y0 - r.y0) < 4]
        for lr, lt in lines:
            if lr.intersects(r) and abs(lr.y0 - r.y0) < 4 and len(lt) <= len(v) + 30:
                target = lr | r
                break
        page.add_redact_annot(target)
        stats["names"] += 1

    # logos et bandeaux : haut de la page 1, ou image répétée en haut de plusieurs pages
    # (une figure placée en haut de la page 2 est gardée)
    for xref, r in _top_images(page):
        if page.number == 0 or xref in logos:
            page.add_redact_annot(r)
            stats["images"] += 1

    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    # ligne vidée par l'effacement (souvent morceau par morceau) : son soulignement n'a plus de raison d'être
    stats["underlines"] = _remove_underlines(page, [lr for lr in touched if not page.get_textbox(lr).strip()])
    return stats


def _remove_underlines(page: fitz.Page, erased: list[fitz.Rect]) -> int:
    """Soulignement d'une ligne effacée (ex. « 2026/2027 Serie … Class : … ») : trait fin collé sous le texte.
    Seuls les traits entièrement couverts sont retirés : tableaux, cadres et figures restent intacts."""
    n = 0
    for d in page.get_drawings() if erased else []:
        r = d["rect"]
        if r.height > 3 or r.width < 40:
            continue
        for lr in erased:
            overlap = min(r.x1, lr.x1) - max(r.x0, lr.x0)
            if lr.y1 - 6 <= r.y0 and r.y1 <= lr.y1 + 4 and overlap > 0.5 * min(r.width, lr.width):
                page.add_redact_annot(r + (-1, -1, 1, 1))
                n += 1
                break
    if n:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return n


def content_box(page: fitz.Page) -> fitz.Rect:
    """Rectangle englobant tout ce qui est encore visible sur la page."""
    pr = page.rect
    box = fitz.Rect()
    for kind, r in page.get_bboxlog():
        r = fitz.Rect(r) & pr
        if r.is_empty or kind.startswith("ignore"):
            continue
        if r.width * r.height > 0.9 * pr.width * pr.height:   # fond de page blanc
            continue
        box |= r
    return box if not box.is_empty else fitz.Rect(pr)


def is_scanned(doc: fitz.Document) -> bool:
    txt = "".join(p.get_text() for p in doc)
    return len(txt.strip()) < 40 * len(doc)


# ---------------------------------------------------------------- en-tête

def _text(page, x, y, t, size, font, align="l", color=(0, 0, 0)):
    w = fitz.get_text_length(t, font, size)
    x = x if align == "l" else (x - w / 2 if align == "c" else x - w)
    page.insert_text((x, y), t, fontsize=size, fontname=font, color=color)


def _fit(t, font, size, maxw):
    while size > 6 and fitz.get_text_length(t, font, size) > maxw:
        size -= 0.25
    return size


def draw_header(page: fitz.Page, ident: Identity, x0: float, x1: float):
    T = TOP
    page.draw_rect(fitz.Rect(x0, T, x1, T + HEADER_H), color=(0, 0, 0), width=1.1)
    page.draw_line((x0, T + HEADER_H / 2), (x1, T + HEADER_H / 2), width=0.6)
    mid, w3 = (x0 + x1) / 2, (x1 - x0) / 3 - 14
    y1, y2 = T + 17.5, T + HEADER_H - 7.5
    for t, x, a in [(ident.ecole, x0 + 10, "l"), (f"PROF : {ident.prof}", mid, "c"), (ident.annee, x1 - 10, "r")]:
        _text(page, x, y1, t, _fit(t, "tibo", 12.5, w3), "tibo", a)
    _text(page, x0 + 10, y2, ident.matiere, _fit(ident.matiere, "tiro", 10.5, w3 * 0.8), "tiro")
    t = ident.titre.upper()
    _text(page, mid, y2 + 0.5, t, _fit(t, "tibo", 13.5, w3 * 1.35), "tibo", "c")
    t = f"Classes : {ident.classes}"
    _text(page, x1 - 10, y2, t, _fit(t, "tiro", 10.5, w3 * 0.8), "tiro", "r")


def draw_footer(page: fitz.Page, ident: Identity, x0: float, x1: float, n: int, total: int):
    y = page.rect.height - FOOTER_Y
    _text(page, x0 + 4, y, f"Classes : {ident.classes}", 10, "tiit")
    _text(page, (x0 + x1) / 2, y, f"Page {n} sur {total}", 9.5, "tiro", "c")
    _text(page, x1 - 4, y, f"Prof : {ident.prof}", 10.5, "tibi", "r")


# ---------------------------------------------------------------- pipeline

def rebrand_pdf(src_pdf: str, out_pdf: str, ident: Identity, old_texts: list[str], margin_texts: list[str] = ()) -> dict:
    with fitz.open(src_pdf) as src:
        report = {"pages": len(src), "stats": []}
        logos = repeated_top_images(src)
        for p in src:
            report["stats"].append(clean_page(src, p, old_texts, logos, margin_texts))
        # sauvegarde intermédiaire : show_pdf_page lit un document « propre »
        cleaned = fitz.open("pdf", src.tobytes(garbage=3, deflate=True))

    out = fitz.open()
    total = len(cleaned)
    for i, p in enumerate(cleaned):
        W, H = p.rect.width, p.rect.height
        box = content_box(p)
        # on ne garde pas un vieux pied de page isolé tout en bas : on coupe au-dessus de notre pied
        box = fitz.Rect(max(box.x0 - 4, 0), max(box.y0 - 4, 0), min(box.x1 + 4, W), min(box.y1 + 4, H))
        np_ = out.new_page(width=W, height=H)
        x0, x1 = min(36, box.x0), max(W - 36, box.x1)
        area = fitz.Rect(x0, TOP + HEADER_H + GAP, x1, H - FOOTER_Y - 14)
        # échelle maximale 1 : on ne grossit jamais le contenu
        s = min(1.0, area.width / box.width, area.height / box.height)
        w, h = box.width * s, box.height * s
        cx = (area.x0 + area.x1) / 2 if s < 1 else box.x0 + w / 2
        target = fitz.Rect(cx - w / 2, area.y0, cx + w / 2, area.y0 + h)
        np_.show_pdf_page(target, cleaned, i, clip=box)
        draw_header(np_, ident, x0, x1)
        draw_footer(np_, ident, x0, x1, i + 1, total)
    out.save(out_pdf, garbage=3, deflate=True)
    out.close()
    cleaned.close()
    report["remaining_old_texts"] = remaining(out_pdf, old_texts)
    return report


def remaining(pdf_path: str, old_texts: list[str]) -> list[str]:
    with fitz.open(pdf_path) as d:
        txt = " ".join(" ".join(p.get_text().split()) for p in d).lower()
    return [s for s in old_texts if " ".join(s.split()).lower() in txt]


def render_pages_png(pdf_path: str, dpi: int = 60) -> list[bytes]:
    with fitz.open(pdf_path) as d:
        return [p.get_pixmap(dpi=dpi).tobytes("png") for p in d]
