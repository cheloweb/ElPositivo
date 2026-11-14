#!/usr/bin/env python3
"""Portal de noticias positivas: ingesta -> filtro -> (IA opcional) -> sitio estático.

Solo usa la librería estándar de Python (sin dependencias).
Uso:
    python build.py             # descarga feeds, cura, genera public/
    python build.py --no-fetch  # solo regenera el sitio con data/articles.json
    python build.py --check     # comprueba que cada feed de config.json responde
"""
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import struct
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "articles.json"
OUT = ROOT / "public"
ASSETS = ROOT / "assets"
UA = "Mozilla/5.0 (compatible; BuenasNoticiasBot/1.0)"
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
PALETTE = ["#4f5bd5", "#e0525a", "#1f9d6e", "#e08a00", "#0891b2", "#c47f2a", "#9b51e0", "#5b6b7d"]


# ---------------------------------------------------------------- utilidades
def esc(s):
    return html.escape(s or "", quote=True)


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "nota"


def strip_html(s):
    s = re.sub(r"<(script|style).*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def clean_summary(s):
    """Quita los pies típicos de WordPress ("La entrada X se publicó primero en Y", "[…]")."""
    s = re.sub(r"\s*(La entrada|El artículo|The post|The article)\b.{0,250}?"
               r"(apareció primero en|se publicó primero en|appeared first on).*$", "", s, flags=re.S | re.I)
    s = re.sub(r"\s*\[\s*(…|\.\.\.|&#8230;)\s*\]\s*$", "…", s)
    s = re.sub(r"\s*(Leer más|Read more|Seguir leyendo)\W*$", "", s, flags=re.I)
    return s.strip()


def truncate(s, n):
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def fmt_date(iso):
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.day} de {MESES[d.month - 1]} de {d.year}"
    except Exception:
        return ""


def short_date(iso):
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.day} {MESES[d.month - 1][:3]}"
    except Exception:
        return ""


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_date(s):
    if not s:
        return datetime.now(timezone.utc)
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def site_tz():
    name = CFG.get("timezone", "Europe/Madrid")
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


# ---------------------------------------------------------------- imágenes
BAD_IMAGE_HINTS = ("1x1", "pixel", "spacer", "blank.gif", "gravatar.com", "/emoji/", "smilies", "transparent.")


def normalize_image(u, base=""):
    """Devuelve una URL https absoluta y utilizable, o "" si no sirve."""
    u = html.unescape((u or "").strip())
    if not u or u.startswith("data:"):
        return ""
    if u.startswith("//"):
        u = "https:" + u
    elif not re.match(r"^https?://", u, re.I):
        u = urljoin(base, u) if base else ""
    if u.lower().startswith("http://"):
        u = "https://" + u[7:]  # evita contenido mixto bloqueado por el navegador
    if not u or any(h in u.lower() for h in BAD_IMAGE_HINTS):
        return ""
    return u


def first_image_in_html(h, base=""):
    """Primera imagen real de un fragmento HTML, entendiendo lazy-load y srcset."""
    for m in re.finditer(r"<img\b[^>]*>", h or "", re.I):
        tag = m.group(0)
        cands = []
        for attr in ("data-src", "data-lazy-src", "data-original"):
            mm = re.search(r"(?<![\w-])" + attr + r"\s*=\s*[\"']([^\"']+)", tag, re.I)
            if mm:
                cands.append(mm.group(1))
        mm = re.search(r"(?<![\w-])srcset\s*=\s*[\"']\s*([^\s\"',]+)", tag, re.I)
        if mm:
            cands.append(mm.group(1))
        mm = re.search(r"(?<![\w-])src\s*=\s*[\"']([^\"']+)", tag, re.I)
        if mm:
            cands.append(mm.group(1))
        for c in cands:
            n = normalize_image(c, base)
            if n:
                return n
    return ""


def fetch_og_image(url):
    """Respaldo: lee la página de la nota y toma su imagen social (og:image)."""
    if not CFG.get("fetch_page_images", True):
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"})
        with urllib.request.urlopen(req, timeout=12) as r:
            page_html = r.read(400000).decode("utf-8", "ignore")
    except Exception:
        return ""
    pats = (
        r"<meta[^>]+(?:property|name)=[\"'](?:og:image(?::secure_url)?|twitter:image)[\"'][^>]*content=[\"']([^\"']+)",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"'](?:og:image|twitter:image)[\"']",
    )
    for p in pats:
        m = re.search(p, page_html, re.I)
        if m:
            img = normalize_image(m.group(1), url)
            if img:
                return img
    return ""


def backfill_images(arts, limit=12):
    """Normaliza imágenes guardadas y busca la imagen de notas que no tienen (una vez por nota)."""
    tried = 0
    for a in arts:
        a["image"] = normalize_image(a.get("image", ""), a.get("url", ""))
        if a["image"] or a.get("img_tried") or tried >= limit:
            continue
        a["img_tried"] = True
        tried += 1
        a["image"] = fetch_og_image(a["url"])
    if tried:
        print(f"Imágenes: se intentó recuperar {tried} notas sin imagen.")


# ---------------------------------------------------------------- ingesta
def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":  # algunos servidores envían gzip aunque no se pida
        data = gzip.decompress(data)
    return data.lstrip(b"\xef\xbb\xbf \t\r\n")  # quita BOM y espacios iniciales


def mrss_image(sub):
    if "mrss" not in sub.tag:
        return None
    u = sub.get("url")
    if not u:
        return None
    t = local(sub.tag)
    if t == "thumbnail":
        return u
    if t == "content":
        med, typ = sub.get("medium") or "", sub.get("type") or ""
        if med == "image" or typ.startswith("image") or (
                not med and not typ and re.search(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", u, re.I)):
            return u
    return None


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.iter():
        if local(e.tag) not in ("item", "entry"):
            continue
        d = {}
        for ch in e:
            tag, t = ch.tag, local(ch.tag)
            if "mrss" in tag:
                continue
            if t == "title":
                d["title"] = strip_html(ch.text)
            elif t == "link":
                link = ch.get("href") or (ch.text or "").strip()
                if link and (ch.get("rel") in (None, "alternate")):
                    d.setdefault("link", link)
            elif t in ("description", "summary"):
                d["desc"] = ch.text or ""
            elif t in ("encoded", "content"):
                d["content"] = ch.text or ""
            elif t in ("pubDate", "published", "updated", "date"):
                d.setdefault("date", ch.text)
            elif t == "enclosure" and (ch.get("type") or "").startswith("image"):
                d.setdefault("image", ch.get("url"))
        if not d.get("link"):
            continue
        if not d.get("image"):
            for sub in e.iter():
                img = mrss_image(sub)
                if img:
                    d["image"] = img
                    break
        d["image"] = normalize_image(d.get("image", ""), d["link"])
        if not d["image"]:
            d["image"] = first_image_in_html((d.get("content") or "") + (d.get("desc") or ""), d["link"])
        if d.get("title"):
            items.append(d)
    return items


# ---------------------------------------------------------------- curación
def passes_blocklist(text):
    t = " " + text.lower() + " "
    return not any(w in t for w in CFG["blocklist"])


def blocked_word(text):
    t = " " + text.lower() + " "
    for w in CFG["blocklist"]:
        if w in t:
            return w
    return ""


def has_positive_keyword(text):
    t = text.lower()
    return any(w in t for w in CFG.get("positive_keywords", []))


def check_feeds():
    """Comprueba cada feed de config.json: python build.py --check"""
    for feed in CFG["feeds"]:
        raw = b""
        try:
            raw = http_get(feed["url"])
            items = parse_feed(raw)
            if not items:
                print(f"AVISO {feed['name']}: responde pero no hay notas (¿no es un feed?). Empieza por: {raw[:150]!r}")
                continue
            imgs = sum(1 for i in items if i.get("image"))
            last = max((parse_date(i.get("date")) for i in items), default=None)
            print(f"OK   {feed['name']}: {len(items)} items, {imgs} con imagen, último: {last.date() if last else '-'}")
            print(f"       ej.: {items[0]['title'][:80]}")
        except Exception as ex:
            print(f"FALLA {feed['name']} ({feed['url']}): {ex}")
            if raw:
                print(f"       la respuesta empieza por: {raw[:150]!r}")


def call_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": CFG["ai_model"], "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    return "".join(b.get("text", "") for b in out.get("content", []))


def ai_curate(title, summary):
    """Devuelve dict {positiva, titulo, resumen, categoria} o None si no hay IA/falla."""
    cats = ", ".join(CFG["categories"])
    prompt = f"""Eres editor de un portal de noticias positivas en español.
Evalúa esta nota y responde SOLO con un JSON válido, sin texto extra:
{{"positiva": true|false, "titulo": "...", "resumen": "...", "categoria": "..."}}

Reglas:
- positiva=false si trata principalmente de tragedia, violencia, política polarizante, o es publicidad/clickbait.
- titulo: título claro en español, sin sensacionalismo (máx. 100 caracteres).
- resumen: 2 frases ORIGINALES en español (no copies frases del texto), máx. {CFG['summary_max_chars']} caracteres.
- categoria: una de [{cats}].

Título original: {title}
Texto: {summary[:1200]}"""
    try:
        txt = call_claude(prompt)
        if not txt:
            return None
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as ex:
        print(f"  (IA falló: {ex})")
        return None


def guess_category(text):
    t = text.lower()
    rules = {
        "Ciencia": ["scien", "research", "study", "discover", "ciencia", "científic", "investig", "descubr"],
        "Salud": ["health", "cancer", "cáncer", "vaccine", "vacuna", "medic", "salud", "hospital", "tratamiento"],
        "Medio ambiente": ["climate", "forest", "renewable", "solar", "ocean", "wildlife", "clima", "ambient",
                           "bosque", "renovable", "energía", "océano", "reciclaj", "sostenib", "conservaci"],
        "Tecnología": ["tech", "ai ", "robot", "software", "tecnolog", "inteligencia artificial", "app "],
        "Animales": ["dog", "cat ", "animal", "species", "rescue", "perro", "gato", "especie", "rescat", "fauna",
                     "jaguar", "ballena", "tortuga"],
        "Cultura": ["art ", "music", "film", "book", "museum", "arte", "música", "cine", "libro", "museo", "festival"],
    }
    for cat, words in rules.items():
        if any(w in t for w in words):
            return cat
    return "Comunidad"


def fetch_new(articles):
    known = {a["id"] for a in articles}
    new, limit = [], CFG["max_new_per_run"]
    for feed in CFG["feeds"]:
        if len(new) >= limit:
            break
        print(f"Feed: {feed['name']}")
        try:
            items = parse_feed(http_get(feed["url"]))
        except Exception as ex:
            print(f"  no disponible: {ex}")
            continue
        st = {"leídas": 0, "ya conocidas": 0, "descartadas por palabra": 0,
              "sin palabra positiva": 0, "rechazadas por IA": 0, "nuevas": 0}
        for it in items[:25]:
            if len(new) >= limit:
                break
            aid = hashlib.sha1(it["link"].encode()).hexdigest()[:10]
            st["leídas"] += 1
            if aid in known:
                st["ya conocidas"] += 1
                continue
            raw = clean_summary(strip_html(it.get("desc") or it.get("content") or ""))
            hit = blocked_word(it["title"] + " " + raw[:400])
            if hit:
                st["descartadas por palabra"] += 1
                print(f"  - descartada por «{hit.strip()}»: {it['title'][:60]}")
                known.add(aid)
                continue
            if feed.get("mode") == "keywords" and not has_positive_keyword(it["title"] + " " + raw[:400]):
                st["sin palabra positiva"] += 1
                known.add(aid)
                continue
            # Feeds en español no necesitan traducción: se omite la IA (y el gasto de API).
            use_ai = feed.get("lang", "en") != "es" or CFG.get("ai_for_spanish", False)
            ai = ai_curate(it["title"], raw) if use_ai else None
            if ai is not None and not ai.get("positiva", False):
                st["rechazadas por IA"] += 1
                known.add(aid)
                continue
            title = (ai or {}).get("titulo") or it["title"]
            summary = (ai or {}).get("resumen") or raw
            cat = (ai or {}).get("categoria")
            if cat not in CFG["categories"]:
                cat = guess_category(title + " " + raw)
            image = it.get("image") or fetch_og_image(it["link"])
            known.add(aid)
            new.append({
                "id": aid,
                "slug": f"{slugify(title)}-{aid}",
                "title": title,
                "summary": truncate(summary, CFG["summary_max_chars"]),
                "category": cat,
                "image": image,
                "img_tried": True,
                "source": feed["name"],
                "url": it["link"],
                "date": parse_date(it.get("date")).isoformat(),
            })
            st["nuevas"] += 1
            print(f"  + {title[:70]}{'' if image else '  (sin imagen)'}")
        print("  resumen: " + ", ".join(f"{v} {k}" for k, v in st.items()))
    return new


def png_size(path):
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        return struct.unpack(">II", head[16:24])
    except Exception:
        return None


LOGO_SIZE = png_size(ASSETS / "logo.png")

# ---------------------------------------------------------------- estilos
CSS_BASE = """
:root{
 --bg:#f4f7fb;--paper:#fff;--ink:#012d5c;--muted:#4b627c;--line:#dbe4ef;--sun:#febd06;--sun-deep:#f5a200;--btn:#012d5c;--btn-ink:#fff;
 --serif:"Avenir Next",Avenir,"Segoe UI Variable Display","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
 --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media(prefers-color-scheme:dark){:root{--bg:#061a33;--paper:#0b2547;--ink:#eaf1f8;--muted:#a3b6cc;--line:#1d3a63;--btn:#febd06;--btn-ink:#012d5c}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 var(--sans)}
a{color:inherit;text-decoration:none}
img{max-width:100%}
:focus-visible{outline:3px solid #3b82f6;outline-offset:2px;border-radius:4px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}

header.site{background:var(--paper)}
.masthead{display:flex;justify-content:center}
.brand{display:block;line-height:0;margin:16px 0 12px}
.brand-text{font:800 1.8rem/1 var(--serif);letter-spacing:-.02em;line-height:1}
.logo{height:clamp(66px,10vw,108px);width:auto;display:block}
.compact .logo{height:clamp(52px,7vw,70px)}
.navbar{position:sticky;top:0;z-index:20;background:var(--paper);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.navrow{display:flex;align-items:center;gap:12px}
.search-btn{flex:none;display:flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;border:1px solid var(--line);
 background:var(--bg);color:var(--ink);font:600 .92rem var(--sans);cursor:pointer}
.search-btn:hover{border-color:var(--ink)}
nav.cats{flex:1;min-width:0;display:flex;gap:6px;overflow-x:auto;padding:9px 0;scrollbar-width:none}
nav.cats a:first-child{margin-left:auto}
nav.cats a:last-child{margin-right:auto}
nav.cats::-webkit-scrollbar{display:none}
nav.cats a{display:flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;white-space:nowrap;
 font:600 .92rem var(--sans);color:var(--ink)}
nav.cats a:hover{background:var(--bg)}
nav.cats a[aria-current]{background:var(--ink);color:var(--paper)}
nav.cats i,.chip i{width:9px;height:9px;border-radius:50%;background:var(--c,#5b6b7d);flex:none;display:block}

.today{display:flex;flex-wrap:wrap;gap:2px 20px;padding:26px 0 4px;color:var(--muted);font:500 .95rem var(--sans)}
.today strong{color:var(--ink);font-weight:700}

.media{position:relative;overflow:hidden;border-radius:14px;aspect-ratio:3/2;--c:#5b6b7d;
 background-color:color-mix(in srgb,var(--c) 16%,var(--paper));
 background-image:radial-gradient(color-mix(in srgb,var(--c) 50%,transparent) 1.5px,transparent 1.7px);
 background-size:13px 13px}
.media img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.chip{display:inline-flex;align-items:center;gap:8px;font:700 .86rem var(--sans);color:var(--ink);--c:#5b6b7d}
.meta{display:flex;flex-wrap:wrap;gap:2px 14px;font:.84rem var(--sans);color:var(--muted)}
.meta b{font-weight:600;color:var(--ink)}
.stretch::after{content:"";position:absolute;inset:0}
.lead,.mini,.card{position:relative}
article a.stretch:hover{text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}

.top{display:grid;grid-template-columns:minmax(0,1.75fr) minmax(0,1fr);gap:44px;padding:18px 0 48px}
.top.solo{grid-template-columns:minmax(0,1fr)}
.lead .media{aspect-ratio:16/9;border-radius:20px}
.lead .body{padding-top:18px;display:flex;flex-direction:column;gap:10px}
.lead h2{margin:0;font:800 clamp(1.95rem,1.2rem + 2.6vw,3.1rem)/1.07 var(--serif);letter-spacing:-.02em}
.lead p{margin:0;font-size:1.15rem;color:var(--muted);max-width:62ch}
.side{display:flex;flex-direction:column;gap:26px}
.mini{display:grid;grid-template-columns:minmax(0,1fr) 116px;gap:18px;align-items:start}
.mini .media{order:2;aspect-ratio:1;border-radius:12px}
.mini .body{display:flex;flex-direction:column;gap:7px}
.mini h3{margin:0;font:700 1.12rem/1.24 var(--serif)}

h2.sec{margin:0 0 24px;font:800 1.7rem/1.1 var(--serif);letter-spacing:-.015em}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:40px 30px}
.card .body{display:flex;flex-direction:column;gap:7px;padding-top:14px}
.card h3{margin:0;font:700 1.26rem/1.22 var(--serif);display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.card p{margin:0;color:var(--muted);font-size:.96rem;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.more-btn{display:block;margin:48px auto 8px;padding:13px 30px;border-radius:999px;border:2px solid var(--ink);
 background:transparent;color:var(--ink);font:700 1rem var(--sans);cursor:pointer}
.more-btn:hover{background:var(--ink);color:var(--paper)}
.empty{padding:60px 0;max-width:52ch;font-size:1.15rem;color:var(--muted)}

.cathead{margin:34px 0 36px;padding-left:20px;border-left:8px solid var(--c,#5b6b7d);--c:#5b6b7d}
.cathead h1{margin:0;font:800 clamp(2.2rem,1.4rem + 3vw,3.4rem)/1.05 var(--serif);letter-spacing:-.02em}
.cathead p{margin:8px 0 0;color:var(--muted)}

.nota{max-width:780px;margin:34px auto 20px}
.nota h1{margin:14px 0 14px;font:800 clamp(2rem,1.3rem + 2.8vw,3.2rem)/1.07 var(--serif);letter-spacing:-.02em}
.nota .media{aspect-ratio:16/9;border-radius:20px;margin:26px 0}
.nota .stand{margin:22px 0 0;font:400 1.32rem/1.55 var(--serif);max-width:62ch}
.nota .media + .stand{margin-top:0}
.source-box{margin:30px 0 18px;padding:24px;border-radius:16px;background:color-mix(in srgb,var(--sun) 24%,var(--paper))}
.source-box p{margin:0 0 14px;font-weight:500}
.btn{display:inline-block;padding:13px 22px;border-radius:12px;background:var(--btn);color:var(--btn-ink);font:700 1rem var(--sans)}
.btn:hover{opacity:.88}
.actions{display:flex;gap:10px;flex-wrap:wrap}
.actions button{padding:9px 18px;border-radius:999px;border:1px solid var(--line);background:var(--paper);color:var(--ink);font:600 .92rem var(--sans);cursor:pointer}
.actions button:hover{border-color:var(--ink)}
.related{margin:56px 0 24px}

dialog.search{border:0;border-radius:20px;padding:0;width:min(660px,calc(100% - 28px));margin:9vh auto auto;background:var(--paper);color:var(--ink);overflow:hidden}
dialog.search::backdrop{background:rgba(8,16,30,.6)}
.srow{display:flex;align-items:center;border-bottom:1px solid var(--line)}
dialog.search input{flex:1;min-width:0;padding:20px 22px;font:500 1.2rem var(--sans);border:0;background:transparent;color:inherit;outline:none}
.srow button{border:0;background:none;color:var(--muted);font:600 .92rem var(--sans);padding:12px 22px;cursor:pointer}
.srow button:hover{color:var(--ink)}
#results{max-height:55vh;overflow:auto}
#results a{display:block;padding:13px 22px}
#results a:hover,#results a:focus-visible{background:var(--bg)}
#results b{display:block;font:700 1.05rem/1.3 var(--serif)}
#results span{font:.84rem var(--sans);color:var(--muted)}
#results p{margin:0;padding:18px 22px;color:var(--muted)}

footer.site{margin-top:72px;padding:34px 0 48px;border-top:1px solid var(--line);color:var(--muted);font:.92rem/1.6 var(--sans)}
footer.site p{margin:0 0 6px;max-width:70ch}
footer.site a{text-decoration:underline}

@media(max-width:900px){
 .top{grid-template-columns:minmax(0,1fr);gap:36px}
 .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:34px 24px}
}
@media(max-width:600px){
 .wrap{padding:0 16px}
 .search-btn span{display:none}.search-btn{padding:10px}
 .grid{grid-template-columns:minmax(0,1fr);gap:24px}
 .card{display:grid;grid-template-columns:minmax(0,1fr) 104px;gap:16px;align-items:start}
 .card .media{order:2;aspect-ratio:1;border-radius:12px}
 .card .body{padding-top:0}
 .card h3{font-size:1.1rem;-webkit-line-clamp:5}
 .card p{display:none}
 .lead .media{border-radius:16px}
 .nota .media{border-radius:16px}
}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
"""

JS = r"""
(function(){
  var root=document.documentElement.getAttribute('data-root')||'';
  var dlg=document.getElementById('search'),input=document.getElementById('q'),out=document.getElementById('results'),idx=null;
  function norm(s){return String(s).toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'')}
  function load(){
    if(idx)return Promise.resolve();
    return fetch(root+'search.json').then(function(r){return r.json()}).then(function(j){
      idx=j.map(function(x){return{x:x,k:norm(x.t+' '+x.s+' '+x.c)}});
    }).catch(function(){idx=[]});
  }
  function render(){
    var q=norm(input.value.trim());
    out.textContent='';
    if(q.length<2)return;
    var words=q.split(/\s+/),hits=idx.filter(function(e){return words.every(function(w){return e.k.indexOf(w)>-1})}).slice(0,8);
    if(!hits.length){var p=document.createElement('p');p.textContent='No hay resultados para «'+input.value.trim()+'». Prueba con otra palabra.';out.appendChild(p);return}
    hits.forEach(function(h){
      var a=document.createElement('a');a.href=root+h.x.u;
      var b=document.createElement('b');b.textContent=h.x.t;
      var s=document.createElement('span');s.textContent=h.x.c;
      a.appendChild(b);a.appendChild(s);out.appendChild(a);
    });
  }
  function open(){
    if(!dlg||!dlg.showModal)return;
    dlg.showModal();input.focus();load().then(render);
  }
  document.querySelectorAll('[data-search]').forEach(function(b){b.addEventListener('click',open)});
  if(input){input.addEventListener('input',function(){load().then(render)})}
  if(dlg){dlg.addEventListener('click',function(e){if(e.target===dlg)dlg.close()})}
  document.querySelectorAll('[data-close]').forEach(function(b){b.addEventListener('click',function(){dlg.close()})});
  document.addEventListener('keydown',function(e){
    var t=e.target&&e.target.tagName;
    if(e.key==='Escape'&&dlg&&dlg.open){dlg.close();return}
    if(e.key==='/'&&t!=='INPUT'&&t!=='TEXTAREA'){e.preventDefault();open()}
  });
  document.querySelectorAll('[data-more]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var grid=btn.previousElementSibling,hid=grid.querySelectorAll('.more[hidden]');
      for(var i=0;i<hid.length&&i<18;i++){hid[i].hidden=false}
      if(grid.querySelectorAll('.more[hidden]').length===0)btn.remove();
    });
  });
  function flash(btn,txt){var o=btn.textContent;btn.textContent=txt;setTimeout(function(){btn.textContent=o},2200)}
  function copy(btn){
    var url=location.href;
    function ok(){flash(btn,'Enlace copiado')}
    function fallback(){
      var t=document.createElement('textarea');t.value=url;t.style.position='fixed';t.style.opacity='0';
      document.body.appendChild(t);t.select();
      try{if(document.execCommand('copy')){ok()}else{flash(btn,'No se pudo copiar')}}catch(e){flash(btn,'No se pudo copiar')}
      t.remove();
    }
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(url).then(ok,fallback)}else{fallback()}
  }
  document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){copy(b)})});
  document.querySelectorAll('[data-share]').forEach(function(b){
    b.addEventListener('click',function(){
      if(navigator.share){navigator.share({title:b.getAttribute('data-title'),url:location.href}).catch(function(){})}
      else{copy(b)}
    });
  });
})();
"""


def build_css():
    rules = "".join(f".cat-{slugify(c)}{{--c:{PALETTE[i % len(PALETTE)]}}}\n"
                    for i, c in enumerate(CFG["categories"]))
    return CSS_BASE + "\n" + rules


# ---------------------------------------------------------------- render
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Ccircle cx='16' cy='16' r='11' fill='%23ffc933'/%3E%3C/svg%3E")
SEARCH_ICON = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
               'stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')


def cat_cls(c):
    return "cat-" + slugify(c)


def chip(c):
    return f'<span class="chip {cat_cls(c)}"><i></i>{esc(c)}</span>'


def meta(a):
    return f'<div class="meta"><b>{esc(a["source"])}</b><time datetime="{esc(a["date"])}">{short_date(a["date"])}</time></div>'


def media(a, eager=False):
    img = ""
    if a.get("image"):
        img = (f'<img src="{esc(a["image"])}" alt="" loading="{"eager" if eager else "lazy"}" decoding="async" '
               f'referrerpolicy="no-referrer" onerror="this.remove()" onload="if(this.naturalWidth<60)this.remove()">')
    return f'<div class="media {cat_cls(a["category"])}">{img}</div>'


def page(title, body, depth, desc="", og_image="", canonical="", current=None):
    r = "../" * depth
    items = [f'<a href="{r}index.html"' + (' aria-current="page"' if current == "home" else "") + ">Portada</a>"]
    for c in CFG["categories"]:
        cur = ' aria-current="page"' if current == c else ""
        items.append(f'<a class="{cat_cls(c)}" href="{r}categoria/{slugify(c)}.html"{cur}><i></i>{esc(c)}</a>')
    if not og_image and CFG["site_url"] and (ASSETS / "og-default.png").exists():
        og_image = CFG["site_url"] + "/og-default.png"
    og = f'<meta property="og:image" content="{esc(og_image)}">' if og_image else ""
    name = esc(CFG["site_name"])
    if LOGO_SIZE:
        alt = esc(CFG["site_name"] + ": " + CFG["tagline"])
        brand = (f'<a class="brand" href="{r}index.html"><picture>'
                 f'<source srcset="{r}logo-dark.png" media="(prefers-color-scheme: dark)">'
                 f'<img class="logo" src="{r}logo.png" width="{LOGO_SIZE[0]}" height="{LOGO_SIZE[1]}" alt="{alt}"></picture></a>')
    else:
        brand = f'<a class="brand brand-text" href="{r}index.html">{name}</a>'
    if (ASSETS / "favicon.png").exists():
        icons = f'<link rel="icon" type="image/png" href="{r}favicon.png"><link rel="apple-touch-icon" href="{r}apple-touch-icon.png">'
    else:
        icons = f'<link rel="icon" href="{FAVICON}">'
    can = f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ""
    feed = (f'<link rel="alternate" type="application/rss+xml" title="{esc(CFG["site_name"])}" href="{r}feed.xml">'
            if CFG["site_url"] else "")
    rss = f' <a href="{r}feed.xml">Suscríbete por RSS</a>.' if CFG["site_url"] else ""
    d = esc(desc or CFG["tagline"])
    return f"""<!doctype html><html lang="{CFG['language']}" data-root="{r}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#012d5c">
<title>{esc(title)}</title><meta name="description" content="{d}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{d}"><meta property="og:type" content="website">
{og}{can}{feed}{icons}<link rel="stylesheet" href="{r}style.css">
<noscript><style>.more[hidden]{{display:block}}.more-btn{{display:none}}</style></noscript></head><body>
<header class="site{' compact' if depth else ''}"><div class="wrap masthead">{brand}</div></header>
<div class="navbar"><div class="wrap navrow"><nav class="cats" aria-label="Secciones">{"".join(items)}</nav>
<button class="search-btn" type="button" data-search aria-label="Buscar historias">{SEARCH_ICON}<span>Buscar</span></button></div></div>
<main class="wrap">{body}</main>
<footer class="site"><div class="wrap"><p><strong>{esc(CFG['site_name'])}</strong> reúne noticias positivas de medios de todo el mundo. Cada historia es un resumen que enlaza a la fuente original.{rss}</p>
<p>© {datetime.now().year}</p></div></footer>
<dialog id="search" class="search" aria-label="Buscar historias"><div class="srow"><input id="q" type="search" placeholder="Buscar historias" autocomplete="off" aria-label="Buscar historias"><button type="button" data-close>Cerrar</button></div><div id="results" aria-live="polite"></div></dialog>
<script src="{r}app.js" defer></script></body></html>"""


def card(a, r, hidden=False):
    cls = "card more" if hidden else "card"
    h = " hidden" if hidden else ""
    return (f'<article class="{cls}"{h}>{media(a)}<div class="body">{chip(a["category"])}'
            f'<h3><a class="stretch" href="{r}nota/{a["slug"]}.html">{esc(a["title"])}</a></h3>'
            f'<p>{esc(a["summary"])}</p>{meta(a)}</div></article>')


def river(items, r, first=18):
    cards = "".join(card(a, r, hidden=i >= first) for i, a in enumerate(items))
    btn = '<button class="more-btn" type="button" data-more>Mostrar más historias</button>' if len(items) > first else ""
    return f'<div class="grid">{cards}</div>{btn}'


def today_line(arts, built):
    fecha = f"{DIAS[built.weekday()].capitalize()} {built.day} de {MESES[built.month - 1]}"
    tz = site_tz()
    n = 0
    for a in arts:
        try:
            if datetime.fromisoformat(a["date"]).astimezone(tz).date() == built.date():
                n += 1
        except Exception:
            pass
    parts = [f"<strong>{fecha}</strong>", f"Actualizado a las {built.strftime('%H:%M')}"]
    if n:
        parts.append(f"{n} historia nueva hoy" if n == 1 else f"{n} historias nuevas hoy")
    return '<div class="today">' + "".join(f"<span>{p}</span>" for p in parts) + "</div>"


def render_home(arts, built):
    if not arts:
        body = ('<div class="empty"><h1 class="sec">Todavía no hay historias</h1>'
                "<p>La primera actualización automática las traerá en unas horas.</p></div>")
        return page(CFG["site_name"], body, 0, current="home")
    lead, side, rest = arts[0], arts[1:5], arts[5:125]
    minis = "".join(
        f'<article class="mini"><div class="body">{chip(a["category"])}'
        f'<h3><a class="stretch" href="nota/{a["slug"]}.html">{esc(a["title"])}</a></h3>{meta(a)}</div>{media(a)}</article>'
        for a in side)
    top = (f'<section class="top{"" if side else " solo"}"><article class="lead">{media(lead, eager=True)}'
           f'<div class="body">{chip(lead["category"])}<h2><a class="stretch" href="nota/{lead["slug"]}.html">{esc(lead["title"])}</a></h2>'
           f'<p>{esc(lead["summary"])}</p>{meta(lead)}</div></article>'
           + (f'<div class="side">{minis}</div>' if side else "") + "</section>")
    more = f'<section><h2 class="sec">Últimas historias</h2>{river(rest, "")}</section>' if rest else ""
    canon = (CFG["site_url"] + "/") if CFG["site_url"] else ""
    return page(f"{CFG['site_name']}: {CFG['tagline']}", today_line(arts, built) + top + more, 0,
                og_image=lead.get("image", ""), canonical=canon, current="home")


def render_article(a, arts):
    same = [x for x in arts if x["category"] == a["category"] and x["id"] != a["id"]][:3]
    if len(same) >= 2:
        rel_title, rel = f"Más de {a['category']}", same
    else:
        rel_title, rel = "Sigue leyendo", [x for x in arts if x["id"] != a["id"]][:3]
    related = (f'<section class="related"><h2 class="sec">{esc(rel_title)}</h2>'
               f'<div class="grid">{"".join(card(x, "../") for x in rel)}</div></section>') if rel else ""
    body = (f'<article class="nota"><a class="chip {cat_cls(a["category"])}" href="../categoria/{slugify(a["category"])}.html">'
            f'<i></i>{esc(a["category"])}</a><h1>{esc(a["title"])}</h1>'
            f'<div class="meta"><b>{esc(a["source"])}</b><time datetime="{esc(a["date"])}">{fmt_date(a["date"])}</time></div>'
            f'{media(a, eager=True) if a.get("image") else ""}<p class="stand">{esc(a["summary"])}</p>'
            f'<div class="source-box"><p>Esto es un resumen. La nota completa está en {esc(a["source"])}.</p>'
            f'<a class="btn" href="{esc(a["url"])}" target="_blank" rel="noopener">Leer la nota completa en {esc(a["source"])}</a></div>'
            f'<div class="actions"><button type="button" data-share data-title="{esc(a["title"])}">Compartir</button>'
            f'<button type="button" data-copy>Copiar enlace</button></div></article>{related}')
    can = f'{CFG["site_url"]}/nota/{a["slug"]}.html' if CFG["site_url"] else ""
    return page(f'{a["title"]} | {CFG["site_name"]}', body, 1, a["summary"], a.get("image", ""), can,
                current=a["category"])


def render_category(cat, items):
    n = len(items)
    count = "Todavía no hay historias en esta sección." if not n else (
        "1 historia" if n == 1 else f"{n} historias")
    head = f'<div class="cathead {cat_cls(cat)}"><h1>{esc(cat)}</h1><p>{count}</p></div>'
    body = head + (river(items, "../") if n else
                   '<div class="empty">Esta sección se llena sola: vuelve en unas horas.</div>')
    return page(f"{cat} | {CFG['site_name']}", body, 1, current=cat)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_site(arts):
    built = datetime.now(site_tz())
    OUT.mkdir(exist_ok=True)
    if ASSETS.exists():
        for f in ASSETS.iterdir():
            if f.is_file():
                shutil.copy2(f, OUT / f.name)
    for old in (OUT / "nota").glob("*.html") if (OUT / "nota").exists() else []:
        old.unlink()
    write(OUT / "style.css", build_css())
    write(OUT / "app.js", JS)
    write(OUT / "index.html", render_home(arts, built))
    for a in arts:
        write(OUT / "nota" / f'{a["slug"]}.html', render_article(a, arts))
    for c in CFG["categories"]:
        write(OUT / "categoria" / f"{slugify(c)}.html", render_category(c, [a for a in arts if a["category"] == c]))
    write(OUT / "search.json", json.dumps(
        [{"t": a["title"], "s": truncate(a["summary"], 160), "c": a["category"], "u": f'nota/{a["slug"]}.html'}
         for a in arts], ensure_ascii=False, separators=(",", ":")))
    write(OUT / "robots.txt", "User-agent: *\nAllow: /\n" + (f"Sitemap: {CFG['site_url']}/sitemap.xml\n" if CFG["site_url"] else ""))
    if CFG["site_url"]:
        u = CFG["site_url"]
        urls = [f"{u}/"] + [f"{u}/nota/{a['slug']}.html" for a in arts] + \
               [f"{u}/categoria/{slugify(c)}.html" for c in CFG["categories"]]
        write(OUT / "sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              + "".join(f"<url><loc>{esc(x)}</loc></url>" for x in urls) + "</urlset>")
        items = "".join(
            f"<item><title>{esc(a['title'])}</title><link>{esc(u)}/nota/{a['slug']}.html</link>"
            f"<guid>{esc(u)}/nota/{a['slug']}.html</guid><description>{esc(a['summary'])}</description>"
            f"<pubDate>{parse_date(a['date']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
            for a in arts[:30])
        write(OUT / "feed.xml", f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{esc(CFG["site_name"])}</title>'
              f'<link>{esc(u)}</link><description>{esc(CFG["tagline"])}</description>{items}</channel></rss>')


def main():
    if os.environ.get("SITE_URL"):
        CFG["site_url"] = os.environ["SITE_URL"].rstrip("/")
    CFG["site_url"] = CFG["site_url"].rstrip("/")
    if "--check" in sys.argv:
        check_feeds()
        return
    arts = json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []
    if "--no-fetch" not in sys.argv:
        new_arts = fetch_new(arts)
        arts = new_arts + arts
        backfill_images(arts)
        print(f"Notas nuevas: {len(new_arts)}. Total: {len(arts)}")
    arts.sort(key=lambda a: a["date"], reverse=True)
    arts = arts[: CFG["max_articles"]]
    write(DATA_FILE, json.dumps(arts, ensure_ascii=False, indent=1))
    build_site(arts)
    print(f"Sitio generado en {OUT}/ con {len(arts)} notas.")


if __name__ == "__main__":
    main()
