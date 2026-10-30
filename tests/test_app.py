"""Tests sans clé API (MOCK_CLAUDE=1). Lancer : pytest -q"""
import os
import time

os.environ["MOCK_CLAUDE"] = "1"

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import main, render
from app.omml_fix import fix_document_xml
from app.rebrand import Identity, rebrand_pdf, remaining


def make_pdf(path, prof="Mohamed Exemple"):
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 60), "Lycee Ancien    2019-2020", fontsize=12)
    p.insert_text((72, 120), "Exercice 1 : calculer 2 + 3", fontsize=12)
    p.insert_text((72, 800), f"Prof : {prof}      Bonne chance", fontsize=11)
    # filigrane en diagonale
    p.insert_text((150, 500), prof, fontsize=40, morph=(fitz.Point(150, 500), fitz.Matrix(45)))
    d.save(path)


def test_rebrand_removes_names(tmp_path):
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    make_pdf(src)
    rep = rebrand_pdf(str(src), str(out), Identity(ecole="ECOLE TEST", prof="DUPONT"), ["Mohamed Exemple", "Lycee Ancien"])
    txt = fitz.open(out)[0].get_text()
    assert "Mohamed" not in txt and "Lycee Ancien" not in txt
    assert "ECOLE TEST" in txt and "DUPONT" in txt and "Exercice 1" in txt
    assert rep["stats"][0]["rotated"] >= 1
    assert remaining(str(out), ["Mohamed Exemple"]) == []


def test_int_list():
    assert main._int_list("1, 3-5;7") == [1, 3, 4, 5, 7]
    assert main._int_list("") == []


def test_sanitize():
    h = render._sanitize('<p onclick="x()">a</p><script>alert(1)</script><img src="javascript:x">')
    assert "script" not in h and "onclick" not in h and "javascript" not in h
    h = render._sanitize('<img src="file:///etc/passwd"><img src="https://x.example/t.png"><img src="fig1">')
    assert "file:" not in h and "https:" not in h and 'src="fig1"' in h


def _png(color):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def test_rebrand_keeps_figures_and_small_rotated_text(tmp_path):
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    logo, fig = _png((200, 0, 0)), _png((0, 0, 200))
    for i in range(2):
        p = d.new_page()
        p.insert_image(fitz.Rect(40, 20, 140, 70), stream=logo)              # logo répété : supprimé
        p.insert_text((72, 300), f"Exercice {i + 1}", fontsize=12)
    d[1].insert_image(fitz.Rect(200, 90, 400, 150), stream=fig)              # figure en haut de la page 2 : gardée
    d[1].insert_text((60, 400), "axe y", fontsize=10, rotate=90)             # libellé vertical : gardé
    d.save(src)
    rep = rebrand_pdf(str(src), str(out), Identity(), [])
    assert [s["images"] for s in rep["stats"]] == [1, 1]
    assert rep["stats"][1]["rotated"] == 0
    o = fitz.open(out)
    assert "axe y" in o[1].get_text()


def test_omml_fix_delimiters():
    xml = (b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           b'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:body><w:p><m:oMath>'
           b'<m:d><m:dPr><m:begChr m:val="|"/><m:endChr m:val="|"/></m:dPr><m:e><m:r><m:t>x</m:t></m:r></m:e></m:d>'
           b'<m:r><m:t>=</m:t></m:r></m:oMath></w:p></w:body></w:document>')
    out = fix_document_xml(xml).decode()
    assert "<m:d>" not in out and out.count("<m:nor/>") >= 3


@pytest.mark.skipif(not os.environ.get("CHROMIUM_PATH") and not os.path.exists(os.path.expanduser("~/.cache/ms-playwright")),
                    reason="navigateur Chromium absent")
def test_api_end_to_end(tmp_path):
    src = tmp_path / "serie.pdf"
    make_pdf(src)
    c = TestClient(main.app)
    assert c.get("/").status_code == 200
    with src.open("rb") as f:
        r = c.post("/api/jobs", files={"fichier": ("serie.pdf", f, "application/pdf")},
                   data={"ecole": "E", "prof": "P", "annee": "2026", "classes": "4AS", "sujet": "true",
                         "correction": "true", "effacer": "Mohamed Exemple"})
    jid = r.json()["id"]
    for _ in range(120):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["etat"] in ("termine", "erreur"):
            break
        time.sleep(0.5)
    assert j["etat"] == "termine", j
    for name in j["resultat"]["fichiers"]:
        assert c.get(f"/api/jobs/{jid}/files/{name}").content[:4] == b"%PDF"


# ---------------------------------------------------------------- sécurité

def _docx(path, document_xml: bytes, extra: dict | None = None):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)
        for k, v in (extra or {}).items():
            z.writestr(k, v)


def test_docx_xxe_refused(tmp_path):
    from app import convert
    src = tmp_path / "x.docx"
    _docx(src, b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///proc/self/environ">]>'
               b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               b'<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>')
    with pytest.raises(RuntimeError, match="non autorisées"):
        convert.prepare_docx(src, tmp_path / "o.docx", erase=["Prof"])


def test_docx_zip_bomb_refused(tmp_path, monkeypatch):
    from app import convert
    monkeypatch.setattr(convert, "MAX_UNZIPPED", 1000)
    src = tmp_path / "b.docx"
    _docx(src, b"<w:document/>", {"word/media/big.bin": b"0" * 5000})
    with pytest.raises(RuntimeError, match="décompressé"):
        convert.prepare_docx(src, tmp_path / "o.docx")


def test_fake_pdf_refused():
    c = TestClient(main.app)
    r = c.post("/api/jobs", files={"fichier": ("x.pdf", b"MZ\x90\x00 pas un pdf", "application/pdf")},
               data={"ecole": "E", "prof": "P"})
    assert r.status_code == 400


def test_security_headers():
    r = TestClient(main.app).get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert TestClient(main.app).get("/docs").status_code == 404


def test_login_flow(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "un-mot-de-passe-solide")
    monkeypatch.setattr(main, "LOGIN_TRIES", 2)
    main.login_fails.clear()
    c = TestClient(main.app)
    assert c.get("/api/health").json()["connecte"] is False
    assert c.post("/api/jobs", files={"fichier": ("x.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 401
    assert c.get("/api/jobs/abc").status_code == 401
    # autre site -> refusé
    assert c.post("/api/login", data={"password": "un-mot-de-passe-solide"},
                  headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/login", data={"password": "faux"}).status_code == 401
    assert c.post("/api/login", data={"password": "faux"}).status_code == 401
    assert c.post("/api/login", data={"password": "un-mot-de-passe-solide"}).status_code == 429   # bloqué
    main.login_fails.clear()
    r = c.post("/api/login", data={"password": "un-mot-de-passe-solide"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/health").json()["connecte"] is True
    assert c.get("/api/jobs/abc").status_code == 404
    c.post("/api/logout")
    assert c.get("/api/health").json()["connecte"] is False


def test_same_file_reuses_claude(tmp_path, monkeypatch):
    """Renvoyer le même fichier (par ex. pour corriger l'en-tête) ne repaie pas Claude."""
    from app import claude_ai, pipeline
    monkeypatch.setattr(pipeline, "CACHE", tmp_path / "cache")
    calls = []
    real = claude_ai.correction
    monkeypatch.setattr(claude_ai, "correction", lambda *a: calls.append(1) or real(*a))
    src = tmp_path / "serie.pdf"
    make_pdf(src)
    for prof in ("DUPONT", "DURAND"):
        wd = tmp_path / prof
        wd.mkdir()
        opt = pipeline.Options(ident=Identity(prof=prof), faire_sujet=False)
        r = pipeline.run(src, wd, opt, lambda m: None)
    assert len(calls) == 1 and r["reutilise"] is True and r["cout_estime"] == 0


def test_output_names_contain_prof(tmp_path, monkeypatch):
    from app import pipeline
    monkeypatch.setattr(pipeline, "CACHE", tmp_path / "cache")
    src = tmp_path / "Serie_reels.pdf"
    make_pdf(src)
    r = pipeline.run(src, tmp_path, pipeline.Options(ident=Identity(prof="TACHEFINE")), lambda m: None)
    assert r["fichiers"] == ["Serie_reels_tachefine_sujet.pdf", "Serie_reels_tachefine_correction.pdf"]


def test_correction_disabled_by_default(tmp_path, monkeypatch):
    """CORRECTION=0 : le serveur ignore la demande de correction (crédits protégés)."""
    from app import pipeline
    monkeypatch.setattr(pipeline, "CACHE", tmp_path / "cache")
    assert main.CORRECTION is False
    c = TestClient(main.app)
    assert c.get("/api/health").json()["correction"] is False
    src = tmp_path / "serie.pdf"
    make_pdf(src)
    with src.open("rb") as f:
        assert c.post("/api/jobs", files={"fichier": ("serie.pdf", f, "application/pdf")},
                      data={"ecole": "E", "prof": "P", "sujet": "false", "correction": "true"}).status_code == 400
    with src.open("rb") as f:
        jid = c.post("/api/jobs", files={"fichier": ("serie.pdf", f, "application/pdf")},
                     data={"ecole": "E", "prof": "P", "sujet": "true", "correction": "true"}).json()["id"]
    for _ in range(60):
        j = c.get(f"/api/jobs/{jid}").json()
        if j["etat"] in ("termine", "erreur"):
            break
        time.sleep(0.5)
    assert j["etat"] == "termine" and not any("correction" in f for f in j["resultat"]["fichiers"])


def test_scanned_pdf_without_resaisie(tmp_path, monkeypatch):
    """RESAISIE=0 : un scan garde son image, avec le nouvel en-tête au-dessus et un avertissement."""
    from app import pipeline
    monkeypatch.setattr(pipeline, "CACHE", tmp_path / "cache")
    assert pipeline.RESAISIE is False
    src = tmp_path / "scan.pdf"
    d = fitz.open()
    p = d.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 420), False)
    pix.set_rect(pix.irect, (250, 250, 240))
    p.insert_image(p.rect, pixmap=pix)                  # page entière = photo, sans texte
    d.save(src)
    r = pipeline.run(src, tmp_path, pipeline.Options(ident=Identity(prof="TACHEFINE"), faire_correction=False),
                     lambda m: None)
    assert any("scannée" in a for a in r["avertissements"])
    o = fitz.open(tmp_path / r["fichiers"][0])
    assert "TACHEFINE" in o[0].get_text() and o[0].get_images()


def test_underline_of_erased_line_removed(tmp_path):
    """Ligne d'en-tête effacée morceau par morceau : son soulignement part, celui des exercices reste."""
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    p = d.new_page()
    p.insert_text((72, 120), "2019/2020        Serie ancienne", fontsize=12)
    p.draw_rect(fitz.Rect(70, 124, 540, 125), color=None, fill=(0, 0, 0))     # soulignement de l'ancienne ligne
    p.insert_text((72, 200), "Exercice 1", fontsize=12)
    p.draw_rect(fitz.Rect(70, 204, 540, 205), color=None, fill=(0, 0, 0))     # soulignement à garder
    d.save(src)
    rep = rebrand_pdf(str(src), str(out), Identity(), ["2019/2020", "Serie ancienne"])
    assert rep["stats"][0]["underlines"] == 1
    assert len([x for x in fitz.open(out)[0].get_drawings() if x["rect"].width > 400 and x["rect"].height < 3]) >= 1


def test_page_and_scripts_revalidated():
    """Après une mise à jour, le navigateur ne mélange pas une ancienne page et un nouveau script."""
    c = TestClient(main.app)
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_generic_old_texts_are_not_erased():
    """« 4AS » ou « Math » seuls seraient effacés dans tous les exercices : on ne les garde pas."""
    from app import pipeline
    ident = Identity(prof="Tachefine", classes="3AS - 4AS", matiere="Mathématiques")
    found = ["Prof    Bekar  Amar", "CN", "4AS   ", "Math   ", "S1", "4AS CN S1 Nombres Réels Math"]
    assert pipeline._specific(found, ident) == ["Prof    Bekar  Amar", "4AS CN S1 Nombres Réels Math"]


def test_generic_header_words_erased_only_in_margins(tmp_path):
    """« 4AS CN S1 Math » : effacé dans l'ancien en-tête, gardé dans un exercice."""
    src, out = tmp_path / "s.pdf", tmp_path / "o.pdf"
    d = fitz.open()
    p = d.new_page()
    p.insert_text((60, 70), "4AS     CN     Nombres Reels     Math     S1     2025-2026", fontsize=12)
    p.insert_text((60, 300), "Exercice 1 : en 4AS, le cours de Math porte sur S1 et CN.", fontsize=12)
    d.save(src)
    rebrand_pdf(str(src), str(out), Identity(prof="Tachefine"), [],
                ["4AS", "CN", "S1", "Math", "Nombres Reels", "2025-2026"])
    t = fitz.open(out)[0].get_text()
    assert "2025-2026" not in t and "Nombres Reels" not in t
    assert "en 4AS, le cours de Math porte sur S1 et CN." in " ".join(t.split())


def test_omml_orphan_parentheses():
    # « (10 » dans un exposant et « ) » à l'extérieur : LibreOffice affichait « ¿¿ »
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:body><w:p><m:oMath>'
           '<m:sSup><m:e><m:sSup><m:e><m:r><m:t>(10</m:t></m:r></m:e><m:sup><m:r><m:t>3</m:t></m:r></m:sup></m:sSup>'
           '<m:r><m:t>)</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>'
           '<m:r><m:t>(a+b)</m:t></m:r></m:oMath></w:p></w:body></w:document>').encode()
    out = fix_document_xml(xml).decode()
    assert "<m:nor/></m:rPr><m:t xml:space=\"preserve\">(</m:t>" in out
    assert "<m:nor/></m:rPr><m:t xml:space=\"preserve\">)</m:t>" in out
    assert "<m:t>(a+b)</m:t>" in out          # parenthèses complètes : inchangées


def _box(text):
    return ('<w:r><w:drawing><wps:wsp><wps:txbx><w:txbxContent><w:p><w:r><w:t>' + text +
            '</w:t></w:r></w:p></w:txbxContent></wps:txbx><wps:bodyPr wrap="square"><a:noAutofit/></wps:bodyPr>'
            '</wps:wsp></w:drawing></w:r>')


def test_docx_text_boxes_grow_and_old_header_box_removed(tmp_path):
    import zipfile
    from app import convert
    xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body><w:p>'
           + _box("4AS  CN  Math  S1  2025-2026") + _box("Prof Bekar Amar")
           + _box("Exercice 2 : simplifier A")
           + _box('</w:t></w:r><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
                  '<m:r><m:t>A=5</m:t></m:r></m:oMath><w:r><w:t>')   # formule seule : jamais retirée
           + '</w:p></w:body></w:document>')
    src = tmp_path / "s.docx"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("word/document.xml", xml)
    convert.prepare_docx(src, tmp_path / "o.docx", erase=["Prof Bekar Amar"],
                         generic=["4AS", "CN", "Math", "S1", "2025-2026"])
    out = zipfile.ZipFile(tmp_path / "o.docx").read("word/document.xml").decode()
    assert "Exercice 2" in out and "4AS" not in out and "Bekar" not in out
    assert out.count("<w:txbxContent>") == 2 and "A=5" in out
    assert "<a:spAutoFit/>" in out and "noAutofit" not in out


def test_blank_last_page_dropped(tmp_path):
    from app import convert
    d = fitz.open()
    d.new_page().insert_text((72, 72), "Exercice 1")
    d.new_page()
    d.save(tmp_path / "s.pdf")
    convert._drop_blank_tail(tmp_path / "s.pdf")
    assert len(fitz.open(tmp_path / "s.pdf")) == 1


def test_old_prof_signature_found_without_claude():
    from app.pipeline import _signatures
    lines = ["Prof    Bekar  Amar   Bekar", "PROF : Tachefine", "Problème 1 : calculer", "Exercice 2"]
    assert _signatures(lines, Identity(prof="Tachefine")) == ["Prof    Bekar  Amar   Bekar"]


def test_file_name_describes_content():
    from app.pipeline import _file_name
    ident = Identity(prof="Tachefine", titre="SERIE : REELS ET ANGLES", classes="3AS - 4AS")
    assert _file_name(ident, "4AS_CN_25_25_S2") == "Serie_Reels_et_Angles_3AS_4AS_tachefine"
    assert _file_name(Identity(prof="Tachefine", titre="SERIE"), "envoi") == "envoi_tachefine"


def test_analyse_uses_haiku_then_main_model_on_refusal(monkeypatch):
    from types import SimpleNamespace as NS
    from app import claude_ai
    calls = []
    usage = NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)

    def create(**kw):
        calls.append(kw["model"])
        if kw["model"] == "claude-haiku-5-5":
            return NS(model=kw["model"], usage=usage, stop_reason="refusal", content=[])
        return NS(model=kw["model"], usage=usage, stop_reason="tool_use",
                  content=[NS(type="tool_use", input={"titre": "SERIE : ANGLES"})])

    monkeypatch.setattr(claude_ai, "MOCK", False)
    monkeypatch.setattr(claude_ai, "BATCH", False)
    monkeypatch.setattr(claude_ai, "_pdf_text", lambda p: "mot " * 50)
    monkeypatch.setattr(claude_ai, "client", lambda: NS(messages=NS(create=create)))
    assert claude_ai.analyse("x.pdf") == {"titre": "SERIE : ANGLES"}
    assert calls == ["claude-haiku-5-5", "claude-opus-5-5"]
