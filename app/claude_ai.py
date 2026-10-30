"""Appels à l'API Claude : analyse du sujet, correction, re-saisie du sujet."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time

import anthropic
import pymupdf as fitz

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
BATCH = os.environ.get("CLAUDE_BATCH", "1") == "1"   # analyse via l'API Batch : moitié prix, quelques minutes d'attente
BATCH_POLL = 15   # secondes entre deux vérifications du lot
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")   # réflexion pour la correction : low, medium, high
MOCK = os.environ.get("MOCK_CLAUDE") == "1"   # pour tester sans clé API
PROMPTS_VERSION = 5  # à augmenter quand les consignes changent : les réponses réutilisées sont alors redemandées

# $ par million de tokens : (entrée, sortie, lecture du cache). La réflexion de Claude compte en sortie.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}
_cost = threading.local()   # coût de la série en cours (une série = un thread)


def reset_cost() -> None:
    _cost.total = 0.0


def cost() -> float:
    return getattr(_cost, "total", 0.0)


def _track(msg, step: str, batch: bool = False) -> None:
    """Affiche les tokens d'un appel dans les logs et ajoute son prix au coût de la série."""
    u = msg.usage
    p_in, p_out, p_read = PRICES.get(msg.model, PRICES["claude-opus-5-5"])
    written, read = u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
    c = (u.input_tokens * p_in + written * p_in * 1.25 + read * p_read + u.output_tokens * p_out) / 1e6
    if batch:
        c /= 2   # l'API Batch coûte moitié prix
    _cost.total = cost() + c
    print(f"[claude] {step} ({msg.model}{', lot' if batch else ''}) : entrée {u.input_tokens}, cache écrit {written}, "
          f"cache lu {read}, sortie {u.output_tokens} -> {c:.4f} $", flush=True)

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # lit ANTHROPIC_API_KEY
    return _client


def _pdf_block(pdf_path: str, cache: bool = True) -> dict:
    with open(pdf_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if cache:
        # le même PDF est envoyé pour la re-saisie, la correction et les demandes de suite
        block["cache_control"] = {"type": "ephemeral"}
    return block


# Si Claude refuse une demande (faux positif des filtres de sécurité), l'API relance sur un autre modèle.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


def _extra(effort: str) -> dict:
    """Paramètres communs : niveau de réflexion (effort) et repli automatique en cas de refus."""
    kw: dict = {"output_config": {"effort": effort}}
    if MODEL in _FALLBACK_MODELS:
        kw["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        kw["extra_body"] = {"fallbacks": "default"}
    return kw


def _check(msg) -> None:
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude a refusé de traiter ce document. Réessaie, ou vérifie que le fichier est bien une série d'exercices.")


def _img_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.standard_b64encode(png).decode()}}


SYSTEM = (
    "Tu es un professeur de mathématiques expérimenté (collège et lycée, programme francophone). "
    "Tu travailles pour un collègue qui adapte des séries d'exercices écrites par d'autres professeurs. "
    "Tu lis les formules avec une extrême attention (fractions imbriquées, exposants, racines, signes). "
    "Tu vérifies chaque calcul deux fois. Tu écris en français."
)

# ------------------------------------------------------------------ analyse

ANALYSE_TOOL = {
    "name": "analyse_sujet",
    "description": "Résultat de l'analyse d'une série d'exercices.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "anciens_textes": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Chaînes EXACTES, telles qu'elles apparaissent dans le texte du PDF, à effacer car elles identifient "
                    "l'ancien auteur ou l'ancienne école : nom du professeur (avec « Prof : » si présent), nom de l'école, "
                    "téléphones, adresse, ancienne année scolaire, ancienne ligne de titre/classe de l'en-tête. "
                    "N'inclus JAMAIS du texte d'exercice. Une chaîne par élément, copiée à l'identique (accents, espaces, casse). "
                    "Pour une ligne d'en-tête ou de pied de page, renvoie la ligne ENTIÈRE (ex. « 4AS CN S1 Nombres Réels Math »), "
                    "jamais des morceaux : un mot générique seul (classe « 4AS », matière « Math », « Série », « S1 ») "
                    "serait effacé partout dans les exercices."),
            },
            "entete_generique": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Morceaux de l'ancienne ligne d'en-tête ou de pied de page qui ne désignent PAS l'ancien prof "
                    "ou l'ancienne école : classe, matière, chapitre, numéro de série, année scolaire "
                    "(ex. « 4AS », « CN », « S1 », « Math », « Nombres Réels », « 2025-2026 »). Copiés à l'identique. "
                    "Ils ne sont effacés qu'en haut et en bas des pages, jamais dans les exercices. "
                    "Liste vide s'il n'y en a pas."),
            },
            "titre": {"type": "string", "description": (
                "Titre COURT de la série en majuscules, 30 caractères au plus, sans accents, ex. « SERIE : NOMBRES REELS ». "
                "Si la série couvre plusieurs notions, donne le chapitre qui les regroupe "
                "(intervalles, valeurs approchées, valeur absolue, radicaux -> « SERIE : NOMBRES REELS »).")},
            "classes_origine": {"type": "string", "description": "Classes indiquées dans le sujet d'origine, ex. « 4AS », ou vide."},
            "nb_exercices": {"type": "integer"},
            "a_des_figures": {"type": "boolean", "description": "Le sujet contient des figures géométriques ou graphiques."},
        },
        "required": ["anciens_textes", "entete_generique", "titre", "classes_origine", "nb_exercices", "a_des_figures"],
    },
}


def _pdf_text(pdf_path: str) -> str:
    with fitz.open(pdf_path) as d:
        return "\n\n".join(f"--- Page {i} ---\n{p.get_text()}" for i, p in enumerate(d, 1))


def _batch(params: dict, step: str):
    """API Batch : moitié prix, mais la réponse arrive en quelques minutes (24 h au pire)."""
    b = client().messages.batches.create(requests=[{"custom_id": step, "params": params}])
    while b.processing_status != "ended":
        time.sleep(BATCH_POLL)
        b = client().messages.batches.retrieve(b.id)
    for r in client().messages.batches.results(b.id):
        if r.result.type == "succeeded":
            return r.result.message
        detail = getattr(getattr(r.result, "error", None), "error", None)
        raise RuntimeError(f"Claude n'a pas pu lire le sujet ({r.result.type}{' : ' + str(detail) if detail else ''}).")
    raise RuntimeError("Claude n'a pas renvoyé de réponse.")


NO_INFO = {"anciens_textes": [], "entete_generique": [], "titre": "", "classes_origine": "", "nb_exercices": 0, "a_des_figures": False}


def analyse(pdf_path: str) -> dict:
    if MOCK:
        return {"anciens_textes": [], "entete_generique": [], "titre": "SERIE", "classes_origine": "", "nb_exercices": 1,
                "a_des_figures": False}
    # Seul le TEXTE du PDF est envoyé (≈ 6 fois moins de tokens que les pages en images) : les anciens noms
    # ne peuvent de toute façon être effacés que s'ils sont dans ce texte, et Claude les recopie ainsi à l'identique.
    text = _pdf_text(pdf_path)
    if len(text.split()) < 20:   # PDF scanné : rien à effacer dans le texte, Claude n'apporterait rien
        return dict(NO_INFO)
    # tool_choice forcé (« tool » / « any ») est refusé par les modèles récents : « auto » + consigne explicite
    params = dict(
        model=MODEL, max_tokens=16000, system=SYSTEM, tools=[ANALYSE_TOOL], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": (
            "Voici le texte extrait d'une série d'exercices (PDF). Repère tout ce qui identifie l'ancien professeur "
            "ou l'ancienne école (en-têtes, pieds de page, bandeaux, filigranes), le titre de la série et le nombre "
            "d'exercices. Réponds uniquement en appelant l'outil analyse_sujet.\n\n" + text)}],
    )
    if BATCH:   # le repli automatique en cas de refus n'existe pas dans l'API Batch
        msg = _batch({**params, "output_config": {"effort": "low"}}, "analyse")
    else:
        msg = client().messages.create(**params, **_extra("low"))
    _track(msg, "analyse", batch=BATCH)
    _check(msg)
    for b in msg.content:
        if b.type == "tool_use":
            return dict(b.input)
    raise RuntimeError("Analyse : pas de réponse structurée de Claude.")


# ------------------------------------------------------------------ texte long (streaming)

MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "64000"))   # inclut la réflexion de Claude


def _long_call(content: list, step: str) -> str:
    """Appel en streaming ; si la réponse est coupée (max_tokens), on demande la suite (jusqu'à 3 fois)."""
    messages = [{"role": "user", "content": content}]
    out = ""
    for _ in range(4):
        with client().messages.stream(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, messages=messages,
                                      **_extra(EFFORT)) as stream:
            msg = stream.get_final_message()
        _track(msg, step)
        _check(msg)
        part = "".join(b.text for b in msg.content if b.type == "text")
        out += part
        if msg.stop_reason != "max_tokens":
            break
        messages = messages[:1] + [
            {"role": "assistant", "content": out.rstrip()},
            {"role": "user", "content": "Continue exactement là où tu t'es arrêté, sans rien répéter."},
        ]
    return out


def _between(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return (m.group(1) if m else "").strip()


HTML_RULES = """
Règles de format (TRÈS important) :
- Écris du HTML simple (pas de <html>, <head>, <body>, pas de CSS, pas de <script>).
- Chaque exercice : <section class="ex"><h2>Exercice N</h2> … </section>
- Mathématiques en LaTeX entre \\( … \\) (en ligne) ou \\[ … \\] (centré). Ex. \\(\\dfrac{3}{4}\\), \\(\\sqrt{2}\\), \\(10^{-3}\\).
  Utilise \\dfrac pour les fractions. Virgule décimale : {,} ex. \\(2{,}5\\).
- Listes : <p>, <ol>, <ul>, tableaux <table> (avec <th>).
- Droite graduée : <div class="droite" data-a="-3" data-b="1" data-ga="]" data-gb="["></div>
  (data-a / data-b = bornes, "-inf" ou "+inf" pour l'infini ; data-ga / data-gb = crochet "[" ou "]").
- Aucun commentaire en dehors des balises demandées.
"""


def correction(pdf_path: str, exercices_retires: list[int]) -> tuple[str, list[str]]:
    """Retourne (html_correction, liste_des_erreurs_de_l_enonce)."""
    if MOCK:
        return MOCK_CORRECTION, ["(mode test) aucune vérification réelle"]
    retires = (f"Les exercices {', '.join(map(str, exercices_retires))} ont été retirés du sujet : ne les corrige pas, "
               "et renumérote les exercices restants à partir de 1 dans l'ordre.") if exercices_retires else ""
    text = _long_call([
        _pdf_block(pdf_path),
        {"type": "text", "text": f"""Rédige la correction COMPLÈTE et détaillée de cette série, pour des élèves.
{retires}
- Corrige tous les exercices et toutes les questions, étape par étape, comme au tableau.
- Mets chaque résultat final en évidence avec <b class="res">…</b>.
- QCM : un tableau <table><tr><th>N°</th><th>Justification</th><th>Réponse</th></tr>…</table>.
- Tableaux de valeurs approchées / arrondis / troncatures : remplis-les entièrement.
- Si l'énoncé contient une erreur (aucune bonne réponse dans un QCM, deux bonnes réponses, « montrer que » faux,
  faute de frappe, lettres incohérentes…), ajoute <p class="note">✱ …</p> dans l'exercice concerné et liste-la aussi plus bas.
{HTML_RULES}
Réponds exactement sous cette forme :
<correction>
… HTML de la correction …
</correction>
<erreurs>
<li>Exercice N : description courte de l'erreur de l'énoncé</li>
</erreurs>"""},
    ], "correction")
    html = _between(text, "correction") or text
    errs = re.findall(r"<li>(.*?)</li>", _between(text, "erreurs"), re.S)
    return html, [re.sub(r"<[^>]+>", "", e).strip() for e in errs]


def transcription(pdf_path: str, exercices_retires: list[int], images: list[bytes]) -> str:
    """Re-saisit le sujet proprement en HTML (utile si le PDF est scanné ou si on retire des exercices)."""
    if MOCK:
        return MOCK_SUJET
    content: list = [_pdf_block(pdf_path)]
    img_txt = ""
    if images:
        img_txt = ("Les figures du sujet sont fournies ci-dessous comme images numérotées fig1, fig2, … "
                   "Place chaque figure au bon endroit avec <img class=\"fig\" src=\"fig1\">.")
        for i, png in enumerate(images, 1):
            content += [{"type": "text", "text": f"fig{i} :"}, _img_block(png)]
    retires = (f"RETIRE les exercices {', '.join(map(str, exercices_retires))} et renumérote les autres à partir de 1."
               if exercices_retires else "Garde tous les exercices avec leur numérotation.")
    content.append({"type": "text", "text": f"""Recopie fidèlement l'ÉNONCÉ de cette série (pas de correction), sans l'en-tête
ni le pied de page d'origine (pas de nom de professeur ni d'école). Ne change aucun nombre, aucune formule.
{retires}
{img_txt}
{HTML_RULES}
Réponds exactement sous la forme :
<sujet>
… HTML …
</sujet>"""})
    text = _long_call(content, "re-saisie")
    return _between(text, "sujet") or text


# ------------------------------------------------------------------ données de test

MOCK_CORRECTION = r"""
<section class="ex"><h2>Exercice 1</h2>
<p>\(A = 27 + \dfrac{5}{3} = \dfrac{81}{3} + \dfrac{5}{3} = \) <b class="res">\(\dfrac{86}{3}\)</b></p>
<p>\(|2x+5| = 2 \iff x = -\dfrac{3}{2}\) ou \(x = -\dfrac{7}{2}\)</p>
<table><tr><th>N°</th><th>Justification</th><th>Réponse</th></tr>
<tr><td>1</td><td>\(5-(-3)=8\)</td><td><b class="res">B</b></td></tr></table>
<div class="droite" data-a="-3" data-b="1" data-ga="]" data-gb="["></div>
<div class="droite" data-a="5" data-b="+inf" data-ga="[" data-gb="["></div>
<p class="note">✱ Exemple de note sur une erreur de l'énoncé.</p>
</section>"""

MOCK_SUJET = r"""
<section class="ex"><h2>Exercice 1</h2><p>Calculer \(A = 27 - (-1)\times\dfrac{5}{3}\).</p></section>"""
