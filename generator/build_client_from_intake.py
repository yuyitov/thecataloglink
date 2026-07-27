#!/usr/bin/env python3
"""Build a real-client page from an automated intake dispatch (Phase 6).

Runs inside GitHub Actions when the service-menu-worker dispatches a
`new-hmu-service-menu` repository_dispatch event. The full sanitized public
payload travels in the event's client_payload (proven upstream pattern), so this
script never talks to KV or Stripe. It does fetch the logo/photo files
directly from their Tally-hosted URLs (no Tally API calls, just the public
upload links already present in the payload).

Input:  env INTAKE_PAYLOAD — JSON string:
        { order_id, submission_id, slug, public_payload: {...} }
Output: data/clients/<slug>.client.json  (client_payload_public v1)
        public/links/<slug>/             (via generate_service_menu.py)
        public/links/<slug>/assets/      (downloaded logo/hero image, if any)
        GITHUB_OUTPUT slug=<slug>        (for later workflow steps)

Security notes:
- The payload contains ONLY approved public fields (the Worker filters).
- This script never prints the payload to stdout (public repo logs).
- After generation it scans the HTML output for the order_id as a guard.
- Downloaded images are only kept if content-type is jpeg/png/webp and size
  is under MAX_IMAGE_BYTES; anything else is skipped (page falls back to the
  placeholder), it never blocks generation.

Translation: the intake is authored in one language (default_language). When
OPENAI_API_KEY is configured, the other language's short_description, service
names, policies and featured_package are translated via the OpenAI API
(gpt-4o-mini). If the key is missing or the call fails, both languages just
publish the same source-language text (original v1 behavior) — translation
never blocks page generation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from merge_intake import answered_keys, load_previous_client, merge_client, sanitize_intake
# El botón que el negocio eligió: la tabla de alias es DATO compartido con el
# worker, no una copia local (ver primary_cta.py — tres copias divergidas).
from primary_cta import normalize_primary_cta
from vertical_config import (
    CATALOGS, DOMAIN, GENERATOR_FOR_TEMPLATE, INTAKE, LEGAL, SCHEMA, TEMPLATE,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "data" / "clients"
LINKS_DIR = REPO_ROOT / "public" / "links"
CLIENT_BASE_URL = f"{DOMAIN}/links"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")
BULLET_RE = re.compile(r"^[\s\-\*•·–—>]+")
TRAIL_SEP_RE = re.compile(r"[\s\-–—·:|,]+$")
# Splits a "Category — Name — Description" service line at an en/em-dash that is
# surrounded by whitespace (the shape intakes use). Requiring the spaces keeps
# ranges like "10–12" or hyphenated names from being split apart.
DASH_SPLIT_RE = re.compile(r"\s+[–—]\s+")
# El mismo separador aceptando además el GUION NORMAL, que es lo que de hecho
# escribe la gente: en el teclado de un celular no hay raya larga.
HYPHEN_SPLIT_RE = re.compile(r"\s+[-–—]\s+")


def split_fields(text: str, maxsplit: int = 0) -> list[str]:
    """Parte una línea de intake en sus campos, por el separador que ELLA usa.

    El formulario enseña "Categoría — Nombre — Descripción — $precio" con raya
    larga. Pero el teclado de un celular no tiene raya larga: da guion normal, y
    con el guion la línea entera se iba de nombre — el cliente perdía su
    categoría (y con ella los filtros de su página) y su descripción. Medido con
    el código real el 2026-07-26.

    Aceptar el guion SIEMPRE sería destructivo, porque el guion es también el
    signo del RANGO: "$200 - $400", "9 a.m. - 5 p.m.". Así que la elección es
    POR LÍNEA y no global: si la línea trae la raya que el formulario enseña,
    ESA es su separador y el guion se queda como texto. El guion solo separa en
    las líneas que no traen raya, o sea donde no hay nada con lo que confundirlo.

    La consecuencia que importa: ninguna línea que hoy funciona cambia de
    resultado (ModaLink usa raya larga en sus 25 separadores), y la misma línea
    escrita con guion produce la MISMA página que escrita con raya.
    """
    rx = DASH_SPLIT_RE if DASH_SPLIT_RE.search(text) else HYPHEN_SPLIT_RE
    return rx.split(text, maxsplit)

IMAGE_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB
# Defensa en profundidad: solo se descargan imágenes hospedadas por Tally
# (los FILE_UPLOAD del intake viven ahí). Cualquier otro host se ignora.
ALLOWED_IMAGE_HOSTS_SUFFIX = (".tally.so",)

# Base pública desde donde el prospector (Cory) publica el catálogo ya armado de
# cada vista previa: `<base>/<slug>/payload.json` y sus fotos en
# `<base>/<slug>/img/…`. Es el transporte "opción A" (decisión de Vero,
# 2026-07-25). Vacía = esta vertical no tiene prospector alimentándola.
#
# Además de la ruta, es el ALLOWLIST: solo se descarga lo que cuelga
# literalmente de esta base. El slug viaja desde el worker (que lo sacó del
# client_reference_id de Stripe) y el `photo_base_url` viene DENTRO del JSON
# descargado, así que sin este cerco un payload manipulado podría hacer que el
# workflow buscara imágenes en cualquier host.
PROSPECT_PAYLOAD_BASE = os.environ.get("PROSPECT_PAYLOAD_BASE_URL", "").strip().rstrip("/")
# Las fotos del catálogo bajadas de Cory se dejan aquí y NO se commitean: el
# generador las copia a public/links/<slug>/img/, que es lo que sí se publica.
# Dejar además la copia de trabajo en git duplicaría el peso de cada catálogo.
PHOTO_STAGING_DIR = REPO_ROOT / ".catalog-photos"
CATALOG_SLUG_RE = re.compile(r"^[a-z0-9-]{3,80}-[0-9a-f]{6}$")


def _allowed_image_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if PROSPECT_PAYLOAD_BASE and url.startswith(PROSPECT_PAYLOAD_BASE + "/"):
        return True
    host = parsed.hostname.lower()
    return host == "tally.so" or host.endswith(ALLOWED_IMAGE_HOSTS_SUFFIX)


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    """Re-apply the host allowlist on every redirect hop.

    A redirect to a non-Tally host would otherwise let an attacker-influenced
    URL reach an arbitrary server (SSRF). Redirects that stay on Tally storage
    (e.g. a signed URL bouncing to its CDN) are still allowed, so legitimate
    downloads keep working.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_image_url(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, f"redirect to disallowed host blocked: {newurl}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_IMAGE_OPENER = urllib.request.build_opener(_AllowlistRedirect)


def download_image(url: str, dest_dir: Path, basename: str) -> str | None:
    """Download a Tally-hosted image and publish it under dest_dir.

    Returns the published filename, or None if the URL is empty, the host
    isn't Tally's storage, the content-type isn't an allowed image format,
    or the download fails. Never raises: an image problem should not block
    page generation.
    """
    url = (url or "").strip()
    if not url:
        return None
    if not _allowed_image_url(url):
        print(f"WARN: image skipped, host not allowed: {url}", file=sys.stderr)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hmulink-generator/1.0"})
        with _IMAGE_OPENER.open(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            ext = IMAGE_EXT_BY_CONTENT_TYPE.get(content_type)
            if not ext:
                print(f"WARN: image skipped, unsupported content-type {content_type!r}: {url}", file=sys.stderr)
                return None
            data = resp.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                print(f"WARN: image skipped, exceeds {MAX_IMAGE_BYTES} bytes: {url}", file=sys.stderr)
                return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"WARN: image download failed ({e}): {url}", file=sys.stderr)
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{basename}{ext}"
    (dest_dir / filename).write_bytes(data)
    return filename


def download_gallery_images(payload: dict, dest_dir: Path) -> list[str]:
    """Download the main image plus up to five extra gallery photos."""
    urls = []
    main = str(payload.get("image_url", "") or "").strip()
    if main:
        urls.append(main)
    extra = payload.get("gallery_image_urls") or []
    if isinstance(extra, list):
        urls.extend(str(url or "").strip() for url in extra)

    files = []
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        filename = download_image(url, dest_dir, f"gallery-{len(files) + 1}")
        if filename:
            files.append(filename)
        if len(files) >= 6:
            break
    return files


# Horario que se publica cuando una sucursal no trajo el suyo. Es el mismo
# literal bilingüe que el motor ya usaba para `opening_hours_text`.
HOURS_FALLBACK = "Consultar horarios / Ask us for hours"
# Tope de fotos del mini-lookbook (el mismo del formulario y del generador).
MAX_LOOKBOOK_PHOTOS = 4


# --------------------------------------------------------------------------- #
# CAMPOS DE VERTICAL (`intake.fields` del vertical.yaml)
#
# El worker extrae estos campos como TEXTO tal cual lo contestó el negocio (no
# conoce los catálogos cerrados del giro, y duplicárselos habría sido una
# segunda fuente de verdad). Aquí se convierten en lo que el generador espera,
# leyendo la config de la vertical. Una vertical sin `intake.fields` no ejecuta
# nada de esto: HMU y PawContact construyen su cliente exactamente igual que
# antes.
# --------------------------------------------------------------------------- #

# Las formas en que alguien contesta que sí. Whole-string, no subcadena: "no"
# contiene... nada de esto, pero "sin costo" sí contendría "si".
YES_ANSWERS = frozenset({"si", "yes", "true", "1"})


def yes_no(value) -> bool:
    """Casilla/opción Sí-No -> booleano. Cualquier otra cosa es False."""
    if isinstance(value, bool):
        return value
    return plain_latin(value).strip() in YES_ANSWERS


def _number_or_none(value):
    """El número tal cual lo escribió el negocio, o None si no contestó.

    No se convierte a int a propósito: "8" y "8 años" se imprimen igual de bien
    y forzar el tipo dejaría fuera al segundo.
    """
    text = str(value or "").strip()
    return text or None


def catalog_key(name: str, value) -> str | None:
    """La opción que el negocio eligió en el formulario -> su clave cerrada.

    Se compara por subcadena sin acentos ni mayúsculas contra
    `catalogs.<name>.intake_match`, en el orden declarado — el orden importa
    ("Cita o walk-in" contiene también "walk-in"). Un catálogo sin
    `intake_match` es uno cuya opción YA es la clave (los estados del
    directorio): entonces se exige coincidencia exacta contra `values`.
    """
    cfg = CATALOGS.get(name) or {}
    text = plain_latin(value)
    if text.strip():
        for key, patterns in (cfg.get("intake_match") or {}).items():
            if any(plain_latin(p) in text for p in patterns):
                return key
        raw = str(value or "").strip()
        if raw in (cfg.get("values") or []):
            return raw
    return cfg.get("fallback")


def catalog_keys(name: str, value) -> list[str]:
    """Igual, para un catálogo `kind: list` (casillas múltiples de Tally).

    El worker las entrega ya unidas por coma, así que se busca cada clave
    dentro del texto completo y se devuelven en el orden del catálogo — un
    orden estable, para que dos intakes iguales no produzcan dos JSON distintos.
    """
    cfg = CATALOGS.get(name) or {}
    text = plain_latin(value)
    match = cfg.get("intake_match") or {}
    if match:
        return [key for key, patterns in match.items()
                if any(plain_latin(p) in text for p in patterns)]
    valores = cfg.get("values") or []
    elegidos = {part.strip() for part in str(value or "").split(",")}
    return [v for v in valores if v in elegidos]


def build_intake_fields(payload: dict) -> tuple[dict, dict]:
    """(campos del cliente, los que ADEMÁS viajan dentro de content.<lang>).

    `per_language: true` existe porque un texto compartido entre los dos idiomas
    sale sin traducir en la mitad de las páginas: los tiempos de entrega y la
    política de anticipo de ModaLink se publicaban en español en su página en
    inglés (§7.11 de su auditoría). Duplicarlos por idioma es lo que deja que
    `apply_translation` los traduzca.
    """
    fields = INTAKE.get("fields") or {}
    out: dict = {}
    per_language: dict = {}
    for name, cfg in fields.items():
        kind = cfg.get("kind")
        raw = payload.get(name)
        if kind == "yes_no":
            value = yes_no(raw)
        elif kind == "number":
            value = _number_or_none(raw)
        elif kind == "catalog":
            es_lista = (CATALOGS.get(name) or {}).get("kind") == "list"
            value = catalog_keys(name, raw) if es_lista else catalog_key(name, raw)
        elif kind == "social":
            value = social_url(raw, cfg["base"])
        else:
            value = str(raw or "").strip()
            if cfg.get("max"):
                value = value[: cfg["max"]]
        out[name] = value

    # `requires`: un campo que depende de otro se apaga si ese otro no quedó.
    # Es el caso del directorio: sin un estado válido no hay forma de agrupar ni
    # de filtrar la ficha, así que marcar la casilla no basta.
    for name, cfg in fields.items():
        dep = str(cfg.get("requires", "") or "").strip()
        if dep and not out.get(dep):
            out[name] = False if cfg.get("kind") == "yes_no" else None

    for name, cfg in fields.items():
        if cfg.get("per_language") and out.get(name):
            per_language[name] = out[name]
    return out, per_language


def build_location_hours(payload: dict, total: int) -> list[str]:
    """Un horario por sucursal, alineado por índice (`schema.hours_source`).

    Que la longitud coincida con la de `locations` no es cosmético: el
    generador imprime `location_hours[i]` dentro del bloque de la sucursal `i`,
    así que un hueco publicaría el horario de una tienda en la otra. Por eso se
    rellena con el texto de respaldo en vez de acortarse.
    """
    return [
        str(payload.get(f"location_{i + 1}_hours", "") or "").strip() or HOURS_FALLBACK
        for i in range(total)
    ]


def download_lookbook(payload: dict, dest_dir: Path) -> list[str]:
    """Las fotos del mini-lookbook (`schema.gallery_source: lookbook_urls`).

    Una foto que no se pueda bajar simplemente no viaja — nunca bloquea la
    generación, misma política que el logo y la foto principal.
    """
    urls = payload.get("lookbook_urls")
    urls = urls if isinstance(urls, list) else []
    out = []
    for i, url in enumerate(urls[:MAX_LOOKBOOK_PHOTOS]):
        filename = download_image(str(url or ""), dest_dir, f"lookbook-{i + 1}")
        if filename:
            out.append(filename)
    return out


# --------------------------------------------------------------------------- #
# LINTER DE COPY (`legal.copy_linter` del vertical.yaml)
#
# WARN-ONLY: nunca bloquea la generación. Cuenta las frases rojas del giro y el
# número viaja al correo de entrega (GITHUB_OUTPUT linter_flags -> el workflow
# -> /notify), que le sugiere al negocio revisar su texto con la modificación
# que ya tiene incluida.
#
# Viene de ModaLink (su D8): en moda, que el propio negocio escriba "réplica de"
# o "inspirado en [marca]" en su página es una autoinculpación. Una vertical que
# no declara la lista no lintea nada y no escribe `linter_flags`, así que el
# workflow de HMU/PawContact no cambia de comportamiento.
# --------------------------------------------------------------------------- #

def lint_text(text: str, lang: str) -> list[str]:
    """Las frases rojas presentes en un texto (sin acentos, sin mayúsculas)."""
    frases = (LEGAL.get("copy_linter") or {}).get(lang) or []
    if not text or not frases:
        return []
    plano = plain_latin(text)
    return [frase for frase in frases if plain_latin(frase) in plano]


def lint_content_block(block: dict, lang: str) -> list[str]:
    """Lintea lo que el negocio ESCRIBIÓ: descripción, servicios, destacado,
    políticas. No los textos que pone el motor."""
    hits = lint_text(block.get("short_description", ""), lang)
    for svc in block.get("services") or []:
        if isinstance(svc, dict):
            hits.extend(lint_text(svc.get("name", ""), lang))
            hits.extend(lint_text(svc.get("description", ""), lang))
    featured = block.get("featured_package")
    if isinstance(featured, dict):
        hits.extend(lint_text(featured.get("name", ""), lang))
        hits.extend(lint_text(featured.get("description", ""), lang))
    for policy in block.get("policies") or []:
        hits.extend(lint_text(policy, lang))
    return hits


def lint_client(client: dict) -> list[str]:
    """Los dos idiomas, DESPUÉS de traducir — la traducción también se revisa."""
    content = client.get("content") or {}
    hits: list[str] = []
    for lang in ("es", "en"):
        block = content.get(lang)
        if isinstance(block, dict):
            hits.extend(lint_content_block(block, lang))
    return hits


def build_locations(payload: dict) -> list[dict]:
    """Assemble the full locations list (1-3) from intake fields.

    The generator can render one Google Maps button per location, so keep every
    location that has a name, address, map link or notes.
    """
    first = {
        "name": str(payload.get("location_1_name", "") or "").strip(),
        "address": str(payload.get("address", "") or "").strip(),
        "google_maps_url": url_or_none(payload.get("google_maps_url", "")),
    }
    notes = str(payload.get("location_1_notes", "") or "").strip()
    if notes:
        first["notes"] = notes
    locations = []
    if any(first.values()):
        locations.append(first)
    for i in (2, 3):
        name = str(payload.get(f"location_{i}_name", "") or "").strip()
        address = str(payload.get(f"location_{i}_address", "") or "").strip()
        loc = {"name": name, "address": address}
        maps = url_or_none(payload.get(f"location_{i}_maps_url", ""))
        if maps:
            loc["google_maps_url"] = maps
        notes = str(payload.get(f"location_{i}_notes", "") or "").strip()
        if notes:
            loc["notes"] = notes
        if not any(loc.values()):
            continue
        locations.append(loc)
    locations.extend(parse_additional_locations(payload.get("additional_locations_text", "")))
    return locations


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def clean_line(line: str) -> str:
    return TRAIL_SEP_RE.sub("", BULLET_RE.sub("", line.strip())).strip()


# La moneda ESCRITA, sin símbolo: "680 MXN", "45 pesos". Son formas normales de
# poner un precio en México, y el reconocedor exigía el "$" — así que el precio
# no se reconocía, la tarjeta salía con "Preguntar precio" Y ADEMÁS el precio
# quedaba impreso dentro de la descripción, contradiciéndose (medido 2026-07-26).
_MONEDA = r"mxn|usd|cad|eur|pesos?|d[oó]lares?|dollars?|euros?"
_CIFRA = r"[\d][\d\s,.\u00a0]*"
# Un RANGO es UN precio, no dos campos. Va PRIMERO en la alternancia, y el orden
# es la mitad del arreglo: si ganara `_CON_SIMBOLO`, "$200 - $400" casaría solo
# "$200" y el "- $400" se quedaría de basura en el nombre. Se exigen las DOS
# mitades con la misma forma de precio (las dos con "$", o la pareja cerrada con
# moneda escrita) para no comerse el número de un nombre: en "Paquete 2 — $500"
# el "2" no es el piso de un rango, y ahí el precio sigue siendo "$500".
#
# Este es además el guion que SÍ llega al separador de campos. Los horarios NO
# llegan nunca (se copian tal cual, más abajo); los rangos sí, y reconocerlos
# aquí es lo que deja al separador libre de aceptar el guion normal.
_RANGO = (r"\$" + _CIFRA + r"\s*[-–—]\s*\$" + _CIFRA + r"(?:" + _MONEDA + r")?\b\s*$"
          r"|" + _CIFRA + r"\s*[-–—]\s*" + _CIFRA + r"(?:" + _MONEDA + r")\b\s*$")
# La moneda escrita, ANCLADA AL FINAL a propósito. El formulario enseña el
# precio al final de la línea, y sin el ancla un "incluye shampoo de 200 pesos
# de valor" se convertiría en el precio del servicio. Con "$" el reconocedor ya
# era greedy así; esto NO extiende esa greediness a la moneda escrita.
_MONEDA_ESCRITA = _CIFRA + r"(?:" + _MONEDA + r")\b\s*$"
# Lo que el reconocedor ya aceptaba, intacto: ni una línea que hoy funciona
# cambia de precio.
_CON_SIMBOLO = (r"\$[\d][\d\s,.\u00a0]*(?:mxn|usd|cad|eur)?(?:\s*(?:/|por|per)\s*[\w\s]+)?"
                r"|(?:desde|from|starting at|starts at)\s+\$?[\d][\d\s,.\u00a0]*(?:mxn|usd|cad|eur)?")
_SIN_CIFRA = r"(?:consultar|cotizar|ask us|inquire|quote|varies)"
PRICE_HINT_RE = re.compile(
    "(?i)(?:" + _RANGO + "|" + _CON_SIMBOLO + "|" + _MONEDA_ESCRITA + "|" + _SIN_CIFRA + ")")


# Hasta dónde puede llegar una etiqueta de precio antes de que deje de parecer
# un precio. El tope es una defensa contra convertir una frase entera en precio,
# no parte del reconocedor; por eso se pasa como argumento y no vive dentro.
MAX_PRICE_LABEL = 60
# El DESTACADO admite más, y no por gusto: uno de los cuatro destacados
# publicados (Black Line Tattoo) lleva "$1,200 MXN. Disponible únicamente los
# viernes con cita previa." — 62 caracteres. Con el tope de los servicios ese
# precio DESAPARECERÍA de una página que está vendiendo (medido 2026-07-27,
# reconstruyendo su línea desde su client.json publicado). 120 es el mismo tope
# que la tarjeta ya le aplica a su nombre.
MAX_FEATURED_PRICE_LABEL = 120


def split_price_label(line: str, max_price_len: int = MAX_PRICE_LABEL) -> tuple[str, str | None]:
    """Split one service line into visible name and optional price label."""
    match = None
    for candidate in PRICE_HINT_RE.finditer(line):
        match = candidate
    if not match:
        return line, None
    name = TRAIL_SEP_RE.sub("", line[:match.start()]).strip()
    price = line[match.start():].strip(" -:|")
    if name and price and len(price) <= max_price_len:
        return name, price
    return line, None


def normalize_price_policy(value: str) -> str:
    plain = plain_latin(value)
    if not plain:
        return "show"
    if "dont" in plain or "don't" in plain or "no mostrar" in plain or "sin precios" in plain:
        return "hide"
    if "mixed" in plain or "mixto" in plain:
        return "mixed"
    return "show"


def parse_service_categories(value: str, lang: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return ["Servicios" if lang == "es" else "Services"]
    lines = []
    for raw in text.replace(";", "\n").splitlines():
        for part in raw.split(","):
            line = clean_line(part)
            if line:
                lines.append(line[:80])
    out = []
    for line in lines:
        if line not in out:
            out.append(line)
    return out or ["Servicios" if lang == "es" else "Services"]


def split_category_name_description(text: str, known: dict[str, str]) -> tuple[str | None, str, str]:
    """Split a service line into (category, name, description).

    A leading "Category — " prefix is recognized only when it matches a known
    category (compared via plain_latin); that category is returned and stripped
    from the line. The remaining "Name — Description" is split at the first
    dash into a name and an optional description. A line with no dash keeps the
    whole line as the name and no description (legacy behavior for plain lines).
    """
    segments = [seg.strip() for seg in split_fields(text) if seg.strip()]
    if not segments:
        return None, text.strip(), ""
    category = None
    if len(segments) > 1 and plain_latin(segments[0]) in known:
        category = known[plain_latin(segments[0])]
        segments = segments[1:]
    name = segments[0]
    description = " — ".join(segments[1:])
    return category, name, description


def parse_services(services_text: str, categories: list[str], price_policy: str) -> list[dict]:
    """Each non-empty line is one service; headings ending in ':' become categories.

    A service line may also begin with a "Category — " prefix naming a known
    category: that prefix sets the service's category (and the running category
    for the plain lines that follow) and is stripped from the visible name, and
    any remaining "Name — Description" is split into a name and a description.
    """
    services = []
    current_category = categories[0] if categories else "Services"
    known = {plain_latin(cat): cat for cat in categories}
    for raw in (services_text or "").splitlines():
        line = clean_line(raw)
        if not line:
            continue
        heading = TRAIL_SEP_RE.sub("", line).strip()
        heading_key = plain_latin(heading)
        if raw.strip().endswith(":") or heading_key in known:
            current_category = known.get(heading_key, heading[:80])
            if current_category not in categories:
                categories.append(current_category)
                known[plain_latin(current_category)] = current_category
            continue

        name, price_label = split_price_label(line)
        category, name, description = split_category_name_description(name, known)
        if category:
            current_category = category
        svc = {"category": category or current_category, "name": name[:120]}
        if description:
            svc["description"] = description[:300]
        if price_label and price_policy != "hide":
            svc["price_label"] = price_label
        services.append(svc)
    return services


def parse_policies(policies_text: str) -> list[str]:
    out = []
    for raw in (policies_text or "").splitlines():
        line = clean_line(raw)
        if line:
            out.append(line[:200])
    return out


def _strip_qa_label(value: str) -> str:
    return re.sub(r"(?i)^\s*(?:q|a|pregunta|respuesta|question|answer)\s*[:.-]\s*", "", value).strip()


def parse_faq(faq_text: str) -> list[dict]:
    """Parse FAQ text from flexible Q/A blocks."""
    text = (faq_text or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n+", text) if b.strip()]
    if len(blocks) == 1:
        lines = [clean_line(line) for line in text.splitlines() if clean_line(line)]
        if len(lines) >= 4:
            blocks = ["\n".join(lines[i : i + 2]) for i in range(0, len(lines), 2)]

    out = []
    for block in blocks:
        lines = [clean_line(line) for line in block.splitlines()]
        lines = [_strip_qa_label(line) for line in lines if line]
        if not lines:
            continue
        question = ""
        answer = ""
        if len(lines) == 1:
            line = lines[0]
            qmark = line.find("?")
            if qmark > 0 and qmark < len(line) - 1:
                question = line[: qmark + 1]
                answer = line[qmark + 1 :].strip(" -:|")
            else:
                parts = re.split(r"\s+[-–—|]\s+|\s*:\s+", line, maxsplit=1)
                if len(parts) == 2:
                    question, answer = parts
        else:
            question = lines[0]
            answer = " ".join(lines[1:])
        question = _strip_qa_label(question)[:160]
        answer = _strip_qa_label(answer)[:400]
        if question and answer:
            out.append({"question": question, "answer": answer})
    return out[:8]


def parse_featured(featured_text: str) -> dict | None:
    lines = [clean_line(l) for l in (featured_text or "").splitlines()]
    lines = [l for l in lines if l]
    if not lines:
        return None
    first = lines[0]
    # EL MISMO reconocedor de precio que los servicios, no uno propio. Tenía el
    # suyo —partir por el ÚLTIMO "$"— y por eso los arreglos del precio no le
    # llegaban: "185 USD" no era precio (se quedaba de descripción) y el rango
    # "$200 - $400" se partía en un precio "$400" y una descripción "$200".
    name, price_label = split_price_label(first, MAX_FEATURED_PRICE_LABEL)
    # A "Name — Description" first line splits at the first dash into a short
    # name and an inline description; any following lines extend it.
    inline_description = ""
    parts = split_fields(name, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        name, inline_description = parts[0].strip(), parts[1].strip()
    description = " ".join(part for part in [inline_description, *lines[1:]] if part)
    featured = {"name": name[:120], "description": description[:300]}
    if price_label:
        featured["price_label"] = price_label
    return featured


def parse_additional_locations(locations_text: str) -> list[dict]:
    """Parse extra location blocks from a flexible textarea.

    Preferred format is one location per blank-line-separated block:
    name, address, Google Maps link, notes. If the customer only writes text,
    keep it as a note so it can still appear on the page.
    """
    text = (locations_text or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n+", text) if b.strip()]
    if len(blocks) == 1:
        lines = [clean_line(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > 4:
            blocks = lines

    out = []
    url_re = re.compile(r"(https?://\S+|(?:maps\.app\.goo\.gl|goo\.gl/maps|google\.com/maps)/\S+)", re.I)
    for block in blocks:
        lines = [clean_line(line) for line in block.splitlines()]
        lines = [line for line in lines if line]
        if not lines:
            continue
        maps_url = None
        clean_lines = []
        for line in lines:
            match = url_re.search(line)
            if match and not maps_url:
                maps_url = url_or_none(match.group(1).rstrip(").,;"))
                line = url_re.sub("", line).strip(" -:|")
            if line:
                clean_lines.append(line)

        loc = {}
        if len(clean_lines) >= 1:
            loc["name"] = clean_lines[0][:120]
        if len(clean_lines) >= 2:
            loc["address"] = clean_lines[1][:200]
        if len(clean_lines) >= 3:
            loc["notes"] = " ".join(clean_lines[2:])[:200]
        if len(clean_lines) == 1 and maps_url:
            loc["address"] = loc.pop("name")
        if maps_url:
            loc["google_maps_url"] = maps_url
        if loc:
            out.append(loc)
    return out


def parse_public_link(value: str) -> dict | None:
    text = (value or "").strip()
    if not text:
        return None
    url_re = re.compile(r"(https?://\S+|[\w.-]+\.[a-z]{2,}(?:/\S*)?)", re.I)
    match = url_re.search(text)
    if not match:
        return None
    url = url_or_none(match.group(1).rstrip(").,;"))
    if not url:
        return None
    label = text[:match.start()].strip(" -:|") or text[match.end():].strip(" -:|")
    # Leave the label empty when the user gave no explicit label; the generator
    # fills a language-appropriate default per page (Portafolio/Open link/…),
    # so a hardcoded English label never leaks onto the Spanish page.
    return {"label": label[:80], "url": url}


def parse_public_links(value: str, default_label: str) -> list[dict]:
    text = (value or "").strip()
    if not text:
        return []
    lines = [clean_line(line) for line in text.replace(";", "\n").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        lines = [text]
    out = []
    seen = set()
    for line in lines:
        link = parse_public_link(line)
        if not link:
            continue
        if not link["label"]:
            link["label"] = default_label
        key = link["url"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
    return out[:6]


def url_or_none(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if not v.lower().startswith(("http://", "https://")):
        v = "https://" + v
    return v


def social_url(value: str, base: str, handle_prefix: str = "") -> str | None:
    """Accepts a full URL, a domain path (instagram.com/x) or a bare handle
    (@unveilmexico / unveilmexico) and returns a valid profile URL."""
    v = (value or "").strip()
    if not v:
        return None
    if v.lower().startswith(("http://", "https://")):
        return v
    if "." in v.split("/")[0]:  # looks like a domain: instagram.com/x, www...
        return "https://" + v
    handle = v.lstrip("@").strip("/").split("/")[0]
    if not handle:
        return None
    return f"https://{base}/{handle_prefix}{handle}"


def normalize_business_type(value: str) -> str | None:
    """Map a business-type answer (Tally dropdown option or free text) to a
    closed category. Whole-word matching (substring matching classified
    "Barbería"/"Barbershop" as food via "bar" and "Spanish" as beauty via
    "spa"); the category with the most matching words wins, ties break by
    the order below (pets before beauty so "Estética para mascotas" → pets).
    """
    plain = plain_latin(value)
    if not plain:
        return None
    words = set(re.findall(r"[a-z]+", plain))
    checks = (
        ("food", ("restaurant", "restaurante", "cafe", "coffee", "bar", "food", "comida", "bakery", "panaderia", "cocina", "taqueria")),
        ("fitness", ("fitness", "gym", "gimnasio", "yoga", "pilates", "class", "classes", "clase", "clases", "taller", "talleres", "workshop", "workshops", "training", "entrenamiento", "danza", "dance", "crossfit")),
        ("tours", ("tour", "tours", "experience", "experiences", "experiencia", "experiencias", "travel", "viaje", "viajes", "actividad", "actividades", "excursion", "excursiones")),
        ("pets", ("pet", "pets", "mascota", "mascotas", "grooming", "dog", "perro", "perros", "veterinaria", "veterinario")),
        ("creative", ("creative", "creativo", "photography", "fotografia", "photo", "photographer", "fotografo", "artist", "artista", "design", "diseno", "designer", "tattoo", "tattoos", "tatuaje", "tatuajes", "piercing", "arte")),
        ("wellness", ("wellness", "bienestar", "spa", "therapy", "terapia", "therapist", "terapeuta", "massage", "massages", "masaje", "masajes", "holistic", "holistico")),
        ("beauty", ("beauty", "belleza", "salon", "barbershop", "barber", "barberia", "nails", "unas", "lashes", "pestanas", "brows", "cejas", "hair", "cabello", "facial", "facials", "faciales", "estetica", "makeup", "maquillaje")),
        ("professional", ("professional", "profesional", "profesionales", "consulting", "consultant", "consultor", "consultora", "consultoria", "consult", "coach", "coaching", "clinic", "clinica", "lawyer", "abogado", "accountant", "contador")),
        ("retail", ("retail", "tienda", "store", "boutique", "shop", "producto", "products", "productos")),
    )
    best_category = None
    best_score = 0
    for category, needles in checks:
        score = sum(1 for needle in needles if needle in words)
        if score > best_score:
            best_category, best_score = category, score
    return best_category or "general"


def _per_language_field_names() -> tuple[str, ...]:
    """Campos de vertical que viven DENTRO de content.<lang> (`per_language`)."""
    return tuple(
        name for name, cfg in (INTAKE.get("fields") or {}).items()
        if cfg.get("per_language")
    )


LANG_NAMES = {"es": "Spanish", "en": "English"}

SPANISH_MARKER_WORDS = {
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

SPANISH_MARKER_PHRASES = (
    " al menos ",
    " antes de tu ",
    " dia de muertos",
    " por persona",
    " para que ",
    " tu experiencia",
)

ENGLISH_MARKER_WORDS = {
    "and",
    "are",
    "at",
    "available",
    "before",
    "booking",
    "for",
    "from",
    "hours",
    "of",
    "or",
    "our",
    "please",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
    "your",
}

ENGLISH_MARKER_PHRASES = (
    " at least ",
    " for the ",
    " in the ",
    " of the ",
    " per person",
    " to the ",
    " we recommend ",
    " your ",
)


def plain_latin(value) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower()


def spanish_signal_score(text: str) -> int:
    plain = f" {plain_latin(text)} "
    score = sum(2 for phrase in SPANISH_MARKER_PHRASES if phrase in plain)
    words = set(re.findall(r"[a-z]+", plain))
    score += sum(1 for word in SPANISH_MARKER_WORDS if word in words)
    return score


def english_signal_score(text: str) -> int:
    plain = f" {plain_latin(text)} "
    score = sum(2 for phrase in ENGLISH_MARKER_PHRASES if phrase in plain)
    words = set(re.findall(r"[a-z]+", plain))
    score += sum(1 for word in ENGLISH_MARKER_WORDS if word in words)
    return score


def content_text(block: dict) -> str:
    parts = [
        block.get("short_description", ""),
        block.get("opening_hours_text", ""),
        block.get("service_area_text", ""),
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


def translate_block(source_lang: str, target_lang: str, fields: dict) -> dict | None:
    """Translate short_description/services/policies/featured via OpenAI.

    `fields` is a flat dict of translatable strings/lists (see call site).
    Returns a dict with the same keys translated, or None if OPENAI_API_KEY
    is missing or the call/response is unusable. Never raises: a translation
    problem must fall back to publishing the source-language text, not block
    page generation.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    prompt = (
        f"Translate the following small-business page content from "
        f"{LANG_NAMES[source_lang]} to {LANG_NAMES[target_lang]}. Keep prices, "
        f"numbers, phone numbers and proper nouns (business names, brand names) "
        f"unchanged. Return ONLY a JSON object with exactly the same keys and "
        f"structure as the input, with translated string values.\n\n"
        f"{json.dumps(fields, ensure_ascii=False)}"
    )
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        translated = json.loads(content)
        return translated if isinstance(translated, dict) else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        print(f"WARN: translation failed ({e}); publishing source-language text for {target_lang}", file=sys.stderr)
        return None


def apply_translation(content: dict, source_lang: str, target_lang: str) -> None:
    """Translate content[target_lang] in place from content[source_lang].

    Every translated field is validated (type + shape) before being applied;
    anything missing or malformed just keeps the original source-language
    fallback text already in content[target_lang].
    """
    src = content[source_lang]
    fields = {
        "short_description": src["short_description"],
        # `.get`: una vertical con horario POR SUCURSAL no trae el campo único
        # (ver `schema.hours_source`); sus horarios se traducen abajo, en lista.
        "opening_hours_text": src.get("opening_hours_text", ""),
        "service_area_text": src.get("service_area_text", ""),
        "client_care_text": src.get("client_care_text", ""),
        "reservations_text": src.get("reservations_text", ""),
        "class_schedule_text": src.get("class_schedule_text", ""),
        "tour_details_text": src.get("tour_details_text", ""),
        "pet_notes_text": src.get("pet_notes_text", ""),
        "service_categories": src.get("service_categories", []),
        "service_names": [s["name"] for s in src["services"]],
        "service_descriptions": [s.get("description", "") for s in src["services"]],
        "policies": src["policies"],
        "faq_questions": [item["question"] for item in src.get("faq", [])],
        "faq_answers": [item["answer"] for item in src.get("faq", [])],
    }
    if "featured_package" in src:
        fields["featured_name"] = src["featured_package"]["name"]
        fields["featured_description"] = src["featured_package"].get("description", "")
    # Horario por sucursal y textos de vertical marcados `per_language`. Solo
    # viajan si el bloque los trae, así que para HMU/PawContact el prompt es
    # exactamente el mismo de antes.
    if isinstance(src.get("location_hours"), list):
        fields["location_hours"] = src["location_hours"]
    for name in _per_language_field_names():
        if src.get(name):
            fields[name] = src[name]

    translated = translate_block(source_lang, target_lang, fields)
    if not translated:
        return

    tgt = content[target_lang]
    hours = translated.get("location_hours")
    if (
        isinstance(hours, list)
        and isinstance(tgt.get("location_hours"), list)
        and len(hours) == len(tgt["location_hours"])
        and all(isinstance(h, str) for h in hours)
    ):
        tgt["location_hours"] = [h[:200] for h in hours]
    for name in _per_language_field_names():
        if name in tgt and isinstance(translated.get(name), str) and translated[name].strip():
            tgt[name] = translated[name][:200]
    if isinstance(translated.get("short_description"), str) and translated["short_description"].strip():
        tgt["short_description"] = translated["short_description"][:300]
    if isinstance(translated.get("opening_hours_text"), str) and translated["opening_hours_text"].strip():
        tgt["opening_hours_text"] = translated["opening_hours_text"][:200]
    if isinstance(translated.get("service_area_text"), str) and translated["service_area_text"].strip():
        tgt["service_area_text"] = translated["service_area_text"][:200]
    if isinstance(translated.get("client_care_text"), str) and translated["client_care_text"].strip():
        tgt["client_care_text"] = translated["client_care_text"][:200]
    if isinstance(translated.get("reservations_text"), str) and translated["reservations_text"].strip():
        tgt["reservations_text"] = translated["reservations_text"][:200]
    if isinstance(translated.get("class_schedule_text"), str) and translated["class_schedule_text"].strip():
        tgt["class_schedule_text"] = translated["class_schedule_text"][:300]
    if isinstance(translated.get("tour_details_text"), str) and translated["tour_details_text"].strip():
        tgt["tour_details_text"] = translated["tour_details_text"][:400]
    if isinstance(translated.get("pet_notes_text"), str) and translated["pet_notes_text"].strip():
        tgt["pet_notes_text"] = translated["pet_notes_text"][:300]

    names = translated.get("service_names")
    if isinstance(names, list) and len(names) == len(tgt["services"]):
        for svc, name in zip(tgt["services"], names):
            if isinstance(name, str) and name.strip():
                svc["name"] = name[:120]

    descriptions = translated.get("service_descriptions")
    if isinstance(descriptions, list) and len(descriptions) == len(tgt["services"]):
        for svc, description in zip(tgt["services"], descriptions):
            if isinstance(description, str) and description.strip():
                svc["description"] = description[:300]

    cats = translated.get("service_categories")
    if isinstance(cats, list) and len(cats) == len(src.get("service_categories", [])):
        if all(isinstance(c, str) for c in cats):
            old_to_new = {}
            for old, new in zip(tgt.get("service_categories", []), cats):
                if str(new).strip():
                    old_to_new[old] = str(new).strip()[:80]
            tgt["service_categories"] = [old_to_new.get(c, c) for c in tgt.get("service_categories", [])]
            for svc in tgt.get("services", []):
                if isinstance(svc, dict) and svc.get("category") in old_to_new:
                    svc["category"] = old_to_new[svc["category"]]

    pols = translated.get("policies")
    if isinstance(pols, list) and len(pols) == len(tgt["policies"]):
        if all(isinstance(p, str) for p in pols):
            tgt["policies"] = [p[:200] for p in pols]

    faq_questions = translated.get("faq_questions")
    faq_answers = translated.get("faq_answers")
    faq = tgt.get("faq", [])
    if (
        isinstance(faq_questions, list)
        and isinstance(faq_answers, list)
        and len(faq_questions) == len(faq)
        and len(faq_answers) == len(faq)
    ):
        for item, question, answer in zip(faq, faq_questions, faq_answers):
            if isinstance(item, dict) and isinstance(question, str) and question.strip():
                item["question"] = question[:160]
            if isinstance(item, dict) and isinstance(answer, str) and answer.strip():
                item["answer"] = answer[:400]

    if "featured_package" in tgt:
        if isinstance(translated.get("featured_name"), str) and translated["featured_name"].strip():
            tgt["featured_package"]["name"] = translated["featured_name"][:120]
        if isinstance(translated.get("featured_description"), str):
            tgt["featured_package"]["description"] = translated["featured_description"][:300]


# --------------------------------------------------------------------------- #
# CATÁLOGO (plantilla `catalog`) — F6 bloque 2
#
# Un catálogo no se arma como un menú de servicios, y su intake llega por dos
# caminos distintos (ver verticals/catalog/tally_form.yaml):
#
#   PRELLENADO — el negocio compró su vista previa. Sus ~20 productos con fotos
#   NO viajan en el formulario: viven en Cory y se bajan aquí por slug. El
#   formulario solo aporta lo que el cliente decidió (botón de venta, nombre,
#   idioma) y eso MANDA sobre lo que trae el prospecto.
#
#   ORGÁNICO — llegó por la tienda y capturó lo suyo: una línea por producto y
#   sus fotos subidas a Tally.
#
# Todo lo de aquí abajo FALLA CERRADO. Es la misma regla que trajo el commit
# 6028b92: aquí ya hay dinero cobrado, así que publicar en silencio un catálogo
# vacío, o con los productos de otro negocio, es peor que no publicar nada —
# nadie se entera hasta que el cliente reclama. Un fallo dispara la alerta de
# embudo muerto del workflow, que sí llega a Vero.
# --------------------------------------------------------------------------- #

def fetch_prospect_catalog(slug: str) -> dict:
    """Baja el `payload.json` que Cory publicó junto a la vista previa."""
    if not PROSPECT_PAYLOAD_BASE:
        fail("PROSPECT_PAYLOAD_BASE_URL no está configurada y este intake viene de "
             "una vista previa: no hay de dónde traer los productos")
    url = f"{PROSPECT_PAYLOAD_BASE}/{slug}/payload.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "catalog-generator/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read(2 * 1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        fail(f"no pude leer el catálogo del prospecto ({e}): {url}")
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        fail(f"el catálogo del prospecto no trae products: {url}")
    return data


def stage_catalog_photos(products: list[dict], photo_base_url: str, slug: str) -> Path:
    """Descarga las fotos del catálogo a una carpeta local y devuelve su ruta.

    El generador ya sabe copiar fotos locales a la página (`photo_source_dir` +
    `image` relativo). Bajarlas en vez de dejar la URL de Cory es deliberado: si
    la página del cliente enlazara las fotos del prospector, su catálogo dejaría
    de tener fotos el día que esa vista previa expire o se borre — y el cliente
    ya pagó por una página que es suya.

    Una foto que no se pueda bajar deja el producto sin imagen (tarjeta con
    placeholder), nunca tumba la construcción: mejor un catálogo con 19 fotos
    que ningún catálogo.
    """
    base = str(photo_base_url or "").rstrip("/")
    dest = PHOTO_STAGING_DIR / slug
    if dest.exists():
        shutil.rmtree(dest)
    for p in products:
        name = str(p.get("image", "") or "").strip()
        if not name:
            continue
        got = download_image(f"{base}/{name}", dest, Path(name).stem)
        p["image"] = got or ""
        if not got:
            print(f"WARN: producto sin foto tras el intento de descarga: {name}", file=sys.stderr)
    return dest


def parse_catalog_products(products_text: str, image_files: list[str], lang: str) -> list[dict]:
    """Camino ORGÁNICO: una línea por producto -> productos del catálogo.

    Formato pedido en el formulario: `Categoría — Nombre — Descripción — $precio`.
    Se tolera de menos (solo el nombre) porque un cliente que ya pagó no puede
    quedarse sin página por no haber usado las rayas: lo que falte se rellena con
    un valor honesto, nunca inventado. El precio se detecta con el mismo
    reconocedor de service-menu; sin precio, el producto sale con "Preguntar
    precio", que en este nicho es la NORMA y no un hueco (0 de 88 productos
    reales traían precio).

    Las fotos se asignan por ORDEN: la n-ésima foto subida es la del n-ésimo
    producto. Es la única correspondencia que el formulario puede expresar.
    """
    default_cat = "Productos" if lang == "es" else "Products"
    out: list[dict] = []
    for raw in (products_text or "").splitlines():
        line = clean_line(raw)
        if not line:
            continue
        rest, price_label = split_price_label(line)
        segments = [s for s in (seg.strip() for seg in split_fields(rest)) if s]
        if not segments:
            continue
        if len(segments) == 1:
            category, name, description = default_cat, segments[0], ""
        elif len(segments) == 2:
            category, name, description = segments[0], segments[1], ""
        else:
            category, name, description = segments[0], segments[1], " — ".join(segments[2:])
        product = {
            "category": category[:80],
            "name_es": name[:120],
            "name_en": name[:120],
            "ask_price": not price_label,
            "image": "",
        }
        if description:
            product["description_es"] = description[:300]
            product["description_en"] = description[:300]
        if price_label:
            product["price_label"] = price_label[:60]
        out.append(product)
        if len(out) >= 20:
            break
    for product, filename in zip(out, image_files):
        product["image"] = filename
    return out


def translate_catalog(products: list[dict], short_description: str,
                      source_lang: str, target_lang: str) -> str:
    """Traduce nombres/descripciones de producto al otro idioma, IN PLACE.

    La página es bilingüe siempre (el generador exige name_es y name_en), así
    que sin esto la mitad de los visitantes ven el catálogo en el idioma que no
    hablan. Si no hay llave de OpenAI o la respuesta no cuadra, cada campo se
    queda con el texto original — misma política que service-menu: una
    traducción fallida nunca bloquea la publicación. Devuelve la descripción
    corta traducida (o la original)."""
    fields = {
        "short_description": short_description,
        "product_names": [p["name_es"] for p in products],
        "product_descriptions": [p.get("description_es", "") for p in products],
    }
    translated = translate_block(source_lang, target_lang, fields)
    if not translated:
        return short_description

    names = translated.get("product_names")
    if isinstance(names, list) and len(names) == len(products):
        for product, name in zip(products, names):
            if isinstance(name, str) and name.strip():
                product["name_en" if target_lang == "en" else "name_es"] = name[:120]
    descriptions = translated.get("product_descriptions")
    if isinstance(descriptions, list) and len(descriptions) == len(products):
        for product, description in zip(products, descriptions):
            if isinstance(description, str) and description.strip():
                product["description_en" if target_lang == "en" else "description_es"] = description[:300]
    out = translated.get("short_description")
    return out[:300] if isinstance(out, str) and out.strip() else short_description


def build_catalog_client(payload: dict, slug: str) -> tuple[dict, Path | None]:
    """Intake de catálogo -> `catalog_payload_public` v1 + carpeta de fotos."""
    prospect_slug = str(payload.get("prospect_slug", "") or "").strip()
    if prospect_slug and not CATALOG_SLUG_RE.match(prospect_slug):
        fail(f"prospect_slug con forma inválida: {prospect_slug!r}")

    products_text = str(payload.get("products_text", "") or "").strip()
    default_language = payload.get("default_language")
    if default_language not in ("es", "en"):
        default_language = "es"
    other_language = "en" if default_language == "es" else "es"

    prospect = fetch_prospect_catalog(prospect_slug) if prospect_slug else {}
    photo_dir: Path | None = None

    if prospect:
        products = [dict(p) for p in prospect["products"]]
        photo_dir = stage_catalog_photos(products, prospect.get("photo_base_url", ""), slug)
    elif products_text:
        # Las fotos del camino orgánico viven en Tally; se bajan igual que el
        # logo, y con el mismo cerco de host.
        photo_dir = PHOTO_STAGING_DIR / slug
        if photo_dir.exists():
            shutil.rmtree(photo_dir)
        files = []
        for i, url in enumerate(payload.get("product_image_urls") or []):
            got = download_image(str(url or ""), photo_dir, f"producto-{i + 1}")
            if got:
                files.append(got)
        products = parse_catalog_products(products_text, files, default_language)
    else:
        fail("el intake no trae productos ni vista previa de la que tomarlos — "
             "revisión manual antes de publicar")

    if not products:
        fail("no pude leer ni un producto del intake — revisión manual")

    # Precedencia: lo que el cliente contestó MANDA sobre lo que trae su vista
    # previa. Es su última palabra, y es lo único por lo que se le preguntó.
    business_name = str(payload.get("business_name", "")).strip() or str(
        prospect.get("business_name", "")).strip()
    # El estilo es la excepción: en el camino prellenado no se le pregunta (ya se
    # lo asignó la fábrica por nicho y color), así que el worker manda su
    # fallback marcado con style_unmapped. Ese marcador es justo la señal de "no
    # eligió": entonces gana el de la vista previa, que es el que ya vio.
    brand_style = payload.get("brand_style")
    if payload.get("style_unmapped") and prospect.get("brand_style"):
        brand_style = prospect["brand_style"]

    short_description = str(payload.get("short_description", "")).strip()
    if not short_description:
        block = (prospect.get("content") or {}).get(default_language) or {}
        short_description = str(block.get("short_description", "")).strip()

    address = str(payload.get("address", "")).strip() or str(prospect.get("address", "")).strip()

    other_description = translate_catalog(
        products, short_description, default_language, other_language)

    logo_file = download_image(payload.get("logo_url", ""), LINKS_DIR / slug / "assets", "logo")

    client = {
        "public_slug": slug,
        "default_language": default_language,
        "brand_style": brand_style,
        "business_name": business_name[:120],
        "sale_button": payload.get("sale_button") or {},
        "address": address[:200],
        "logo_url": f"{CLIENT_BASE_URL}/{slug}/assets/{logo_file}" if logo_file else None,
        "content": {
            default_language: {"short_description": short_description[:300]},
            other_language: {"short_description": other_description[:300]},
        },
        "products": products,
    }
    # rating/rating_count son de Google y ya salían en la vista previa que el
    # negocio compró; se conservan para que su página no pierda la prueba social
    # que vio. Solo si vienen del prospecto: el formulario no los pregunta.
    for field in ("rating", "rating_count"):
        if prospect.get(field) is not None:
            client[field] = prospect[field]
    if photo_dir is not None:
        client["photo_source_dir"] = str(photo_dir)

    # Lo que ESTA vertical agrega al esquema (`intake.fields`), igual que la rama
    # de service-menu. Vacío para toda vertical que no abra la sección.
    #
    # LA SEGUNDA PUERTA (linkFactory/17, 2026-07-27). El catálogo perdía
    # `photo_rights_confirmed` en DOS lugares, no en uno: el worker no lo emitía
    # (su rama era cerrada) y esta función retornaba antes de que
    # `build_intake_fields` corriera nunca. Arreglar solo el worker habría hecho
    # que el campo llegara al payload para tirarse aquí mismo — la forma exacta
    # de un arreglo que no arregla nada. Por eso son SIEMPRE dos declaraciones:
    # el alias en `tally-field-aliases.json` (el worker lo extrae) y esta
    # sección en el `vertical.yaml` (el client.json lo guarda).
    #
    # `per_language` NO se usa aquí: la plantilla catalog arma su `content.<lang>`
    # con `short_description` y nada más, y meterle claves sueltas dejaría al
    # generador del catálogo con un bloque que no sabe pintar. Una vertical de
    # catálogo que declare `per_language: true` no rompe nada — el campo queda
    # igual en la raíz, que es donde el generador lo lee.
    vertical_fields, _ = build_intake_fields(payload)
    # Un nombre que el motor ya escribe se rechaza contra el cliente REAL recién
    # construido (misma guarda que la rama de service-menu): pisarlo dejaría a un
    # cliente que ya pagó sin ese dato y sin un solo error.
    chocan = sorted(set(vertical_fields) & set(client))
    if chocan:
        fail(f"intake.fields de esta vertical redefine campos del motor: {chocan}")
    client.update(vertical_fields)
    return client, photo_dir


def previous_client(slug: str) -> dict:
    """El `client.json` ya publicado de este slug, o `{}` si es un alta nueva."""
    return load_previous_client(CLIENTS_DIR / f"{slug}.client.json")


def merge_with_existing(client: dict, previous: dict, payload: dict, cleared: set[str]) -> dict:
    """Fusiona lo recién construido sobre la página que ya estaba publicada.

    El porqué de la fusión y el mecanismo de borrado explícito están en
    `merge_intake.py`.
    """
    if not previous:
        return client
    print(f"merge: regeneración sobre una página ya publicada "
          f"(campos contestados: {len(answered_keys(payload, cleared))})")
    return merge_client(previous, client, payload, cleared)


def main() -> int:
    raw = os.environ.get("INTAKE_PAYLOAD", "")
    if not raw:
        fail("INTAKE_PAYLOAD env var is empty")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"INTAKE_PAYLOAD is not valid JSON: {e}")

    order_id = str(event.get("order_id", "")).strip()
    slug = str(event.get("slug", "")).strip()
    payload = event.get("public_payload") or {}

    if not slug or not SLUG_RE.match(slug):
        fail(f"invalid or missing slug")
    # Los tokens de borrado explícito salen del payload ANTES de cualquier
    # validación o construcción: así "BORRAR" nunca acaba impreso en la página,
    # y un borrado sobre un campo obligatorio (business_name) cae en la guarda
    # de abajo en vez de publicar una página sin nombre.
    payload, cleared = sanitize_intake(payload)
    if not isinstance(payload, dict) or not payload.get("business_name"):
        fail("public_payload missing business_name")

    previous = previous_client(slug)

    if TEMPLATE == "catalog":
        client, _ = build_catalog_client(payload, slug)
        client = merge_with_existing(client, previous, payload, cleared)
        return publish(client, slug, order_id)

    default_language = payload.get("default_language")
    if default_language not in ("es", "en"):
        default_language = "es"

    price_policy = normalize_price_policy(payload.get("price_display", ""))
    categories_es = parse_service_categories(payload.get("service_categories_text", ""), "es")
    categories_en = parse_service_categories(payload.get("service_categories_text", ""), "en")
    services_es = parse_services(payload.get("services_text", ""), categories_es, price_policy)
    services_en = parse_services(payload.get("services_text", ""), categories_en, price_policy)
    # Sin servicios NO se publica una página nueva. Pero al REGENERAR una que ya
    # existe, un `services_text` vacío es justo el caso que la fusión atiende
    # ("no cambies mis servicios"), así que la guarda se la deja a la fusión: si
    # de verdad no hubiera servicios anteriores, el generador vuelve a rechazar
    # el cliente más abajo.
    if not services_es and not previous:
        fail("intake has no parseable services — manual review required")

    policies = parse_policies(payload.get("policies_text", ""))
    faq = parse_faq(payload.get("faq_text", ""))
    featured = parse_featured(payload.get("featured_text", ""))
    if featured and price_policy == "hide":
        featured.pop("price_label", None)

    short_description = str(payload.get("short_description", "")).strip() or str(payload.get("business_name", "")).strip()
    hours = str(payload.get("opening_hours_text", "")).strip() or "Consultar horarios / Ask us for hours"
    service_area = str(payload.get("service_area_text", "")).strip()
    client_care = str(payload.get("client_care_text", "")).strip()
    reservations = str(payload.get("reservations_text", "")).strip()
    class_schedule = str(payload.get("class_schedule_text", "")).strip()
    tour_details = str(payload.get("tour_details_text", "")).strip()
    pet_notes = str(payload.get("pet_notes_text", "")).strip()
    address = str(payload.get("address", "")).strip()

    # Lo que ESTA vertical agrega al esquema (`intake.fields`). Vacío para toda
    # vertical que no abra la sección, o sea para todas menos ModaLink hoy.
    vertical_fields, per_language_fields = build_intake_fields(payload)

    locations = build_locations(payload)
    # Sin sucursales NO se publica una página nueva en una vertical que exige al
    # menos una (`schema.locations_min`). Al REGENERAR, venir en blanco es justo
    # lo que la fusión atiende, así que la guarda se la deja a ella.
    if SCHEMA.get("locations_min") and not locations and not previous:
        fail("no valid locations in intake — manual review required")
    # `hours_source` decide DÓNDE viven los horarios: el `opening_hours_text`
    # único de siempre, o uno por sucursal dentro de content.<lang>. En el
    # segundo caso el campo único no se emite — el generador ya no lo exige y
    # dejarlo vacío imprimiría una fila de horarios en blanco.
    per_location_hours = SCHEMA.get("hours_source") == "location_hours"
    location_hours = build_location_hours(payload, len(locations)) if per_location_hours else []

    def content_block(lang: str) -> dict:
        # Each language gets its own copies of the mutable structures: later,
        # apply_translation() overwrites content[target_lang] in place and
        # must never affect content[source_lang]'s original text.
        block = {
            "short_description": short_description[:300],
            "address": address[:200],
            # Se escribe EN ESTE LUGAR (y no al final) para que el JSON de una
            # vertical con horario único salga con las claves en el mismo orden
            # que antes: `json.dumps` respeta el orden de inserción y el
            # client.json de HMU/PawContact no tiene por qué moverse.
            **({"location_hours": list(location_hours)} if per_location_hours
               else {"opening_hours_text": hours[:200]}),
            "service_area_text": service_area[:200],
            "client_care_text": client_care[:200],
            "reservations_text": reservations[:200],
            "class_schedule_text": class_schedule[:300],
            "tour_details_text": tour_details[:400],
            "pet_notes_text": pet_notes[:300],
            "price_display": price_policy,
            "service_categories": list(categories_es if lang == "es" else categories_en),
            "services": [dict(s) for s in (services_es if lang == "es" else services_en)],
            "policies": list(policies),
            "faq": [dict(item) for item in faq],
        }
        block.update(per_language_fields)
        if featured:
            block["featured_package"] = dict(featured)
        return block

    assets_dir = LINKS_DIR / slug / "assets"
    logo_file = download_image(payload.get("logo_url", ""), assets_dir, "logo")
    # De dónde salen las fotos de la banda del héroe lo decide la vertical
    # (`schema.gallery_source`). Con `lookbook_urls` la foto principal y el
    # mini-lookbook son dos cosas distintas y se publican con sus propios
    # nombres (hero / lookbook-N), que es como ya están las páginas vivas.
    lookbook_gallery = SCHEMA.get("gallery_source") == "lookbook_urls"
    if lookbook_gallery:
        hero_file = download_image(payload.get("image_url", ""), assets_dir, "hero")
        lookbook_files = download_lookbook(payload, assets_dir)
        gallery_files = []
    else:
        hero_file = None
        lookbook_files = []
        gallery_files = download_gallery_images(payload, assets_dir)

    # Pass an empty default so unlabeled links keep an empty label; each page
    # then renders the localized group label via s["delivery_pickup_label"]
    # (Entrega / recoger — Delivery / pickup) instead of a frozen English one.
    delivery_pickup_links = parse_public_links(
        payload.get("delivery_pickup_links_text", ""), ""
    )
    # An unlabeled portfolio link keeps its empty label so each page renders the
    # localized heading (Portafolio / Portfolio) via s["portfolio_label"].
    portfolio_link = parse_public_link(payload.get("portfolio_link", ""))

    client = {
        "public_slug": slug,
        "default_language": default_language,
        "brand_style": payload.get("brand_style", "warm-sand"),
        "business_type": normalize_business_type(payload.get("business_type", "")),
        "business_name": str(payload.get("business_name", "")).strip()[:120],
        "logo_url": f"{CLIENT_BASE_URL}/{slug}/assets/{logo_file}" if logo_file else None,
        "primary_image_url": (
            f"{CLIENT_BASE_URL}/{slug}/assets/{hero_file or (gallery_files[0] if gallery_files else '')}"
            if (hero_file or gallery_files) else None
        ),
        **({"lookbook_urls": [f"{CLIENT_BASE_URL}/{slug}/assets/{f}" for f in lookbook_files]}
           if lookbook_gallery
           else {"gallery_images": [{"url": f"{CLIENT_BASE_URL}/{slug}/assets/{filename}"}
                                    for filename in gallery_files]}),
        "whatsapp": str(payload.get("whatsapp", "")).strip() or None,
        "phone": str(payload.get("phone", "")).strip() or None,
        "public_email": str(payload.get("public_email", "")).strip() or None,
        "instagram": social_url(payload.get("instagram", ""), "instagram.com"),
        "facebook": social_url(payload.get("facebook", ""), "facebook.com"),
        "tiktok": social_url(payload.get("tiktok", ""), "www.tiktok.com", handle_prefix="@"),
        "website": url_or_none(payload.get("website", "")),
        "booking_url": url_or_none(payload.get("booking_url", "")),
        "primary_cta": normalize_primary_cta(payload.get("primary_cta", "")),
        "google_maps_url": url_or_none(payload.get("google_maps_url", "")),
        "google_reviews_url": url_or_none(payload.get("google_reviews_url", "")),
        "other_public_link": parse_public_link(payload.get("other_public_link", "")),
        "delivery_pickup_links": delivery_pickup_links,
        "portfolio_link": portfolio_link,
        "content": {"es": content_block("es"), "en": content_block("en")},
    }
    if locations:
        client["locations"] = locations
    # Lo del giro (specialty, formas de pago, directorio…) se aplica ENCIMA. Un
    # nombre que el motor ya escribe se rechaza aquí y no en la config: esta
    # comparación es contra el cliente REAL que se acaba de construir, así que
    # no puede quedarse corta el día que el motor aprenda un campo nuevo —
    # `RESERVED_CLIENT_FIELDS` es el aviso temprano, esto es la guarda.
    chocan = sorted(set(vertical_fields) & set(client))
    if chocan:
        fail(f"intake.fields de esta vertical redefine campos del motor: {chocan}")
    client.update(vertical_fields)

    # The intake is usually authored in default_language, but not always: a
    # customer can fill the English form and still pick Spanish as the default
    # page language (or vice versa). Translate FROM the language the text is
    # actually written in — otherwise the "translation" runs in the wrong
    # direction and the default page publishes untranslated.
    source_lang = default_language
    default_text = content_text(client["content"][default_language])
    if default_language == "en" and spanish_signal_score(default_text) >= 5:
        source_lang = "es"
    elif (
        default_language == "es"
        and english_signal_score(default_text) >= 5
        and english_signal_score(default_text) > spanish_signal_score(default_text)
    ):
        source_lang = "en"

    other_lang = "en" if source_lang == "es" else "es"
    apply_translation(client["content"], source_lang, other_lang)

    # La fusión va DESPUÉS de traducir y ANTES de las guardas. Después de
    # traducir, porque un campo que se conserva ya venía traducido en los dos
    # idiomas y volver a traducirlo sería gastar tokens para llegar al mismo
    # lugar. Antes de las guardas, porque son las guardas de la página FINAL:
    # si el formulario no preguntó por el WhatsApp, la página sigue teniendo el
    # suyo y no hay por qué mandarla a revisión manual.
    client = merge_with_existing(client, previous, payload, cleared)

    if default_language == "en" and spanish_signal_score(content_text(client["content"]["en"])) >= 5:
        fail(
            "default_language is 'en' but the English page still looks Spanish; "
            "set OPENAI_API_KEY or translate the intake manually before publishing"
        )
    if default_language == "es":
        es_text = content_text(client["content"]["es"])
        if english_signal_score(es_text) >= 5 and english_signal_score(es_text) > spanish_signal_score(es_text):
            fail(
                "default_language is 'es' but the Spanish page still looks English; "
                "set OPENAI_API_KEY or translate the intake manually before publishing"
            )

    if not (
        client["whatsapp"]
        or client["phone"]
        or client["public_email"]
        or client["booking_url"]
        or client["website"]
        or client["tiktok"]
        or client["other_public_link"]
        or client["delivery_pickup_links"]
        or client["portfolio_link"]
    ):
        fail("no public contact or public link - manual review required")

    # Linter de copy (warn-only), después de traducir para que el texto
    # traducido también se revise. Se loguean las frases literales, nunca el
    # payload. El conteo viaja al correo de entrega vía GITHUB_OUTPUT.
    hits = lint_client(client)
    if hits:
        print(f"WARN: copy linter flagged {len(hits)} phrase(s): {sorted(set(hits))}",
              file=sys.stderr)

    return publish(client, slug, order_id, linter_flags=hits)


# El tramo final es el mismo para las dos plantillas: escribir el JSON del
# cliente, correr SU generador, comprobar que salió la página y que el order_id
# no se filtró al HTML público. Vive aparte porque la rama de catálogo lo
# necesita igual — y porque duplicarlo sería duplicar justo la guarda que evita
# publicar un identificador de compra en un repo público.
def publish(client: dict, slug: str, order_id: str,
            linter_flags: list | None = None) -> int:
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    client_path = CLIENTS_DIR / f"{slug}.client.json"
    client_path.write_text(json.dumps(client, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"client JSON written: {client_path.relative_to(REPO_ROOT)}")

    # El generador sale de la PLANTILLA de la vertical, no está fijo.
    #
    # Hasta el 2026-07-25 esta línea llamaba `generate_service_menu.py` a secas.
    # Con las 5 verticales de service-menu eso funcionaba de casualidad, y el
    # camino post-pago del catálogo no se veía roto hasta intentar producir el
    # repo: un cliente del catálogo pagaba y recibía una página de menú de
    # servicios. `GENERATOR_FOR_TEMPLATE` ya existía en `vertical_config` como
    # fuente única; solo faltaba usarla aquí.
    #
    # FALLA CERRADO a propósito. En `build_prospect.py` una plantilla
    # desconocida degrada a service-menu con un aviso, porque ahí lo peor que
    # pasa es una vista previa fea que nadie compró. Aquí YA PAGÓ un cliente:
    # generarle en silencio el tipo de página equivocado es peor que no
    # generarle nada, porque nadie se entera hasta que reclama.
    generator_name = GENERATOR_FOR_TEMPLATE.get(TEMPLATE)
    if not generator_name:
        fail(
            f"no generator registered for template {TEMPLATE!r} "
            f"(known: {', '.join(sorted(GENERATOR_FOR_TEMPLATE))}) — "
            "refusing to build a paid page with the wrong generator"
        )
    generator_path = REPO_ROOT / "generator" / f"{generator_name}.py"
    if not generator_path.exists():
        fail(f"generator missing on disk: {generator_path}")

    result = subprocess.run(
        [sys.executable, str(generator_path), "--client", str(client_path)],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        fail(f"generator failed with exit code {result.returncode}")

    out_dir = LINKS_DIR / slug
    index_html = out_dir / "index.html"
    if not index_html.exists():
        fail(f"expected output missing: {index_html}")

    # QA guard: the order_id must never appear in published HTML.
    if order_id:
        for html_file in out_dir.rglob("*.html"):
            if order_id in html_file.read_text(encoding="utf-8"):
                html_file.unlink()
                fail(f"order_id leaked into {html_file.name} — build blocked")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"slug={slug}\n")
            # Solo si esta vertical declara `legal.copy_linter`: sin la lista,
            # el workflow de HMU/PawContact no ve una salida nueva.
            if LEGAL.get("copy_linter"):
                f.write(f"linter_flags={len(linter_flags or [])}\n")

    print(f"page generated: public/links/{slug}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
