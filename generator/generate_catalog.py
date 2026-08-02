#!/usr/bin/env python3
"""Catalog App — static product-catalog HTML + QR generator (Fase F4).

Second base template of the engine, next to generate_service_menu.py. Where
service-menu renders a business's SERVICES, this renders a business's PRODUCTS
as a photo-forward "vitrina" (shop window): a searchable, category-filterable
grid of product cards, each with a photo, a bilingual name + description, a
price OR an intentional "Preguntar precio" pill (the norm in this niche — small
shops rarely post prices on Instagram), and a sale button that pushes the order
to WhatsApp / SMS / a phone call.

This is NOT e-commerce (decisión de producto CERRADA, PLAN §F4): no cart, no
checkout, no inventory, no shipping. It is a catalog that ends in a conversation.

Fed by Cory's F2 extraction (`catalog_products[]`): the bridge in
hmu-prospector/prospector/build_prospect.py::build_payload_catalog maps a
prospect's extracted products into the `catalog_payload_public` shape this
generator consumes. Cory imports this module exactly like it imports
generate_service_menu (same OUTPUT_DIR / DEMO_BASE_URL / BRAND_STYLES /
build_demo interface).

Styles = layout + palette. A style name is `<layout>-<palette>`
(e.g. `vitrina-crema`): the LAYOUT (structural template
templates/catalog-<layout>.html) comes from the prefix, the palette
(styles/<name>.css) is just color variables. F4 step 1-2 ships the `vitrina`
layout (luminous grid) with 4 palettes; `revista` and `elegante` layouts are
the styles session (see CENTRO).

Photo origin is a SINGLE configurable point (`photo_base_url` + per-product
`image`, or a local `photo_source_dir` that gets copied next to the page). The
F3 photo-enhancement step plugs in here later by swapping the origin — nothing
else in the generator needs to change.

Usage:
    LINK_FACTORY_VERTICAL=catalog python generate_catalog.py            # all demos
    LINK_FACTORY_VERTICAL=catalog python generate_catalog.py a.json     # specific demo(s)
    LINK_FACTORY_VERTICAL=catalog python generate_catalog.py --client c.json
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import unicodedata

import wallet
from blocks import block_enabled, fill_tokens
from pathlib import Path
from urllib.parse import quote

# Shared, pure helpers from the service-menu generator. Importing it does NOT
# change HMU output (no rendering runs at import) and keeps escaping/QR logic in
# one place — the golden stays byte-identical.
from generate_service_menu import (
    QR_ASSET_NAME,
    QR_PNG_ASSET_NAME,
    VCARD_ASSET_NAME,
    ValidationError,
    _initials,
    accent_hex,
    build_vcard_action,
    build_wallet_action,
    esc,
    make_qr_png,
    make_qr_svg,
    make_vcard,
    safe_href,
)
from strings_base import CATALOG_CATEGORY_EN
from vertical_config import (
    BLOCKS, BRAND_NAME, DOMAIN, LEGAL, STRINGS, STYLES_CATALOG, TEMPLATE,
)

# Repo layout (this file lives in <repo>/generator/) — standalone export,
# produced by link-factory/scripts/export_vertical.py. To change this
# vertical, edit it in link-factory and re-export; do not hand-edit here.
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STYLES_DIR = Path(__file__).resolve().parent / "styles"
DEMOS_DIR = REPO_ROOT / "data" / "demos"
OUTPUT_DIR = REPO_ROOT / "public" / "demos"
CLIENTS_DIR = REPO_ROOT / "data" / "clients"
CLIENT_OUTPUT_DIR = REPO_ROOT / "public" / "links"

BRAND_STYLES = STYLES_CATALOG
DEMO_BASE_URL = f"{DOMAIN}/demos"
CLIENT_BASE_URL = f"{DOMAIN}/links"
CLIENT_LANGS = ("es", "en")

DEMO_HEAD_META = '<meta name="robots" content="noindex">'

# Favicon del catálogo (lote v3, H2 de charly/45): mismo criterio que el
# generador de tarjetas — toda página generada lleva el favicon del producto.
FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/assets/brand/favicon.svg">'

# Slug shape guard (same alphabet as the service-menu generator).
CATALOG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")

SALE_KINDS = ("whatsapp", "sms", "tel")

# Small inline icons per sale channel (no external assets).
_SALE_ICONS = {
    "whatsapp": '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.9c0 1.75.46 3.45 1.32 4.95L2 22l5.3-1.39a9.9 9.9 0 0 0 4.74 1.21h.01c5.46 0 9.9-4.45 9.9-9.9C21.95 6.45 17.5 2 12.04 2Zm5.8 14.16c-.24.68-1.42 1.32-1.95 1.36-.5.05-.5.4-3.15-.66-2.66-1.06-4.3-3.8-4.43-3.97-.13-.17-1.05-1.4-1.05-2.67 0-1.27.66-1.9.9-2.16.24-.26.53-.32.7-.32l.5.01c.16.01.38-.06.6.46.24.56.8 1.94.87 2.08.07.14.12.3.02.48-.1.18-.14.29-.29.44-.14.14-.3.32-.43.43-.14.14-.3.29-.13.57.17.28.75 1.24 1.62 2 1.11.99 2.05 1.3 2.33 1.44.28.14.44.12.6-.07.17-.2.7-.81.88-1.09.18-.28.36-.23.6-.14.24.09 1.55.73 1.82.86.28.14.46.2.53.32.07.11.07.65-.17 1.32Z"/></svg>',
    "sms": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.7A8.5 8.5 0 0 1 12.5 3 8.38 8.38 0 0 1 21 11.5Z"/></svg>',
    "tel": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.1-8.7A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.7a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.4-1.1a2 2 0 0 1 2.1-.5c.9.3 1.8.5 2.7.6a2 2 0 0 1 1.7 2Z"/></svg>',
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _plain(value) -> str:
    """Lowercase, accent-stripped — for the static search index and cat slugs."""
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower().strip()


def cat_slug(category: str) -> str:
    """Category -> stable filter slug (`Talleres y cursos` -> `talleres-y-cursos`)."""
    slug = re.sub(r"[^a-z0-9]+", "-", _plain(category)).strip("-")
    return slug or "otros"


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def sale_href(kind: str, value: str, text: str | None = None) -> str | None:
    """Build the sale link for the configured channel.

    - whatsapp -> https://wa.me/<digits>[?text=…]  (prefilled text supported)
    - sms      -> sms:+<digits>   (no body: the ?body/&body split across iOS and
                  Android is unreliable, so we just open the composer to the number)
    - tel      -> tel:+<digits>
    """
    kind = str(kind or "").strip().lower()
    digits = _digits(value)
    if not digits:
        # Not a phone: allow a raw public URL as an escape hatch.
        return safe_href(value)
    if kind == "whatsapp":
        href = f"https://wa.me/{digits}"
        if text:
            href += "?text=" + quote(text)
        return href
    if kind == "sms":
        return f"sms:+{digits}"
    if kind == "tel":
        return f"tel:+{digits}"
    return f"https://wa.me/{digits}"


def sale_label(kind: str, s: dict) -> str:
    return {
        "whatsapp": s["catalog_sale_whatsapp"],
        "sms": s["catalog_sale_sms"],
        "tel": s["catalog_sale_tel"],
    }.get(str(kind or "").strip().lower(), s["catalog_sale_generic"])


def layout_for_style(brand_style: str) -> str:
    """`vitrina-crema` -> `vitrina` (the structural layout / template)."""
    return str(brand_style or "").split("-", 1)[0]


def palettes_hermanas(brand_style: str) -> list[str]:
    """TODAS las paletas del mismo layout, incluida la propia, en orden.

    `vitrina-crema` -> ['vitrina-crema', 'vitrina-durazno', 'vitrina-rosa',
    'vitrina-salvia'].

    Incluye la propia a proposito. La tienda no sabe con que paleta se horneo
    cada demo —y no siempre es la primera de la lista: `mesa-bonita` esta
    horneada en `revista-salvia` mientras la tienda pinta Crema de primera— asi
    que el enlace manda SIEMPRE la paleta elegida y la pagina la aplica sea cual
    sea. Suponer "la primera es la horneada" dejaba justo ese caso roto.
    """
    layout = layout_for_style(brand_style)
    return [estilo for estilo in BRAND_STYLES if layout_for_style(estilo) == layout]


def bloque_paletas_alternas(brand_style: str) -> str:
    """CSS de las paletas hermanas, listo para activarse por atributo.

    Cada `<paleta>.css` es exactamente un `:root{...}` de variables de color, asi
    que reescribirlo como `html[data-palette="<slug>"]{...}` deja las cuatro
    paletas en la pagina y hace que gane la elegida (0,1,1 le gana a 0,1,0 de
    `:root`). Sin `?p=` no se activa ninguna y la pagina se ve exactamente como
    antes.

    Por que existe (fallo de Vero, 2026-07-31): en la tienda, elegir una paleta
    repinta la MINIATURA, pero el enlace "ver demo" siempre abria la demo con la
    paleta horneada. La clienta elegia Salvia y se le abria Crema.

    Solo se hornea en las DEMOS. Una pagina de cliente real vendio UNA paleta:
    no debe traer las otras tres ni poder cambiarse con un parametro.
    """
    # Las cuatro, la propia incluida: `:root` sigue siendo el default sin `?p=`,
    # y la version por atributo es la que permite pedirla explicitamente.
    fragmentos = []
    for slug in palettes_hermanas(brand_style):
        ruta = STYLES_DIR / f"{slug}.css"
        if not ruta.exists():
            continue
        css = ruta.read_text(encoding="utf-8")
        if ":root{" not in css.replace(" ", ""):
            continue
        fragmentos.append(
            css.replace(":root{", f'html[data-palette="{slug}"]{{', 1)
        )
    if not fragmentos:
        return ""
    return (
        "\n/* ---- Las otras paletas de este layout, inertes hasta que ?p= las "
        "encienda (solo demos) ---- */\n" + "\n".join(fragmentos)
    )


# Corre en el <head>, antes de pintar: sin esto la demo se veria un instante con
# la paleta horneada y luego saltaria a la elegida. Solo acepta un slug de la
# lista horneada, asi que ?p= no puede inyectar nada.
PALETTE_SWITCH_JS = """<script>
(function () {
  try {
    var permitidas = %s;
    var p = new URLSearchParams(location.search).get('p');
    if (p && permitidas.indexOf(p) !== -1) {
      document.documentElement.setAttribute('data-palette', p);
    }
  } catch (e) { /* la paleta horneada sigue siendo valida */ }
})();
</script>"""


def translate_category(category: str, lang: str) -> str:
    """Categoría (F2 la entrega SOLO en español) -> etiqueta a MOSTRAR por idioma.

    En español devuelve la categoría tal cual. En inglés la traduce con el
    vocabulario del nicho (CATALOG_CATEGORY_EN, clave = categoría normalizada);
    si no está en la tabla, cae de forma elegante a la etiqueta en español (mejor
    una palabra en español que un hueco). SOLO afecta el TEXTO visible: el slug
    del filtro (`cat_slug`) se sigue derivando de la categoría en español para
    que chips y tarjetas casen igual en ambos idiomas.
    """
    cat = str(category or "").strip()
    if lang != "en" or not cat:
        return cat
    return CATALOG_CATEGORY_EN.get(_plain(cat), cat)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_catalog(payload: dict) -> None:
    """Validate a `catalog_payload_public` v1 payload."""
    slug = str(payload.get("public_slug", "")).strip()
    if not slug:
        raise ValidationError("Catálogo: falta public_slug.")
    if not CATALOG_SLUG_RE.match(slug):
        raise ValidationError(f"Catálogo: public_slug invalido: {slug!r}.")
    if payload.get("default_language") not in CLIENT_LANGS:
        raise ValidationError("Catálogo: default_language debe ser 'es' o 'en'.")
    if payload.get("brand_style") not in BRAND_STYLES:
        raise ValidationError(
            f"Catálogo: brand_style invalido: {payload.get('brand_style')!r} "
            f"(validos: {', '.join(BRAND_STYLES)})."
        )
    layout = layout_for_style(payload.get("brand_style"))
    if not (TEMPLATES_DIR / f"catalog-{layout}.html").exists():
        raise ValidationError(
            f"Catálogo: no existe la plantilla de layout 'catalog-{layout}.html' "
            f"para el estilo {payload.get('brand_style')!r}."
        )
    if not str(payload.get("business_name", "")).strip():
        raise ValidationError("Catálogo: falta business_name.")

    sale = payload.get("sale_button")
    if not isinstance(sale, dict):
        raise ValidationError("Catálogo: falta sale_button (el botón de venta).")
    if sale.get("kind") not in SALE_KINDS:
        raise ValidationError(
            f"Catálogo: sale_button.kind debe ser uno de {SALE_KINDS} "
            f"(recibido: {sale.get('kind')!r})."
        )
    if not sale_href(sale.get("kind"), sale.get("value")):
        raise ValidationError("Catálogo: sale_button.value no produce un enlace válido.")

    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValidationError("Catálogo: falta content con 'es' y 'en'.")
    for lang in CLIENT_LANGS:
        if not isinstance(content.get(lang), dict):
            raise ValidationError(f"Catálogo: falta content.{lang}.")

    products = payload.get("products")
    if not isinstance(products, list) or not products:
        raise ValidationError("Catálogo: products debe tener al menos un producto.")
    for i, p in enumerate(products):
        if not isinstance(p, dict):
            raise ValidationError(f"Catálogo: products[{i}] debe ser un objeto.")
        # ES/EN parity: a product missing one language is invisible to half the
        # visitors (same house rule as strings/disclaimers).
        for field in ("name_es", "name_en"):
            if not str(p.get(field, "")).strip():
                raise ValidationError(f"Catálogo: products[{i}] requiere {field}.")
        if not str(p.get("category", "")).strip():
            raise ValidationError(f"Catálogo: products[{i}] requiere category.")


# --------------------------------------------------------------------------- #
# Photo origin (single configurable point — F3 plugs in here)
# --------------------------------------------------------------------------- #
def resolve_images(payload: dict, root_dir: Path, payload_dir: Path | None = None) -> list[dict]:
    """Return, per product, the image URL for the root page and the /en/ page.

    Three modes, in order:
    1. absolute http(s) `image`  -> used verbatim for both languages.
    2. relative `image` + local `photo_source_dir` -> copied once into
       `<slug>/img/<basename>`; referenced as `img/<name>` (root) and
       `../img/<name>` (/en/). Makes the demo self-contained for HTTP review.
    3. relative `image` + `photo_base_url` -> `photo_base_url + image` for both
       (Cory supplies absolute preview URLs this way).

    Missing image -> None (the card renders a neutral placeholder tile; an image
    problem must never block generation, same philosophy as service-menu).
    """
    base = str(payload.get("photo_base_url", "") or "").rstrip("/")
    source_dir = payload.get("photo_source_dir")
    source = Path(source_dir) if source_dir else None
    # Una ruta RELATIVA se resuelve contra la carpeta del propio payload, no
    # contra el directorio de trabajo. Es lo que permite que una demo con fotos
    # versionadas en el repo se construya igual desde la fabrica
    # (verticals/catalog/data/demos/) que desde el repo exportado (data/demos/),
    # donde las rutas quedan aplanadas. Cory sigue mandando rutas ABSOLUTAS a la
    # carpeta del prospecto y ese camino no cambia.
    if source is not None and not source.is_absolute() and payload_dir is not None:
        source = (payload_dir / source).resolve()
    img_dir = root_dir / "img"
    resolved: list[dict] = []
    copied_names: set[str] = set()
    for p in payload.get("products", []):
        image = str(p.get("image", "") or "").strip()
        if not image:
            resolved.append({"es": None, "en": None})
            continue
        if image.lower().startswith(("http://", "https://")):
            href = safe_href(image)
            resolved.append({"es": href, "en": href})
            continue
        if source is not None:
            src = source / image
            if src.exists():
                img_dir.mkdir(parents=True, exist_ok=True)
                name = Path(image).name
                shutil.copyfile(src, img_dir / name)
                copied_names.add(name)
                resolved.append({"es": f"img/{esc(name)}", "en": f"../img/{esc(name)}"})
                continue
        joined = f"{base}/{image}" if base else image
        href = safe_href(joined)
        resolved.append({"es": href, "en": href})
    # Poda de huérfanos: al re-generar un prospecto que antes servía fotos crudas
    # (.jpg) y ahora sirve las optimizadas de F3 (.webp), las copias viejas
    # quedarían como peso muerto en el deploy. Se borra del img/ todo lo que este
    # build ya no referencia — la página solo carga lo vigente.
    if img_dir.exists():
        for old in img_dir.iterdir():
            if old.is_file() and old.name not in copied_names:
                old.unlink()
    return resolved


# --------------------------------------------------------------------------- #
# HTML fragment builders (all dynamic values escaped here)
# --------------------------------------------------------------------------- #
def build_logo(payload: dict) -> str:
    href = safe_href(payload.get("logo_url"))
    alt = esc(payload.get("business_name"))
    if href:
        return f'<img class="monogram" src="{href}" alt="{alt}">'
    return f'<div class="monogram" aria-hidden="true">{_initials(str(payload.get("business_name", "")))}</div>'


def hero_title_html(business_name: str) -> str:
    """Business name with the last word in accent italics (editorial signature)."""
    words = [w for w in re.split(r"\s+", str(business_name or "").strip()) if w]
    if not words:
        return ""
    if len(words) == 1:
        return esc(words[0])
    head = esc(" ".join(words[:-1]))
    tail = esc(words[-1])
    return f"{head} <em>{tail}</em>"


_PIN_SVG = (
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" '
    'stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<path d="M21 10c0 6-9 12-9 12s-9-6-9-12a9 9 0 0 1 18 0Z"/>'
    '<circle cx="12" cy="10" r="3"/></svg>'
)


def hero_meta_html(payload: dict) -> str:
    parts = []
    address = str(payload.get("address") or "").strip()
    maps_url = safe_href(payload.get("maps_url"))
    if address and maps_url:
        # Con `maps_url` en el payload (los demos del prospector de Cory lo
        # traen de Places) el chip pinta la DIRECCION COMPLETA como link a su
        # Google Maps — decision de Vero en charly/44 (2026-08-01): "Granja" a
        # secas era el dato correcto pareciendo categoria equivocada. Sin
        # `maps_url` (paginas de clientes por intake) se queda la ciudad de
        # siempre: ese comportamiento lo fija linkFactory/9 (C) y no cambia.
        # safe_href ya devuelve la URL escapada — re-escaparla rompería los
        # `&` que los maps_url reales sí traen.
        parts.append(
            f'<a href="{maps_url}" target="_blank" rel="noopener noreferrer">'
            f'{_PIN_SVG}{esc(address)}</a>'
        )
    else:
        city = _city_from_address(address)
        if city:
            parts.append(f"<span>{_PIN_SVG}{esc(city)}</span>")
    rating = payload.get("rating")
    if isinstance(rating, (int, float)) and rating > 0:
        count = payload.get("rating_count")
        label = f"{rating:.1f}"
        if isinstance(count, int) and count > 0:
            label += f" · {count}"
        parts.append(f'<span aria-hidden="true">★</span><span>{esc(label)}</span>')
    # Redes sociales del negocio, si el payload las trae (demos del prospector:
    # el Instagram/Facebook real cosechado de Places) — misma decision charly/44.
    for link in payload.get("social_links") or []:
        url = safe_href((link or {}).get("url"))
        label_txt = str((link or {}).get("label") or "").strip()
        if url and label_txt:
            parts.append(
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'{esc(label_txt)}</a>'
            )
    if not parts:
        return ""
    return f'<div class="hero__meta">{"".join(parts)}</div>'


# Un tipo de vía al arranque de la parte: "Calle …", "Av. …", "Carretera …".
# Solo van los inequívocos en español. Los ingleses NO hacen falta y meterlos
# sería peor: van detrás del número ("123 Main St"), así que los caza la regla
# del número, y poner "st" o "dr" en esta lista rompería "St. Louis" y "Dr Phillips".
_VIA_RE = re.compile(
    r"(?i)^\s*(?:calle|av|ave|avda|avenida|blvd|boulevard|bulevar|carretera|carr"
    r"|camino|callej[oó]n|andador|privada|priv|prolongaci[oó]n|circuito|retorno"
    r"|cerrada|plaza|paseo|colonia|col|fracc|fraccionamiento|manzana|mza|lote"
    r"|local|interior|int)\b\.?\s+"
)


def _es_calle(parte: str) -> bool:
    """La parte es CALLE y no ciudad: lleva número, o arranca con un tipo de vía.

    Se mira la parte CRUDA a propósito, antes de que se le quite el código
    postal: en "Av. Mexico 1234" el número es de 4 cifras y el limpiado se lo
    come, así que sobre la parte limpia esto no vería el número.
    """
    return bool(re.search(r"\d", parte) or _VIA_RE.match(parte))


def _city_from_address(address) -> str:
    """La ciudad que se pinta bajo el icono de ubicación.

    La ciudad es la PRIMERA parte que no es calle. Antes se tomaba siempre la
    PENÚLTIMA, que acierta en "calle, ciudad, estado" y publica LA CALLE en
    "calle, ciudad" — la forma que escribe cualquiera que no ponga el estado
    ("Calle Josefa Ortiz 12, Puerto Vallarta" -> "Calle Josefa Ortiz 12", medido
    el 2026-07-26). La dirección completa sigue publicándose donde corresponde;
    lo que estaba mal era el renglón que pretende ser solo la ciudad.
    """
    addr = str(address or "").strip()
    if not addr:
        return ""
    parts = []  # (cruda, limpia)
    for part in addr.split(","):
        cleaned = re.sub(r"\b(?:c\.?\s*p\.?|cp|zip)\s*[:#-]?\s*", "", part, flags=re.I)
        cleaned = re.sub(r"\b\d{4,6}\b", "", cleaned).strip(" .,-")
        if cleaned:
            parts.append((part, cleaned))
    for cruda, limpia in parts:
        if not _es_calle(cruda):
            return limpia
    # Todo parece calle (p. ej. una dirección sin ciudad): se devuelve la última,
    # que es el comportamiento anterior para ese caso y no inventa una ciudad.
    return parts[-1][1] if parts else ""


def build_sale_cta(payload: dict, s: dict, *, variant: str) -> str:
    """Sale button in one of three placements: topbar / hero / dock."""
    sale = payload.get("sale_button") or {}
    kind = sale.get("kind")
    href = sale_href(kind, sale.get("value"))
    if not href:
        return ""
    icon = _SALE_ICONS.get(kind, _SALE_ICONS["whatsapp"])
    label = esc(sale_label(kind, s))
    href_a = esc(href)
    if variant == "topbar":
        return (
            f'<a class="btn-sale" href="{href_a}" target="_blank" rel="noopener noreferrer">'
            f'{icon}<span>{label}</span></a>'
        )
    if variant == "dock":
        return (
            '<div class="dock" id="dock"><a class="btn-sale" href="'
            f'{href_a}" target="_blank" rel="noopener noreferrer">{icon}{label}</a></div>'
        )
    # hero
    return (
        f'<a class="btn-sale" href="{href_a}" target="_blank" rel="noopener noreferrer">'
        f'{icon}{label}</a>'
    )


def _categories_in_order(products: list) -> list[str]:
    seen, order = set(), []
    for p in products:
        cat = str(p.get("category", "") or "").strip()
        if cat and cat not in seen:
            seen.add(cat)
            order.append(cat)
    return order


def build_filters(products: list, s: dict, lang: str = "es") -> str:
    # El slug (data-cat) se deriva SIEMPRE de la categoría en español, así el
    # chip y la tarjeta casan igual en ambos idiomas; solo el TEXTO se traduce.
    cats = _categories_in_order(products)
    chips = [
        '<button class="chip" type="button" data-cat="all" aria-pressed="true">'
        f'{esc(s["catalog_filter_all"])}</button>'
    ]
    for cat in cats:
        chips.append(
            f'<button class="chip" type="button" data-cat="{esc(cat_slug(cat))}" '
            f'aria-pressed="false">{esc(translate_category(cat, lang))}</button>'
        )
    return "".join(chips)


def _price_html(product: dict, s: dict) -> str:
    """Real price when present; otherwise the intentional "ask price" pill.

    A product has a real price only when it is not flagged ask_price AND carries
    a price_label. Everything else — the norm in this niche — renders the pill,
    which reads as an invitation ("preguntar precio"), never as a broken field.
    """
    ask = bool(product.get("ask_price"))
    price_label = str(product.get("price_label", "") or "").strip()
    if not ask and price_label:
        return f'<span class="price">{esc(price_label)}</span>'
    return f'<span class="price price--ask">{esc(s["catalog_ask_price"])}</span>'


def build_product_cards(payload: dict, s: dict, lang: str, images: list[dict]) -> str:
    products = payload.get("products", [])
    sale = payload.get("sale_button") or {}
    kind = sale.get("kind")
    name_key = f"name_{lang}"
    desc_key = f"description_{lang}"
    cards = []
    for i, p in enumerate(products):
        name = str(p.get(name_key) or p.get("name_es") or "").strip()
        desc = str(p.get(desc_key) or "").strip()
        cat = str(p.get("category", "") or "").strip()
        img_url = images[i][lang] if i < len(images) else None
        alt = esc(name or payload.get("business_name"))
        if img_url:
            media = (
                f'<img class="card__img" src="{img_url}" alt="{alt}" loading="lazy">'
            )
        else:
            media = '<div class="card__img" aria-hidden="true"></div>'
        cat_display = translate_category(cat, lang)
        cat_badge = f'<span class="card__cat">{esc(cat_display)}</span>' if cat else ""
        desc_html = f'<p class="card__desc">{esc(desc)}</p>' if desc else ""
        # Per-card CTA reuses the sale channel. En WhatsApp lleva el copy y la
        # precarga aprobados por Vero (charly/44, 2026-08-01): saludo + nombre
        # del producto, no el nombre a secas. En sms/tel NO se menciona
        # WhatsApp (candado test_canal_por_pais: un demo de EE. UU. vende por
        # texto) y el composer/dialer no honra texto precargado.
        is_wa = str(kind or "").strip().lower() == "whatsapp"
        wa_text = (
            s["catalog_product_wa_text"].replace("{product}", name)
            if is_wa and name else None
        )
        cta_href = sale_href(kind, sale.get("value"), text=wa_text)
        cta_label = s["catalog_product_cta_whatsapp"] if is_wa else s["catalog_product_cta"]
        cta = (
            f'<a class="card__cta" href="{esc(cta_href)}" target="_blank" '
            f'rel="noopener noreferrer">{esc(cta_label)}</a>'
            if cta_href else ""
        )
        # Índice de búsqueda: nombre + descripción (ya por idioma) + categoría en
        # AMBOS idiomas, así "boards" encuentra "Tablas" en la página en inglés.
        search_blob = _plain(" ".join(filter(None, [name, desc, cat, cat_display])))
        cards.append(
            f'<article class="card" data-cat="{esc(cat_slug(cat))}" '
            f'data-search="{esc(search_blob)}">'
            f'<div class="card__media">{media}{cat_badge}</div>'
            '<div class="card__body">'
            f'<h3 class="card__name">{esc(name)}</h3>{desc_html}'
            f'<div class="card__foot">{_price_html(p, s)}{cta}</div>'
            '</div></article>'
        )
    return "".join(cards)


def products_count_label(n: int, s: dict) -> str:
    if n == 1:
        return s["catalog_results_count_one"]
    return s["catalog_results_count_many"].replace("{n}", str(n))


# --------------------------------------------------------------------------- #
# vCard — bloque `vcard` en la SEGUNDA plantilla base (linkFactory/22)
#
# El bloque nacio en generate_service_menu.py (linkFactory/14). El catalogo no
# lo heredaba: se renderiza aqui, con otras plantillas, y encender el bloque en
# su vertical.yaml no habria producido un solo boton — medido el 2026-07-30 y
# anotado en la ficha. Portarlo es lo que hace que "activado" y "visible"
# signifiquen lo mismo en las cinco verticales.
#
# El FORMATO del .vcf no se duplica: se reusa `make_vcard` tal cual. Lo unico
# que vive aqui es el adaptador de payload, porque el catalogo no captura
# `phone` — captura `sale_button`, que cuando es whatsapp/sms/tel ES el
# telefono publico del negocio.
# --------------------------------------------------------------------------- #
def vcard_view(payload: dict) -> dict:
    """El payload del catalogo en los campos que `make_vcard` sabe leer.

    Solo datos que la pagina YA publica: el nombre, la direccion del heroe y el
    numero del boton de venta. Si el boton de venta no es un canal telefonico
    (hoy los tres lo son) el vCard sale sin TEL, no con un dato inventado.
    """
    sale = payload.get("sale_button")
    phone = ""
    if isinstance(sale, dict) and sale.get("kind") in SALE_KINDS:
        phone = str(sale.get("value", "") or "").strip()
    return {
        "business_name": payload.get("business_name", ""),
        "phone": phone,
        "address": payload.get("address", ""),
        "website": payload.get("website", ""),
    }


def vcard_enabled(payload: dict) -> bool:
    """El mismo gate del otro generador: registro del motor + vertical.yaml."""
    return block_enabled(BLOCKS, "vcard", payload.get("business_type"))


def write_vcard(payload: dict, root_dir: Path, page_url: str) -> None:
    """Escribe el contact.vcf del cliente/demo SOLO con el bloque encendido.

    `newline=""`: el RFC pide CRLF y `make_vcard` ya los pone; sin esto Windows
    los duplicaria y el archivo dejaria de ser el mismo en los dos sistemas.
    """
    if not vcard_enabled(payload):
        return
    with (root_dir / VCARD_ASSET_NAME).open("w", encoding="utf-8", newline="") as fh:
        fh.write(make_vcard(vcard_view(payload), page_url))


def build_share(share_url: str, s: dict, qr_src: str = QR_ASSET_NAME,
                vcard_html: str = "", wallet_html: str = "") -> str:
    href = safe_href(share_url)
    shown = esc(re.sub(r"^https?://(www\.)?", "", str(share_url)).rstrip("/"))
    alt = esc(f"{s['qr_alt']} {share_url}")
    link_line = (
        f'<a class="share__url" href="{href}" target="_blank" rel="noopener noreferrer">{shown}</a>'
        if href else f'<span class="share__url">{shown}</span>'
    )
    share_button = (
        f'<div class="share__actions"><button class="btn-sale share-btn" type="button" '
        f'data-share-url="{href}" data-share-title="{esc(share_url)}" '
        f'data-copied="{esc(s["share_copied"])}">{esc(s["share_button"])}</button></div>'
        if href else ""
    )
    return (
        '<section class="share" id="compartir">'
        f'<p class="share__eyebrow">{esc(s["share_eyebrow"])}</p>'
        f'<h2 class="share__title">{s["share_title_html"]}</h2>'
        f'<p class="share__lead">{esc(s["share_lead"])}</p>'
        f'<div class="qrbox"><img src="{esc(qr_src)}" alt="{alt}" width="180" height="180"></div>'
        f'<div>{link_line}</div>{share_button}{vcard_html}{wallet_html}'
        '</section>'
    )


DISCLAIMER_WRAP_STYLE = "margin-top:12px;font-size:.76rem;line-height:1.55;opacity:.85"
DISCLAIMER_ITEM_STYLE = "margin:0 0 4px"


def build_footer(payload: dict, footer_text: str, lang: str) -> str:
    name = esc(payload.get("business_name"))
    privacy_url = f"{DOMAIN}/privacy/" if lang == "en" else f"{DOMAIN}/es/privacidad/"
    s = STRINGS[lang]
    credit = (
        f'<a class="footer__link" href="{DOMAIN}/" target="_blank" rel="noopener">'
        f'{esc(footer_text)}</a>'
        '<span aria-hidden="true"> · </span>'
        f'<a class="footer__link" href="{esc(privacy_url)}" target="_blank" rel="noopener">'
        f'{esc(s["footer_privacy"])}</a>'
    )
    texts = [
        str(item.get(lang, "")).strip()
        for item in (LEGAL.get("disclaimers") or ())
        if str(item.get(lang, "")).strip()
    ]
    notes = ""
    if texts:
        items = "".join(
            f'<p style="{DISCLAIMER_ITEM_STYLE}">{esc(t)}</p>' for t in texts
        )
        notes = f'<div class="footer__disclaimers" style="{DISCLAIMER_WRAP_STYLE}">{items}</div>'
    return (
        '<footer class="footer"><div class="shell">'
        f'<span class="serif">{name}</span>{credit}{notes}'
        '</div></footer>'
    )


def build_og_meta(payload: dict, canonical_url: str, lang: str, first_image: str | None) -> str:
    title = esc(payload.get("business_name"))
    desc = str((payload.get("content", {}).get(lang) or {}).get("short_description") or "").strip()
    if len(desc) > 150:
        desc = desc[:149].rstrip() + "…"
    locale = "es_MX" if lang == "es" else "en_US"
    tags = [
        '<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{title}">',
    ]
    if desc:
        tags.append(f'<meta property="og:description" content="{esc(desc)}">')
    tags.append(f'<meta property="og:url" content="{esc(canonical_url)}">')
    tags.append(f'<meta property="og:locale" content="{locale}">')
    img = safe_href(first_image) if first_image and first_image.lower().startswith(("http://", "https://")) else None
    if img:
        tags.append(f'<meta property="og:image" content="{img}">')
        tags.append('<meta name="twitter:card" content="summary_large_image">')
        tags.append(f'<meta name="twitter:image" content="{img}">')
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    return "\n".join(tags)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_page(
    payload: dict,
    lang: str,
    *,
    head_meta: str,
    lang_switch_html: str,
    footer_text: str,
    share_url: str,
    images: list[dict],
    qr_src: str = QR_ASSET_NAME,
    vcard_src: str = "",
    palettes_alternas: bool = False,
) -> str:
    s = STRINGS[lang]
    brand = payload["brand_style"]
    layout = layout_for_style(brand)
    template_path = TEMPLATES_DIR / f"catalog-{layout}.html"
    if not template_path.exists():
        raise ValidationError(f"No existe el template de layout: {template_path}")
    style_path = STYLES_DIR / f"{brand}.css"
    if not style_path.exists():
        raise ValidationError(f"No existe el estilo para brand_style={brand!r}: {style_path}")

    wallet_url = (
        wallet.build_google_wallet_url(
            payload, share_url, brand=BRAND_NAME,
            # El color de marca del estilo del catalogo, del mismo CSS que ya
            # se inserta en la pagina: sin el, la tarjeta sale gris.
            background=accent_hex(style_path.read_text(encoding="utf-8")),
            etiquetas={"abrir": s["wallet_pass_open"]})
        if block_enabled(BLOCKS, "wallet_google", payload.get("business_type"))
        else ""
    )

    products = payload.get("products", [])
    short_desc = str((payload.get("content", {}).get(lang) or {}).get("short_description") or "").strip()
    hero_desc = f'<p class="hero__desc">{esc(short_desc)}</p>' if short_desc else ""
    js_strings = json.dumps({
        "one": s["catalog_results_count_one"],
        "many": s["catalog_results_count_many"],
        "noResults": s["catalog_no_results"],
    }, ensure_ascii=False)

    tokens = {
        "{{LANG}}": lang,
        "{{HEAD_META}}": FAVICON_LINK + "\n" + head_meta,
        "{{STYLE_NAME}}": esc(brand),
        "{{STYLE_CSS}}": style_path.read_text(encoding="utf-8"),
        "{{PALETTE_ALTS}}": (
            bloque_paletas_alternas(brand) if palettes_alternas else ""
        ),
        "{{PALETTE_SWITCH_JS}}": (
            PALETTE_SWITCH_JS % json.dumps(palettes_hermanas(brand))
            if palettes_alternas and palettes_hermanas(brand)
            else ""
        ),
        "{{PAGE_TITLE}}": esc(f'{payload.get("business_name")} · {s["catalog_title_suffix"]}'),
        "{{SKIP_LINK_BLOCK}}": f'<a class="skip" href="#content">{esc(s["skip_to_content"])}</a>',
        "{{LOGO_BLOCK}}": build_logo(payload),
        "{{BUSINESS_NAME}}": esc(payload.get("business_name")),
        "{{LANG_SWITCH_BLOCK}}": lang_switch_html,
        "{{SALE_CTA_TOPBAR}}": build_sale_cta(payload, s, variant="topbar"),
        "{{HERO_EYEBROW}}": esc(s["catalog_products_eyebrow"]),
        "{{HERO_TITLE}}": hero_title_html(payload.get("business_name")),
        "{{HERO_DESC}}": hero_desc,
        "{{HERO_META}}": hero_meta_html(payload),
        "{{SALE_CTA_HERO}}": build_sale_cta(payload, s, variant="hero"),
        "{{PRODUCTS_COUNT}}": esc(products_count_label(len(products), s)),
        "{{SEARCH_LABEL}}": esc(s["catalog_search_label"]),
        "{{SEARCH_PLACEHOLDER}}": esc(s["catalog_search_placeholder"]),
        "{{FILTER_LABEL}}": esc(s["catalog_products_eyebrow"]),
        "{{FILTER_CHIPS}}": build_filters(products, s, lang),
        "{{PRODUCT_CARDS}}": build_product_cards(payload, s, lang, images),
        "{{NO_RESULTS}}": esc(s["catalog_no_results"]),
        # El boton del vCard reusa `.share__actions` y `.card__cta`, las dos
        # clases que las TRES plantillas de catalogo ya declaran identicas.
        # `display:inline-block` va inline por la misma razon que los
        # disclaimers de abajo: `.card__cta` nacio como item de un flex y aqui
        # no lo es — meter la regla en el CSS moveria los bytes de las 12
        # demos aunque el bloque este apagado.
        "{{SHARE_BLOCK}}": build_share(
            share_url, s, qr_src,
            build_vcard_action(s, vcard_src, css_class="card__cta",
                               extra_style="display:inline-block")
            if vcard_src and vcard_enabled(payload) else "",
            # Las MISMAS dos puertas que en la otra plantilla base: el bloque
            # de la vertical y el interruptor de publicacion de wallet.py,
            # ENCENDIDO desde el 2026-07-30 (Google aprobo el Issuer).
            build_wallet_action(s, wallet_url) if wallet_url else "",
        ),
        "{{FOOTER_BLOCK}}": build_footer(payload, footer_text, lang),
        "{{DOCK_BLOCK}}": build_sale_cta(payload, s, variant="dock"),
        "{{JS_STRINGS}}": js_strings,
    }
    # Una sola pasada (hallazgo #17): ver blocks.fill_tokens.
    return fill_tokens(template_path.read_text(encoding="utf-8"), tokens)


# --------------------------------------------------------------------------- #
# Demo / client builders (bilingual, mirror service-menu structure)
# --------------------------------------------------------------------------- #
def _lang_switch(default_lang: str, alt_lang: str) -> dict:
    return {
        default_lang: (
            f'<div class="lang-switch"><a href="{alt_lang}/" lang="{alt_lang}">'
            f'{STRINGS[default_lang]["lang_switch"]}</a></div>'
        ),
        alt_lang: (
            f'<div class="lang-switch"><a href="../" lang="{default_lang}">'
            f'{STRINGS[alt_lang]["lang_switch"]}</a></div>'
        ),
    }


def _first_http_image(images: list[dict]) -> str | None:
    for im in images:
        url = im.get("es")
        if url and url.lower().startswith(("http://", "https://")):
            return url
    return None


def build_demo(json_path: Path) -> Path:
    """Generate both language pages + one QR for a bilingual catalog demo.

    ES at the slug root, EN in `en/`, language switch on every page, `noindex`
    (demos don't rank), footer "Demo hecho con <brand>".
    """
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    validate_catalog(payload)

    slug = str(payload["public_slug"]).strip()
    default_lang = payload.get("default_language", "es")
    alt_lang = "en" if default_lang == "es" else "es"

    root_url = f"{DEMO_BASE_URL}/{slug}/"
    root_dir = OUTPUT_DIR / slug
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / alt_lang).mkdir(parents=True, exist_ok=True)

    images = resolve_images(payload, root_dir, json_path.parent)
    first_img = _first_http_image(images)
    switch = _lang_switch(default_lang, alt_lang)
    lang_urls = {default_lang: root_url, alt_lang: f"{root_url}{alt_lang}/"}

    for lang in (default_lang, alt_lang):
        qr_src = QR_ASSET_NAME if lang == default_lang else f"../{QR_ASSET_NAME}"
        html = render_page(
            payload, lang,
            head_meta=DEMO_HEAD_META + "\n" + build_og_meta(payload, lang_urls[lang], lang, first_img),
            lang_switch_html=switch[lang],
            footer_text=STRINGS[lang]["footer_demo_credit"],
            share_url=root_url,
            images=images,
            qr_src=qr_src,
            vcard_src=(VCARD_ASSET_NAME if lang == default_lang
                       else f"../{VCARD_ASSET_NAME}"),
            # Solo la demo lleva las otras paletas del layout: es la vitrina, y
            # el selector de la tienda enlaza aqui con ?p=<paleta>. Una pagina
            # de cliente real vendio UNA paleta y no debe traer las otras.
            palettes_alternas=True,
        )
        out_dir = root_dir if lang == default_lang else root_dir / alt_lang
        (out_dir / "index.html").write_text(html, encoding="utf-8")

    (root_dir / QR_ASSET_NAME).write_text(make_qr_svg(root_url), encoding="utf-8")
    # La demo enseña el producto completo: con el bloque encendido lleva su
    # contact.vcf igual que una página de cliente.
    write_vcard(payload, root_dir, root_url)
    return root_dir / "index.html"


def build_client(json_path: Path) -> Path:
    """Generate both language pages + QR (svg+png) for a real catalog client."""
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    validate_catalog(payload)

    slug = str(payload["public_slug"]).strip()
    default_lang = payload["default_language"]
    alt_lang = "en" if default_lang == "es" else "es"

    root_url = f"{CLIENT_BASE_URL}/{slug}/"
    lang_urls = {default_lang: root_url, alt_lang: f"{root_url}{alt_lang}/"}
    root_dir = CLIENT_OUTPUT_DIR / slug
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / alt_lang).mkdir(parents=True, exist_ok=True)

    images = resolve_images(payload, root_dir, json_path.parent)
    first_img = _first_http_image(images)
    switch = _lang_switch(default_lang, alt_lang)

    def head(lang: str) -> str:
        canonical = lang_urls[lang]
        hreflang = (
            f'<link rel="canonical" href="{esc(canonical)}">\n'
            f'<link rel="alternate" hreflang="es" href="{esc(lang_urls.get("es"))}">\n'
            f'<link rel="alternate" hreflang="en" href="{esc(lang_urls.get("en"))}">\n'
            f'<link rel="alternate" hreflang="x-default" href="{esc(root_url)}">'
        )
        return hreflang + "\n" + build_og_meta(payload, canonical, lang, first_img)

    for lang in (default_lang, alt_lang):
        qr_src = QR_ASSET_NAME if lang == default_lang else f"../{QR_ASSET_NAME}"
        html = render_page(
            payload, lang,
            head_meta=head(lang),
            lang_switch_html=switch[lang],
            footer_text=STRINGS[lang]["footer_credit"],
            share_url=root_url,
            images=images,
            qr_src=qr_src,
            vcard_src=(VCARD_ASSET_NAME if lang == default_lang
                       else f"../{VCARD_ASSET_NAME}"),
        )
        out_dir = root_dir if lang == default_lang else root_dir / alt_lang
        (out_dir / "index.html").write_text(html, encoding="utf-8")

    (root_dir / QR_ASSET_NAME).write_text(make_qr_svg(root_url), encoding="utf-8")
    (root_dir / QR_PNG_ASSET_NAME).write_bytes(make_qr_png(root_url))
    write_vcard(payload, root_dir, root_url)
    return root_dir / "index.html"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _client_jsons() -> list[Path]:
    if not CLIENTS_DIR.exists():
        return []
    return sorted(p for p in CLIENTS_DIR.glob("*.json") if not p.name.startswith("_"))


def main(argv: list[str]) -> int:
    if TEMPLATE != "catalog":
        print(
            f"AVISO: LINK_FACTORY_VERTICAL apunta a una vertical cuyo template no es "
            f"'catalog' (es {TEMPLATE!r}). ¿Querías correr generate_service_menu.py?",
            file=sys.stderr,
        )

    demo_paths: list[Path] = []
    client_paths: list[Path] = []
    if argv:
        as_client = False
        for arg in argv:
            if arg == "--client":
                as_client = True
                continue
            (client_paths if as_client else demo_paths).append(Path(arg))
    else:
        demo_paths = sorted(DEMOS_DIR.glob("*.json")) if DEMOS_DIR.exists() else []
        client_paths = _client_jsons()

    if not demo_paths and not client_paths:
        print("No se encontraron payloads JSON de catálogo para generar.", file=sys.stderr)
        return 1

    failures = 0
    for path in demo_paths:
        try:
            out_file = build_demo(path)
            print(f"[ok]   {path.name} -> {out_file.relative_to(REPO_ROOT)} (+ alterno bilingue)")
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            print(f"[fail] {path.name}: {exc}", file=sys.stderr)
    for path in client_paths:
        try:
            out_file = build_client(path)
            print(f"[ok]   {path.name} -> {out_file.relative_to(REPO_ROOT)} (+ alterno bilingue)")
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            print(f"[fail] {path.name}: {exc}", file=sys.stderr)

    total = len(demo_paths) + len(client_paths)
    print(f"\nGeneradas {total - failures}/{total} paginas de catálogo.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
