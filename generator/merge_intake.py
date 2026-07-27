#!/usr/bin/env python3
"""FUSIÓN al regenerar: un campo vacío significa "no cambies esto", no "bórralo".

POR QUÉ EXISTE
--------------
Una compra incluye modificaciones. El cliente abre SU formulario de Tally —
prellenado con lo que ya publicó— y lo reenvía; el worker despacha el mismo
evento de siempre y `build_client_from_intake.py` **regeneraba la página
completa desde ese envío**, reemplazando `data/clients/<slug>.client.json`.

Eso hace que el prellenado sea *portante*: si Tally no logra prellenar un campo
(cambió el editor, se renombró una pregunta, el hidden field no viajó), el
cliente reenvía un formulario medio vacío y **su página en vivo pierde
contenido**. Es el hallazgo MEDIO #4 de `AUDITORIA_ATAQUE_2026-07-23.md`. Hoy el
peor caso es que un cliente que YA PAGÓ se quede sin página.

DECISIÓN DE VERO (2026-07-25, `charly/reportes/2026-07-25-mufasa-decisiones-
auditoria-medios.md`): rediseñar el generador para que **FUSIONE en vez de
reemplazar**. Mata la categoría de falla entera en vez de parchar una instancia:
el prellenado pasa de requisito de seguridad a comodidad, y el producto deja de
depender de cómo se comporte el editor de un tercero.

LA REGLA, EN UNA LÍNEA
----------------------
Si el intake **no trae respuesta** para un campo, ese campo se conserva tal cual
estaba en el `client.json` anterior. Si sí la trae, la respuesta nueva manda —
igual que siempre.

Ojo con lo que la fusión NO cambia: un campo CONTESTADO sigue reemplazando por
completo su valor anterior. Quitar un servicio de la lista se sigue haciendo
como siempre (reenviar la lista sin él). Lo único que la fusión vuelve imposible
es **vaciar** un campo dejándolo en blanco — y para eso está el borrado
explícito de abajo.

BORRADO EXPLÍCITO
-----------------
Con fusión, "vacío" ya no borra. Hace falta una forma de decir "esto sí, quítalo".
El mecanismo es el más simple que funciona y no necesita tocar el editor de Tally
ni agregar preguntas: **escribir una palabra de borrado como ÚNICO contenido del
campo** (ver `DELETE_TOKENS`). Se acepta en español y en inglés, sin acentos y sin
importar mayúsculas, y también entre corchetes (`[BORRAR]`):

    Políticas: [ BORRAR ]      -> la página se queda sin políticas
    WhatsApp:  n/a             -> se quita el botón de WhatsApp
    Instagram: -               -> se quita el enlace de Instagram

Por qué así y no con una pregunta "¿qué quieres borrar?": esa pregunta habría que
cablearla en 2 formularios × 5 productos y el cliente tendría que nombrar campos
internos. Una palabra dentro del propio campo se explica sola, viaja en cualquier
formulario que ya exista y **no puede aplicarse al campo equivocado**.

Un token de borrado se limpia del payload ANTES de construir (nunca acaba siendo
el texto de la página) y marca ese campo como CONTESTADO — así el campo cae por
el camino normal y queda con el valor vacío/por defecto que el generador ya sabe
producir. Dicho de otro modo: **borrar explícitamente = comportarse como antes de
esta fusión**.

Las guardas que ya existían siguen mandando: si el borrado deja la página sin
ningún contacto público, o sin servicios, `build_client_from_intake.py` falla
cerrado y dispara la alerta de embudo muerto, igual que siempre.

GRANULARIDAD: LA UNIDAD ES EL GRUPO
-----------------------------------
La fusión trabaja campo por campo, pero algunos campos NO se pueden mover solos
sin dejar el JSON incoherente, así que comparten fuentes y viajan juntos:

  · `services` + `service_categories` — un servicio apunta a su categoría; si
    las categorías fueran las viejas y los servicios los nuevos, un servicio
    podría quedar en una categoría que ya no existe.
  · `locations` + `google_maps_url` + `content.*.location_hours` + `address` —
    los horarios van alineados por índice con las sucursales.

Consecuencia a tener presente: si el cliente toca UN campo de un grupo, el grupo
entero se reconstruye desde el formulario. La fusión protege del formulario
vacío, no de un formulario a medias dentro de un mismo grupo.

DÓNDE NO HACE FALTA
-------------------
`apply_correction.py` (la corrección de texto libre con gpt-4o-mini) no necesita
esto: ya parte del `client.json` existente y lo EDITA, y `validate_and_sanitize`
rechaza cualquier salida a la que le falte una clave del original, se quede sin
servicios o sin contacto público.

PORTABILIDAD
------------
Este archivo se copia VERBATIM a los repos standalone (HMU Link, ModaLink,
Dr Link y, vía `export_vertical.py`, PawContact). Por eso no importa nada del
motor —ni `vertical_config`— y sus tablas son un SUPERCONJUNTO de los campos de
las 4 plantillas: una vertical que no tenga un campo simplemente nunca trae esa
clave, y sobrar no hace daño. Si se edita, se edita aquí y se re-porta.
"""

from __future__ import annotations

import copy
import json
import sys
import unicodedata
from pathlib import Path


def load_previous_client(client_path) -> dict:
    """Lee el `client.json` que ya estaba publicado. `{}` si no hay ninguno.

    Que el archivo EXISTA es la señal de que esto es una REGENERACIÓN y no un
    alta: el worker conserva a propósito el slug de la página en una
    modificación (`if (modification.slug) publicPayload.public_slug = ...`),
    mientras que un alta nueva estrena un slug derivado de su submission_id. Por
    eso la fusión no necesita una bandera nueva en el evento ni que los workers
    ya desplegados cambien: el disco ya lo dice.

    Falla ABIERTO: si el JSON anterior no se puede leer, lo peor que pasa es
    regenerar como antes de la fusión. Negarse a publicar dejaría al cliente sin
    su modificación por culpa de un archivo roto.
    """
    path = Path(client_path)
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"WARN: no pude leer el client.json anterior ({e}); se regenera sin fusionar",
              file=sys.stderr)
        return {}
    return previous if isinstance(previous, dict) else {}

# --------------------------------------------------------------------------- #
# Borrado explícito
# --------------------------------------------------------------------------- #

# Un campo cuyo contenido ENTERO (ya sin espacios, corchetes ni acentos, en
# minúsculas) sea uno de estos, se entiende como "quítalo".
#
# La lista es corta y deliberada: son las formas en que una persona escribe
# "aquí no va nada". `n/a` y `none` entran porque un cliente que escribe "N/A"
# está pidiendo justo esto, aunque no sepa que existe el mecanismo.
DELETE_TOKENS = frozenset({
    "borrar", "borralo", "borrarlo", "eliminar", "quitar", "quitalo",
    "delete", "remove",
    "ninguno", "ninguna", "nada", "none", "n/a", "na",
    "-", "--", "---", "—",
})


def _plain(value: str) -> str:
    """minúsculas, sin acentos y sin los corchetes de `[BORRAR]`."""
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.strip().strip("[](){}").strip().lower()


def is_delete_token(value) -> bool:
    return isinstance(value, str) and _plain(value) in DELETE_TOKENS


def _scrub(value):
    """Devuelve (valor_limpio, hubo_token). Recursivo sobre listas y dicts."""
    if is_delete_token(value):
        return "", True
    if isinstance(value, list):
        out, hit = [], False
        for item in value:
            clean, found = _scrub(item)
            out.append(clean)
            hit = hit or found
        return out, hit
    if isinstance(value, dict):
        out, hit = {}, False
        for key, item in value.items():
            clean, found = _scrub(item)
            out[key] = clean
            hit = hit or found
        return out, hit
    return value, False


def sanitize_intake(payload: dict) -> tuple[dict, set[str]]:
    """Saca los tokens de borrado del payload y dice qué campos los traían.

    El payload que sale de aquí es el que se usa para CONSTRUIR: así la palabra
    "BORRAR" jamás termina impresa en la página del cliente. El conjunto que sale
    es la lista de claves que el cliente pidió vaciar a propósito, y cuenta como
    "contestada" para la fusión.
    """
    if not isinstance(payload, dict):
        return {}, set()
    clean: dict = {}
    cleared: set[str] = set()
    for key, value in payload.items():
        scrubbed, hit = _scrub(value)
        clean[key] = scrubbed
        if hit:
            cleared.add(key)
    return clean, cleared


# --------------------------------------------------------------------------- #
# Qué campo del cliente lo alimenta qué respuesta del intake
# --------------------------------------------------------------------------- #

# Las sucursales, sus horarios y el mapa se mueven en bloque (ver "GRANULARIDAD"
# arriba): `build_locations()` arma UNA lista desde todas estas respuestas, y
# `location_hours` va alineado por índice con esa lista.
LOCATION_KEYS = (
    "location_1_name", "address", "google_maps_url", "location_1_notes",
    "location_2_name", "location_2_address", "location_2_maps_url", "location_2_notes",
    "location_3_name", "location_3_address", "location_3_maps_url", "location_3_notes",
    "additional_locations_text",
    # Horario POR SUCURSAL (verticales con `schema.hours_source: location_hours`,
    # hoy ModaLink; también lo necesita Dr Link). Van en el grupo y no aparte
    # justamente porque `content.<lang>.location_hours` se alinea por índice con
    # `locations`: si las sucursales se rehicieran y los horarios se conservaran
    # viejos, una tienda publicaría el horario de la otra.
    "location_1_hours", "location_2_hours", "location_3_hours",
)

# Las categorías y los servicios se leen de las MISMAS dos respuestas
# (`parse_services` usa las categorías como estado al recorrer las líneas), así
# que ninguno de los dos puede quedarse viejo mientras el otro se renueva.
SERVICE_KEYS = ("services_text", "service_categories_text")

# El catálogo se arma o desde la vista previa del prospecto (`prospect_slug`) o
# desde el texto del formulario: cualquiera de las tres respuestas rehace la
# lista de productos entera, sus fotos y la calificación heredada de Google.
CATALOG_KEYS = ("prospect_slug", "products_text", "product_image_urls")

# Campo del client.json  ->  respuestas del intake que lo alimentan.
# Superconjunto de las 4 plantillas (service-menu, catalog, ModaLink, Dr Link).
FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    # --- identidad y presentación (común) ---
    "default_language": ("default_language",),
    "brand_style": ("brand_style", "style_unmapped"),
    "business_type": ("business_type",),
    "business_name": ("business_name",),
    # --- imágenes ---
    "logo_url": ("logo_url",),
    "primary_image_url": ("image_url",),
    "gallery_images": ("image_url", "gallery_image_urls"),
    "lookbook_urls": ("lookbook_urls",),
    # --- contacto y enlaces públicos ---
    "whatsapp": ("whatsapp",),
    "phone": ("phone",),
    "public_email": ("public_email",),
    "instagram": ("instagram",),
    "facebook": ("facebook",),
    "tiktok": ("tiktok",),
    "pinterest": ("pinterest",),
    "website": ("website",),
    "booking_url": ("booking_url",),
    "primary_cta": ("primary_cta",),
    "google_reviews_url": ("google_reviews_url",),
    "other_public_link": ("other_public_link",),
    "delivery_pickup_links": ("delivery_pickup_links_text",),
    "portfolio_link": ("portfolio_link",),
    # --- sucursales (grupo) ---
    "locations": LOCATION_KEYS,
    "google_maps_url": ("google_maps_url",) + LOCATION_KEYS,
    "address": ("address",) + LOCATION_KEYS,
    # --- catálogo (plantilla `catalog`) ---
    "sale_button": ("sale_button",),
    "products": CATALOG_KEYS,
    "photo_source_dir": CATALOG_KEYS,
    "rating": CATALOG_KEYS,
    "rating_count": CATALOG_KEYS,
    # --- ModaLink ---
    "specialty": ("specialty", "specialty_other"),
    "specialty_other": ("specialty", "specialty_other"),
    "years_experience": ("years_experience",),
    "training_text": ("training_text",),
    "delivery_time_text": ("delivery_time_text",),
    "appointment_mode": ("appointment_mode",),
    "home_service": ("home_service",),
    "payment_methods": ("payment_methods",),
    "invoicing": ("invoicing",),
    "deposit_policy_text": ("deposit_policy_text",),
    "photo_rights_confirmed": ("photo_rights_confirmed",),
    "directory_state": ("directory_state", "directory_opt_in"),
    "directory_opt_in": ("directory_state", "directory_opt_in"),
    # --- Dr Link ---
    "country": ("country",),
    "profession": ("profession",),
    "credentials": ("credentials",),
    "telemedicine": ("telemedicine",),
    "insurance": ("insurance",),
    "privacy_notice_url": ("privacy_notice_url",),
}

# Campo dentro de content.<lang>  ->  respuestas del intake que lo alimentan.
CONTENT_FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "short_description": ("short_description",),
    "address": ("address",) + LOCATION_KEYS,
    "opening_hours_text": ("opening_hours_text",),
    "location_hours": ("location_hours",) + LOCATION_KEYS,
    "service_area_text": ("service_area_text",),
    "client_care_text": ("client_care_text",),
    "reservations_text": ("reservations_text",),
    "class_schedule_text": ("class_schedule_text",),
    "tour_details_text": ("tour_details_text",),
    "pet_notes_text": ("pet_notes_text",),
    "price_display": ("price_display",),
    "service_categories": SERVICE_KEYS,
    "services": SERVICE_KEYS,
    "policies": ("policies_text",),
    "faq": ("faq_text",),
    "featured_package": ("featured_text",),
    "delivery_time_text": ("delivery_time_text",),
    "deposit_policy_text": ("deposit_policy_text",),
    "appointment_policy_text": ("appointment_policy_text",),
    "emergency_policy_text": ("emergency_policy_text",),
}

# El slug es la dirección pública de la página: lo fija el worker desde la orden
# y una regeneración jamás debe moverlo (mover una página que el cliente ya
# compartió es peor que cualquier pérdida de contenido).
PINNED_FIELDS = ("public_slug",)


# --------------------------------------------------------------------------- #
# Fusión
# --------------------------------------------------------------------------- #

def _has_answer(value) -> bool:
    """¿Esta respuesta trae algo?

    Un booleano SIEMPRE cuenta como respuesta, incluso `False`: una casilla
    desmarcada es una respuesta ("no facturo"), no un hueco. Los huecos que esta
    fusión existe para atajar son los de texto. Si el formulario ni siquiera trae
    la pregunta, la clave no está en el payload y eso sí es "sin contestar".
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, (list, tuple)):
        return any(_has_answer(item) for item in value)
    if isinstance(value, dict):
        return any(_has_answer(item) for item in value.values())
    return False


def answered_keys(payload: dict, cleared: set[str] | None = None) -> set[str]:
    """Respuestas que el intake SÍ trae (incluidas las de borrado explícito)."""
    payload = payload if isinstance(payload, dict) else {}
    out = {key for key, value in payload.items() if _has_answer(value)}
    out |= set(cleared or ())
    return out


def _field_answered(field: str, sources: dict[str, tuple[str, ...]], answered: set[str]) -> bool:
    keys = sources.get(field)
    if keys is None:
        # Campo que la tabla no conoce: se comporta como antes de la fusión (lo
        # nuevo manda). Es la opción conservadora — nunca resucita datos viejos
        # en un campo cuyo significado no entendemos — y el WARN deja rastro para
        # agregarlo a la tabla.
        print(f"WARN: merge_intake no conoce el campo {field!r}; se toma el valor nuevo",
              file=sys.stderr)
        return True
    return any(key in answered for key in keys)


def _merge_block(previous: dict, new: dict, sources: dict[str, tuple[str, ...]],
                 answered: set[str], skip: tuple[str, ...] = ()) -> dict:
    merged = dict(new)
    for field in set(new) | set(previous):
        if field in skip:
            continue
        if _field_answered(field, sources, answered):
            continue
        if field in previous:
            merged[field] = copy.deepcopy(previous[field])
        else:
            # Sin contestar y sin valor anterior: el campo no existía, y el valor
            # que trae el build nuevo es puro relleno por defecto.
            merged.pop(field, None)
    return merged


def _apply_price_policy(block: dict) -> None:
    """Coherencia: si la página oculta precios, ningún precio sobrevive.

    `price_display` y los servicios pueden venir de lados distintos tras la
    fusión (el cliente reenvió su lista de servicios pero el formulario no traía
    la pregunta de precios). Sin esta pasada, una página que el cliente había
    dejado en "no mostrar precios" volvería a mostrarlos.
    """
    if block.get("price_display") != "hide":
        return
    for svc in block.get("services") or []:
        if isinstance(svc, dict):
            svc.pop("price_label", None)
    featured = block.get("featured_package")
    if isinstance(featured, dict):
        featured.pop("price_label", None)


def merge_client(previous: dict, new: dict, payload: dict,
                 cleared: set[str] | None = None) -> dict:
    """Fusiona el cliente recién construido sobre el `client.json` que ya existía.

    `payload` es el intake YA limpiado por `sanitize_intake`; `cleared`, las
    claves que traían un token de borrado. Sin `previous` (alta nueva) devuelve
    `new` sin tocarlo: no hay nada que conservar.
    """
    if not isinstance(previous, dict) or not previous:
        return new
    if not isinstance(new, dict):
        return new

    answered = answered_keys(payload, cleared)

    merged = _merge_block(previous, new, FIELD_SOURCES, answered, skip=PINNED_FIELDS + ("content",))
    for field in PINNED_FIELDS:
        if field in new:
            merged[field] = new[field]

    new_content = new.get("content")
    prev_content = previous.get("content")
    if isinstance(new_content, dict) and isinstance(prev_content, dict):
        content = {}
        for lang, new_block in new_content.items():
            prev_block = prev_content.get(lang)
            if isinstance(new_block, dict) and isinstance(prev_block, dict):
                content[lang] = _merge_block(prev_block, new_block, CONTENT_FIELD_SOURCES, answered)
            else:
                content[lang] = new_block
            if isinstance(content[lang], dict):
                _apply_price_policy(content[lang])
        # Un idioma que existía antes y que el build nuevo no produjo se conserva:
        # perder la mitad bilingüe de una página vendida es exactamente la falla
        # que esta fusión ataca.
        for lang, prev_block in prev_content.items():
            content.setdefault(lang, copy.deepcopy(prev_block))
        merged["content"] = content
    elif isinstance(prev_content, dict) and not isinstance(new_content, dict):
        merged["content"] = copy.deepcopy(prev_content)

    return merged


def unknown_fields(client: dict) -> set[str]:
    """Campos que las tablas no cubren. Existe para que un test lo vigile.

    Si el generador aprende un campo nuevo y nadie lo agrega aquí, ese campo
    quedaría fuera de la fusión (se tomaría siempre del formulario) y volvería a
    poder vaciarse solo. Un test que llame a esto falla el día que pase.
    """
    client = client if isinstance(client, dict) else {}
    out = {f for f in client if f not in FIELD_SOURCES and f not in PINNED_FIELDS and f != "content"}
    for block in (client.get("content") or {}).values():
        if isinstance(block, dict):
            out |= {f"content.{f}" for f in block if f not in CONTENT_FIELD_SOURCES}
    return out
