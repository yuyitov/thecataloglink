"""Directorio publico de una vertical — subsistema OPT-IN del motor.

Que es. Una pagina estatica por idioma que lista a los clientes que pidieron
aparecer, agrupados por una categoria (en ModaLink, la especialidad de moda) y
filtrables por otra (su estado). Es un canal de adquisicion: el negocio que
aparece tiene una razon mas para renovar, y la pagina posiciona sola.

Por que vive aqui y no en el repo de un producto. Nacio dentro de ModaLink, que
era un fork; al migrarlo al motor deja de ser codigo de un solo producto y pasa
a ser algo que cualquier vertical puede encender desde su `vertical.yaml`. Una
vertical que no declara `directory:` no ejecuta una sola linea de este modulo y
no publica nada.

Que NO comparte con las paginas de cliente. Su HTML es completo y aparte: no usa
`templates/base.html` ni los estilos de marca, porque no es la pagina de un
negocio sino un indice del producto. Por eso su CSS y su JS viven aqui, inline.

**Un directorio vacio FALLA.** Es la decision que el inventario de la migracion
pedia dejar escrita: `collect_entries` descarta en silencio a cualquier cliente
que no pase `validate_client`, asi que una validacion nueva mas estricta podia
dejar el directorio sin nadie y publicar su estado vacio sin un solo error. Ese
es el peor fallo posible —silencioso y en produccion—, asi que si la vertical
tiene clientes que optaron por aparecer y ninguno sobrevive la validacion, esto
revienta en vez de publicar. El estado vacio legitimo (todavia no hay ningun
cliente que haya optado) si se publica: esa pagina existe para invitar.
"""

from __future__ import annotations

CSS = """
:root { --cotton:#F5F1E8; --noir:#17130E; --cherry:#9E1B32; --maroon:#641E26; --camel:#B48A5A; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--cotton); color: var(--noir); line-height: 1.6; padding: 0 0 60px; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 22px; }
header.dir-top { padding: 20px 0; border-bottom: 1px solid rgba(23,19,14,0.12); margin-bottom: 34px; }
header.dir-top a.home { color: var(--maroon); text-decoration: none; font-weight: 700; font-size: 0.9rem; }
.dir-hero { padding: 10px 0 30px; }
.dir-hero .kicker { display: inline-block; font-size: 0.74rem; font-weight: 800; letter-spacing: 0.14em; text-transform: uppercase; color: var(--maroon); background: rgba(158,27,50,0.08); border-radius: 999px; padding: 6px 14px; margin-bottom: 14px; }
.dir-hero h1 { font-size: clamp(1.7rem, 4vw, 2.5rem); line-height: 1.15; margin-bottom: 12px; }
.dir-hero p { color: #4a443c; max-width: 62ch; }
.dir-filter { display: flex; align-items: center; gap: 10px; margin: 28px 0 36px; flex-wrap: wrap; }
.dir-filter label { font-weight: 700; font-size: 0.9rem; }
.dir-filter select { font: inherit; padding: 9px 14px; border-radius: 999px; border: 1.5px solid rgba(23,19,14,0.25); background: #fff; color: var(--noir); }
.dir-cat { margin-bottom: 40px; }
.dir-cat__title { font-size: 1.2rem; margin-bottom: 14px; color: var(--maroon); }
.dir-cat__list { list-style: none; display: grid; gap: 14px; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.dir-card { background: #fff; border: 1.5px solid rgba(23,19,14,0.12); border-radius: 16px; padding: 18px 20px; }
.dir-card__name { font-weight: 700; font-size: 1.02rem; margin-bottom: 4px; }
.dir-card__state { font-size: 0.82rem; color: var(--camel); font-weight: 700; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.04em; }
.dir-card__tagline { color: #4a443c; font-size: 0.92rem; margin-bottom: 10px; }
.dir-card__link { color: var(--maroon); font-weight: 700; text-decoration: none; font-size: 0.9rem; }
.dir-card__link:hover { text-decoration: underline; }
.dir-empty { text-align: center; padding: 70px 20px; background: #fff; border-radius: 24px; border: 1.5px solid rgba(23,19,14,0.1); }
.dir-empty h2 { font-size: 1.5rem; margin-bottom: 10px; }
.dir-empty p { color: #4a443c; max-width: 52ch; margin: 0 auto 22px; }
.dir-empty .cta { display: inline-block; background: var(--maroon); color: #fff; font-weight: 800; text-decoration: none; padding: 13px 28px; border-radius: 999px; }
.dir-no-results { display: none; text-align: center; padding: 50px 20px; color: #4a443c; }
.dir-disclaimer { display: block; text-align: center; padding: 24px 20px 40px; color: #6b6355; font-size: 0.8rem; }
"""

JS = """
(function () {
  var select = document.getElementById('dir-state-filter');
  if (!select) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll('.dir-card'));
  var cats = Array.prototype.slice.call(document.querySelectorAll('.dir-cat'));
  var noResults = document.querySelector('.dir-no-results');
  select.addEventListener('change', function () {
    var value = select.value;
    var visibleCount = 0;
    cards.forEach(function (card) {
      var show = value === '' || card.getAttribute('data-state') === value;
      card.style.display = show ? '' : 'none';
      if (show) visibleCount += 1;
    });
    cats.forEach(function (cat) {
      var anyVisible = Array.prototype.some.call(
        cat.querySelectorAll('.dir-card'),
        function (c) { return c.style.display !== 'none'; }
      );
      cat.style.display = anyVisible ? '' : 'none';
    });
    if (noResults) noResults.style.display = visibleCount === 0 ? '' : 'none';
  });
})();
"""

# Claves que el `directory.strings` de una vertical debe traer en los DOS
# idiomas. Se valida en vertical_config: una clave faltante saldria como el
# literal vacio en una pagina publica y nadie se enteraria.
REQUIRED_STRINGS = (
    "html_lang",
    "page_title",
    "kicker",
    "title",
    "lead",
    "state_filter_label",
    "state_filter_all",
    "view_page",
    "empty_title",
    "empty_body",
    "cta",
    "back_home",
    "no_results_title",
    "no_results_body",
)


class DirectoryError(RuntimeError):
    """Se lanza cuando el directorio no se puede publicar de forma honesta."""


def hreflang(urls: dict, pairs, default_lang: str) -> str:
    """Empareja las versiones del directorio para Google.

    Los codigos tienen que coincidir de los dos lados: poner `es` de un lado y
    `es-MX` del otro rompe el par en silencio.
    """
    alt = "".join(
        f'<link rel="alternate" hreflang="{code}" href="{urls[lang]}">\n'
        for lang, code in pairs
    )
    return alt + f'<link rel="alternate" hreflang="x-default" href="{urls[default_lang]}">'


def render_page(
    entries: list,
    lang: str,
    *,
    strings: dict,
    urls: dict,
    hreflang_html: str,
    category_of,
    category_label,
    category_order,
    state_of,
    tagline_of,
    url_of,
    disclaimer: str,
    esc,
    safe_href,
) -> str:
    """Arma una pagina del directorio.

    Todo lo que depende de la vertical (como se saca la categoria de un cliente,
    como se etiqueta, en que orden van) entra por parametro: este modulo sabe
    maquetar un directorio, no sabe de moda.
    """
    s = strings
    home_href = "../"

    by_category: dict = {}
    states_present: set = set()
    for payload in entries:
        by_category.setdefault(category_of(payload), []).append(payload)
        state = str(state_of(payload) or "").strip()
        if state:
            states_present.add(state)

    ordered = [key for key in category_order if key in by_category]

    if not entries:
        body = (
            '<div class="dir-empty">'
            f'<h2>{esc(s["empty_title"])}</h2>'
            f'<p>{esc(s["empty_body"])}</p>'
            f'<a class="cta" href="{home_href}#precio">{esc(s["cta"])}</a>'
            "</div>"
        )
    else:
        options = "".join(
            f'<option value="{esc(state)}">{esc(state)}</option>'
            for state in sorted(states_present)
        )
        filter_html = (
            '<div class="dir-filter">'
            f'<label for="dir-state-filter">{esc(s["state_filter_label"])}</label>'
            f'<select id="dir-state-filter"><option value="">{esc(s["state_filter_all"])}</option>{options}</select>'
            "</div>"
        )
        sections = "".join(
            _category_section(key, by_category[key], lang, s, category_label,
                              state_of, tagline_of, url_of, esc, safe_href)
            for key in ordered
        )
        no_results_html = (
            '<div class="dir-no-results">'
            f'<h2>{esc(s["no_results_title"])}</h2><p>{esc(s["no_results_body"])}</p>'
            "</div>"
        )
        disclaimer_html = (
            f'<span class="dir-disclaimer">{esc(disclaimer)}</span>' if disclaimer else ""
        )
        body = filter_html + sections + no_results_html + disclaimer_html

    return f"""<!DOCTYPE html>
<html lang="{s['html_lang']}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(s['page_title'])}</title>
<meta name="description" content="{esc(s['lead'])}">
<link rel="canonical" href="{urls[lang]}">
{hreflang_html}
<style>{CSS}</style>
</head>
<body>
<header class="dir-top"><div class="wrap"><a class="home" href="{home_href}">{esc(s['back_home'])}</a></div></header>
<main class="wrap">
<section class="dir-hero">
  <span class="kicker">{esc(s['kicker'])}</span>
  <h1>{esc(s['title'])}</h1>
  <p>{esc(s['lead'])}</p>
</section>
{body}
</main>
<script>{JS}</script>
</body>
</html>
"""


def _entry_card(payload, lang, s, state_of, tagline_of, url_of, esc, safe_href) -> str:
    name = esc(payload.get("business_name"))
    state_raw = str(state_of(payload) or "")
    url = safe_href(url_of(payload, lang))
    tagline = esc(tagline_of(payload, lang))
    tagline_html = f'<p class="dir-card__tagline">{tagline}</p>' if tagline else ""
    link_html = (
        f'<a class="dir-card__link" href="{url}">{s["view_page"]} →</a>' if url else ""
    )
    return (
        f'<li class="dir-card" data-state="{esc(state_raw.strip())}">'
        f'<p class="dir-card__name">{name}</p>'
        f'<p class="dir-card__state">{esc(state_raw)}</p>'
        f'{tagline_html}{link_html}'
        "</li>"
    )


def _category_section(key, payloads, lang, s, category_label,
                      state_of, tagline_of, url_of, esc, safe_href) -> str:
    label = category_label(key, lang) or key
    cards = "".join(
        _entry_card(p, lang, s, state_of, tagline_of, url_of, esc, safe_href)
        for p in payloads
    )
    return (
        f'<section class="dir-cat" data-category="{esc(key)}">'
        f'<h2 class="dir-cat__title">{esc(label)}</h2>'
        f'<ul class="dir-cat__list">{cards}</ul></section>'
    )
