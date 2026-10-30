# Séries de maths – en-tête + correction automatique

Petite application web pour les professeurs de maths. On y envoie une série d'exercices
(PDF ou Word), et on récupère :

- **le sujet avec son propre en-tête** : école, nom du prof, année, classes. Les anciens noms, logos, bandeaux et filigranes sont enlevés ;
- **une correction détaillée**, dans un PDF à part. Les étapes de calcul sont expliquées, les droites graduées sont dessinées et les QCM sont justifiés ;
- **la liste des erreurs trouvées dans l'énoncé** : QCM sans bonne réponse, « montrer que » faux, etc.

La lecture du sujet et la correction sont faites par Claude, via l'API Anthropic.

## Démarrage rapide (Docker)

```bash
cp .env.example .env        # puis mets ta clé ANTHROPIC_API_KEY et un APP_PASSWORD
docker compose up --build
```

Ouvre ensuite http://localhost:8000.

## Sans Docker (développement)

Il faut Python 3.11 ou plus récent et LibreOffice (avec le module Math) pour lire les fichiers Word.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium
# Ubuntu/Debian : sudo apt install libreoffice-writer libreoffice-math
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

Pour tester **sans clé API**, lance avec `MOCK_CLAUDE=1`. Claude est alors simulé : les PDF sont générés, mais la correction est un exemple.

```bash
MOCK_CLAUDE=1 pytest -q
```

## Mise en ligne (pour partager l'app)

N'importe quel hébergeur qui accepte un Dockerfile convient : Render, Railway, Fly.io, un VPS…

1. Pousse ce dossier sur GitHub.
2. Crée un service « Docker » chez l'hébergeur. Le port est lu dans la variable `PORT` (8000 par défaut).
3. Ajoute les variables d'environnement `ANTHROPIC_API_KEY` et `APP_PASSWORD`.
4. Prévois au moins **1 Go de RAM**, car LibreOffice et Chromium en consomment.
5. Envoie l'adresse et le mot de passe à tes collègues.

### Render (gratuit)

L'offre gratuite de Render (512 Mo de RAM) suffit si l'app traite **une série à la fois** : le pic mesuré
est d'environ 300 Mo. Le fichier `render.yaml` règle tout (`WORKERS=1`, `FORWARDED_ALLOW_IPS=*`, test de santé).

1. Pousse ce dossier sur GitHub (un dépôt privé convient).
2. Sur https://render.com : **New → Blueprint**, puis choisis le dépôt. Render demande `ANTHROPIC_API_KEY`
   et `APP_PASSWORD`.
3. Le premier build prend 5 à 10 minutes. Ensuite, chaque `git push` redéploie l'app.

Le service gratuit s'endort après 15 minutes sans visite. La visite suivante le réveille en une minute
environ, et les séries de la veille sont alors perdues : il suffit de les renvoyer.

> ⚠️ Mets toujours un `APP_PASSWORD` d'au moins 12 caractères. Si tu l'oublies, l'app en tire un au hasard à chaque démarrage et l'affiche dans les logs : personne ne peut utiliser ta clé sans mot de passe.
>
> Mets l'app derrière **HTTPS** (Render, Railway et Fly.io le font d'office). Sinon le mot de passe circule en clair sur le réseau.

## Sécurité

- **Connexion** : le mot de passe est demandé une fois. Le serveur donne ensuite un cookie de session `HttpOnly`, `SameSite=Strict`, et `Secure` en HTTPS, valable 7 jours. Toutes les routes `/api/jobs…`, téléchargements compris, l'exigent.
- **Essais limités** : 5 mauvais mots de passe en 15 minutes depuis la même adresse bloquent les essais. Chez un hébergeur qui passe par un proxy (Render, Railway…), mets `FORWARDED_ALLOW_IPS=*` pour qu'uvicorn voie la vraie adresse des visiteurs. Ne le mets pas si le port 8000 est exposé directement.
- **Crédits protégés** : 30 séries par 24 h au maximum (`MAX_JOBS_PER_DAY`), 5 séries en cours à la fois, 20 pages par document (`MAX_PAGES`). Un même fichier renvoyé dans les 24 h (par exemple pour corriger l'en-tête) réutilise la réponse de Claude déjà payée.
- **Fichiers vérifiés** : le contenu doit vraiment être un PDF ou un Word, quelle que soit l'extension. Les fichiers Word avec `DOCTYPE` ou `ENTITY` sont refusés (attaques XXE, qui pourraient lire des fichiers du serveur comme la clé API), ainsi que les « bombes zip ».
- **Navigateur** : Content-Security-Policy stricte, page impossible à intégrer dans un autre site, pas de `/docs` public, vérification de l'origine des requêtes (CSRF).
- **Rendu PDF** : Chromium n'a aucun accès réseau, et les liens `file:`, `http:`, `https:` et `javascript:` sont retirés du HTML écrit par Claude.

## Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `ANTHROPIC_API_KEY` | Clé API Claude (obligatoire) | – |
| `APP_PASSWORD` | Mot de passe demandé dans la page (12 caractères minimum conseillés) | tiré au hasard, affiché dans les logs |
| `CLAUDE_MODEL` | Modèle utilisé | `claude-opus-5-5` |
| `CORRECTION` | `1` pour proposer aussi la correction détaillée (coûte beaucoup plus de crédits et de temps) | `0` (désactivée) |
| `RESAISIE` | `1` pour proposer aussi « re-saisir proprement » (scans, exercices à retirer). Sinon, un scan garde son image avec le nouvel en-tête | `0` (désactivée) |
| `CLAUDE_BATCH` | `1` : l'analyse passe par l'API Batch d'Anthropic, moitié prix mais 1 à quelques minutes d'attente (24 h au pire). `0` : réponse en quelques secondes | `1` |
| `CLAUDE_EFFORT` | Réflexion de Claude pour la correction et la re-saisie : `low`, `medium` ou `high` (moins = moins cher) | `medium` |
| `CLAUDE_MAX_TOKENS` | Longueur maximale d'une réponse (une suite est demandée automatiquement si besoin) | `64000` |
| `MAX_UPLOAD_MB` | Taille maximale d'un fichier | `25` |
| `MAX_PAGES` | Nombre maximal de pages d'un document | `20` |
| `MAX_JOBS_PER_DAY` | Nombre maximal de séries par 24 h | `30` |
| `FORWARDED_ALLOW_IPS` | Proxies dont uvicorn croit l'en-tête `X-Forwarded-For` (`*` derrière Render, Railway…) | `127.0.0.1` |
| `WORKERS` | Nombre de séries traitées en parallèle | `2` |
| `DATA_DIR` | Dossier des fichiers temporaires (effacés après 24 h) | `/tmp/series-jobs` |
| `CHROMIUM_PATH` | Chemin vers Chrome si Playwright ne l'a pas installé | – |

`claude-sonnet-5-5` coûte moins cher. `claude-opus-5-5` est plus fiable pour lire les formules et
éviter les erreurs de calcul, et c'est le modèle conseillé pour les corrections.

## Comment ça marche

```
fichier ──► (Word ? ─► nettoyage du .docx ─► LibreOffice ─► PDF)
        ──► Claude : analyse (anciens noms, titre, nb d'exercices)
        ──► SUJET
              • mode « garder la mise en page » : PyMuPDF
                  - neutralise les en-têtes/pieds de page Word (filigranes, bandeaux)
                  - efface le texte en diagonale et les anciens noms (ligne entière si courte)
                  - supprime les logos (haut de la page 1, ou répétés sur plusieurs pages)
                  - recadre le contenu sous le nouvel en-tête + nouveau pied de page
              • mode « re-saisir » : Claude recopie l'énoncé en HTML + LaTeX ─► PDF
                (automatique si le PDF est scanné ou si on retire des exercices)
        ──► CORRECTION : Claude rédige en HTML + LaTeX ─► KaTeX + Chromium ─► PDF
```

| Fichier | Contenu |
|---|---|
| `app/main.py` | Serveur FastAPI, tâches en arrière-plan, téléchargement des fichiers |
| `app/pipeline.py` | Enchaînement des étapes |
| `app/claude_ai.py` | Prompts et appels à l'API Claude (PDF envoyé en document) |
| `app/rebrand.py` | Nettoyage du PDF et nouvel en-tête (PyMuPDF) |
| `app/convert.py` | Word ─► PDF, effacement des noms dans le .docx, extraction des figures |
| `app/omml_fix.py` | Correctif des formules Word que LibreOffice affiche mal (« ¿ ») |
| `app/render.py` | HTML ─► PDF (KaTeX local, droites graduées, en-tête, pied de page) |
| `app/templates/index.html`, `app/static/app.js` | L'interface (une seule page en Vue 3, en français) |

## Limites connues

- **Toujours relire la correction** avant de la distribuer. Claude est très bon en calcul, mais il peut se tromper, surtout si une formule est mal lisible dans le PDF.
- Les PDF **scannés** (photos) sont re-saisis : les figures dessinées à la main ne sont pas reprises.
- Un nom intégré **dans une image** (logo) n'est effacé que s'il est en haut de la page 1, ou en haut de plusieurs pages.
- Les tâches sont gardées en mémoire. Si le serveur redémarre, les tâches en cours sont perdues ; il suffit de renvoyer le fichier. Les séries terminées, elles, sont enregistrées dans le navigateur de chaque utilisateur (historique avec les PDF), sur cet appareil seulement. Les séries terminées, elles, sont enregistrées dans le navigateur de chaque utilisateur (historique avec les PDF), sur cet appareil seulement.
- Pour plusieurs dizaines d'utilisateurs simultanés, il faudrait passer à une vraie file de tâches (Redis/RQ, Celery).
