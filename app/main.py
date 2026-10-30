"""Serveur web : page d'accueil + API de traitement (tâches en arrière-plan)."""
from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline
from .rebrand import Identity

DATA = Path(os.environ.get("DATA_DIR", "/tmp/series-jobs"))
DATA.mkdir(parents=True, exist_ok=True)
MOCK = os.environ.get("MOCK_CLAUDE") == "1"
PASSWORD = os.environ.get("APP_PASSWORD", "")
if not PASSWORD and not MOCK:
    # jamais d'accès libre à la clé API : sans APP_PASSWORD, un mot de passe est tiré au sort à chaque démarrage
    PASSWORD = secrets.token_urlsafe(12)
    print(f"[sécurité] APP_PASSWORD n'est pas défini. Mot de passe pour cette session : {PASSWORD}", flush=True)
elif PASSWORD and len(PASSWORD) < 12:
    print("[sécurité] APP_PASSWORD est court : choisis au moins 12 caractères.", flush=True)
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
MAX_JOBS_DAY = int(os.environ.get("MAX_JOBS_PER_DAY", "30"))   # protège les crédits si le mot de passe fuite
CORRECTION = os.environ.get("CORRECTION", "0") == "1"   # la correction coûte cher en crédits : désactivée par défaut
MAX_PENDING = 5
KEEP_HOURS = 24
SESSION_DAYS = 7
COOKIE = "sm_session"
LOGIN_TRIES, LOGIN_WINDOW = 5, 15 * 60

# pas de /docs ni /openapi.json : inutile de publier le plan de l'API
app = FastAPI(title="Séries de maths", docs_url=None, redoc_url=None, openapi_url=None)
# Les tâches sont gardées en mémoire : lancer un seul processus uvicorn (pas de --workers).
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
pool = ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "2")))
jobs: dict[str, dict] = {}
sessions: dict[str, float] = {}          # jeton -> date d'expiration
login_fails: dict[str, list[float]] = {}  # adresse IP -> dates des essais ratés
INDEX = Path(__file__).parent / "templates" / "index.html"

CSP = ("default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")   # unsafe-eval : Vue compile les gabarits de la page


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    r = await call_next(request)
    r.headers["Content-Security-Policy"] = CSP
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        r.headers["Cache-Control"] = "no-store"
    elif request.url.path == "/" or request.url.path.endswith((".js", ".css")):
        # le navigateur revérifie la page et ses scripts : après une mise à jour, jamais d'ancien HTML avec un nouveau script
        r.headers["Cache-Control"] = "no-cache"
    if _https(request):
        r.headers["Strict-Transport-Security"] = "max-age=31536000"
    return r


def _logged_in(request: Request) -> bool:
    if not PASSWORD:
        return True
    exp = sessions.get(request.cookies.get(COOKIE) or "")
    return bool(exp and exp > time.time())


def require_login(request: Request):
    if not _logged_in(request):
        raise HTTPException(401, "Connecte-toi d'abord.")


def same_origin(request: Request):
    """Contre les requêtes envoyées depuis un autre site (CSRF), en plus du cookie SameSite=Strict."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Requête refusée.")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _cleanup():
    """Supprime les tâches de plus de 24 h, y compris les dossiers laissés par un redémarrage du serveur."""
    now = time.time()
    limit = now - KEEP_HOURS * 3600
    for tok, exp in list(sessions.items()):
        if exp < now:
            sessions.pop(tok, None)
    for jid, j in list(jobs.items()):
        if j["created"] < limit:
            jobs.pop(jid, None)
    for d in DATA.iterdir():
        try:
            if d.name == "cache":   # réponses de Claude réutilisables : chaque fichier vit 24 h
                for f in d.iterdir():
                    if f.stat().st_mtime < limit:
                        f.unlink()
            elif d.is_dir() and d.name not in jobs and d.stat().st_mtime < limit:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _int_list(s: str) -> list[int]:
    out = []
    for part in (s or "").replace(";", ",").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a.strip().isdigit() and b.strip().isdigit():
                out += list(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out))


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.read_text(encoding="utf-8")   # relu à chaque fois : page et app.js toujours de la même version


@app.get("/api/health")
def health(request: Request):
    return {"ok": True, "api_key": bool(os.environ.get("ANTHROPIC_API_KEY")) or MOCK, "mode_test": MOCK,
            "mot_de_passe": bool(PASSWORD), "connecte": _logged_in(request), "max_mb": MAX_MB,
            "correction": CORRECTION, "resaisie": pipeline.RESAISIE}


@app.post("/api/login", dependencies=[Depends(same_origin)])
async def login(request: Request, response: Response, password: str = Form("")):
    if not PASSWORD:
        return {"ok": True}
    ip, now = _client_ip(request), time.time()
    recent = [t for t in login_fails.get(ip, []) if t > now - LOGIN_WINDOW]
    login_fails[ip] = recent
    if len(recent) >= LOGIN_TRIES:
        raise HTTPException(429, "Trop d'essais ratés. Réessaie dans 15 minutes.")
    if not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        recent.append(now)
        await asyncio.sleep(1)   # ralentit les essais en série
        raise HTTPException(401, "Mot de passe incorrect.")
    login_fails.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    sessions[tok] = now + SESSION_DAYS * 86400
    response.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict",
                        secure=_https(request), path="/")
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(same_origin)])
def logout(request: Request, response: Response):
    sessions.pop(request.cookies.get(COOKIE) or "", None)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


_MAGIC = {".pdf": lambda h: b"%PDF" in h[:1024],
          ".docx": lambda h: h.startswith(b"PK\x03\x04"),
          ".doc": lambda h: h.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))}


@app.post("/api/jobs", dependencies=[Depends(require_login), Depends(same_origin)])
async def create_job(
    fichier: UploadFile = File(...),
    ecole: str = Form("ECOLE", max_length=120), prof: str = Form("PROF", max_length=120),
    annee: str = Form("", max_length=40), classes: str = Form("", max_length=120),
    matiere: str = Form("Mathématiques", max_length=80), titre: str = Form("", max_length=160),
    sujet: bool = Form(True), correction: bool = Form(True), mode: str = Form("original"),
    retirer: str = Form("", max_length=200), effacer: str = Form("", max_length=3000),
):
    _cleanup()
    correction = correction and CORRECTION
    name = Path(fichier.filename or "sujet.pdf").name
    ext = Path(name).suffix.lower()
    if ext not in _MAGIC:
        raise HTTPException(400, "Formats acceptés : PDF ou Word (.docx).")
    if not (sujet or correction):
        raise HTTPException(400, "Coche au moins « sujet » ou « correction »." if CORRECTION else "Coche « sujet ».")
    if sum(j["etat"] in ("en_attente", "en_cours") for j in jobs.values()) >= MAX_PENDING:
        raise HTTPException(429, "Plusieurs séries sont déjà en cours : attends qu'elles se terminent.")
    if sum(j["created"] > time.time() - 86400 for j in jobs.values()) >= MAX_JOBS_DAY:
        raise HTTPException(429, f"Limite de {MAX_JOBS_DAY} séries par 24 h atteinte (MAX_JOBS_PER_DAY).")

    jid = uuid.uuid4().hex   # 128 bits : le lien de téléchargement ne se devine pas
    wd = DATA / jid
    wd.mkdir(parents=True)
    src = wd / ("source" + ext)
    size = 0
    with src.open("wb") as f:
        while chunk := await fichier.read(1 << 20):
            size += len(chunk)
            if size > MAX_MB << 20:
                shutil.rmtree(wd, ignore_errors=True)
                raise HTTPException(413, f"Fichier trop gros (max {MAX_MB} Mo).")
            f.write(chunk)
    with src.open("rb") as f:
        if not _MAGIC[ext](f.read(1024)):
            shutil.rmtree(wd, ignore_errors=True)
            raise HTTPException(400, "Le contenu du fichier ne correspond pas à un PDF ou un Word.")
    src = src.rename(wd / (pipeline.slug(Path(name).stem) + ext))

    opt = pipeline.Options(
        ident=Identity(ecole=ecole.strip(), prof=prof.strip(), annee=annee.strip(), classes=classes.strip(),
                       matiere=matiere.strip(), titre=titre.strip() or "SERIE"),
        titre_auto=not titre.strip(), faire_sujet=sujet, faire_correction=correction,
        mode="resaisir" if mode == "resaisir" and pipeline.RESAISIE else "original",
        exercices_retires=_int_list(retirer) if pipeline.RESAISIE else [],   # retirer un exercice demande la re-saisie
        textes_a_effacer=[t.strip() for t in effacer.splitlines() if len(t.strip()) >= 3],
    )
    job = {"id": jid, "nom": name, "etat": "en_attente", "etapes": [], "created": time.time(), "resultat": None, "erreur": None}
    jobs[jid] = job

    def log(msg):
        job["etapes"].append(msg)

    def work():
        job["etat"] = "en_cours"
        try:
            job["resultat"] = pipeline.run(src, wd, opt, log)
            job["etat"] = "termine"
        except Exception as e:   # message lisible pour l'utilisateur, détail dans les logs
            traceback.print_exc()
            job["etat"] = "erreur"
            job["erreur"] = _friendly(e)

    pool.submit(work)
    return {"id": jid}


def _friendly(e: Exception) -> str:
    t = type(e).__name__
    if t == "AuthenticationError":
        return "Clé API Claude invalide ou absente (ANTHROPIC_API_KEY)."
    if t == "RateLimitError":
        return "Trop de demandes à Claude en même temps, réessaie dans une minute."
    if t in ("APIConnectionError", "APITimeoutError"):
        return "Impossible de joindre l'API Claude. Vérifie la connexion du serveur."
    if t == "BadRequestError" and "credit" in str(e).lower():
        return "Crédit API Claude épuisé."
    return str(e) if isinstance(e, RuntimeError) else f"Erreur inattendue ({t}). Réessaie ou contacte l'administrateur."


@app.get("/api/jobs/{jid}", dependencies=[Depends(require_login)])
def get_job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Tâche introuvable (les fichiers sont gardés 24 h).")
    return JSONResponse({k: v for k, v in j.items() if k != "created"})


@app.get("/api/jobs/{jid}/files/{name}", dependencies=[Depends(require_login)])
def get_file(jid: str, name: str):
    j = jobs.get(jid)
    if not j or not j.get("resultat") or name not in j["resultat"]["fichiers"]:
        raise HTTPException(404, "Fichier introuvable.")
    return FileResponse(DATA / jid / name, media_type="application/pdf", filename=name)
