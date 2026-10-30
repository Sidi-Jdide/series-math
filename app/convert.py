"""Conversion Word -> PDF (LibreOffice) et extraction des figures."""
from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pymupdf as fitz

from .omml_fix import SAFE_PARSER, fix_document_xml

MAX_UNZIPPED = 200 << 20   # contre les « bombes zip »
MAX_ZIP_ENTRIES = 5000
_DTD = re.compile(rb"<!(DOCTYPE|ENTITY)", re.I)
_WM = re.compile(r"<w:r\b(?:(?!</w:r>).)*?PowerPlusWaterMarkObject.*?</w:r>", re.S)
# zones de texte à hauteur fixe : LibreOffice coupe ce qui dépasse (Word le laisse déborder)
_NOFIT = re.compile(r"(<wps:bodyPr\b[^>]*>(?:(?!</wps:bodyPr>).)*?)<a:noAutofit/>", re.S)


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _box_text(run) -> str | None:
    """Texte d'une zone de texte posée dans ce morceau Word. None s'il n'y a pas de zone de texte,
    ou si elle contient une formule ou une image : on n'y touche jamais."""
    tags = [el.tag for el in run.iter()]
    if not any(t.endswith("}txbxContent") for t in tags):
        return None
    if any(t.endswith(("}blip", "}imagedata", "}oMath", "}oMathPara", "}chart", "}object")) for t in tags):
        return None
    return "".join(t.text or "" for t in run.iter(f"{{{W_NS}}}t")).strip()


def _pattern(s: str) -> re.Pattern:
    return re.compile(r"\s*".join(map(re.escape, "".join(s.split()))), re.I)


def erase_texts_xml(xml: bytes, erase: list[str], generic: list[str] = ()) -> bytes:
    """Efface les anciens noms dans les paragraphes Word, même s'ils sont coupés en plusieurs morceaux.
    Si le paragraphe est court (ligne d'en-tête ou de signature), on vide tout le paragraphe.
    Une zone de texte qui ne contient plus que des morceaux génériques de l'ancien en-tête
    (classe, matière, chapitre…) est retirée avec son cadre."""
    from lxml import etree
    root = etree.fromstring(xml, SAFE_PARSER)
    pats = [_pattern(e) for e in erase if e.strip()]
    gen = [_pattern(g) for g in generic if g.strip()]
    boxes = [r for r in root.iter(f"{{{W_NS}}}r") if _box_text(r) is not None and _box_text(r)]
    for p in root.iter(f"{{{W_NS}}}p"):
        ts = [t for t in p.iter(f"{{{W_NS}}}t") if t.text]
        if not ts:
            continue
        joined = "".join(t.text for t in ts)
        spans = [(m.start(), m.end()) for pat in pats for m in pat.finditer(joined)]
        if not spans:
            continue
        found = sum(e - s for s, e in spans)
        if len(joined.strip()) <= found + 30:
            spans = [(0, len(joined))]
        kill = set()
        for s_, e_ in spans:
            kill.update(range(s_, e_))
        pos = 0
        for t in ts:
            n = len(t.text)
            t.text = "".join(ch for i, ch in enumerate(t.text, pos) if i not in kill)
            pos += n
    for r in boxes:                     # zone de texte vidée (ancien nom du prof) : on retire le cadre
        rest = _box_text(r)
        for g in gen:
            rest = g.sub("", rest)
        if not re.sub(r"[\W_]", "", rest) and r.getparent() is not None:
            r.getparent().remove(r)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def prepare_docx(src: Path, dst: Path, erase: list[str] | None = None, generic: list[str] = ()) -> int:
    """Copie du .docx prête pour LibreOffice : filigranes Word supprimés (formes VML
    « PowerPlusWaterMarkObject »), zones de texte agrandies selon leur contenu et formules corrigées. Retourne le nombre de filigranes supprimés."""
    n = 0
    try:
        zin = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise RuntimeError("Le fichier Word est abîmé ou n'est pas un vrai .docx.")
    items = zin.infolist()
    if len(items) > MAX_ZIP_ENTRIES or sum(i.file_size for i in items) > MAX_UNZIPPED:
        zin.close()
        raise RuntimeError("Fichier Word refusé : trop volumineux une fois décompressé.")
    with zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in items:
            data = zin.read(item.filename)
            if item.filename.endswith((".xml", ".rels")) and _DTD.search(data):
                raise RuntimeError("Fichier Word refusé : il contient des déclarations XML non autorisées.")
            if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                txt = data.decode("utf-8")
                new, k = _WM.subn("", txt)
                if k:
                    n += k
                    data = new.encode("utf-8")
                if "<a:noAutofit/>" in txt:
                    data = _NOFIT.sub(r"\1<a:spAutoFit/>", data.decode("utf-8")).encode("utf-8")
                if (erase or generic) and b"<w:t" in data:
                    try:
                        data = erase_texts_xml(data, erase or [], generic)
                    except Exception:
                        pass
                if b"<m:oMath" in data:
                    try:
                        data = fix_document_xml(data)
                    except Exception:   # formule exotique : on garde l'original
                        pass
            zout.writestr(item, data)
    return n


def docx_lines(src: Path) -> list[str]:
    """Texte brut de chaque paragraphe du Word (corps, zones de texte, en-têtes, pieds de page),
    même ce que LibreOffice n'affiche pas."""
    from lxml import etree
    lines = []
    try:
        with zipfile.ZipFile(src) as z:
            for name in z.namelist():
                if not re.fullmatch(r"word/(document|header\d*|footer\d*)\.xml", name):
                    continue
                data = z.read(name)
                if _DTD.search(data) or len(data) > MAX_UNZIPPED:
                    continue
                root = etree.fromstring(data, SAFE_PARSER)
                for para in root.iter(f"{{{W_NS}}}p"):
                    t = "".join(x.text or "" for x in para.iter(f"{{{W_NS}}}t")
                                if next(x.iterancestors(f"{{{W_NS}}}p")) is para)
                    if t.strip():
                        lines.append(t)
    except (zipfile.BadZipFile, etree.XMLSyntaxError):
        pass
    return lines


def docx_to_pdf(docx: Path, outdir: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice n'est pas installé : impossible de lire les fichiers Word.")
    profile = outdir / "lo-profile"
    subprocess.run([soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                    "--convert-to", "pdf", "--outdir", str(outdir), str(docx)],
                   check=True, capture_output=True, timeout=180)
    pdf = outdir / (docx.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError("La conversion du fichier Word a échoué.")
    _drop_blank_tail(pdf)
    return pdf


def _drop_blank_tail(pdf: Path) -> None:
    """Retire les pages vides à la fin (paragraphes vides ou cadre effacé qui débordent du Word)."""
    with fitz.open(pdf) as d:
        n = len(d)
        while n > 1 and not d[n - 1].get_text().strip() and not d[n - 1].get_images() \
                and not d[n - 1].get_drawings():
            n -= 1
        if n == len(d):
            return
        d.select(range(n))
        d.save(pdf.with_suffix(".tmp.pdf"), garbage=3, deflate=True)
    pdf.with_suffix(".tmp.pdf").replace(pdf)


def extract_figures(pdf_path: str, max_figs: int = 12) -> list[bytes]:
    """Images de taille raisonnable (figures), hors logos du haut de page."""
    figs = []
    with fitz.open(pdf_path) as d:
        for p in d:
            H = p.rect.height
            for info in p.get_image_info(xrefs=True):
                r = fitz.Rect(info["bbox"])
                if r.y1 < 0.2 * H or r.width < 60 or r.height < 60:
                    continue
                if r.width * r.height > 0.6 * p.rect.width * H:   # page scannée entière : pas une figure
                    continue
                figs.append(p.get_pixmap(clip=r, dpi=150).tobytes("png"))
                if len(figs) >= max_figs:
                    return figs
    return figs
