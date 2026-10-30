"""Enchaîne les étapes pour un fichier envoyé."""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

from . import claude_ai, convert, rebrand, render
from .rebrand import Identity

MAX_PAGES = int(os.environ.get("MAX_PAGES", "20"))   # une série dépasse rarement 20 pages ; protège les crédits
# re-saisie du sujet par Claude (scans, exercices retirés) : coûte des crédits, désactivée par défaut
RESAISIE = os.environ.get("RESAISIE", "0") == "1"
CACHE = Path(os.environ.get("DATA_DIR", "/tmp/series-jobs")) / "cache"   # réponses de Claude, effacées après 24 h


@dataclass
class Options:
    ident: Identity
    titre_auto: bool = True
    faire_sujet: bool = True
    faire_correction: bool = True
    mode: str = "original"            # "original" (garder la mise en page) ou "resaisir"
    exercices_retires: list[int] = field(default_factory=list)
    textes_a_effacer: list[str] = field(default_factory=list)   # en plus de ceux trouvés par Claude


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "serie"


def _reuse(key: list, ask):
    """Même fichier, mêmes réglages : on reprend la réponse de Claude déjà payée au lieu de la redemander.
    Retourne (réponse, réutilisée ?)."""
    f = CACHE / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json")
    try:
        return json.loads(f.read_text(encoding="utf-8")), True
    except (OSError, ValueError):
        pass
    value = ask()
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value, False


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _header_text(ident: Identity) -> str:
    return " ".join([ident.ecole, ident.prof, ident.annee, ident.classes, ident.matiere, ident.titre])


def _specific(texts: list[str], ident: Identity) -> list[str]:
    """Textes trouvés par Claude, sauf les mots génériques : ils seraient effacés partout dans les exercices
    (« 4AS », « Math », « S1 »…), et ceux qui font partie du nouvel en-tête."""
    new = _squash(_header_text(ident))
    return [t for t in texts if len(_squash(t)) >= 4 and _squash(t) not in new]


_PETITS = {"et", "de", "des", "du", "la", "le", "les", "en", "a", "aux", "au", "sur", "dans", "d", "l"}


def _file_name(ident: Identity, base: str) -> str:
    """Nom qui dit ce qu'il y a dedans : chapitre (trouvé par Claude), classes, prof.
    Sans chapitre connu, on garde le nom du fichier envoyé."""
    prof = slug(ident.prof).lower()
    chapitre = re.sub(r"^\s*s[ée]rie\s*(n\s*°?\s*\d+)?\s*[:\-–]?\s*", "", ident.titre, flags=re.I)
    mots = slug(chapitre).split("_") if slug(chapitre) != "serie" else []
    if mots:
        mots = [m.lower() if i and m.lower() in _PETITS else m.capitalize() for i, m in enumerate(mots)]
        classes = slug(ident.classes)
        nom = "_".join(["Serie", *mots] + ([classes] if classes != "serie" else []))[:70].strip("_")
    else:
        nom = base
    return nom if prof in nom.lower() else f"{nom}_{prof}"


_SIGNATURE = re.compile(r"^\s*(?:prof(?:esseur)?|enseignante?|pr)\b\.?\s*[:\-–]?\s*\S.{1,60}$", re.I)


def _signatures(lines: list[str], ident: Identity) -> list[str]:
    """Lignes « Prof : Nom » d'un autre prof, trouvées sans Claude (un cadre de signature peut lui échapper)."""
    me = _squash(ident.prof)
    return list(dict.fromkeys(line.strip() for line in lines
                              if _SIGNATURE.match(line) and me not in _squash(line)))


def run(src: Path, workdir: Path, opt: Options, log) -> dict:
    """log(msg) affiche une étape à l'utilisateur. Retourne {fichiers, erreurs_enonce, avertissements}."""
    result = {"fichiers": [], "erreurs_enonce": [], "avertissements": []}
    base = slug(src.stem)
    claude_ai.reset_cost()
    # clé de réutilisation : contenu du fichier envoyé + modèle et réflexion utilisés
    key = [hashlib.sha256(src.read_bytes()).hexdigest(), claude_ai.MODEL, claude_ai.EFFORT, claude_ai.MOCK,
           claude_ai.PROMPTS_VERSION]
    reused = []

    # 1. tout ramener en PDF
    if src.suffix.lower() in (".docx", ".doc"):
        log("Conversion du fichier Word en PDF…")
        if src.suffix.lower() == ".docx":
            clean = workdir / f"{base}_prepare.docx"
            convert.prepare_docx(src, clean)
            pdf = convert.docx_to_pdf(clean, workdir)
        else:
            pdf = convert.docx_to_pdf(src, workdir)
    else:
        pdf = src
    try:
        with fitz.open(pdf) as doc:
            if doc.is_encrypted:
                raise RuntimeError("Ce PDF est protégé par un mot de passe.")
            result["pages"] = len(doc)
            if len(doc) > MAX_PAGES:
                raise RuntimeError(f"Document trop long ({len(doc)} pages, maximum {MAX_PAGES}).")
    except fitz.FileDataError:
        raise RuntimeError("Le fichier n'est pas un PDF valide.")

    # 2. analyse
    log("Lecture du sujet par Claude (quelques minutes : demande groupée, moitié prix)…" if claude_ai.BATCH
        else "Lecture du sujet par Claude…")
    info, hit = _reuse(["analyse", *key], lambda: claude_ai.analyse(str(pdf)))
    reused += [hit]
    ident = opt.ident
    if opt.titre_auto and info.get("titre"):
        ident = Identity(**{**ident.__dict__, "titre": info["titre"]})
    old = _specific(info.get("anciens_textes", []), ident) + opt.textes_a_effacer
    with fitz.open(pdf) as doc:
        lines = [ln for p in doc for ln in p.get_text().splitlines()]
    if src.suffix.lower() == ".docx":
        lines += convert.docx_lines(src)
    old += [t for t in _signatures(lines, ident) if t not in old]
    margin = [t for t in info.get("entete_generique", []) if len(_squash(t)) >= 2]
    if info.get("classes_origine") and info["classes_origine"].replace(" ", "") not in ident.classes.replace(" ", ""):
        result["avertissements"].append(
            f"Le sujet d'origine était pour « {info['classes_origine']} » ; l'en-tête indique « {ident.classes} ».")
    # fichiers produits : chapitre + classes + prof (ex. Serie_Reels_et_Angles_4AS_tachefine_sujet.pdf)
    nom = _file_name(ident, base)

    # Word : on efface aussi les anciens noms directement dans le fichier, puis on reconvertit
    if src.suffix.lower() == ".docx" and (old or margin) and opt.faire_sujet:
        clean = workdir / f"{base}_prepare.docx"
        convert.prepare_docx(src, clean, erase=old, generic=margin)
        pdf = convert.docx_to_pdf(clean, workdir)

    # 3. sujet
    if opt.faire_sujet:
        mode = opt.mode
        with fitz.open(pdf) as doc:
            scanned = rebrand.is_scanned(doc)
        if mode == "original" and scanned and RESAISIE:
            mode = "resaisir"
            result["avertissements"].append("Le PDF est une image scannée : le sujet a été re-saisi.")
        elif scanned:
            result["avertissements"].append(
                "Le PDF est une image scannée : les anciens noms écrits dans l'image n'ont pas pu être effacés.")
        if mode == "original" and opt.exercices_retires:
            mode = "resaisir"
            result["avertissements"].append("Pour retirer des exercices, le sujet a été re-saisi proprement.")
        out = workdir / f"{nom}_sujet.pdf"
        if mode == "original":
            log("Nettoyage du PDF (anciens noms, filigranes) et nouvel en-tête…")
            rep = rebrand.rebrand_pdf(str(pdf), str(out), ident, old, margin)
            restes = [t for t in rep["remaining_old_texts"] if _squash(t) not in _squash(_header_text(ident))]
            if restes:
                result["avertissements"].append("Textes peut-être encore présents : " + " ; ".join(restes))
        else:
            log("Re-saisie du sujet par Claude…")
            figs = convert.extract_figures(str(pdf))
            body, hit = _reuse(["sujet", *key, opt.exercices_retires, len(figs)],
                               lambda: claude_ai.transcription(str(pdf), opt.exercices_retires, figs))
            reused += [hit]
            html = render.build_html(body, ident, corrige=False,
                                     figures={f"fig{i}": png for i, png in enumerate(figs, 1)})
            log("Mise en page du sujet…")
            render.html_to_pdf(html, str(out), ident, str(workdir))
            if figs == [] and info.get("a_des_figures"):
                result["avertissements"].append(
                    "Le sujet contient des figures dessinées qui n'ont pas pu être reprises en mode re-saisie.")
        result["fichiers"].append(out.name)

    # 4. correction
    if opt.faire_correction:
        log("Rédaction de la correction par Claude (1 à 4 minutes)…")
        (body, errs), hit = _reuse(["correction", *key, opt.exercices_retires],
                                   lambda: list(claude_ai.correction(str(pdf), opt.exercices_retires)))
        reused += [hit]
        result["erreurs_enonce"] = errs
        log("Mise en page de la correction…")
        out = workdir / f"{nom}_correction.pdf"
        render.html_to_pdf(render.build_html(body, ident, corrige=True), str(out), ident, str(workdir))
        result["fichiers"].append(out.name)

    result["titre"] = ident.titre
    result["exercices"] = info.get("nb_exercices") or 0   # pour les statistiques de la page
    result["cout_estime"] = round(claude_ai.cost(), 2)
    result["reutilise"] = any(reused)
    log(f"Terminé. Coût Claude estimé : {claude_ai.cost():.2f} $")
    return result
