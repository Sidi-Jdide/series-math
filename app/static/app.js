// Interface de l'application (Vue 3, sans étape de compilation).
const { createApp, reactive, ref, computed, watch, onMounted, onUnmounted, markRaw } = Vue;

// Stockage sûr (navigation privée, stockage bloqué…)
const store = s => ({
  get(k, d) { try { const v = s().getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { s().setItem(k, JSON.stringify(v)); } catch (e) {} },
});
const local = store(() => localStorage);

// Historique des séries terminées, PDF compris, gardé dans le navigateur (IndexedDB) : il survit aux 24 h du serveur.
const historique = (() => {
  let base;
  const open = () => base || (base = new Promise((ok, ko) => {
    const r = indexedDB.open("series-maths", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("series", { keyPath: "id" });
    r.onsuccess = () => ok(r.result);
    r.onerror = () => ko(r.error);
  }));
  const run = async (mode, fn) => {
    const t = (await open()).transaction("series", mode);
    const q = fn(t.objectStore("series"));
    return new Promise((ok, ko) => { t.oncomplete = () => ok(q.result); t.onerror = () => ko(t.error); });
  };
  return {
    all: () => run("readonly", s => s.getAll()),
    put: v => run("readwrite", s => s.put(v)),
    del: id => run("readwrite", s => s.delete(id)),
  };
})();
const blobUrls = fichiers => Object.fromEntries(fichiers.map(f => [f.nom, URL.createObjectURL(f.blob)]));
// Téléphone : partage des PDF par la feuille de partage du système (WhatsApp, Gmail, Drive…)
const canShare = (() => {
  try { return !!navigator.canShare && navigator.canShare({ files: [new File(["%PDF"], "t.pdf", { type: "application/pdf" })] }); }
  catch (e) { return false; }
})();
const tactile = (() => { try { return matchMedia("(pointer: coarse)").matches; } catch (e) { return false; } })();
const pdfFiles = fichiers => fichiers.map(f => new File([f.blob], f.nom, { type: "application/pdf" }));

const FIELDS = ["ecole", "prof", "annee", "classes", "matiere", "titre"];
function loadIdent() {
  const saved = local.get("sm_ident", null);
  if (saved) return { ...Object.fromEntries(FIELDS.map(k => [k, ""])), matiere: "Mathématiques", ...saved };
  // ancienne version de la page : une clé par champ, en texte brut
  const old = {};
  FIELDS.forEach(k => { try { old[k] = localStorage.getItem("sm_" + k) || ""; } catch (e) { old[k] = ""; } });
  try { localStorage.removeItem("sm_password"); } catch (e) {}
  return { ...old, matiere: old.matiere || "Mathématiques" };
}

createApp({
  setup() {
    const ident = reactive(loadIdent());
    const opts = reactive({ sujet: true, correction: true, mode: "original", retirer: "", effacer: "", ...local.get("sm_opts", {}) });
    const health = reactive({ ok: null, api_key: true, mode_test: false, mot_de_passe: false, connecte: true, max_mb: 25,
                              correction: false, resaisie: false });
    const login = reactive({ password: "", error: "", busy: false });
    const jobs = ref([]);
    const dragging = ref(false), showErrors = ref(false), dropError = ref("");
    const fileInput = ref(null);
    const now = ref(Date.now());
    let seq = 0, ticker;
    // deux vues : les séries et les statistiques (#stats), le bouton « retour » du téléphone fonctionne
    const vue = ref(location.hash === "#stats" ? "stats" : "series");
    const go = v => { vue.value = v; history.replaceState(null, "", v === "stats" ? "#stats" : location.pathname); scrollTo(0, 0); };
    const stats = ref(local.get("sm_stats", []));   // une ligne par série terminée, gardée même si on l'efface de l'historique

    watch(ident, v => local.set("sm_ident", v), { deep: true });
    // correction désactivée sur le serveur (CORRECTION=0) : on ne la demande pas
    watch(() => health.correction, on => { if (!on) opts.correction = false; }, { immediate: true });
    // re-saisie désactivée (RESAISIE=0) : mise en page d'origine seulement, pas d'exercices retirés
    watch(() => health.resaisie, on => { if (!on) Object.assign(opts, { mode: "original", retirer: "" }); }, { immediate: true });
    watch(() => ({ sujet: opts.sujet, correction: opts.correction, mode: opts.mode }), v => local.set("sm_opts", v));
    try { sessionStorage.removeItem("sm_pw"); localStorage.removeItem("sm_password"); } catch (e) {}
    // séries en cours seulement : les terminées sont dans l'historique du navigateur
    const saveJobs = () => local.set("sm_jobs", jobs.value.filter(j => j.id && !j.garde)
      .map(j => ({ id: j.id, nom: j.nom, debut: j.debut, prevu: j.prevu })));

    const y = new Date().getFullYear(), m = new Date().getMonth();
    const anneeCourante = m >= 7 ? `${y}.${y + 1}` : `${y - 1}.${y}`;

    const identOk = computed(() => ident.ecole.trim() && ident.prof.trim());
    const editIdent = ref(!identOk.value);   // en-tête déjà rempli : on l'affiche replié
    const needLogin = computed(() => health.mot_de_passe && !health.connecte);
    const canSend = computed(() => (opts.sujet || opts.correction) && health.api_key !== false);
    const status = computed(() => {
      if (health.ok === null) return { cls: "", label: "Connexion…", title: "" };
      if (health.ok === false) return { cls: "err", label: "Serveur injoignable", title: "Le serveur ne répond pas." };
      if (!health.api_key) return { cls: "err", label: "Clé API manquante", title: "ANTHROPIC_API_KEY n'est pas configurée sur le serveur." };
      if (health.mode_test) return { cls: "warn", label: "Mode test", title: "Claude est simulé : la correction est un exemple." };
      return { cls: "ok", label: "Prêt", title: "Le serveur et Claude sont prêts." };
    });

    // ---------------------------------------------------------------- envoi
    const pick = () => { if (canSend.value) fileInput.value.click(); };
    const onPick = e => { send([...e.target.files]); e.target.value = ""; };
    const onDrop = e => { dragging.value = false; send([...e.dataTransfer.files]); };

    function send(files) {
      dropError.value = "";
      if (!files.length) return;
      showErrors.value = true;
      if (!opts.sujet && !opts.correction) return (dropError.value = "Coche au moins « Sujet » ou « Correction ».");
      if (!identOk.value) return (dropError.value = "Remplis au moins l'école et le nom du professeur (étape 1).");
      const bad = files.filter(f => !/\.(pdf|docx?)$/i.test(f.name));
      const big = files.filter(f => !bad.includes(f) && f.size > health.max_mb * 1048576);
      dropError.value = [
        bad.length && `Ignoré (pas un PDF ni un Word) : ${bad.map(f => f.name).join(", ")}.`,
        big.length && `Trop gros (max ${health.max_mb} Mo) : ${big.map(f => f.name).join(", ")}.`,
      ].filter(Boolean).join(" ");
      files.filter(f => !bad.includes(f) && !big.includes(f)).forEach(f => upload(newJob(f)));
      showErrors.value = false;
      editIdent.value = false;
    }

    function expectedSteps(name) {
      let n = 1;                                            // lecture par Claude
      if (!/\.pdf$/i.test(name)) n++;                       // conversion Word
      if (opts.sujet) n += opts.mode === "resaisir" || opts.retirer.trim() ? 2 : 1;
      if (opts.correction) n += 2;
      return n;
    }

    function newJob(file) {
      const j = reactive({ key: ++seq, id: null, nom: file.name, etat: "envoi", envoi: 0, etapes: [], resultat: null,
                           erreur: null, debut: Date.now(), prevu: expectedSteps(file.name), file: markRaw(file) });
      jobs.value.unshift(j);
      return j;
    }

    function upload(j) {
      const fd = new FormData();
      fd.append("fichier", j.file);
      FIELDS.forEach(k => fd.append(k, ident[k].trim()));
      fd.append("sujet", opts.sujet);
      fd.append("correction", opts.correction);
      fd.append("mode", opts.mode);
      fd.append("retirer", opts.retirer);
      fd.append("effacer", opts.effacer);
      // XMLHttpRequest plutôt que fetch : on peut suivre la progression de l'envoi
      const x = new XMLHttpRequest();
      x.open("POST", "/api/jobs");
      x.upload.onprogress = e => { if (e.lengthComputable) j.envoi = Math.round(100 * e.loaded / e.total); };
      x.onerror = () => fail(j, "Le serveur ne répond pas.");
      x.onload = () => {
        let data = {};
        try { data = JSON.parse(x.responseText); } catch (e) {}
        if (x.status === 401) health.connecte = false;
        if (x.status >= 400) return fail(j, data.detail || "Erreur à l'envoi.");
        j.id = data.id;
        j.etat = "en_attente";
        saveJobs();
        poll(j);
      };
      x.send(fd);
    }

    function fail(j, msg) { j.etat = "erreur"; j.erreur = msg; }

    async function poll(j) {
      if (!jobs.value.includes(j)) return;
      let r, data;
      try {
        r = await fetch(`/api/jobs/${j.id}`);
        data = await r.json();
      } catch (e) { return setTimeout(() => poll(j), 4000); }
      if (r.status === 404) { remove(j); return; }             // tâche expirée (24 h) ou serveur redémarré
      if (r.status === 401) { health.connecte = false; return; } // session expirée : on reprendra après connexion
      if (!r.ok) return fail(j, data.detail || "Erreur.");
      Object.assign(j, { etat: data.etat, etapes: data.etapes || [], resultat: data.resultat, erreur: data.erreur });
      if (j.etat === "en_attente" || j.etat === "en_cours") setTimeout(() => poll(j), 2500);
      if (j.etat === "termine") { record(j.id, j.debut, j.resultat); keep(j); }
    }

    function record(id, debut, r) {
      if (!r || stats.value.some(x => x.id === id)) return;
      stats.value = [...stats.value, { id, d: debut || Date.now(), cout: r.cout_estime || 0, pages: r.pages || 0,
                                       ex: r.exercices || 0, titre: r.titre || "" }];
      local.set("sm_stats", stats.value);
    }

    // copie des PDF dans le navigateur dès que la série est terminée
    async function keep(j) {
      try {
        const fichiers = [];
        for (const f of j.resultat.fichiers) {
          const r = await fetch(fileUrl(j, f));
          if (!r.ok) return;
          fichiers.push({ nom: f, blob: await r.blob() });
        }
        await historique.put({ id: j.id, nom: j.nom, debut: j.debut, resultat: JSON.parse(JSON.stringify(j.resultat)), fichiers });
        Object.assign(j, { garde: true, urls: blobUrls(fichiers), pdfs: markRaw(pdfFiles(fichiers)) });
        saveJobs();
        if (navigator.storage && navigator.storage.persist) navigator.storage.persist();   // évite l'effacement auto
      } catch (e) {}   // navigation privée, stockage plein… : les liens du serveur restent valables 24 h
    }

    async function loadHistory() {
      let saved = [];
      try { saved = await historique.all(); } catch (e) { return; }
      for (const h of saved.sort((a, b) => b.debut - a.debut)) {
        record(h.id, h.debut, h.resultat);   // séries enregistrées avant l'arrivée des statistiques
        if (jobs.value.some(j => j.id === h.id)) continue;
        jobs.value.push(reactive({ key: ++seq, id: h.id, nom: h.nom, debut: h.debut, etat: "termine", envoi: 100, etapes: [],
                                   resultat: h.resultat, erreur: null, file: null, prevu: 1, garde: true, urls: blobUrls(h.fichiers),
                                   pdfs: markRaw(pdfFiles(h.fichiers)) }));
      }
    }

    function retry(j) {
      const file = j.file;
      remove(j);
      upload(newJob(file));
    }

    function remove(j) { jobs.value = jobs.value.filter(x => x !== j); saveJobs(); }
    async function forget(j) {   // bouton ✕ : une série de l'historique est supprimée pour de bon
      if (j.garde) {
        if (!confirm(`Supprimer « ${j.nom} » de l'historique ? Les PDF enregistrés dans ce navigateur seront effacés.`)) return;
        try { await historique.del(j.id); } catch (e) {}
        Object.values(j.urls || {}).forEach(u => URL.revokeObjectURL(u));
      }
      remove(j);
    }
    // les PDF sont déjà en mémoire : navigator.share est appelé tout de suite, dans le geste de l'utilisateur
    async function share(j) {
      try { await navigator.share({ files: j.pdfs, title: j.resultat.titre || j.nom }); }
      catch (e) { if (e.name !== "AbortError") alert("Partage impossible : télécharge le PDF puis partage-le."); }
    }
    function clearErrors() { jobs.value = jobs.value.filter(j => j.etat !== "erreur"); saveJobs(); }

    // ---------------------------------------------------------------- statistiques
    const JOURS = 30;
    const dayKey = t => { const d = new Date(t); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
    const st = computed(() => {
      const l = stats.value, n = new Date();
      const debutMois = new Date(n.getFullYear(), n.getMonth(), 1).getTime();
      const cout = l.reduce((a, x) => a + x.cout, 0);
      const parJour = {};
      l.forEach(x => { const k = dayKey(x.d); parJour[k] = (parJour[k] || 0) + 1; });
      const jours = [];
      for (let i = JOURS - 1; i >= 0; i--) {
        const d = new Date(n.getFullYear(), n.getMonth(), n.getDate() - i);
        jours.push({ d, n: parJour[dayKey(d)] || 0 });
      }
      const max = Math.max(1, ...jours.map(j => j.n));
      const chap = {};
      l.forEach(x => { const t = (x.titre || "").replace(/^S[ÉE]RIE\s*(N°\s*\d+)?\s*:?\s*/i, "").trim() || "Sans titre";
                       chap[t] = (chap[t] || 0) + 1; });
      const chapitres = Object.entries(chap).sort((a, b) => b[1] - a[1]).slice(0, 5);
      return {
        total: l.length, mois: l.filter(x => x.d >= debutMois).length,
        pages: l.reduce((a, x) => a + x.pages, 0), exercices: l.reduce((a, x) => a + x.ex, 0),
        cout, moyen: l.length ? cout / l.length : 0, depuis: l.length ? Math.min(...l.map(x => x.d)) : null,
        jours, max, recents: jours.reduce((a, j) => a + j.n, 0),
        chapitres, chapMax: chapitres.length ? chapitres[0][1] : 1,
      };
    });
    const tip = ref(null);   // colonne survolée ou touchée
    // colonne du graphique : arrondie en haut (rayon 3), carrée sur la ligne de base
    function colPath(i, n) {
      const h = 150 * n / st.value.max, x = i * 20 + 3, w = 14, r = Math.min(3, h), y = 150 - h;
      return `M${x},150 V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V150 Z`;
    }
    const dollars = (v, dec = 2) => v.toFixed(dec).replace(".", ",") + " $";
    const dateCourte = d => new Date(d).toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
    const dateAn = d => new Date(d).toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
    const dateLongue = d => new Date(d).toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
    const moisCourant = new Date().toLocaleDateString("fr-FR", { month: "long", year: "numeric" });
    function resetStats() {
      if (!confirm("Remettre les statistiques à zéro ? L'historique des séries n'est pas touché.")) return;
      stats.value = []; local.set("sm_stats", []);
    }

    // ---------------------------------------------------------------- affichage
    const running = j => ["envoi", "en_attente", "en_cours"].includes(j.etat);
    const ext = n => (n.split(".").pop() || "").toUpperCase().slice(0, 4);
    const fileUrl = (j, f) => (j.urls && j.urls[f]) || `/api/jobs/${j.id}/files/${encodeURIComponent(f)}`;
    function progress(j) {
      if (j.etat === "termine") return 100;
      if (j.etat === "envoi") return Math.max(2, j.envoi * 0.08);
      return Math.min(95, 8 + 87 * j.etapes.length / (j.prevu + 0.5));
    }
    function badge(j) {
      return { envoi: `Envoi ${j.envoi} %`, en_attente: "En file d'attente", en_cours: "En cours",
               termine: "Terminé", erreur: "Erreur" }[j.etat] || j.etat;
    }
    function duree(ms) {
      const s = Math.max(0, Math.round(ms / 1000));
      return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")}`;
    }
    function subtitle(j) {
      if (running(j)) return `Depuis ${duree(now.value - j.debut)}` + (health.correction ? " · la correction prend 1 à 4 minutes" : "");
      const d = new Date(j.debut);
      const t = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
      return (new Date().toDateString() === d.toDateString() ? "Aujourd'hui" : d.toLocaleDateString("fr-FR")) + " à " + t;
    }

    // reprendre les séries envoyées avant un rechargement de la page (ou avant la connexion)
    function resumeJobs() {
      for (const s of local.get("sm_jobs", [])) {
        if (jobs.value.some(j => j.id === s.id)) continue;   // déjà dans l'historique
        const j = reactive({ key: ++seq, etat: "en_attente", envoi: 100, etapes: [], resultat: null, erreur: null, file: null, prevu: 5, ...s });
        jobs.value.push(j);
        poll(j);
      }
      jobs.value.filter(j => j.id && running(j)).forEach(poll);
    }

    async function doLogin() {
      login.busy = true; login.error = "";
      const fd = new FormData();
      fd.append("password", login.password);
      try {
        const r = await fetch("/api/login", { method: "POST", body: fd });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { login.error = data.detail || "Connexion impossible."; return; }
        login.password = "";
        health.connecte = true;
        resumeJobs();
      } catch (e) { login.error = "Le serveur ne répond pas."; }
      finally { login.busy = false; }
    }

    async function logout() {
      try { await fetch("/api/logout", { method: "POST" }); } catch (e) {}
      health.connecte = false;
    }

    onMounted(async () => {
      ticker = setInterval(() => (now.value = Date.now()), 1000);
      try { Object.assign(health, await (await fetch("/api/health")).json()); }
      catch (e) { health.ok = false; }
      await loadHistory();
      if (!needLogin.value) resumeJobs();
    });
    onUnmounted(() => clearInterval(ticker));

    return { ident, opts, login, needLogin, doLogin, logout, health, jobs, dragging, showErrors, dropError, fileInput, anneeCourante,
             identOk, editIdent, canSend, status, vue, go, st, tip, colPath, dollars, dateCourte, dateAn, dateLongue, moisCourant, resetStats, pick, onPick, onDrop, retry, forget, clearErrors, share, canShare, tactile,
             running, ext, fileUrl, progress, badge, subtitle };
  },
}).mount("#app");
