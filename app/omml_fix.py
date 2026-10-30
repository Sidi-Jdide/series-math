"""Corrige les formules Word (OMML) que LibreOffice affiche mal (« ¿ », crochets ou parenthèses faux).

- Les délimiteurs <m:d> (parenthèses, crochets, barres) sont remplacés par des caractères simples.
- Les morceaux contenant = ∈ < ≤ [ ] | ; … sont marqués « texte normal » (m:nor) : LibreOffice
  ne les interprète plus comme des opérateurs incomplets.
- Les parenthèses sans partenaire au même niveau (« (10 » dans un exposant, « ) » à l'extérieur)
  passent aussi en texte normal.
Le fichier Word ainsi modifié ne sert qu'à la conversion en PDF.
"""
from __future__ import annotations

import copy
import re

from lxml import etree

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"m": M, "w": W}
SPECIAL = re.compile(r"[=∈∉<>≤≥\[\]|;⟦⟧{}]")
# fichiers envoyés par des inconnus : aucune entité, aucune DTD, aucun accès réseau (attaques XXE)
SAFE_PARSER = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)


def _run(text: str, rpr_src=None, nor: bool = False) -> etree._Element:
    r = etree.Element(f"{{{M}}}r")
    if nor or text not in ("(", ")"):   # parenthèses : LibreOffice les gère bien en mode maths
        mrpr = etree.SubElement(r, f"{{{M}}}rPr")
        etree.SubElement(mrpr, f"{{{M}}}nor")
    if rpr_src is not None:
        r.append(etree.fromstring(etree.tostring(rpr_src)))
    t = etree.SubElement(r, f"{{{M}}}t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return r


def fix_document_xml(xml: bytes) -> bytes:
    root = etree.fromstring(xml, SAFE_PARSER)
    # 1. délimiteurs -> caractères (du plus profond au plus externe)
    for d in reversed(root.xpath("//m:d", namespaces=NS)):
        pr = d.find("m:dPr", NS)
        beg, end, sep = "(", ")", "|"
        wrpr = None
        if pr is not None:
            for tag, default in (("begChr", "("), ("endChr", ")"), ("sepChr", "|")):
                el = pr.find(f"m:{tag}", NS)
                val = el.get(f"{{{M}}}val") if el is not None else default
                if tag == "begChr":
                    beg = val if val is not None else ""
                elif tag == "endChr":
                    end = val if val is not None else ""
                else:
                    sep = val if val is not None else ""
            wrpr = pr.find("m:ctrlPr/w:rPr", NS)
        new = []
        if beg:
            new.append(_run(beg, wrpr))
        es = d.findall("m:e", NS)
        for i, e in enumerate(es):
            if i and sep:
                new.append(_run(sep, wrpr))
            new.extend(list(e))
        if end:
            new.append(_run(end, wrpr))
        parent = d.getparent()
        idx = parent.index(d)
        parent.remove(d)
        for k, el in enumerate(new):
            parent.insert(idx + k, el)
    # 2. opérateurs / crochets -> texte normal (les parenthèses restent en mode maths)
    for r in root.xpath("//m:r", namespaces=NS):
        t = r.find("m:t", NS)
        if t is None or not t.text or not SPECIAL.search(t.text):
            continue
        wrpr = r.find("w:rPr", NS)
        pieces = [p for p in re.split(r"([()])", t.text) if p]
        parent = r.getparent()
        idx = parent.index(r)
        parent.remove(r)
        for k, p in enumerate(pieces):
            parent.insert(idx + k, _run(p, wrpr))
    # 3. parenthèses orphelines (ouvertes et fermées à des niveaux différents) -> texte normal
    for parent in {r.getparent() for r in root.xpath("//m:r", namespaces=NS)}:
        runs = [r for r in parent if r.tag == f"{{{M}}}r" and r.find("m:nor", NS) is None
                and r.find("m:rPr/m:nor", NS) is None and r.find("m:t", NS) is not None]
        stack, alone = [], set()
        for i, r in enumerate(runs):
            for j, ch in enumerate(r.find("m:t", NS).text or ""):
                if ch == "(":
                    stack.append((i, j))
                elif ch == ")":
                    if stack:
                        stack.pop()
                    else:
                        alone.add((i, j))
        alone.update(stack)
        for i, r in enumerate(runs):
            cut = sorted(j for k, j in alone if k == i)
            if not cut:
                continue
            text = r.find("m:t", NS).text
            wrpr = r.find("w:rPr", NS)
            pieces, last = [], 0
            for j in cut:
                pieces += [(text[last:j], False), (text[j], True)]
                last = j + 1
            pieces.append((text[last:], False))
            idx = parent.index(r)
            parent.remove(r)
            for k, (p, nor) in enumerate([x for x in pieces if x[0]]):
                if nor:
                    el = _run(p, wrpr, nor=True)
                else:                   # le reste du morceau garde son style d'origine
                    el = copy.deepcopy(r)
                    el.find("m:t", NS).text = p
                    el.find("m:t", NS).set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                parent.insert(idx + k, el)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
