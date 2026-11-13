#!/usr/bin/env python3
"""Portal de noticias positivas: ingesta -> filtro -> (IA opcional) -> sitio estático.

Solo usa la librería estándar de Python (sin dependencias).
Uso:
    python build.py             # descarga feeds, cura, genera public/
    python build.py --no-fetch  # solo regenera el sitio con data/articles.json
"""
import gzip
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "articles.json"
OUT = ROOT / "public"
UA = "Mozilla/5.0 (compatible; BuenasNoticiasBot/1.0)"
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


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


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.iter():
        if local(e.tag) not in ("item", "entry"):
            continue
        d = {}
        for ch in e:
            tag, t = ch.tag, local(ch.tag)
            if "mrss" in tag:  # media:content / media:thumbnail
                if t in ("content", "thumbnail") and ch.get("url"):
                    if t == "thumbnail" or ch.get("medium") in (None, "image") \
                            or (ch.get("type") or "").startswith("image"):
                        d.setdefault("image", ch.get("url"))
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
        if not d.get("image"):
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', (d.get("content") or "") + (d.get("desc") or ""))
            if m:
                d["image"] = m.group(1)
        if d.get("title") and d.get("link"):
            items.append(d)
    return items


# ---------------------------------------------------------------- curación
def passes_blocklist(text):
    t = " " + text.lower() + " "
    return not any(w in t for w in CFG["blocklist"])


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
            imgs = sum(1 for i in items if i.get("image"))
            last = max((parse_date(i.get("date")) for i in items), default=None)
            if not items:
                print(f"AVISO {feed['name']}: responde pero no hay notas (¿no es un feed?). Empieza por: {raw[:150]!r}")
                continue
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
        "Animales": ["dog", "cat ", "animal", "species", "rescue", "perro", "gato", "especie", "rescat", "fauna"],
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
        for it in items[:25]:
            if len(new) >= limit:
                break
            aid = hashlib.sha1(it["link"].encode()).hexdigest()[:10]
            if aid in known:
                continue
            raw = strip_html(it.get("desc") or it.get("content") or "")
            if not passes_blocklist(it["title"] + " " + raw[:400]):
                known.add(aid)
                continue
            if feed.get("mode") == "keywords" and not has_positive_keyword(it["title"] + " " + raw[:400]):
                known.add(aid)
                continue
            # Feeds en español no necesitan traducción: se omite la IA (y el gasto de API).
            use_ai = feed.get("lang", "en") != "es" or CFG.get("ai_for_spanish", False)
            ai = ai_curate(it["title"], raw) if use_ai else None
            if ai is not None and not ai.get("positiva", False):
                known.add(aid)
                continue
            title = (ai or {}).get("titulo") or it["title"]
            summary = (ai or {}).get("resumen") or raw
            cat = (ai or {}).get("categoria")
            if cat not in CFG["categories"]:
                cat = guess_category(title + " " + raw)
            known.add(aid)
            new.append({
                "id": aid,
                "slug": f"{slugify(title)}-{aid}",
                "title": title,
                "summary": truncate(summary, CFG["summary_max_chars"]),
                "category": cat,
                "image": it.get("image", ""),
                "source": feed["name"],
                "url": it["link"],
                "date": parse_date(it.get("date")).isoformat(),
            })
            print(f"  + {title[:70]}")
    return new


# ---------------------------------------------------------------- render
CSS = """
:root{--bg:#fbf8f3;--fg:#23201c;--mut:#6b655c;--card:#fff;--acc:#d9822b;--line:#e8e1d6}
@media(prefers-color-scheme:dark){:root{--bg:#171512;--fg:#efe9df;--mut:#a39b8e;--card:#201d19;--line:#34302a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 Georgia,'Times New Roman',serif}
a{color:inherit;text-decoration:none}.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header.top{border-bottom:1px solid var(--line);padding:22px 0 12px}
.brand{font-size:2rem;font-weight:700;letter-spacing:-.5px}.brand span{color:var(--acc)}
.tag{color:var(--mut);font-style:italic;margin:2px 0 12px}
nav{display:flex;flex-wrap:wrap;gap:6px 16px;font:600 .85rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.5px}
nav a{color:var(--mut)}nav a:hover{color:var(--acc)}
.hero{display:grid;grid-template-columns:1.4fr 1fr;gap:28px;margin:28px 0;align-items:center}
.hero img,.hero .ph{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:10px}
.hero h2{font-size:2rem;line-height:1.2;margin:.3em 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:22px;margin:24px 0 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column;transition:transform .15s}
.card:hover{transform:translateY(-3px)}.card img,.card .ph{width:100%;aspect-ratio:16/10;object-fit:cover}
.ph{background:linear-gradient(135deg,#f3d9b1,#e8a65b)}.cb{padding:14px 16px 18px}
.cb h3{font-size:1.1rem;line-height:1.3;margin:.3em 0}.cb p{color:var(--mut);font-size:.93rem;margin:.3em 0}
.cat{font:700 .72rem system-ui,sans-serif;text-transform:uppercase;letter-spacing:.8px;color:var(--acc)}
time,.meta{font:.8rem system-ui,sans-serif;color:var(--mut)}
article.nota{max-width:720px;margin:32px auto 56px}article.nota h1{font-size:2.3rem;line-height:1.15;margin:.2em 0}
article.nota img{width:100%;border-radius:10px;margin:18px 0}.lead{font-size:1.2rem}
.btn{display:inline-block;background:var(--acc);color:#fff;padding:10px 18px;border-radius:8px;font:600 .95rem system-ui,sans-serif;margin-top:10px}
footer{border-top:1px solid var(--line);padding:24px 0 40px;color:var(--mut);font:.85rem system-ui,sans-serif}
h1.sec{margin:28px 0 0}
@media(max-width:760px){.hero{grid-template-columns:1fr}.hero h2{font-size:1.6rem}article.nota h1{font-size:1.8rem}}
"""


def page(title, body, depth, desc="", og_image="", canonical=""):
    r = "../" * depth
    nav = f'<a href="{r}index.html">Portada</a>' + "".join(
        f'<a href="{r}categoria/{slugify(c)}.html">{esc(c)}</a>' for c in CFG["categories"])
    og = f'<meta property="og:image" content="{esc(og_image)}">' if og_image else ""
    can = f'<link rel="canonical" href="{esc(canonical)}">' if canonical else ""
    feed = f'<link rel="alternate" type="application/rss+xml" title="{esc(CFG["site_name"])}" href="{r}feed.xml">' \
        if CFG["site_url"] else ""
    year = datetime.now().year
    return f"""<!doctype html><html lang="{CFG['language']}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><meta name="description" content="{esc(desc or CFG['tagline'])}">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc or CFG['tagline'])}">
{og}{can}{feed}<link rel="stylesheet" href="{r}style.css"></head><body>
<header class="top"><div class="wrap"><a class="brand" href="{r}index.html">☀ <span>{esc(CFG['site_name'])}</span></a>
<div class="tag">{esc(CFG['tagline'])}</div><nav>{nav}</nav></div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap">© {year} {esc(CFG['site_name'])}. Selección automática de noticias positivas; cada nota enlaza a su fuente original.</div></footer>
</body></html>"""


def card(a, r):
    img = f'<img src="{esc(a["image"])}" alt="" loading="lazy">' if a.get("image") else '<div class="ph"></div>'
    return (f'<a class="card" href="{r}nota/{a["slug"]}.html">{img}<div class="cb">'
            f'<span class="cat">{esc(a["category"])}</span><h3>{esc(a["title"])}</h3>'
            f'<p>{esc(a["summary"])}</p><time>{fmt_date(a["date"])}</time></div></a>')


def render_home(arts):
    if not arts:
        body = "<p>Aún no hay notas. Ejecuta <code>python build.py</code> para cargar las primeras.</p>"
        return page(CFG["site_name"], body, 0)
    h = arts[0]
    img = f'<img src="{esc(h["image"])}" alt="">' if h.get("image") else '<div class="ph"></div>'
    hero = (f'<a class="hero" href="nota/{h["slug"]}.html">{img}<div><span class="cat">{esc(h["category"])}</span>'
            f'<h2>{esc(h["title"])}</h2><p class="lead">{esc(h["summary"])}</p><time>{fmt_date(h["date"])}</time></div></a>')
    grid = '<div class="grid">' + "".join(card(a, "") for a in arts[1:30]) + "</div>"
    return page(f"{CFG['site_name']} — {CFG['tagline']}", hero + grid, 0,
                og_image=h.get("image", ""), canonical=(CFG["site_url"] + "/") if CFG["site_url"] else "")


def render_article(a):
    img = f'<img src="{esc(a["image"])}" alt="">' if a.get("image") else ""
    body = (f'<article class="nota"><span class="cat">{esc(a["category"])}</span><h1>{esc(a["title"])}</h1>'
            f'<div class="meta">{fmt_date(a["date"])} · Fuente: {esc(a["source"])}</div>{img}'
            f'<p class="lead">{esc(a["summary"])}</p>'
            f'<a class="btn" href="{esc(a["url"])}" target="_blank" rel="noopener">Leer la nota completa en {esc(a["source"])} →</a>'
            f'</article>')
    can = f'{CFG["site_url"]}/nota/{a["slug"]}.html' if CFG["site_url"] else ""
    return page(f'{a["title"]} — {CFG["site_name"]}', body, 1, a["summary"], a.get("image", ""), can)


def render_category(cat, arts):
    body = f'<h1 class="sec">{esc(cat)}</h1>'
    body += '<div class="grid">' + "".join(card(a, "../") for a in arts) + "</div>" if arts \
        else "<p>Todavía no hay notas en esta categoría.</p>"
    return page(f"{cat} — {CFG['site_name']}", body, 1)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_site(arts):
    OUT.mkdir(exist_ok=True)
    for old in (OUT / "nota").glob("*.html") if (OUT / "nota").exists() else []:
        old.unlink()
    write(OUT / "style.css", CSS)
    write(OUT / "index.html", render_home(arts))
    for a in arts:
        write(OUT / "nota" / f'{a["slug"]}.html', render_article(a))
    for c in CFG["categories"]:
        write(OUT / "categoria" / f"{slugify(c)}.html", render_category(c, [a for a in arts if a["category"] == c]))
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
        arts = fetch_new(arts) + arts
        print(f"Notas nuevas incorporadas. Total: {len(arts)}")
    arts.sort(key=lambda a: a["date"], reverse=True)
    arts = arts[: CFG["max_articles"]]
    write(DATA_FILE, json.dumps(arts, ensure_ascii=False, indent=1))
    build_site(arts)
    print(f"Sitio generado en {OUT}/ con {len(arts)} notas.")


if __name__ == "__main__":
    main()
