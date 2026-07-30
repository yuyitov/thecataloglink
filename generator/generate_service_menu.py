#!/usr/bin/env python3
"""Service Menu App - static HTML + QR generator (Phase 2 demos + Phase 5 clients).

Reads `service_menu_payload_public` JSONs, validates the minimum required
fields, escapes all dynamic content, and renders mobile-first static pages
using one of twelve closed brand styles:
black-gold / soft-blush / charcoal-clean / warm-sand / aqua-clean / sage-calm /
electric-slate / terracotta-warm / sunny-paws / midnight-ink /
clarity-editorial / horizon-teal.

Two page kinds:

- **Demos** (`data/demos/*.json` -> `public/demos/<slug>/`): bilingual pages
  (Spanish at the slug root, English in `en/`), `noindex`, footer
  "HMU Link - Demo", with a language switch. Being noindex, they carry no
  canonical/hreflang.
- **Real clients** (`data/clients/*.json` -> `public/links/<slug>/`): bilingual
  pages (Spanish + English). The client's `default_language` renders at the
  root of the slug; the alternate language renders in a subfolder (`en/` or
  `es/`). Both carry static canonical + hreflang links and a language switch.
  One QR per client, at the root, encoding the default-language URL.
  Client JSONs must contain ONLY approved public business data (see
  docs/CLIENT_PUBLIC_DATA_CHECKLIST.md). Files starting with `_` are treated
  as templates and skipped.

Rendering uses a single shared structural template (templates/base.html) plus a
per-style palette file (styles/<brand_style>.css). Colors are never free-form:
only the twelve approved closed styles are accepted.

Scope note. This script still does NOT talk to Stripe, Tally, Cloudflare
Worker/KV or email, does NOT deploy to GitHub Pages, and does NOT download or
process user images. The QR is a static asset (not dynamic, no tracking, no
tokens). The only third-party dependency is `segno` (pure Python, QR -> SVG).

Usage:
    python generator/generate_service_menu.py                 # build all demos + all clients
    python generator/generate_service_menu.py a.json b.json   # build specific demo payloads
    python generator/generate_service_menu.py --client c.json # build specific client payloads
"""

from __future__ import annotations

import html
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

try:
    import segno
except ImportError:  # pragma: no cover - clear guidance if dependency missing
    segno = None

import directory
import wallet
from blocks import block_enabled, fill_tokens
# El botón que el negocio eligió: la tabla de alias es DATO compartido con el
# worker, no una copia local (ver primary_cta.py — tres copias divergidas).
from primary_cta import normalize_primary_cta
from vertical_config import (
    BLOCKS, BRAND_NAME, CATALOGS, DIRECTORY, DOMAIN, LEGAL, SCHEMA,
    STRINGS, STYLES_CATALOG, TEMPLATE_COMMENT_OVERRIDES, TYPOGRAPHY,
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

# Visual styles this vertical may use, from `verticals/<id>/vertical.yaml`
# (`styles.catalog`). Colors are NOT free-form; a payload must pick exactly
# one of these. Each has a matching styles/<name>.css palette.
BRAND_STYLES = STYLES_CATALOG

# Fallback base URL used only if a demo payload omits `public_url`.
# Phase 4E: demos are published on the custom domain (still no secrets and no private links).
DEMO_BASE_URL = f"{DOMAIN}/demos"
# Phase 5: real client pages live under /links/, separate from /demos/.
CLIENT_BASE_URL = f"{DOMAIN}/links"

CLIENT_LANGS = ("es", "en")

# Mini-lookbook (Fase 3.2, ported from ModaLink): at most this many extra
# photos, beyond primary_image_url/gallery_images.
MAX_LOOKBOOK_PHOTOS = 4

# URL schemes we are willing to emit into href attributes.
_ALLOWED_SCHEMES = ("http://", "https://")
PRIMARY_CTA_CHOICES = ("whatsapp", "phone", "booking", "website", "tiktok", "email", "other", "maps")

class ValidationError(ValueError):
    """Raised when a payload is missing or has invalid required fields."""


# --------------------------------------------------------------------------- #
# Escaping / sanitizing helpers
# --------------------------------------------------------------------------- #
def esc(value) -> str:
    """HTML-escape text content and attribute values (quote-safe)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def safe_href(value):
    """Return an escaped http(s) URL, or None if the value is unsafe/empty.

    Only http/https are allowed. Anything else (javascript:, data:, empty)
    is rejected so we never emit an unsafe link into the page.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw.lower().startswith(_ALLOWED_SCHEMES):
        return None
    return html.escape(raw, quote=True)


def whatsapp_href(value):
    """Build a wa.me link from a phone string, keeping only digits."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def tel_href(value):
    """Build a tel: link from a phone string (digits and leading + only)."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    return f"tel:+{digits}"


def mailto_href(value):
    """Build a mailto: link from a plain email address (very light check)."""
    if not value:
        return None
    raw = str(value).strip()
    if "@" not in raw or " " in raw or raw.count("@") != 1:
        return None
    return "mailto:" + html.escape(raw, quote=True)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_SPANISH_MARKER_WORDS = {
    "artesanias",
    "cancela",
    "cancelaciones",
    "conozcan",
    "creando",
    "cultura",
    "descubre",
    "divierten",
    "economia",
    "experiencia",
    "extranjeros",
    "mientras",
    "muertos",
    "politicas",
    "servicios",
}

_SPANISH_MARKER_PHRASES = (
    " al menos ",
    " antes de tu ",
    " dia de muertos",
    " por persona",
    " para que ",
    " tu experiencia",
)


def _plain_latin(value) -> str:
    """Lowercase text with accents removed for lightweight language checks."""
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower()


def _content_text(block: dict) -> str:
    """Collect visible content fields that should match the block language."""
    parts = [
        block.get("short_description", ""),
        block.get("opening_hours_text", ""),
        block.get("service_area_text", ""),
        block.get("client_care_text", ""),
        block.get("reservations_text", ""),
        block.get("class_schedule_text", ""),
        block.get("tour_details_text", ""),
        block.get("pet_notes_text", ""),
    ]
    parts.extend(block.get("service_categories") or [])
    for svc in block.get("services") or []:
        if isinstance(svc, dict):
            parts.extend(
                [
                    svc.get("category", ""),
                    svc.get("name", ""),
                    svc.get("description", ""),
                ]
            )
    parts.extend(block.get("policies") or [])
    for item in block.get("faq") or []:
        if isinstance(item, dict):
            parts.extend([item.get("question", ""), item.get("answer", "")])
    featured = block.get("featured_package")
    if isinstance(featured, dict):
        parts.extend(
            [
                featured.get("name", ""),
                featured.get("description", ""),
                featured.get("price_label", ""),
            ]
        )
    return " ".join(str(part) for part in parts if part)


def _spanish_signal_score(text: str) -> int:
    plain = f" {_plain_latin(text)} "
    score = sum(2 for phrase in _SPANISH_MARKER_PHRASES if phrase in plain)
    words = set(re.findall(r"[a-z]+", plain))
    score += sum(1 for word in _SPANISH_MARKER_WORDS if word in words)
    return score


def validate_language_quality(payload: dict) -> None:
    """Catch obvious untranslated English blocks before publishing."""
    block = payload.get("content", {}).get("en")
    if not isinstance(block, dict):
        return
    score = _spanish_signal_score(_content_text(block))
    if score >= 5:
        raise ValidationError(
            "Cliente: content.en parece contener texto en espanol. "
            "Traduce descripcion, servicios, horarios, politicas y destacado "
            "antes de publicar."
        )


def _validate_lookbook(payload: dict) -> None:
    """lookbook_urls (Fase 3.2, ported from ModaLink) must be a list if present;
    the block itself renders nothing without it, so an absent key is fine."""
    urls = payload.get("lookbook_urls")
    if urls is not None and not isinstance(urls, list):
        raise ValidationError("Cliente: lookbook_urls debe ser una lista.")


# Slug shape guard so the rendering stage is self-defending: the output path
# is built from public_slug, so it must never contain path separators, dots, or
# anything outside a safe slug alphabet — even though the intake builder also
# validates it upstream.
CLIENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")


def catalog_label(name: str, key, lang: str) -> str:
    """Etiqueta bilingue de un valor de catalogo (vertical.yaml -> `catalogs:`).

    Devuelve "" si la vertical no declara ese catalogo o no tiene etiqueta para
    ese valor — imprimir el valor crudo (`servicios_produccion`) seria peor que
    no imprimir nada.
    """
    labels = (CATALOGS.get(name) or {}).get("labels") or {}
    return str((labels.get(lang) or {}).get(key, "") or "")


def _validate_catalog_fields(payload: dict) -> None:
    """Valida los campos del payload contra los `catalogs:` de la vertical.

    Una vertical sin `catalogs:` no ejecuta nada de esto, asi que HMU y
    PawContact validan exactamente igual que antes.
    """
    for name, cfg in CATALOGS.items():
        values = cfg.get("values") or []
        kind = cfg.get("kind", "one")
        required = bool(cfg.get("required"))
        required_when = cfg.get("required_when")
        if required_when and not payload.get(required_when):
            # El campo solo es obligatorio si el otro campo esta encendido
            # (p. ej. directory_state solo importa si directory_opt_in es true).
            # Apagado: ni se exige ni se valida.
            continue
        raw = payload.get(name)
        if kind == "list":
            items = raw or []
            if not isinstance(items, list) or any(item not in values for item in items):
                raise ValidationError(
                    f"Cliente: {name} debe ser una lista subconjunto de {tuple(values)}."
                )
            continue
        value = str(raw or "").strip() if not isinstance(raw, bool) else raw
        if not value and not (required or required_when):
            continue
        if value not in values:
            raise ValidationError(
                f"Cliente: {name} invalido: {raw!r}. Debe ser uno de {tuple(values)}."
            )


def _validate_required_true(payload: dict) -> None:
    """Declaraciones obligatorias de la vertical (`schema.require_true`).

    Son casillas de cumplimiento que el negocio firma en el intake y que NO son
    opcionales: sin ellas no se genera la pagina. ModaLink usa
    `photo_rights_confirmed` (la marca declara que las fotos son suyas).
    """
    for field in SCHEMA.get("require_true") or []:
        if payload.get(field) is not True:
            raise ValidationError(
                f"Cliente: {field} debe ser true (declaracion obligatoria de la vertical)."
            )


def _validate_locations(payload: dict, content: dict) -> None:
    """Cuenta de ubicaciones y horario por sucursal (`schema.locations_*`).

    Con `hours_source: location_hours` cada idioma trae una lista con UN horario
    por ubicacion. Que la longitud tenga que coincidir no es cosmetico: si no
    coincide, los horarios se imprimirian corridos y una sucursal publicaria el
    horario de la otra.
    """
    lo = SCHEMA.get("locations_min") or 0
    hi = SCHEMA.get("locations_max") or 0
    locations = payload.get("locations")
    if lo or hi:
        if not isinstance(locations, list) or len(locations) < lo or (hi and len(locations) > hi):
            rango = f"{lo} a {hi}" if hi else f"al menos {lo}"
            raise ValidationError(
                f"Cliente: locations debe ser una lista de {rango} ubicaciones."
            )
        for i, loc in enumerate(locations):
            if not isinstance(loc, dict) or not str(loc.get("address", "") or "").strip():
                raise ValidationError(f"Cliente: locations[{i}] requiere al menos 'address'.")

    if SCHEMA.get("hours_source") != "location_hours":
        return
    total = len(locations) if isinstance(locations, list) else 0
    for lang in CLIENT_LANGS:
        hours = (content.get(lang) or {}).get("location_hours")
        if not isinstance(hours, list) or len(hours) != total:
            raise ValidationError(
                f"Cliente: content.{lang}.location_hours debe tener exactamente "
                f"{total} elementos (uno por ubicacion)."
            )


# Lo minimo que necesita render_view para producir una pagina coherente desde un
# payload PLANO de una demo monolingue (ver build_demo_monolingual).
FLAT_DEMO_REQUIRED = ("public_slug", "business_name", "short_description", "brand_style")


def validate_client_flat(payload: dict) -> None:
    """Valida una demo monolingue: payload plano, sin objeto `content`.

    Mas laxa que validate_client a proposito. Una demo es una vista previa
    armada con datos publicos de un prospecto, no lo que un cliente entrego: no
    la deben frenar ni las declaraciones obligatorias de la vertical, ni sus
    catalogos cerrados, ni la regla de ubicaciones. Lo que si se exige es lo que
    sin ello daria una pagina rota.
    """
    missing = [f for f in FLAT_DEMO_REQUIRED if not str(payload.get(f, "") or "").strip()]
    if missing:
        raise ValidationError("Demo: faltan campos requeridos: " + ", ".join(missing))
    if payload.get("brand_style") not in BRAND_STYLES:
        raise ValidationError(f"Demo: brand_style invalido: {payload.get('brand_style')!r}.")
    services = payload.get("services")
    if not isinstance(services, list) or not services:
        raise ValidationError("Demo: services debe ser una lista con al menos un servicio.")
    for i, svc in enumerate(services):
        if not isinstance(svc, dict) or not str(svc.get("name", "")).strip():
            raise ValidationError(f"Demo: services[{i}] requiere al menos 'name'.")
    _validate_lookbook(payload)


def validate_client(payload: dict) -> None:
    """Validate a bilingual real-client payload (client_payload_public v1)."""
    slug = str(payload.get("public_slug", "")).strip()
    if not slug:
        raise ValidationError("Cliente: falta public_slug.")
    if not CLIENT_SLUG_RE.match(slug):
        raise ValidationError(f"Cliente: public_slug invalido: {slug!r}.")
    if payload.get("default_language") not in CLIENT_LANGS:
        raise ValidationError("Cliente: default_language debe ser 'es' o 'en'.")
    if payload.get("brand_style") not in BRAND_STYLES:
        raise ValidationError(
            f"Cliente: brand_style invalido: {payload.get('brand_style')!r}."
        )
    if not str(payload.get("business_name", "")).strip():
        raise ValidationError("Cliente: falta business_name.")

    contact_keys = ("whatsapp", "phone", "public_email", "booking_url", "website", "tiktok")
    if not any(str(payload.get(k, "") or "").strip() for k in contact_keys):
        has_public_link = bool(
            payload.get("other_public_link")
            or payload.get("portfolio_link")
            or payload.get("delivery_pickup_links")
        )
        if not has_public_link:
            raise ValidationError(
                "Cliente: se requiere al menos un contacto publico "
                "(whatsapp, phone, public_email, booking_url, website o link publico)."
            )

    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValidationError("Cliente: falta el objeto content con 'es' y 'en'.")
    for lang in CLIENT_LANGS:
        block = content.get(lang)
        if not isinstance(block, dict):
            raise ValidationError(f"Cliente: falta content.{lang}.")
        # `hours_source` (vertical.yaml -> schema:) decide DONDE viven los
        # horarios. Por defecto siguen siendo un `opening_hours_text` unico por
        # idioma — lo que HMU y PawContact publican. Una vertical con horario
        # por sucursal declara "location_hours" y entonces ese campo unico deja
        # de ser obligatorio: lo exige _validate_locations, uno por ubicacion.
        required_fields = ["short_description"]
        if SCHEMA.get("hours_source") == "opening_hours_text":
            required_fields.append("opening_hours_text")
        for field in required_fields:
            if not str(block.get(field, "")).strip():
                raise ValidationError(f"Cliente: falta content.{lang}.{field}.")
        services = block.get("services")
        if not isinstance(services, list) or len(services) == 0:
            raise ValidationError(
                f"Cliente: content.{lang}.services debe tener al menos un servicio."
            )
        for i, svc in enumerate(services):
            if not isinstance(svc, dict) or not str(svc.get("name", "")).strip():
                raise ValidationError(
                    f"Cliente: content.{lang}.services[{i}] requiere al menos 'name'."
                )
    _validate_lookbook(payload)
    _validate_catalog_fields(payload)
    _validate_required_true(payload)
    _validate_locations(payload, content)
    validate_language_quality(payload)


# --------------------------------------------------------------------------- #
# HTML fragment builders (all dynamic values are escaped here)
# --------------------------------------------------------------------------- #
def _initials(business_name: str) -> str:
    parts = [p for p in re.split(r"\s+", business_name.strip()) if p]
    letters = "".join(p[0] for p in parts[:2])
    return esc(letters.upper() or "?")


def build_logo(payload: dict) -> str:
    """Topbar monogram: round logo image, or initials in a hairline circle."""
    href = safe_href(payload.get("logo_url"))
    alt = esc(payload.get("business_name"))
    if href:
        return f'<img class="monogram" src="{href}" alt="{alt}">'
    return f'<div class="monogram" aria-hidden="true">{_initials(str(payload.get("business_name", "")))}</div>'


def build_intro(payload: dict, s: dict) -> str:
    """Entrance curtain: business name words staggered, then the curtain lifts."""
    words = [w for w in re.split(r"\s+", str(payload.get("business_name", "")).strip()) if w]
    if not words:
        return ""
    spans = "".join(f"<span>{esc(w)}</span>" for w in words)
    return (
        '<div class="intro" id="intro" aria-hidden="true">'
        f'<div class="intro__brand">{spans}</div>'
        f'<div class="intro__sub">{s["title_suffix"]}</div></div>'
    )


def build_hero_title(payload: dict) -> str:
    """Giant serif H1: the business name split over up to 3 animated lines,
    middle line in accent italics (the WOW editorial signature)."""
    words = [w for w in re.split(r"\s+", str(payload.get("business_name", "")).strip()) if w]
    if not words:
        return '<h1 id="ht"></h1>'
    n = len(words)
    if n == 1:
        groups = [words]
    elif n == 2:
        groups = [[words[0]], [words[1]]]
    elif n == 3:
        groups = [[words[0]], [words[1]], [words[2]]]
    else:
        third = (n + 2) // 3
        groups = [words[:third], words[third:2 * third], words[2 * third:]]
        groups = [g for g in groups if g]
    em_index = len(groups) // 2  # middle line (or 2nd of 2) gets the italics
    lines = []
    for i, group in enumerate(groups):
        text = esc(" ".join(group))
        if i == em_index and len(groups) > 1:
            text = f"<em>{text}</em>"
        lines.append(f'<span class="line"><span>{text}</span></span>')
    long_cls = " h1--long" if max(len(w) for w in words) >= 10 else ""
    name = esc(str(payload.get("business_name", "")))
    return f'<h1 id="ht" class="hero__title{long_cls}" aria-label="{name}">{"".join(lines)}</h1>'


def _kicker_source_address(payload: dict) -> str:
    """La direccion de la que sale el kicker del heroe.

    Primero la direccion plana, que es la que HMU y PawContact traen siempre
    (`content.<lang>.address`); si no hay, la de la primera ubicacion. El orden
    importa: al reves, un cliente con las dos —posible en HMU— cambiaria de
    kicker. Una vertical que solo captura ubicaciones (ModaLink no tiene campo
    de direccion plana) cae sola en el segundo camino.
    """
    addr = str(payload.get("address", "") or "").strip()
    if addr:
        return addr
    locations = payload.get("locations")
    if isinstance(locations, list) and locations and isinstance(locations[0], dict):
        return str(locations[0].get("address", "") or "").strip()
    return ""


def build_hero_kicker(payload: dict) -> str:
    """Letterspaced kicker over the H1: city · neighborhood from the address."""
    addr = _kicker_source_address(payload)
    if not addr:
        return ""
    parts = []
    for part in addr.split(","):
        cleaned = re.sub(r"\b(?:c\.?\s*p\.?|cp|zip)\s*[:#-]?\s*", "", part, flags=re.I)
        cleaned = re.sub(r"\b\d{4,6}\b", "", cleaned).strip(" .,-")
        if cleaned:
            parts.append(cleaned)
    if len(parts) >= 2:
        text = f"{parts[-1]} · {parts[-2]}"
    else:
        text = parts[0] if parts else ""
    return f'<p class="hero__kicker" id="hk">{esc(text)}</p>' if text else ""


def _contact_options(payload: dict, s: dict) -> dict:
    """Available public contact links keyed by CTA kind."""
    options = {}
    wa = whatsapp_href(payload.get("whatsapp"))
    if wa:
        options["whatsapp"] = (esc(wa), s["btn_whatsapp"])
    tel = tel_href(payload.get("phone"))
    if tel:
        options["phone"] = (tel, s["btn_phone"])
    booking = safe_href(payload.get("booking_url"))
    if booking:
        options["booking"] = (booking, s["btn_booking"])
    web = safe_href(payload.get("website"))
    if web:
        options["website"] = (web, s["btn_website"])
    tik = safe_href(payload.get("tiktok"))
    if tik:
        options["tiktok"] = (tik, "TikTok")
    ig = safe_href(payload.get("instagram"))
    if ig:
        options["instagram"] = (ig, "Instagram")
    fb = safe_href(payload.get("facebook"))
    if fb:
        options["facebook"] = (fb, "Facebook")
    mail = mailto_href(payload.get("public_email"))
    if mail:
        options["email"] = (mail, s["btn_email"])
    other = payload.get("other_public_link")
    if isinstance(other, dict):
        href = safe_href(other.get("url"))
        label = str(other.get("label", "") or "").strip() or s["btn_other"]
        if href:
            options["other"] = (href, esc(label))
    first_map = _first_location_map_href(payload)
    if first_map:
        options["maps"] = (first_map, s["btn_maps"])
    return options


def _primary_contact(payload: dict, s: dict):
    """Preferred public contact becomes the primary CTA (hero + fixed dock)."""
    options = _contact_options(payload, s)
    preferred = normalize_primary_cta(payload.get("primary_cta"))
    if preferred in options:
        href, label = options[preferred]
        return preferred, href, label
    for kind in PRIMARY_CTA_CHOICES:
        if kind in options:
            href, label = options[kind]
            return kind, href, label
    return None, None, None


def _all_locations(payload: dict) -> list:
    """Return every location-like entry with at least one public detail."""
    raw_locations = payload.get("locations")
    locations = []
    if isinstance(raw_locations, list):
        source = raw_locations
    else:
        source = [
            {
                "name": payload.get("location_name"),
                "address": payload.get("address"),
                "google_maps_url": payload.get("google_maps_url"),
            }
        ]

    for loc in source:
        if not isinstance(loc, dict):
            continue
        item = {
            "name": str(loc.get("name", "") or "").strip(),
            "address": str(loc.get("address", "") or "").strip(),
            "google_maps_url": loc.get("google_maps_url"),
            "notes": str(loc.get("notes", "") or "").strip(),
        }
        # El horario de ESTA sucursal solo viaja si la ubicacion lo trae. La
        # clave se copia tal cual (presente-pero-vacia sigue siendo presente):
        # es la senal que lee _has_per_location_hours.
        if "hours_text" in loc:
            item["hours_text"] = str(loc.get("hours_text", "") or "").strip()
        if item["name"] or item["address"] or safe_href(item["google_maps_url"]):
            locations.append(item)

    if not locations:
        maps = payload.get("google_maps_url")
        addr = str(payload.get("address", "") or "").strip()
        if addr or safe_href(maps):
            locations.append({"name": "", "address": addr, "google_maps_url": maps, "notes": ""})
    return locations


def _first_location_map_href(payload: dict):
    for loc in _all_locations(payload):
        href = safe_href(loc.get("google_maps_url"))
        if href:
            return href
    return None


def _location_map_links(payload: dict, s: dict) -> list:
    """Google Maps buttons, one per location that has a map URL."""
    candidates = []
    seen = set()
    for i, loc in enumerate(_all_locations(payload), start=1):
        href = safe_href(loc.get("google_maps_url"))
        if not href or href in seen:
            continue
        seen.add(href)
        name = str(loc.get("name", "") or "").strip()
        fallback = f'{s["location_label"]} {i}'
        candidates.append((href, name or fallback))

    if not candidates:
        return []
    if len(candidates) == 1:
        href, _ = candidates[0]
        return [(href, "Google Maps")]
    return [(href, f"Google Maps - {esc(label)}") for href, label in candidates]


def _block_enabled(payload: dict, block_id: str) -> bool:
    """Gate a conditional block by the payload's business_type.

    The registry (engine defaults + vertical.yaml `blocks:` overrides) lives
    in blocks.py / vertical_config.BLOCKS.
    """
    return block_enabled(BLOCKS, block_id, payload.get("business_type"))


def _special_public_links(payload: dict, s: dict) -> list:
    links = []
    if _block_enabled(payload, "delivery_pickup"):
        for item in payload.get("delivery_pickup_links") or []:
            if not isinstance(item, dict):
                continue
            href = safe_href(item.get("url"))
            label = str(item.get("label", "") or "").strip() or s["delivery_pickup_label"]
            if href:
                links.append((href, esc(label)))

    portfolio = payload.get("portfolio_link")
    if isinstance(portfolio, dict) and _block_enabled(payload, "portfolio"):
        href = safe_href(portfolio.get("url"))
        label = str(portfolio.get("label", "") or "").strip() or s["portfolio_label"]
        if href:
            links.append((href, esc(label)))
    return links


def _secondary_links(payload: dict, s: dict, primary_kind) -> list:
    """(href, label) for every public link that is not the primary CTA."""
    links = []
    contact_options = _contact_options(payload, s)
    for kind in PRIMARY_CTA_CHOICES:
        if kind == primary_kind or kind == "maps":
            continue
        option = contact_options.get(kind)
        if option:
            links.append(option)
    for key, label in (
        ("instagram", "Instagram"),
        ("facebook", "Facebook"),
        ("tiktok", "TikTok"),
        # Pinterest venia de ModaLink y es puro data-gating: ningun payload de
        # HMU ni de PawContact tiene el campo, asi que sumarlo no mueve un byte
        # de los suyos y el motor gana una red social mas para todos.
        ("pinterest", "Pinterest"),
    ):
        if key == primary_kind:
            continue
        href = safe_href(payload.get(key))
        if href:
            links.append((href, label))
    links.extend(_location_map_links(payload, s))
    reviews = safe_href(payload.get("google_reviews_url"))
    if reviews:
        links.append((reviews, "Google Reviews"))
    links.extend(_special_public_links(payload, s))

    deduped = []
    seen = set()
    for href, label in links:
        key = str(href).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((href, label))
    return deduped


def build_cta_row(payload: dict, s: dict) -> str:
    """Hero CTA row: solid primary contact + ghost link to the menu."""
    _, href, label = _primary_contact(payload, s)
    buttons = []
    if href:
        buttons.append(
            f'<a class="btn btn--solid" href="{href}" target="_blank" '
            f'rel="noopener noreferrer">{label}</a>'
        )
    buttons.append(f'<a class="btn btn--ghost" href="#menu">{s["view_menu"]}</a>')
    return f'<div class="cta-row" id="hc">{"".join(buttons)}</div>'


def build_dock(payload: dict, s: dict) -> str:
    """Fixed bottom booking pill; appears once the visitor scrolls past the hero."""
    _, href, label = _primary_contact(payload, s)
    if not href:
        return ""
    return (
        f'<div class="dock" id="dock"><a href="{href}" target="_blank" '
        f'rel="noopener noreferrer">{label}</a></div>'
    )


def _gallery_images(payload: dict) -> list[str]:
    """Fotos de la banda/carrusel del heroe.

    De donde salen lo decide la vertical (`schema.gallery_source`): por defecto
    `gallery_images` (hasta 6, lo que HMU y PawContact publican). Una vertical
    puede mandar ahi sus `lookbook_urls` en vez de renderizarlas como seccion
    aparte — es lo que hace ModaLink, que apaga `blocks.lookbook` y manda esas
    fotos al carrusel. El tope entonces es el del lookbook (4) mas la principal.
    """
    if SCHEMA.get("gallery_source") == "lookbook_urls":
        source_key, cap = "lookbook_urls", MAX_LOOKBOOK_PHOTOS
    else:
        source_key, cap = "gallery_images", 6
    images = []
    raw = payload.get(source_key)
    if isinstance(raw, list):
        for item in raw:
            url = item.get("url") if isinstance(item, dict) else item
            href = safe_href(url)
            if href and href not in images:
                images.append(href)
            if len(images) >= cap:
                break
    primary = safe_href(payload.get("primary_image_url"))
    if primary and primary not in images:
        images.insert(0, primary)
    return images[: cap + 1 if source_key == "lookbook_urls" else cap]


def build_hero_image(payload: dict, s: dict) -> str:
    """Optional full-width photo band under the hero; becomes a carousel at 2+ photos."""
    images = _gallery_images(payload)
    if not images:
        return ""
    alt = esc(payload.get("business_name"))
    if len(images) == 1:
        return (
            '<div class="shell figure" data-reveal>'
            '<div class="figure__viewport">'
            f'<img class="figure__media" src="{images[0]}" alt="{alt}" loading="lazy">'
            '</div></div>'
        )

    slides = []
    dots = []
    for i, href in enumerate(images):
        load = "eager" if i == 0 else "lazy"
        slides.append(
            '<div class="figure__slide">'
            f'<img class="figure__media" src="{href}" alt="{alt}" loading="{load}">'
            '</div>'
        )
        active = " is-active" if i == 0 else ""
        dots.append(
            f'<button class="gallery-dot{active}" type="button" '
            f'aria-label="{esc(s["gallery_photo"])} {i + 1}"></button>'
        )
    return (
        '<div class="shell figure figure--carousel" data-reveal data-gallery>'
        '<div class="figure__viewport">'
        '<div class="figure__track">'
        f'{"".join(slides)}'
        '</div>'
        f'<button class="gallery-btn gallery-btn--prev" type="button" aria-label="{esc(s["gallery_prev"])}">‹</button>'
        f'<button class="gallery-btn gallery-btn--next" type="button" aria-label="{esc(s["gallery_next"])}">›</button>'
        '</div>'
        f'<div class="gallery-dots">{"".join(dots)}</div>'
        '</div>'
    )


def build_marquee(payload: dict) -> str:
    """Infinite marquee strip built from service categories (or service names)."""
    items = [str(c).strip() for c in (payload.get("service_categories") or []) if str(c).strip()]
    if not items:
        items = [
            str(svc.get("name", "")).strip()
            for svc in (payload.get("services") or [])
            if str(svc.get("name", "")).strip()
        ][:6]
    if not items:
        return ""
    parts = []
    for i, item in enumerate(items):
        text = esc(item)
        if i % 2 == 1:
            text = f"<em>{text}</em>"
        parts.append(f'<span>{text} <span class="dot">✦</span></span>')
    base_repeat = max(4, 12 // len(parts))
    track = "".join(parts * base_repeat) * 2  # duplicated for the seamless CSS loop
    return (
        '<div class="marquee" aria-hidden="true">'
        f'<div class="marquee__track">{track}</div></div>'
    )


# CSS del mini-lookbook. Vive aqui y no en base.html porque se emite SOLO si la
# pagina de verdad lleva lookbook (gate HMU 2026-07-25): estas 4 lineas eran CSS
# muerto en toda pagina de HMU/PawContact —ninguna vertical usa `lookbook_urls`—
# y eran la unica divergencia entre el motor y el repo standalone de HMU. El
# token va PEGADO al final de la linea anterior en la plantilla (no en una linea
# propia) para que, cuando esta vacio, no deje ni un salto de linea de sobra:
# esa es la diferencia entre un diff de 6 lineas y un diff de cero.
LOOKBOOK_CSS = (
    "\n\n/* ---------- mini-lookbook (bloque opcional, Fase 3.2) ---------- */"
    "\n.lookbook{padding-top:0}"
    "\n.lookbook__grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-top:22px}"
    "\n.lookbook__img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:18px;"
    "border:1px solid var(--hair)}"
)


def build_lookbook(payload: dict, s: dict) -> str:
    """Optional mini-lookbook: 1-4 extra photos, gated by the `lookbook`
    conditional block (Fase 3.2, ported from ModaLink). Purely data-driven —
    an absent/empty `lookbook_urls` renders nothing, same philosophy as
    build_hero_image (an image problem must never block generation)."""
    if not _block_enabled(payload, "lookbook"):
        return ""
    urls = payload.get("lookbook_urls") or []
    images = [safe_href(u) for u in urls[:MAX_LOOKBOOK_PHOTOS]]
    images = [u for u in images if u]
    if not images:
        return ""
    alt = esc(payload.get("business_name"))
    imgs = "".join(
        f'<img class="lookbook__img" src="{u}" alt="{alt}" loading="lazy">' for u in images
    )
    return (
        '<section class="section lookbook" data-theme="base"><div class="shell">'
        f'<p class="eyebrow" data-reveal>{s["lookbook_eyebrow"]}</p>'
        f'<h2 data-reveal>{s["lookbook_title_html"]}</h2>'
        f'<div class="lookbook__grid" data-reveal>{imgs}</div>'
        "</div></section>"
    )


def build_services(payload: dict, s: dict) -> str:
    """Editorial price list: italic serif category titles + dotted-leader rows."""
    services = payload.get("services") or []
    declared = payload.get("service_categories") or []
    # La leyenda "Consultar"/"Ask us" de un servicio SIN precio es una decision
    # de copy de la casa, no del negocio: por eso se apaga por vertical
    # (`blocks.price_ask_legend`) y no por payload. Encendida (el default) el
    # comportamiento es identico al de siempre.
    ask_legend = _block_enabled(payload, "price_ask_legend")

    # Preserve declared category order, then append any leftover categories.
    order = list(declared)
    for svc in services:
        cat = svc.get("category")
        if cat and cat not in order:
            order.append(cat)

    def render_service(svc: dict) -> str:
        name = esc(svc.get("name"))
        desc = svc.get("description")
        price = svc.get("price_label")
        left = f'<span class="mrow__name">{name}</span>'
        if desc:
            left += f'<span class="mrow__desc">{esc(desc)}</span>'
        # Con precio se muestra. Sin precio, la leyenda "Consultar"/"Ask us"
        # depende solo de la opción de la vertical.
        if price:
            price_text = esc(price)
        elif ask_legend:
            price_text = esc(s["price_ask"])
        else:
            price_text = ""
        price_html = (
            f'<span class="mrow__dots"></span><span class="mrow__price">{price_text}</span>'
            if price_text
            else ""
        )
        return f'<div class="mrow"><div>{left}</div>{price_html}</div>'

    def render_category(title: str, items: list) -> str:
        rows = "".join(render_service(item) for item in items)
        return (
            '<div class="menu-cat" data-reveal>'
            f'<div class="menu-cat__head"><h3 class="menu-cat__title">{title}</h3>'
            '<span class="menu-cat__rule"></span></div>'
            f'{rows}</div>'
        )

    blocks = []
    used = set()
    for cat in order:
        items = [svc for svc in services if svc.get("category") == cat]
        if not items:
            continue
        for item in items:
            used.add(id(item))
        blocks.append(render_category(esc(cat), items))

    # Services without a (matching) category go under a generic group.
    leftovers = [svc for svc in services if id(svc) not in used]
    if leftovers:
        blocks.append(render_category(s["services_fallback"], leftovers))

    return (
        '<section class="section" id="menu" data-theme="base"><div class="shell">'
        f'<p class="eyebrow" data-reveal>{s["services_eyebrow"]}</p>'
        f'<h2 data-reveal>{s["menu_title_html"]}</h2>'
        + "".join(blocks)
        + "</div></section>"
    )


def build_featured(payload: dict, s: dict) -> str:
    """Featured package as the glowing "signature" card (spinning accent border)."""
    pkg = payload.get("featured_package")
    if not isinstance(pkg, dict) or not str(pkg.get("name", "")).strip():
        return ""
    name = esc(pkg.get("name"))
    desc = esc(pkg.get("description"))
    price = pkg.get("price_label")
    price_html = f'<div class="ritual__price">{esc(price)}</div>' if price else ""
    # Sin descripción (o igual al nombre) no se repite el texto.
    desc_html = f'<p class="ritual__desc">{desc}</p>' if desc and desc != name else ""
    return (
        '<section class="section section--featured" data-theme="alt"><div class="shell">'
        '<div class="ritual" data-reveal><div class="ritual__in">'
        f'<span class="ritual__badge">{s["featured_badge"]}</span>'
        f'<h3 class="ritual__name serif">{name}</h3>'
        f'{desc_html}{price_html}'
        "</div></div></div></section>"
    )


def _address_map_link(maps_url, s: dict) -> str:
    maps = safe_href(maps_url)
    if not maps:
        return ""
    return (
        f'<a class="mapl" href="{maps}" target="_blank" '
        f'rel="noopener noreferrer">{s["address_map"]}</a>'
    )


def _location_block(loc: dict, s: dict, with_name: bool) -> str:
    """Una ubicacion con su horario propio: direccion + horario + notas + mapa.

    Solo se usa cuando el payload trae horario POR SUCURSAL (ver
    `_address_row_body`): es la forma que necesita una vertical con
    `hours_source: location_hours`.
    """
    addr = str(loc.get("address", "") or "").strip()
    link = _address_map_link(loc.get("google_maps_url"), s)
    if not (addr or link):
        return ""
    name = str(loc.get("name", "") or "").strip()
    name_html = f'<h4 class="address__name">{esc(name)}</h4>' if (with_name and name) else ""
    hours = str(loc.get("hours_text", "") or "").strip()
    hours_html = f"<p>{esc(hours)}</p>" if hours else ""
    notes = str(loc.get("notes", "") or "").strip()
    notes_html = f'<p class="address__notes">{esc(notes)}</p>' if notes else ""
    main = esc(addr) if addr else link
    tail = "<br>" + link if addr and link else ""
    return f'<div class="address__item">{name_html}<p>{main}{tail}</p>{hours_html}{notes_html}</div>'


def _address_row_body(payload: dict, s: dict) -> str:
    """Inner HTML for the address info-row ('' if the payload has no address).

    La forma la decide la VERTICAL, no el payload: una vertical con
    `hours_source: location_hours` imprime SIEMPRE un bloque por sucursal (con
    su horario dentro), tambien en una demo de una sola direccion — si
    dependiera del payload, la misma vertical publicaria dos maquetados
    distintos segun el dato, que es justo lo que la comparacion byte a byte
    encontro.
    """
    if SCHEMA.get("hours_source") == "location_hours":
        locations = _all_locations(payload)
        if not locations:
            return ""
        with_name = len(locations) > 1
        return "".join(_location_block(loc, s, with_name) for loc in locations)

    locations = _all_locations(payload)
    if len(locations) > 1:
        items = []
        for loc in locations:
            addr = str(loc.get("address", "") or "").strip()
            link = _address_map_link(loc.get("google_maps_url"), s)
            if not (addr or link):
                continue
            name = str(loc.get("name", "") or "").strip()
            name_html = f'<h4 class="address__name">{esc(name)}</h4>' if name else ""
            notes = str(loc.get("notes", "") or "").strip()
            notes_html = f'<p class="address__notes">{esc(notes)}</p>' if notes else ""
            main = esc(addr) if addr else link
            items.append(
                f'<div class="address__item">{name_html}'
                f'<p>{main}{"<br>" + link if addr and link else ""}</p>{notes_html}</div>'
            )
        return "".join(items)

    loc = locations[0] if locations else {}
    addr = str(loc.get("address", "") or "").strip()
    link = _address_map_link(loc.get("google_maps_url"), s)
    if not (addr or link):
        return ""
    if addr:
        return f'<p>{esc(addr)}{"<br>" + link if link else ""}</p>'
    return f"<p>{link}</p>"


# CSS que separa parrafos apilados dentro de una misma fila de info. Se emite
# junto con las filas que APILAN parrafos (las de abajo) y solo con ellas —
# mismo criterio que LOOKBOOK_CSS: el bloque y su CSS van juntos o no va
# ninguno. Sin esto, una vertical sin filas apiladas cargaria una regla muerta y
# los bytes de HMU/PawContact cambiarian.
INFO_STACK_CSS = "\n.info-row p+p{margin-top:8px}"


def _brand_row(payload: dict, s: dict, lang: str) -> str:
    """"Sobre la marca": especialidad + anios de experiencia + formacion.

    Data-gated: sin `specialty`/`years_experience`/`training_text` no imprime
    nada. Ninguno de los tres puede salir del intake del motor (verificado
    campo por campo contra build_client_from_intake.py y worker.js), asi que
    para HMU y PawContact esta fila no existe.
    """
    specialty = payload.get("specialty")
    specialty_other = str(payload.get("specialty_other", "") or "").strip()
    label = catalog_label("specialty", specialty, lang)
    if specialty_other and not label:
        # El valor "otra" del catalogo no tiene etiqueta propia: la escribe el
        # negocio en su campo libre.
        label = specialty_other
    lines = []
    if label:
        lines.append(f"<p>{esc(label)}</p>")
    years = payload.get("years_experience")
    if years:
        lines.append(f'<p>{esc(years)} {s["years_experience"]}</p>')
    training = str(payload.get("training_text", "") or "").strip()
    if training:
        lines.append(f"<p>{esc(training)}</p>")
    if not lines:
        return ""
    return (
        f'<div class="info-row" data-reveal><h3>{s["brand_title"]}</h3>'
        f'{"".join(lines)}</div>'
    )


def _practice_rows(payload: dict, s: dict) -> list:
    """"Como trabajamos" + tiempos de entrega + politica de anticipo.

    Las tres son data-gated por sus propios campos (`appointment_mode`,
    `home_service`, `delivery_time_text`, `deposit_policy_text`).
    """
    rows = []
    mode = str(payload.get("appointment_mode", "") or "").strip()
    work_lines = []
    if mode and f"appointment_{mode}" in s:
        work_lines.append(f'<p>{s[f"appointment_{mode}"]}</p>')
    if payload.get("home_service"):
        work_lines.append(f'<p>{s["home_service_label"]}</p>')
    if work_lines:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["how_we_work_title"]}</h3>'
            f'{"".join(work_lines)}</div>'
        )

    delivery = str(payload.get("delivery_time_text", "") or "").strip()
    if delivery:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["delivery_time_title"]}</h3>'
            f'<p>{esc(delivery)}</p></div>'
        )

    deposit = str(payload.get("deposit_policy_text", "") or "").strip()
    if deposit:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["deposit_policy_title"]}</h3>'
            f'<p>{esc(deposit)}</p></div>'
        )
    return rows


def _payment_row(payload: dict, s: dict, lang: str) -> str:
    """"Formas de pago" (+ factura disponible). Data-gated por
    `payment_methods`/`invoicing`; las etiquetas salen del catalogo de la
    vertical (vertical.yaml -> catalogs.payment_methods.labels)."""
    methods = payload.get("payment_methods") or []
    labels = [catalog_label("payment_methods", m, lang) or str(m) for m in methods if m]
    lines = []
    if labels:
        lines.append(f'<p>{esc(", ".join(labels))}</p>')
    if payload.get("invoicing"):
        lines.append(f'<p>{s["invoicing_yes"]}</p>')
    if not lines:
        return ""
    return (
        f'<div class="info-row" data-reveal><h3>{s["payment_title"]}</h3>'
        f'{"".join(lines)}</div>'
    )


def _list_values(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _credentials_row(payload: dict, s: dict) -> str:
    if not _block_enabled(payload, "credentials"):
        return ""
    nested = payload.get("credentials")
    nested = nested if isinstance(nested, dict) else {}
    country = str(payload.get("country", "") or "").strip().lower()
    pairs = []
    if country in ("mx", "mexico", "méxico"):
        pairs = [
            (s["credential_institution"], nested.get("institution") or payload.get("credentials_institution")),
            (s["credential_cedula"], nested.get("cedula_profesional") or payload.get("cedula_profesional")),
            (
                s["credential_specialty_license"],
                nested.get("cedula_especialidad") or payload.get("cedula_especialidad"),
            ),
        ]
    elif country in ("us", "usa", "united states", "estados unidos"):
        state = nested.get("state") or payload.get("license_state")
        number = nested.get("license_number") or payload.get("license_number")
        license_value = " · ".join(
            str(value).strip() for value in (state, number) if str(value or "").strip()
        )
        pairs = [
            (s["credential_license"], license_value),
            (s["credential_npi"], nested.get("npi") or payload.get("npi")),
            (s["credential_board"], nested.get("board_name") or payload.get("board_name")),
        ]
    lines = [
        f"<p><strong>{esc(label)}:</strong> {esc(value)}</p>"
        for label, value in pairs
        if str(value or "").strip()
    ]
    if not lines:
        return ""
    return (
        f'<div class="info-row" data-reveal><h3>{s["credentials_title"]}</h3>'
        f'{"".join(lines)}</div>'
    )


def _languages_row(payload: dict, s: dict) -> str:
    if not _block_enabled(payload, "languages"):
        return ""
    values = _list_values(payload.get("languages"))
    if not values:
        return ""
    return (
        f'<div class="info-row" data-reveal><h3>{s["languages_title"]}</h3>'
        f'<p>{esc(", ".join(values))}</p></div>'
    )


def _telemedicine_row(payload: dict, s: dict) -> str:
    if not _block_enabled(payload, "telemedicine"):
        return ""
    nested = payload.get("telemedicine")
    nested = nested if isinstance(nested, dict) else {}
    offered = nested.get("offered", payload.get("telemedicine_offered"))
    if not offered:
        return ""
    modality = str(
        nested.get("modality") or payload.get("telemedicine_modality") or ""
    ).strip().lower()
    label = s.get(f"telemedicine_{modality}", s["telemedicine_yes"])
    return (
        f'<div class="info-row" data-reveal><h3>{s["telemedicine_title"]}</h3>'
        f'<p>{esc(label)}</p></div>'
    )


def _health_billing_row(payload: dict, s: dict, lang: str) -> str:
    if not _block_enabled(payload, "health_billing"):
        return ""
    insurance = _list_values(payload.get("insurance"))
    lines = []
    if insurance:
        lines.append(
            f'<p><strong>{s["insurance_accepted"]}:</strong> '
            f'{esc(", ".join(insurance))}</p>'
        )
    else:
        lines.append(f'<p>{s["insurance_direct"]}</p>')
    methods = payload.get("payment_methods") or []
    labels = [catalog_label("payment_methods", item, lang) or str(item) for item in methods if item]
    if labels:
        lines.append(f'<p>{esc(", ".join(labels))}</p>')
    if payload.get("invoicing"):
        lines.append(f'<p>{s["invoicing_yes"]}</p>')
    return (
        f'<div class="info-row" data-reveal><h3>{s["insurance_title"]}</h3>'
        f'{"".join(lines)}</div>'
    )


def _appointment_policy_rows(payload: dict, s: dict) -> list[str]:
    if not _block_enabled(payload, "appointment_policies"):
        return []
    rows = []
    for field, title in (
        ("appointment_policy_text", s["appointment_policy_title"]),
        ("emergency_policy_text", s["emergency_policy_title"]),
    ):
        value = str(payload.get(field, "") or "").strip()
        if value:
            rows.append(
                f'<div class="info-row" data-reveal><h3>{title}</h3>'
                f'<p>{esc(value)}</p></div>'
            )
    return rows


def _provider_privacy_row(payload: dict, s: dict) -> str:
    if not _block_enabled(payload, "provider_privacy"):
        return ""
    url = safe_href(payload.get("privacy_notice_url"))
    if not url:
        return ""
    return (
        f'<div class="info-row" data-reveal><h3>{s["provider_privacy_title"]}</h3>'
        f'<p><a class="mapl" href="{url}" target="_blank" rel="noopener noreferrer">'
        f'{s["provider_privacy_link"]}</a></p></div>'
    )


def _has_stacked_info_rows(payload: dict, s: dict, lang: str) -> bool:
    """True si esta pagina lleva alguna de las filas que apilan parrafos.

    Es lo que decide si se emite INFO_STACK_CSS. Se pregunta por las filas ya
    construidas y no por los campos sueltos para que la regla no pueda quedar
    desalineada del render.
    """
    return bool(
        _brand_row(payload, s, lang)
        or _practice_rows(payload, s)
        or _payment_row(payload, s, lang)
        or _credentials_row(payload, s)
        or _languages_row(payload, s)
        or _telemedicine_row(payload, s)
        or _health_billing_row(payload, s, lang)
        or _appointment_policy_rows(payload, s)
    )


def build_info(payload: dict, s: dict, lang: str) -> str:
    """"Find us" section (alt theme): hours, address, policies and extra links
    as hairline info rows — this is where the background fusion happens."""
    rows = []

    brand_row = _brand_row(payload, s, lang)
    if brand_row:
        rows.append(brand_row)

    for medical_row in (
        _credentials_row(payload, s),
        _languages_row(payload, s),
        _telemedicine_row(payload, s),
    ):
        if medical_row:
            rows.append(medical_row)

    hours = str(payload.get("opening_hours_text", "") or "").strip()
    if hours:
        # Un renglon por linea del payload (p. ej. un dia por renglon).
        hours_html = "<br>".join(
            esc(line.strip()) for line in hours.splitlines() if line.strip()
        )
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["hours_title"]}</h3>'
            f'<p>{hours_html}</p></div>'
        )

    rows.extend(_practice_rows(payload, s))

    service_area = str(payload.get("service_area_text", "") or "").strip()
    if service_area:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["service_area_title"]}</h3>'
            f'<p>{esc(service_area)}</p></div>'
        )

    client_care = str(payload.get("client_care_text", "") or "").strip()
    if client_care:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["client_care_title"]}</h3>'
            f'<p>{esc(client_care)}</p></div>'
        )

    reservations = str(payload.get("reservations_text", "") or "").strip()
    if reservations:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["reservations_title"]}</h3>'
            f'<p>{esc(reservations)}</p></div>'
        )

    class_schedule = str(payload.get("class_schedule_text", "") or "").strip()
    if class_schedule:
        sched_html = "<br>".join(esc(l.strip()) for l in class_schedule.splitlines() if l.strip())
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["class_schedule_title"]}</h3>'
            f'<p>{sched_html}</p></div>'
        )

    tour_details = str(payload.get("tour_details_text", "") or "").strip()
    if tour_details:
        tour_html = "<br>".join(esc(l.strip()) for l in tour_details.splitlines() if l.strip())
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["tour_details_title"]}</h3>'
            f'<p>{tour_html}</p></div>'
        )

    pet_notes = str(payload.get("pet_notes_text", "") or "").strip()
    if pet_notes:
        pet_html = "<br>".join(esc(l.strip()) for l in pet_notes.splitlines() if l.strip())
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["pet_notes_title"]}</h3>'
            f'<p>{pet_html}</p></div>'
        )

    address_body = _address_row_body(payload, s)
    if address_body:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["address_title"]}</h3>'
            f'{address_body}</div>'
        )

    payment_row = (
        _health_billing_row(payload, s, lang)
        if _block_enabled(payload, "health_billing")
        else _payment_row(payload, s, lang)
    )
    if payment_row:
        rows.append(payment_row)

    rows.extend(_appointment_policy_rows(payload, s))

    policies = payload.get("policies") or []
    items = [f"<li>{esc(p)}</li>" for p in policies if str(p).strip()]
    if items:
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["policies_title"]}</h3>'
            f'<ul>{"".join(items)}</ul></div>'
        )

    privacy_row = _provider_privacy_row(payload, s)
    if privacy_row:
        rows.append(privacy_row)

    primary_kind, _, _ = _primary_contact(payload, s)
    links = _secondary_links(payload, s, primary_kind)
    if links:
        pills = "".join(
            f'<a class="btn btn--ghost btn--sm" href="{href}" target="_blank" '
            f'rel="noopener noreferrer">{label}</a>'
            for href, label in links
        )
        rows.append(
            f'<div class="info-row" data-reveal><h3>{s["links_title"]}</h3>'
            f'<div class="cta-row">{pills}</div></div>'
        )

    if not rows:
        return ""
    return (
        '<section class="section" data-theme="alt"><div class="shell">'
        f'<p class="eyebrow" data-reveal>{s["visit_eyebrow"]}</p>'
        f'<h2 data-reveal>{s["visit_title_html"]}</h2>'
        f'<div class="info-grid">{"".join(rows)}</div>'
        "</div></section>"
    )


# CSS de la seccion de preguntas frecuentes. Sale de base.html al motor por la
# misma razon que LOOKBOOK_CSS: una vertical que apaga `blocks.faq` no debe
# cargar CSS de una seccion que nunca imprime. Con el bloque encendido (el
# default) se emite SIEMPRE, igual que antes, asi que HMU y PawContact no mueven
# un byte — sus demos no tienen FAQ y aun asi llevan este CSS hoy.
FAQ_CSS = (
    "\n\n/* ---------- preguntas frecuentes ---------- */"
    "\n.faq-list{margin-top:22px;border-top:1px solid var(--hair)}"
    "\n.faq-item{padding:22px 0;border-bottom:1px solid var(--hair)}"
    "\n.faq-item h3{font-family:var(--sans);font-size:15.5px;font-weight:500;"
    "letter-spacing:.02em;margin:0 0 8px}"
    "\n.faq-item p{margin:0;color:var(--soft);font-size:15.5px}"
)


def build_faq(payload: dict, s: dict) -> str:
    """Simple FAQ section for conversion-critical practical questions."""
    if not _block_enabled(payload, "faq"):
        return ""
    faq = payload.get("faq") or []
    items = []
    for item in faq:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "") or "").strip()
        answer = str(item.get("answer", "") or "").strip()
        if not question or not answer:
            continue
        items.append(
            '<div class="faq-item" data-reveal>'
            f'<h3>{esc(question)}</h3><p>{esc(answer)}</p></div>'
        )
    if not items:
        return ""
    return (
        '<section class="section" data-theme="base"><div class="shell">'
        f'<p class="eyebrow" data-reveal>{s["faq_eyebrow"]}</p>'
        f'<h2 data-reveal>{s["faq_title_html"]}</h2>'
        f'<div class="faq-list">{"".join(items)}</div>'
        "</div></section>"
    )


QR_ASSET_NAME = "qr.svg"


def make_qr_svg(public_url: str) -> str:
    """Return a scannable QR code as a STANDALONE SVG document encoding `public_url`.

    Uses segno (pure Python, no image libraries) via `save(kind="svg")`, which
    emits a proper standalone SVG (XML declaration + `xmlns`). This is required
    so the file works both when opened directly and when loaded as an external
    image via `<img src="qr.svg">` (segno's `svg_inline` omits the namespace and
    is only valid when embedded inline in HTML — that produced the broken image).

    Dark modules on a solid white background for high contrast on every style,
    including the dark black-gold theme.
    """
    if segno is None:
        raise ValidationError(
            "Falta la dependencia 'segno' para generar el QR. "
            "Instala con: pip install -r requirements.txt"
        )
    qr = segno.make(public_url, error="m")
    buff = io.BytesIO()
    # save(kind="svg") -> standalone SVG (xmldecl + svgns default True).
    qr.save(buff, kind="svg", scale=4, border=2, dark="#111111", light="#ffffff")
    return buff.getvalue().decode("utf-8")


QR_PNG_ASSET_NAME = "qr.png"


def make_qr_png(public_url: str) -> bytes:
    """Return the same QR as a PNG, for the delivery email (Ola 1c).

    PNG and not SVG on purpose: email clients do not render SVG attachments, so
    the QR that goes inline in the delivery email has to be a raster image. The
    page keeps using `qr.svg` (crisper at any size). Portado de ModaLink, que
    era el único producto que ya mandaba el QR por correo.
    """
    if segno is None:
        raise ValidationError(
            "Falta la dependencia 'segno' para generar el QR. "
            "Instala con: pip install -r requirements.txt"
        )
    qr = segno.make(public_url, error="m")
    buff = io.BytesIO()
    qr.save(buff, kind="png", scale=8, border=2, dark="#111111")
    return buff.getvalue()


# --------------------------------------------------------------------------- #
# vCard — bloque `vcard` (linkFactory/14)
# --------------------------------------------------------------------------- #
VCARD_ASSET_NAME = "contact.vcf"


def vcard_escape(value) -> str:
    """Escapa un valor de texto para una propiedad de vCard 3.0 (RFC 2426).

    Backslash primero (o re-escaparia los demas escapes), luego `;` y `,`
    (separadores estructurales) y los saltos de linea como `\\n` literal.
    """
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def make_vcard(view: dict, page_url: str) -> str:
    """El contact.vcf del negocio: SOLO datos que su pagina ya publica.

    Campo por campo es el patron VIVO de My Guest (`vcardText` en su
    master.html: N/FN/ORG con el nombre, TEL tipo CELL, EMAIL, ADR con la
    direccion completa en el slot de calle, URL) — formas que iPhone y Android
    ya aceptaron en produccion. Diferencias a proposito:

    - Aqui el .vcf se ARMA EN BUILD y se sirve como archivo estatico, no con
      JS en el navegador: en My Guest el telefono del anfitrion es privado
      (lo inyecta su worker con token); aqui todos los datos son publicos.
    - El link de la pagina SI viaja en el vCard. En My Guest se excluye porque
      su link lleva token; el nuestro es publico, y es EL punto del boton
      (insight de Vero: el cliente recurrente olvida el enlace — guardado el
      contacto, el enlace viaja adentro).

    Telefono: `phone`, o `whatsapp` como respaldo — los dos son numeros y lo
    que importa es el caller-id. CRLF por RFC; el llamador escribe el archivo
    con `newline=""` para que Windows no lo duplique.
    """
    name = str(view.get("business_name", "") or "").strip()
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:;{vcard_escape(name)};;;",
        f"FN:{vcard_escape(name)}",
        f"ORG:{vcard_escape(name)}",
    ]
    tel = tel_href(view.get("phone")) or tel_href(view.get("whatsapp"))
    if tel:
        lines.append(f"TEL;TYPE=CELL,VOICE:{vcard_escape(tel[len('tel:'):])}")
    mail = str(view.get("public_email", "") or "").strip()
    if mail and "@" in mail and " " not in mail:
        lines.append(f"EMAIL;TYPE=INTERNET:{vcard_escape(mail)}")
    addr = _kicker_source_address(view)
    if addr:
        lines.append(f"ADR;TYPE=WORK:;;{vcard_escape(addr)};;;;")
    page = str(page_url or "").strip()
    if page:
        lines.append(f"URL:{vcard_escape(page)}")
    website = str(view.get("website", "") or "").strip()
    if website.lower().startswith(("http://", "https://")) and website.rstrip("/") != page.rstrip("/"):
        lines.append(f"URL:{vcard_escape(website)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def build_vcard_action(s: dict, vcf_src: str,
                       css_class: str = "btn btn--ghost btn--sm",
                       extra_style: str = "") -> str:
    """La fila de accion del boton "Guardar en contactos".

    Un `<a download>` al archivo estatico: cero JS. GitHub Pages sirve `.vcf`
    como text/vcard, con lo que iPhone abre la tarjeta de contacto directo y
    Android la descarga a Contactos. Reusa `.share__actions` y `.btn` tal
    cual: sin CSS nuevo no hay bytes nuevos para quien no activa el bloque.

    Cero JS y cero terceros a proposito: es lo que deja al boton pasar la
    sobriedad de Dr Link (`blocks.motion` y `blocks.third_party_assets` en
    none, ver Dr Link/docs/DESVIOS_ESPERADOS.md B1 y C1) sin una sola
    excepcion — el mismo marcado sirve para los cinco.

    `css_class`/`extra_style` existen para la SEGUNDA plantilla base del motor
    (generate_catalog.py, linkFactory/22): sus paginas no tienen `.btn`, pero
    si `.share__actions` y `.card__cta`. El default deja byte-identico a quien
    ya usa el bloque.
    """
    style = f' style="{esc(extra_style)}"' if extra_style else ""
    return (
        f'<div class="share__actions"><a class="{esc(css_class)}" '
        f'href="{esc(vcf_src)}" download{style}>{s["vcard_button"]}</a></div>'
    )


_ACCENT_RE = re.compile(r"--(?:base-)?accent\s*:\s*(#[0-9a-fA-F]{3,8})")


def accent_hex(style_css: str) -> str:
    """El color de marca del estilo que el cliente eligio, sacado de su CSS.

    Existe para el pase de wallet: sin `hexBackgroundColor` Google pinta la
    tarjeta de un gris de sistema, y Vero lo dijo con todas sus letras al ver
    el primer pase — "es muy feo". El gris no lo decide Google: lo decide no
    mandar color.

    Sale del MISMO CSS que ya se inserta en la pagina, asi que el pase de un
    cliente combina con su pagina sin que nadie mantenga una segunda tabla de
    colores. Las dos plantillas base nombran su variable distinto
    (`--base-accent` en service-menu, `--accent` en catalogo) y el patron
    acepta las dos; devuelve "" si no encuentra nada, y entonces el pase sale
    con el gris de Google en vez de con un color inventado.
    """
    m = _ACCENT_RE.search(style_css or "")
    return m.group(1) if m else ""


def build_wallet_action(s: dict, wallet_url: str) -> str:
    """La fila del boton "Agregar a Google Wallet" (bloque `wallet_google`).

    Un `<a>` al link firmado de `pay.google.com`: cero JS propio, como el del
    vCard. La diferencia con aquel es que ESTE si carga un tercero al tocarlo
    (el dominio de Google), asi que una vertical con `third_party_assets`
    apagado no deberia encenderlo sin decidirlo — hoy Dr Link es la unica en
    ese caso y el bloque nace apagado en las cinco, asi que no hay conflicto
    que resolver todavia.

    NOTA DE MARCA, para cuando Google apruebe: Google exige su boton oficial
    antes de salir a produccion (brand guidelines). Hoy usa el estilo de la
    pagina, igual que My Guest, y se cambia en el mismo momento en que se
    enciende LINK_FACTORY_GOOGLE_WALLET_PUBLISH.
    """
    return (
        f'<div class="share__actions"><a class="btn btn--ghost btn--sm" '
        f'href="{esc(wallet_url)}" target="_blank" rel="noopener noreferrer">'
        f'{s["wallet_google_button"]}</a></div>'
    )


def build_share(public_url: str, s: dict, qr_src: str = QR_ASSET_NAME,
                vcard_html: str = "", wallet_html: str = "") -> str:
    """"Share" section: QR image + visible link, centered, base theme.

    The QR references the static asset written next to the page (qr.svg) or at
    the client root (../qr.svg for alternate-language pages). No JavaScript,
    no external scripts, no tracking.
    """
    href = safe_href(public_url)
    shown = esc(re.sub(r"^https?://(www\.)?", "", str(public_url)).rstrip("/"))
    alt = esc(f"{s['qr_alt']} {public_url}")

    link_line = (
        f'<a class="share__url" href="{href}" target="_blank" rel="noopener noreferrer">{shown}</a>'
        if href
        else f'<span class="share__url">{shown}</span>'
    )
    share_button = (
        f'<div class="share__actions"><button class="btn btn--solid btn--sm share-btn" '
        f'type="button" data-share-url="{href}" data-share-title="{esc(public_url)}" '
        f'data-copied="{esc(s["share_copied"])}">{s["share_button"]}</button></div>'
        if href
        else ""
    )
    return (
        '<section class="section share" id="compartir" data-theme="base"><div class="shell">'
        f'<p class="eyebrow" data-reveal>{s["share_eyebrow"]}</p>'
        f'<h2 data-reveal>{s["share_title_html"]}</h2>'
        f'<p class="lead" data-reveal>{s["share_lead"]}</p>'
        f'<div class="qrbox" data-reveal><img src="{esc(qr_src)}" '
        f'alt="{alt}" width="180" height="180"></div>'
        f'{link_line}{share_button}{vcard_html}{wallet_html}'
        "</div></section>"
    )


# Per-vertical legal disclaimers (vertical.yaml -> `legal.disclaimers`) are
# rendered with INLINE styles on purpose: the template's stylesheet is inlined
# into every page, so adding a `.footer__disclaimer` rule there would change the
# bytes of all 12 golden demos even for verticals that declare no disclaimers.
# Inline styles keep a vertical with `disclaimers: []` byte-identical to before.
DISCLAIMER_WRAP_STYLE = "margin-top:12px;font-size:.76rem;line-height:1.55;opacity:.85"
DISCLAIMER_ITEM_STYLE = "margin:0 0 4px"

# CSS del formato alterno `footer-span` (ver build_footer). Se emite SOLO con
# ese formato: con el formato `inline` los estilos van en el atributo y esta
# regla seria CSS muerto que moveria los bytes de HMU y PawContact.
FOOTER_DISCLAIMER_CSS = (
    "\n.footer .disclaimer{display:block;margin:14px auto 0;max-width:46ch;"
    "font-size:11.5px;opacity:.8}"
)

THIRD_PARTY_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,'
    'wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Outfit:'
    'wght@300;400;500;600&display=swap" rel="stylesheet">'
)
THIRD_PARTY_GSAP_LINKS = (
    '<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js"></script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/ScrollTrigger.min.js"></script>'
)
MOTION_COMMENT = "Mejora progresiva: sin JS todo es visible; GSAP añade el cine."
NO_MOTION_COMMENT = "Animacion desactivada por configuracion de la vertical."
MOTION_OFF_CSS = (
    "\n/* motion desactivado por la vertical */"
    "\n*,*::before,*::after{animation:none!important;transition:none!important;"
    "scroll-behavior:auto!important}"
    "\n.intro{display:none!important}"
)

# Runtime sin movimiento. Conserva únicamente el comportamiento funcional
# (tema por sección, dock, carrusel y compartir), sin descargar ni mencionar
# librerías de animación. B3/C1 de Dr Link exigen que el HTML final no conserve
# ni siquiera el runtime inerte: un `return` temprano seguía publicando todo el
# código y hacía imposible demostrar "cero terceros / cero GSAP" con curl.
STATIC_RUNTIME_SCRIPT = """<script>
(function(){
  var doc=document;

  var themed=doc.querySelectorAll('[data-theme]');
  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if(e.isIntersecting){
          doc.body.classList.toggle('theme-alt',e.target.dataset.theme==='alt');
        }
      });
    },{rootMargin:'-45% 0px -45% 0px'});
    themed.forEach(function(s){io.observe(s)});
  }

  var dock=doc.getElementById('dock'), hero=doc.querySelector('.hero');
  if(dock){
    if('IntersectionObserver' in window){
      var io2=new IntersectionObserver(function(entries){
        dock.classList.toggle('show',!entries[0].isIntersecting);
      },{threshold:.15});
      io2.observe(hero);
    }else{dock.classList.add('show')}
  }

  doc.querySelectorAll('.figure--carousel').forEach(function(gallery){
    var track=gallery.querySelector('.figure__track');
    var slides=[].slice.call(gallery.querySelectorAll('.figure__slide'));
    var dots=[].slice.call(gallery.querySelectorAll('.gallery-dot'));
    var prev=gallery.querySelector('.gallery-btn--prev');
    var next=gallery.querySelector('.gallery-btn--next');
    if(!track||slides.length<2)return;
    var active=0,ticking=false;
    function setActive(index){
      active=(index+slides.length)%slides.length;
      dots.forEach(function(dot,i){dot.classList.toggle('is-active',i===active)});
    }
    function go(index){
      setActive(index);
      track.scrollTo({left:slides[active].offsetLeft,behavior:'auto'});
    }
    if(prev)prev.addEventListener('click',function(){go(active-1)});
    if(next)next.addEventListener('click',function(){go(active+1)});
    dots.forEach(function(dot,i){dot.addEventListener('click',function(){go(i)})});
    track.addEventListener('scroll',function(){
      if(ticking)return;
      ticking=true;
      requestAnimationFrame(function(){
        setActive(Math.round(track.scrollLeft/Math.max(1,track.clientWidth)));
        ticking=false;
      });
    },{passive:true});
  });

  doc.querySelectorAll('.share-btn').forEach(function(btn){
    btn.addEventListener('click',function(){
      var url=btn.getAttribute('data-share-url');
      var original=btn.textContent;
      var copied=btn.getAttribute('data-copied')||original;
      if(navigator.share){
        navigator.share({title:btn.getAttribute('data-share-title')||doc.title,url:url}).catch(function(){});
        return;
      }
      if(navigator.clipboard&&url){
        navigator.clipboard.writeText(url).then(function(){
          btn.textContent=copied;
          setTimeout(function(){btn.textContent=original},1600);
        });
      }
    });
  });
})();
</script>"""


def without_motion_runtime(rendered: str) -> str:
    """Remove the complete cinematic runtime and its CSS hooks.

    The motion-on output is intentionally untouched (golden products remain
    byte-identical). Only a vertical that explicitly disables motion takes
    this branch.
    """
    rendered = rendered.replace(
        "html:not(.gsap-on) .intro{animation:introUp .9s "
        "cubic-bezier(.76,0,.24,1) 1.9s forwards}\n",
        "",
    )
    rendered = rendered.replace(
        "/* ---------- reveals (fallback CSS si no hay GSAP) ---------- */",
        "/* ---------- reveals ---------- */",
    )
    rendered = rendered.replace(
        ".gsap-on [data-reveal]{opacity:0;transform:translateY(36px)}\n",
        "",
    )
    rendered = rendered.replace(
        "  html.gsap-on [data-reveal]{opacity:1;transform:none}\n",
        "",
    )
    start = rendered.rfind("<script>\n(function(){")
    end = rendered.find("</script>", start)
    if start < 0 or end < 0:
        raise ValidationError("No se encontro el runtime de la plantilla para apagar movimiento.")
    rendered = rendered[:start] + STATIC_RUNTIME_SCRIPT + rendered[end + len("</script>"):]
    if re.search(r"gsap", rendered, flags=re.IGNORECASE):
        raise ValidationError("motion=none dejo una referencia al runtime de animacion.")
    return rendered

NOTICE_BOX_STYLE = (
    "border:1px solid var(--hair);border-radius:18px;padding:22px;"
    "background:var(--bg)"
)
NOTICE_LABEL_STYLE = (
    "margin:0 0 8px;font-size:11px;letter-spacing:.22em;"
    "text-transform:uppercase;color:var(--accent);font-weight:600"
)
NOTICE_TEXT_STYLE = "margin:0;color:var(--soft);font-size:14px;line-height:1.65"


def build_required_notices(lang: str, position: str) -> str:
    """Render non-removable vertical notices at a structural page position."""
    notices = [
        item
        for item in (LEGAL.get("required_notices") or ())
        if item.get("position") == position
    ]
    if not notices:
        return ""
    cards = []
    for item in notices:
        label = item.get("label") or {}
        label_html = (
            f'<p class="notice__label" style="{NOTICE_LABEL_STYLE}">'
            f'{esc(label.get(lang))}</p>'
            if str(label.get(lang, "")).strip()
            else ""
        )
        cards.append(
            f'<div class="notice__card" data-notice-id="{esc(item.get("id"))}" '
            f'role="note" style="{NOTICE_BOX_STYLE}">{label_html}'
            f'<p class="notice__text" style="{NOTICE_TEXT_STYLE}">'
            f'{esc(item.get(lang))}</p></div>'
        )
    return (
        f'\n  <section class="section section--notices" '
        f'data-notice-position="{esc(position)}"><div class="shell">'
        f'{"".join(cards)}</div></section>'
    )


def build_footer(
    payload: dict,
    text: str = f"{BRAND_NAME} - Demo",
    privacy_url: str = "",
    privacy_label: str = "",
    disclaimers: tuple = (),
) -> str:
    name = esc(payload.get("business_name"))
    # Attribution credit links back to this vertical's own site (derived from
    # `domain` in vertical.yaml — HMU hardcoded hmulink.com here); a minimal
    # privacy link points to the same site's policy. Links are underlined so
    # they don't rely on color alone (WCAG 1.4.1).
    credit = (
        f'<a class="footer__link" href="{DOMAIN}/" '
        f'target="_blank" rel="noopener">{esc(text)}</a>'
    )
    legal = ""
    if privacy_url and privacy_label:
        legal = (
            '<span class="footer__sep" aria-hidden="true"> · </span>'
            f'<a class="footer__link" href="{esc(privacy_url)}" '
            f'target="_blank" rel="noopener">{esc(privacy_label)}</a>'
        )
    notes = ""
    texts = [str(item).strip() for item in disclaimers if str(item).strip()]
    if texts and LEGAL.get("disclaimers_style") == "footer-span":
        # Formato `footer-span`: un <span class="disclaimer"> por aviso, con su
        # regla de CSS en la plantilla (ver FOOTER_DISCLAIMER_CSS). Es lo que
        # ModaLink lleva publicado desde antes de compartir el motor; cambiarlo
        # reescribiria paginas vivas, asi que el formato es una opcion.
        notes = "".join(f'<span class="disclaimer">{esc(item)}</span>' for item in texts)
    elif texts:
        items = "".join(
            f'<p class="footer__disclaimer" style="{DISCLAIMER_ITEM_STYLE}">{esc(item)}</p>'
            for item in texts
        )
        notes = f'<div class="footer__disclaimers" style="{DISCLAIMER_WRAP_STYLE}">{items}</div>'
    return (
        '<footer class="footer">'
        f'<span class="serif">{name}</span>'
        f'{credit}{legal}{notes}'
        "</footer>"
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
DEMO_HEAD_META = '<meta name="robots" content="noindex">'

# Los 3 comentarios de cabecera de la plantilla. Son tokens con DEFAULT LITERAL
# —no se derivan de `brand_name` ni de la cuenta de estilos— justamente para que
# ninguna vertical pueda moverlos sin decirlo: derivarlos habria cambiado los
# bytes de PawContact el dia que se agrego. Una vertical los sobreescribe con
# `template_comments:` en su vertical.yaml.
TEMPLATE_COMMENT_DEFAULTS = {
    "brand_line": 'HMU Link — plantilla "editorial motion" (tier WOW)',
    "styles_line": "Estructura compartida por los 12 estilos cerrados",
    "palette_line": "uno de los 12 estilos cerrados",
    "figure_line": "si el negocio tiene fotos",
}
TEMPLATE_COMMENTS = {**TEMPLATE_COMMENT_DEFAULTS, **(TEMPLATE_COMMENT_OVERRIDES or {})}


def render_view(
    view: dict,
    lang: str,
    *,
    head_meta: str,
    lang_switch_html: str,
    footer_text: str,
    share_url: str,
    qr_src: str = QR_ASSET_NAME,
    vcard_src: str = "",
) -> str:
    """Render one page (any language) from a flat single-language view dict.

    `vcard_src` es la ruta relativa al contact.vcf del cliente (misma mecanica
    que `qr_src`). El boton solo se imprime con el bloque `vcard` encendido Y
    una ruta dada: el default vacio deja a todo llamador existente byte-identico.

    El pase de Google Wallet no necesita parametro: se arma aqui a partir del
    mismo `view` y `share_url`, y sus DOS puertas (bloque + interruptor de
    publicacion) viven donde se pueden auditar, no en la firma.
    """
    s = STRINGS[lang]
    brand = view["brand_style"]
    template_path = TEMPLATES_DIR / "base.html"
    if not template_path.exists():
        raise ValidationError(f"No existe el template base: {template_path}")
    style_path = STYLES_DIR / f"{brand}.css"
    if not style_path.exists():
        raise ValidationError(f"No existe el estilo para brand_style={brand!r}: {style_path}")
    template = template_path.read_text(encoding="utf-8")
    style_css = style_path.read_text(encoding="utf-8")
    # El pase se arma DESPUES de leer el estilo, para que su tarjeta lleve el
    # color de marca del estilo que el cliente eligio (ver accent_hex).
    wallet_url = (
        wallet.build_google_wallet_url(
            view, share_url, brand=BRAND_NAME,
            background=accent_hex(style_css),
            # Las filas del pase se escriben en el idioma de la pagina y
            # salen de STRINGS, no del codigo: "Ver la carta" habria sido
            # correcto en un restaurante y falso en un consultorio.
            etiquetas={"abrir": s["wallet_pass_open"]})
        if _block_enabled(view, "wallet_google") else ""
    )
    # Se calcula una sola vez: decide el bloque Y su CSS (ver LOOKBOOK_CSS).
    lookbook_html = build_lookbook(view, s)
    # Mismo criterio para los otros tres pares bloque+CSS. `faq_on` mira el
    # BLOQUE, no si esta pagina trae preguntas: con el bloque encendido el CSS
    # y la linea del token se emiten siempre (es lo que HMU y PawContact ya
    # publican, aunque ninguna de sus demos tenga FAQ).
    faq_on = _block_enabled(view, "faq")
    faq_html = build_faq(view, s)
    stacked_rows = _has_stacked_info_rows(view, s, lang)
    disclaimer_texts = tuple(item.get(lang, "") for item in LEGAL.get("disclaimers") or ())
    footer_span = bool(
        LEGAL.get("disclaimers_style") == "footer-span"
        and any(str(t).strip() for t in disclaimer_texts)
    )
    motion_on = _block_enabled(view, "motion")
    third_party_on = _block_enabled(view, "third_party_assets")

    tokens = {
        "{{LANG}}": lang,
        "{{HEAD_META}}": head_meta,
        "{{THIRD_PARTY_FONT_LINKS}}": THIRD_PARTY_FONT_LINKS if third_party_on else "",
        "{{THIRD_PARTY_GSAP_LINKS}}": (
            THIRD_PARTY_GSAP_LINKS if third_party_on and motion_on else ""
        ),
        "{{CINEMATIC_EXPR}}": "hasGsap&&!reduced" if motion_on else "false",
        "{{MOTION_OFF_CSS}}": "" if motion_on else MOTION_OFF_CSS,
        "{{TPL_MOTION_LINE}}": MOTION_COMMENT if motion_on else NO_MOTION_COMMENT,
        "{{STYLE_NAME}}": esc(brand),
        "{{STYLE_CSS}}": style_css,
        "{{SERIF_STACK}}": TYPOGRAPHY["serif"],
        "{{SANS_STACK}}": TYPOGRAPHY["sans"],
        "{{TPL_BRAND_LINE}}": TEMPLATE_COMMENTS["brand_line"],
        "{{TPL_STYLES_LINE}}": TEMPLATE_COMMENTS["styles_line"],
        "{{TPL_PALETTE_LINE}}": TEMPLATE_COMMENTS["palette_line"],
        "{{TPL_FIGURE_LINE}}": TEMPLATE_COMMENTS["figure_line"],
        "{{PAGE_TITLE}}": esc(f'{view.get("business_name")} - {s["title_suffix"]}'),
        "{{SHORT_DESCRIPTION}}": esc(view.get("short_description")),
        "{{LANG_SWITCH_BLOCK}}": lang_switch_html,
        "{{LOGO_BLOCK}}": build_logo(view),
        "{{INTRO_BLOCK}}": build_intro(view, s) if motion_on else "",
        "{{HERO_KICKER_BLOCK}}": build_hero_kicker(view),
        "{{HERO_TITLE_BLOCK}}": build_hero_title(view),
        "{{CTA_ROW_BLOCK}}": build_cta_row(view, s),
        "{{HERO_IMAGE_BLOCK}}": build_hero_image(view, s),
        # Ver LOOKBOOK_CSS: bloque y CSS se emiten juntos o no se emite ninguno.
        "{{LOOKBOOK_BLOCK}}": f"\n  {lookbook_html}" if lookbook_html else "",
        "{{LOOKBOOK_CSS}}": LOOKBOOK_CSS if lookbook_html else "",
        "{{MARQUEE_BLOCK}}": build_marquee(view),
        "{{SERVICES_BLOCK}}": build_services(view, s),
        "{{FEATURED_BLOCK}}": build_featured(view, s),
        "{{INFO_BLOCK}}": build_info(view, s, lang),
        "{{REQUIRED_NOTICES_AFTER_HERO}}": build_required_notices(lang, "after_hero"),
        "{{REQUIRED_NOTICES_AFTER_INFO}}": build_required_notices(lang, "after_info"),
        "{{REQUIRED_NOTICES_BEFORE_FOOTER}}": build_required_notices(lang, "before_footer"),
        "{{INFO_STACK_CSS}}": INFO_STACK_CSS if stacked_rows else "",
        # La LINEA entera del FAQ, no solo su contenido: con el bloque apagado
        # no queda ni el salto de linea. Con el encendido el resultado es
        # exactamente el de antes ("\n" + lo que devuelva build_faq).
        "{{FAQ_LINE}}": f"\n{faq_html}" if faq_on else "",
        "{{FAQ_CSS}}": FAQ_CSS if faq_on else "",
        "{{FOOTER_DISCLAIMER_CSS}}": FOOTER_DISCLAIMER_CSS if footer_span else "",
        "{{SHARE_BLOCK}}": build_share(
            share_url, s, qr_src,
            build_vcard_action(s, vcard_src)
            if vcard_src and _block_enabled(view, "vcard") else "",
            # DOS puertas, no una (linkFactory/18): el bloque de la vertical Y
            # el interruptor de publicacion de wallet.py, que hoy esta APAGADO
            # porque el Issuer sigue en revision. Con cualquiera de las dos
            # cerrada, `build_google_wallet_url` devuelve "" y la pagina no
            # menciona pay.google.com.
            build_wallet_action(s, wallet_url) if wallet_url else "",
        ),
        "{{FOOTER_BLOCK}}": build_footer(
            view,
            footer_text,
            f"{DOMAIN}{LEGAL['privacy_paths'][lang]}",
            s["footer_privacy"] if _block_enabled(view, "footer_privacy") else "",
            # `legal.disclaimers` in vertical.yaml, in this page's language. The
            # generator inserts them so they are not removable by hand-editing a
            # published page (house rule). Empty for verticals that declare none.
            disclaimer_texts,
        ),
        "{{DOCK_BLOCK}}": build_dock(view, s),
        "{{SCROLL_HINT}}": s["scroll_hint"],
        "{{SKIP_LINK_BLOCK}}": f'<a class="skip" href="#content">{esc(s["skip_to_content"])}</a>',
    }

    # Una sola pasada (hallazgo #17): un dato del negocio que contenga
    # literalmente otro token ya no se re-expande.
    rendered = fill_tokens(template, tokens)
    return rendered if motion_on else without_motion_runtime(rendered)


# --------------------------------------------------------------------------- #
# Client rendering (bilingual, Phase 5)
# --------------------------------------------------------------------------- #
def client_lang_view(payload: dict, lang: str) -> dict:
    """Flatten shared fields + per-language content into one render view."""
    view = {
        key: payload.get(key)
        for key in (
            "business_name",
            "brand_style",
            "business_type",
            "logo_url",
            "primary_image_url",
            "gallery_images",
            "lookbook_urls",
            "whatsapp",
            "phone",
            "public_email",
            "instagram",
            "facebook",
            "tiktok",
            "website",
            "booking_url",
            "primary_cta",
            "google_maps_url",
            "google_reviews_url",
            "other_public_link",
            "delivery_pickup_links",
            "portfolio_link",
            "locations",
            "pinterest",
            # Campos de vertical (hoy los del giro de moda). Viajan aqui como
            # cualquier otro campo compartido: si el payload no los trae, la
            # vista los ve en None y sus filas no se imprimen.
            "specialty",
            "specialty_other",
            "years_experience",
            "training_text",
            "appointment_mode",
            "home_service",
            "delivery_time_text",
            "deposit_policy_text",
            "payment_methods",
            "invoicing",
            # Salud (T2). Se aceptan tanto los campos planos que construye el
            # intake generico como los objetos historicos del fork de Dr Link.
            "country",
            "profession",
            "credentials",
            "credentials_institution",
            "cedula_profesional",
            "cedula_especialidad",
            "license_state",
            "license_number",
            "npi",
            "board_name",
            "languages",
            "telemedicine",
            "telemedicine_offered",
            "telemedicine_modality",
            "insurance",
            "privacy_notice_url",
        )
    }
    content_block = payload["content"][lang]
    view.update(content_block)

    # Horario por sucursal: se pega el horario de ESTE idioma a su ubicacion,
    # para que el bloque de direccion lo imprima sin un token aparte. Solo pasa
    # cuando el payload trae `location_hours` — una vertical con horario global
    # no toca `locations` y su vista queda exactamente igual que antes.
    if isinstance(content_block.get("location_hours"), list):
        hours = content_block["location_hours"]
        view["locations"] = [
            {**loc, "hours_text": hours[i] if i < len(hours) else ""}
            for i, loc in enumerate(payload.get("locations") or [])
            if isinstance(loc, dict)
        ]
    return view


def client_head_meta(canonical: str, es_url: str, en_url: str, default_url: str) -> str:
    meta = (
        f'<link rel="canonical" href="{esc(canonical)}">\n'
        f'<link rel="alternate" hreflang="es" href="{esc(es_url)}">\n'
        f'<link rel="alternate" hreflang="en" href="{esc(en_url)}">\n'
        f'<link rel="alternate" hreflang="x-default" href="{esc(default_url)}">'
    )
    if SCHEMA.get("client_noindex"):
        meta += '\n<meta name="robots" content="noindex">'
    return meta


def build_og_meta(view: dict, canonical_url: str, lang: str) -> str:
    """Open Graph + Twitter Card tags so a shared link unfurls with photo +
    title in WhatsApp/SMS. Reuses the page's own hero photo and canonical URL
    (demos and clients alike); omits the image tags rather than link a photo
    that doesn't exist when the business has no real photo — no default
    brand image is guaranteed to exist for every vertical."""
    images = _gallery_images(view)
    title = esc(view.get("business_name"))
    desc = str(view.get("short_description") or "").strip()
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
    if images:
        image = images[0]
        tags.append(f'<meta property="og:image" content="{image}">')
        tags.append('<meta name="twitter:card" content="summary_large_image">')
        tags.append(f'<meta name="twitter:image" content="{image}">')
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    return "\n".join(tags)


def build_client(json_path: Path) -> Path:
    """Generate both language pages + one QR for a real client payload."""
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    validate_client(payload)

    slug = str(payload["public_slug"]).strip()
    default_lang = payload["default_language"]
    alt_lang = "en" if default_lang == "es" else "es"

    root_url = f"{CLIENT_BASE_URL}/{slug}/"
    alt_url = f"{root_url}{alt_lang}/"
    lang_urls = {default_lang: root_url, alt_lang: alt_url}

    root_dir = CLIENT_OUTPUT_DIR / slug
    root_dir.mkdir(parents=True, exist_ok=True)
    alt_dir = root_dir / alt_lang
    alt_dir.mkdir(parents=True, exist_ok=True)

    views = {lang: client_lang_view(payload, lang) for lang in CLIENT_LANGS}
    head = {
        lang: client_head_meta(
            lang_urls[lang], lang_urls.get("es"), lang_urls.get("en"), root_url
        )
        + "\n"
        + build_og_meta(views[lang], lang_urls[lang], lang)
        for lang in CLIENT_LANGS
    }
    # Language switch: default page links into the subfolder; the alternate
    # page links back to the client root. Labels come from the *target* page
    # language so the visitor reads the switch in the language they want.
    switch = {
        default_lang: (
            f'<div class="lang-switch"><a href="{alt_lang}/" lang="{alt_lang}">'
            f'{STRINGS[default_lang]["lang_switch"]}</a></div>'
        ),
        alt_lang: (
            f'<div class="lang-switch"><a href="../" lang="{default_lang}">'
            f'{STRINGS[alt_lang]["lang_switch"]}</a></div>'
        ),
    }

    default_html = render_view(
        views[default_lang],
        default_lang,
        head_meta=head[default_lang],
        lang_switch_html=switch[default_lang],
        footer_text=STRINGS[default_lang]["footer_credit"],
        share_url=root_url,
        qr_src=QR_ASSET_NAME,
        vcard_src=VCARD_ASSET_NAME,
    )
    alt_html = render_view(
        views[alt_lang],
        alt_lang,
        head_meta=head[alt_lang],
        lang_switch_html=switch[alt_lang],
        footer_text=STRINGS[alt_lang]["footer_credit"],
        share_url=root_url,
        qr_src=f"../{QR_ASSET_NAME}",
        vcard_src=f"../{VCARD_ASSET_NAME}",
    )

    (root_dir / "index.html").write_text(default_html, encoding="utf-8")
    (alt_dir / "index.html").write_text(alt_html, encoding="utf-8")
    # One QR per client, encoding the default-language URL. The SVG is the one
    # the page shows; the PNG existe solo para que el workflow lo mande en
    # base64 al worker y el correo de entrega lo adjunte inline (los clientes de
    # correo no renderizan SVG). Se emite SOLO para clientes: un archivo nuevo
    # en las demos rompería el golden, que compara el árbol byte a byte.
    (root_dir / QR_ASSET_NAME).write_text(make_qr_svg(root_url), encoding="utf-8")
    (root_dir / QR_PNG_ASSET_NAME).write_bytes(make_qr_png(root_url))
    # Un contact.vcf por cliente, SOLO con el bloque `vcard` encendido: con el
    # default ("none") ni el archivo ni el boton existen y el arbol queda
    # byte-identico. Datos del idioma por defecto (la direccion viaja por
    # idioma); `newline=""` para que Windows no convierta el CRLF del RFC.
    if _block_enabled(payload, "vcard"):
        with (root_dir / VCARD_ASSET_NAME).open("w", encoding="utf-8", newline="") as fh:
            fh.write(make_vcard(views[default_lang], root_url))
    return root_dir / "index.html"


# --------------------------------------------------------------------------- #
# Demo rendering (bilingual)
# --------------------------------------------------------------------------- #
def _demo_public_url(payload: dict, slug: str) -> str:
    """URL publica de una demo monolingue.

    Respeta un `public_url` explicito en el payload: el prospector (Cory)
    publica sus previews en su propio dominio, no bajo el de la vertical.
    """
    url = str(payload.get("public_url", "") or "").strip()
    return url or f"{DEMO_BASE_URL}/{slug}"


def build_demo_monolingual(json_path: Path) -> Path:
    """Una demo de UN idioma, en un payload plano (sin objeto `content`).

    Es el camino de Cory: sus previews se arman de datos publicos de un
    prospecto, en el idioma en el que ese prospecto se anuncia, y no hay una
    traduccion que ofrecer. Solo corre en verticales con
    `schema.demo_mode: flexible` — con el default (`bilingual`) un payload sin
    `content` sigue fallando ruidosamente, que es lo que debe pasar cuando a
    una demo bilingue se le perdio un idioma.
    """
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    validate_client_flat(payload)
    slug = str(payload.get("public_slug", "")).strip() or json_path.stem
    lang = payload.get("default_language") if payload.get("default_language") in CLIENT_LANGS else "es"
    public_url = _demo_public_url(payload, slug)

    html = render_view(
        payload,
        lang,
        head_meta=DEMO_HEAD_META + "\n" + build_og_meta(payload, public_url, lang),
        lang_switch_html="",
        footer_text=STRINGS[lang]["footer_demo_credit"],
        share_url=public_url,
        vcard_src=VCARD_ASSET_NAME,
    )
    out_dir = OUTPUT_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")
    (out_dir / QR_ASSET_NAME).write_text(make_qr_svg(public_url), encoding="utf-8")
    # La demo enseña el producto completo: con el bloque `vcard` encendido
    # lleva su contact.vcf igual que un cliente (con el default no hay archivo).
    if _block_enabled(payload, "vcard"):
        with (out_dir / VCARD_ASSET_NAME).open("w", encoding="utf-8", newline="") as fh:
            fh.write(make_vcard(payload, public_url))
    return out_file


def build_demo(json_path: Path) -> Path:
    """Generate both language pages + one QR for a bilingual demo payload.

    Spanish is the default at the slug root (`demos/<slug>/`); English lives in
    `demos/<slug>/en/`. Every page carries a language switch and is `noindex`
    (demos are not meant to rank), so — unlike real clients — they get no
    canonical/hreflang. The footer reads "HMU Link - Demo".

    Con `schema.demo_mode: flexible` un payload PLANO (sin `content`) se
    desvia al camino monolingue de arriba. Se decide por la forma del payload y
    no por un campo aparte: un payload sin `content` no tiene un segundo idioma
    que renderizar, y decirlo dos veces es una oportunidad de que se contradigan.
    """
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if SCHEMA.get("demo_mode") == "flexible" and not isinstance(payload.get("content"), dict):
        return build_demo_monolingual(json_path)
    validate_client(payload)

    slug = str(payload["public_slug"]).strip()
    default_lang = payload.get("default_language", "es")
    alt_lang = "en" if default_lang == "es" else "es"

    root_url = f"{DEMO_BASE_URL}/{slug}/"
    alt_url = f"{root_url}{alt_lang}/"
    lang_urls = {default_lang: root_url, alt_lang: alt_url}

    root_dir = OUTPUT_DIR / slug
    root_dir.mkdir(parents=True, exist_ok=True)
    alt_dir = root_dir / alt_lang
    alt_dir.mkdir(parents=True, exist_ok=True)

    # Language switch: the default page links into the subfolder; the alternate
    # page links back to the root. Labels are in the *target* language.
    switch = {
        default_lang: (
            f'<div class="lang-switch"><a href="{alt_lang}/" lang="{alt_lang}">'
            f'{STRINGS[default_lang]["lang_switch"]}</a></div>'
        ),
        alt_lang: (
            f'<div class="lang-switch"><a href="../" lang="{default_lang}">'
            f'{STRINGS[alt_lang]["lang_switch"]}</a></div>'
        ),
    }

    views = {default_lang: client_lang_view(payload, default_lang), alt_lang: client_lang_view(payload, alt_lang)}
    head = {
        lang: DEMO_HEAD_META + "\n" + build_og_meta(views[lang], lang_urls[lang], lang)
        for lang in (default_lang, alt_lang)
    }

    default_html = render_view(
        views[default_lang],
        default_lang,
        head_meta=head[default_lang],
        lang_switch_html=switch[default_lang],
        footer_text=STRINGS[default_lang]["footer_demo_credit"],
        share_url=root_url,
        qr_src=QR_ASSET_NAME,
        vcard_src=VCARD_ASSET_NAME,
    )
    alt_html = render_view(
        views[alt_lang],
        alt_lang,
        head_meta=head[alt_lang],
        lang_switch_html=switch[alt_lang],
        footer_text=STRINGS[alt_lang]["footer_demo_credit"],
        share_url=root_url,
        qr_src=f"../{QR_ASSET_NAME}",
        vcard_src=f"../{VCARD_ASSET_NAME}",
    )

    (root_dir / "index.html").write_text(default_html, encoding="utf-8")
    (alt_dir / "index.html").write_text(alt_html, encoding="utf-8")
    # One QR per demo, encoding the default-language (root) URL.
    (root_dir / QR_ASSET_NAME).write_text(make_qr_svg(root_url), encoding="utf-8")
    # Mismo criterio que build_client: el contact.vcf solo existe con el
    # bloque `vcard` encendido — el golden de HMU compara el arbol byte a byte.
    if _block_enabled(payload, "vcard"):
        with (root_dir / VCARD_ASSET_NAME).open("w", encoding="utf-8", newline="") as fh:
            fh.write(make_vcard(views[default_lang], root_url))
    return root_dir / "index.html"


# --------------------------------------------------------------------------- #
# Directorio publico (subsistema opt-in — ver engine/generator/directory.py)
# --------------------------------------------------------------------------- #
def _directory_urls() -> dict:
    return {lang: f"{DOMAIN}/{path.strip('/')}/" for lang, path in DIRECTORY["paths"].items()}


def _directory_opted_in() -> list:
    """Los clientes que PIDIERON aparecer, sin filtrar por validacion."""
    opted = []
    for path in _client_jsons():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if payload.get("directory_opt_in") is True:
            opted.append((path, payload))
    return opted


def collect_directory_entries() -> list:
    """Las fichas publicables, con el candado del directorio vacio.

    Un cliente que pidio aparecer pero ya no pasa `validate_client` se descarta
    —publicar una ficha con datos que el generador considera invalidos seria
    peor—, pero si se descartan TODOS, esto falla en vez de publicar una pagina
    vacia: un directorio que se vacia solo no es un directorio vacio, es una
    regresion, y sin este candado se publicaria sin un solo error (era el fallo
    silencioso que el inventario de la migracion pedia cerrar).
    """
    opted = _directory_opted_in()
    entries, descartados = [], []
    for path, payload in opted:
        try:
            validate_client(payload)
        except ValidationError as exc:
            descartados.append(f"{path.name}: {exc}")
            continue
        entries.append(payload)
    if opted and not entries:
        raise directory.DirectoryError(
            "Directorio: hay "
            f"{len(opted)} cliente(s) que pidieron aparecer y NINGUNO pasa la "
            "validacion, asi que el directorio saldria vacio. Eso casi siempre "
            "es una validacion nueva que dejo fuera a los clientes de siempre, "
            "no un directorio que de verdad esta vacio. Motivos:\n  - "
            + "\n  - ".join(descartados)
        )
    return entries


def build_directory() -> list:
    """Escribe una pagina de directorio por idioma. Devuelve las rutas escritas.

    No hace nada —ni un archivo— si la vertical no declara `directory:`.
    """
    if not DIRECTORY.get("enabled"):
        return []
    entries = collect_directory_entries()
    urls = _directory_urls()
    pairs = tuple(DIRECTORY["hreflang"].items())
    hreflang_html = directory.hreflang(urls, pairs, DIRECTORY["default_language"])
    group = DIRECTORY["group_by"]
    order = tuple((CATALOGS.get(group) or {}).get("values") or ())
    written = []
    for lang, rel in DIRECTORY["paths"].items():
        out_dir = REPO_ROOT / "public" / Path(rel)
        out_dir.mkdir(parents=True, exist_ok=True)
        html_out = directory.render_page(
            entries,
            lang,
            strings=DIRECTORY["strings"][lang],
            urls=urls,
            hreflang_html=hreflang_html,
            category_of=lambda p: str(p.get(group) or DIRECTORY["fallback_category"]),
            category_label=lambda key, lg: catalog_label(group, key, lg),
            category_order=order,
            state_of=lambda p: p.get(DIRECTORY["filter_by"]),
            tagline_of=lambda p, lg: p.get("content", {}).get(lg, {}).get("short_description", ""),
            url_of=_directory_client_url,
            disclaimer=next(
                (item.get(lang, "") for item in LEGAL.get("disclaimers") or ()), ""
            ),
            esc=esc,
            safe_href=safe_href,
        )
        out_file = out_dir / "index.html"
        out_file.write_text(html_out, encoding="utf-8")
        written.append(out_file)
    return written


def _directory_client_url(payload: dict, lang: str) -> str:
    slug = str(payload["public_slug"]).strip()
    root_url = f"{CLIENT_BASE_URL}/{slug}/"
    return root_url if lang == payload.get("default_language") else f"{root_url}{lang}/"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _publicar_directorio() -> None:
    """Escribe el directorio y lo dice, o no hace nada si esta apagado."""
    if not DIRECTORY.get("enabled"):
        return
    escritos = build_directory()
    rutas = ", ".join(str(p.parent.relative_to(REPO_ROOT / "public")) for p in escritos)
    print(f"Directorio regenerado: {rutas}")


def _client_jsons() -> list[Path]:
    """Client payloads to build: data/clients/*.json, skipping _templates."""
    if not CLIENTS_DIR.exists():
        return []
    return sorted(
        p for p in CLIENTS_DIR.glob("*.json") if not p.name.startswith("_")
    )


def main(argv: list[str]) -> int:
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
        demo_paths = sorted(DEMOS_DIR.glob("*.json"))
        client_paths = _client_jsons()

    if not demo_paths and not client_paths:
        print("No se encontraron payloads JSON para generar.", file=sys.stderr)
        # El directorio se escribe IGUAL: su estado vacio es una pagina
        # legitima que invita a ser el primero, y una vertical con directorio
        # encendido no puede quedarse sin publicarlo solo porque todavia no
        # tiene clientes. No hace nada en una vertical que no lo declara.
        _publicar_directorio()
        return 1

    failures = 0
    for path in demo_paths:
        try:
            out_file = build_demo(path)
            rel = out_file.relative_to(REPO_ROOT)
            print(f"[ok]   {path.name} -> {rel} (+ alterno bilingue)")
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            print(f"[fail] {path.name}: {exc}", file=sys.stderr)

    for path in client_paths:
        try:
            out_file = build_client(path)
            rel = out_file.relative_to(REPO_ROOT)
            print(f"[ok]   {path.name} -> {rel} (+ alterno bilingue)")
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            failures += 1
            print(f"[fail] {path.name}: {exc}", file=sys.stderr)

    total = len(demo_paths) + len(client_paths)
    print(f"\nGeneradas {total - failures}/{total} paginas.")

    # El directorio se rehace SIEMPRE desde TODOS los clientes con
    # `directory_opt_in`, no solo los que vinieron por argv: igual que
    # public/links/, es un artefacto derivado del estado completo de
    # data/clients/. Generar la pagina de una clienta nueva sin rehacerlo la
    # dejaria fuera del directorio en el que pidio aparecer, sin un solo error.
    _publicar_directorio()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
