"""HTML (avec LaTeX) -> PDF, avec l'en-tête du professeur. Utilise Chromium via Playwright et KaTeX en local."""
from __future__ import annotations

import base64
import html
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from .rebrand import Identity

STATIC = Path(__file__).parent / "static"
CHROMIUM = os.environ.get("CHROMIUM_PATH") or None   # sinon : navigateur installé par « playwright install »

CSS = """
body{font-family:'Times New Roman',Times,serif;font-size:12pt;color:#111;line-height:1.4}
.head{border:1.3px solid #000;margin-bottom:8px}
.head .r{display:flex;justify-content:space-between;align-items:center;padding:4px 10px}
.head .r1{border-bottom:.8px solid #000;font-weight:bold;font-size:13pt}
.head .r2 .t{font-weight:bold;font-size:14pt;text-transform:uppercase}
.head .r2 .s{font-size:10.5pt}
h1{text-align:center;font-size:17pt;margin:6px 0 8px;letter-spacing:1px}
h2{font-size:13pt;margin:13px 0 5px;border-bottom:1.4px solid #000;padding-bottom:1px;color:#1a3c6e;break-after:avoid}
.sujet h2{color:#000;border:none;text-decoration:underline}
p{margin:3px 0}
table{border-collapse:collapse;margin:5px 0 7px;break-inside:avoid;font-size:11pt}
td,th{border:1px solid #444;padding:3px 7px;text-align:center;vertical-align:middle}
th{background:#eef2f8}
.sujet th{background:#f3f3f3}
.res{color:#a0281c;font-weight:bold}
.note{font-style:italic;font-size:10.8pt;color:#444}
img.fig{display:block;max-width:60%;max-height:260px;margin:6px auto}
.katex{font-size:1.06em}
.sign{text-align:right;font-weight:bold;margin-top:14px}
"""

# Droites graduées : <div class="droite" data-a="-3" data-b="1" data-ga="]" data-gb="[">
JS_DROITE = r"""
function nb(s){return (s+'').replace('.',',').replace('-','−')}
document.querySelectorAll('.droite').forEach(function(el){
  var A=el.dataset.a, B=el.dataset.b, ga=el.dataset.ga||'[', gb=el.dataset.gb||']';
  var a=(A==='-inf')?null:parseFloat(A), b=(B==='+inf')?null:parseFloat(B);
  var vals=[0]; if(a!==null)vals.push(a); if(b!==null)vals.push(b);
  var lo=Math.min.apply(null,vals), hi=Math.max.apply(null,vals); var pad=Math.max(1,(hi-lo)*0.25);
  lo-=pad; hi+=pad; var W=360,x0=15,x1=330; var X=function(v){return x0+(v-lo)/(hi-lo)*(x1-x0)};
  var s='<svg width="'+W+'" height="46"><line x1="'+(x0-8)+'" y1="22" x2="'+(x1+8)+'" y2="22" stroke="#333"/>'+
        '<polygon points="'+(x1+12)+',22 '+(x1+4)+',18 '+(x1+4)+',26" fill="#333"/>';
  var xa=(a===null)?x0-6:X(a), xb=(b===null)?x1+4:X(b);
  s+='<line x1="'+xa+'" y1="22" x2="'+xb+'" y2="22" stroke="#c0392b" stroke-width="4"/>';
  var ticks=[0]; if(a!==null)ticks.push(a); if(b!==null)ticks.push(b);
  ticks.forEach(function(v){s+='<line x1="'+X(v)+'" y1="17" x2="'+X(v)+'" y2="27" stroke="#333"/><text x="'+X(v)+'" y="42" font-size="11" text-anchor="middle">'+nb(v)+'</text>'});
  if(a!==null)s+='<text x="'+X(a)+'" y="28" font-size="20" font-weight="bold" fill="#c0392b" text-anchor="middle">'+ga+'</text>';
  if(b!==null)s+='<text x="'+X(b)+'" y="28" font-size="20" font-weight="bold" fill="#c0392b" text-anchor="middle">'+gb+'</text>';
  el.innerHTML=s+'</svg>';
});
"""


def _sanitize(fragment: str) -> str:
    """Le HTML vient de Claude : on retire scripts, styles et gestionnaires d'événements."""
    fragment = re.sub(r"<\s*(script|style|iframe|object|embed|link|meta)[^>]*>.*?<\s*/\s*\1\s*>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<\s*(script|style|iframe|object|embed|link|meta)[^>]*/?>", "", fragment, flags=re.I)
    fragment = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", fragment, flags=re.I)
    fragment = re.sub(r"(href|src)\s*=\s*([\"'])\s*(javascript|file|https?):[^\"']*\2", "", fragment, flags=re.I)
    return fragment


def build_html(body: str, ident: Identity, *, corrige: bool, figures: dict[str, bytes] | None = None) -> str:
    body = _sanitize(body)
    for name, png in (figures or {}).items():
        uri = "data:image/png;base64," + base64.b64encode(png).decode()
        body = re.sub(rf'src=["\']{re.escape(name)}["\']', f'src="{uri}"', body)
    e = html.escape
    head = f"""<div class="head"><div class="r r1"><span>{e(ident.ecole)}</span><span>PROF : {e(ident.prof)}</span><span>{e(ident.annee)}</span></div>
<div class="r r2"><span class="s">{e(ident.matiere)}</span><span class="t">{e(ident.titre)}</span><span class="s">Classes : {e(ident.classes)}</span></div></div>"""
    k = STATIC / "katex"
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<link rel="stylesheet" href="{(k / 'katex.min.css').as_uri()}">
<style>{CSS}</style></head><body class="{'corrige' if corrige else 'sujet'}">
{head}{'<h1>CORRIGÉ</h1>' if corrige else ''}
{body}
<p class="sign">Prof : {e(ident.prof)}</p>
<script src="{(k / 'katex.min.js').as_uri()}"></script>
<script src="{(k / 'auto-render.min.js').as_uri()}"></script>
<script>
{JS_DROITE}
renderMathInElement(document.body,{{delimiters:[{{left:'\\\\[',right:'\\\\]',display:true}},{{left:'\\\\(',right:'\\\\)',display:false}}],throwOnError:false}});
document.body.dataset.ready='1';
</script></body></html>"""


def html_to_pdf(html_text: str, out_pdf: str, ident: Identity, workdir: str) -> None:
    src = Path(workdir) / (Path(out_pdf).stem + ".html")
    src.write_text(html_text, encoding="utf-8")
    footer = (f'<div style="font-size:9px;width:100%;padding:0 14mm;display:flex;justify-content:space-between;font-family:Times">'
              f'<i>Classes : {html.escape(ident.classes)}</i><span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span>'
              f'<b><i>Prof : {html.escape(ident.prof)}</i></b></div>')
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        page = browser.new_page()
        # aucun accès réseau : seuls la page locale, KaTeX et les images intégrées sont chargés
        allowed = (src.as_uri(), STATIC.as_uri())
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("data:", *allowed))
                   else route.abort())
        page.goto(src.as_uri())
        page.wait_for_selector("body[data-ready='1']", timeout=30000)
        page.pdf(path=out_pdf, format="A4", print_background=True, display_header_footer=True,
                 header_template="<span></span>", footer_template=footer,
                 margin={"top": "13mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
        browser.close()
