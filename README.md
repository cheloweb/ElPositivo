# Buenas Noticias — portal de noticias positivas autoactualizable

Sitio estático generado con Python (solo librería estándar, sin dependencias).
Cada 3 horas un workflow de GitHub Actions:

1. Lee los feeds RSS de `config.json` (fuentes especializadas en noticias positivas).
2. Descarta notas con palabras negativas (`blocklist`).
3. Si hay `ANTHROPIC_API_KEY`, Claude valida que la nota sea positiva, reescribe título y resumen en español (texto original, no copiado) y asigna categoría.
4. Acumula las notas en `data/articles.json` y regenera `public/` (portada, una página por nota, categorías, RSS, sitemap).
5. Publica en GitHub Pages.

Cada nota muestra un resumen corto y enlaza a la fuente original (no se copian artículos completos).

## Probar en local

```bash
python build.py              # descarga, cura y genera public/
python build.py --no-fetch   # solo regenera con lo que ya hay en data/
python -m http.server -d public 8000
```

## Publicar (GitHub Pages)

1. Sube esta carpeta a un repo de GitHub (rama `main`).
2. Settings → Pages → Source: **GitHub Actions**.
3. Settings → Secrets and variables → Actions:
   - Secret `ANTHROPIC_API_KEY` (opcional pero recomendado: mejor filtro y resúmenes en español).
   - Variable `SITE_URL` (ej. `https://tuusuario.github.io/repo`), habilita sitemap y RSS.
4. Actions → "Actualizar y publicar portal" → Run workflow. Desde ahí corre solo.

## Personalizar

Todo está en `config.json`: nombre, categorías, feeds, palabras bloqueadas, notas nuevas por ejecución (`max_new_per_run`) y notas máximas conservadas (`max_articles`).

## Notas

- Verifica las URLs de los feeds; si una fuente cambia su RSS, el script la omite y sigue con las demás.
- Sin API key el sitio funciona, pero mostrará títulos y resúmenes en el idioma original (inglés) y el filtro será solo por palabras.
- Revisa los términos de uso de cada fuente; el diseño usa solo titular + resumen corto + enlace.
