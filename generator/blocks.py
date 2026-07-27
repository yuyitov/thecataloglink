"""Conditional-block registry for the engine (Fase 1.3).

Some page blocks only make sense for certain business types: the engine
renders them conditionally based on the payload's `business_type`. Until
Fase 1.3 that gating lived as hardcoded constants inside
`generate_service_menu.py` (DELIVERY_PICKUP_TYPES / PORTFOLIO_TYPES); now
every conditional block declares its `enabled_for` here, and a vertical can
override any of them via the `blocks:` section of its `vertical.yaml`.

`enabled_for` accepts three shapes:

- a list of business types  -> the block renders for those types. Payloads
  with no `business_type` (or `"general"`) always keep the block: an untyped
  business gave us no reason to hide anything (same behavior the HMU engine
  always had).
- the string "all"          -> the block renders for every business type.
- the string "none"         -> the vertical does not use the block at all
  (not even for untyped/general payloads).

New optional blocks (e.g. the ModaLink mini-lookbook, Fase 3.2) get an entry
in ENGINE_BLOCKS when their builder lands in the engine; verticals then tune
them per giro without touching Python.

`lookbook` (Fase 3.2) is the first of those: a mini-lookbook photo grid
ported from ModaLink, gated purely by data (it renders nothing without
`lookbook_urls`), so its engine default is "all" rather than a type list —
any business type may add extra photos; a vertical can still narrow it via
`blocks:` if a giro should never show it.
"""

from __future__ import annotations

import copy
import re

# Engine defaults, extracted verbatim from the HMU generator so the golden
# output stays byte-identical when a vertical defines no `blocks:` overrides.
ENGINE_BLOCKS = {
    "delivery_pickup": {
        "enabled_for": ("food", "retail"),
    },
    "portfolio": {
        "enabled_for": ("creative", "beauty", "wellness", "professional", "fitness"),
    },
    "lookbook": {
        "enabled_for": "all",
    },
    # Los 3 siguientes nacen de la migracion de ModaLink (2026-07-26). Los tres
    # arrancan en "all" — o sea, el comportamiento que el motor YA tenia — asi
    # que una vertical que no los declara no mueve un byte. ModaLink los apaga
    # porque no aplican a su producto, que es exactamente el caso que el
    # principio de arquitectura de Vero contempla: lo distinto es una OPCION.
    #
    # `faq`: la seccion de preguntas frecuentes. Apagarla quita tambien su CSS
    # y la linea del token en la plantilla (ver FAQ_CSS en generate_service_menu
    # y {{FAQ_LINE}} en templates/base.html) — si solo se quitara el bloque, la
    # pagina quedaria con un salto de linea de sobra, que fue justo el error que
    # el gate de HMU tuvo que corregir con el mini-lookbook.
    "faq": {
        "enabled_for": "all",
    },
    # `price_ask_legend`: la leyenda "Consultar"/"Ask us" que el motor imprime
    # en un servicio SIN precio cuando la politica no es "ocultar". ModaLink no
    # la imprime: deja el renglon sin columna de precio.
    "price_ask_legend": {
        "enabled_for": "all",
    },
    # `footer_privacy`: el enlace de Privacidad del pie. ModaLink no lo lleva en
    # las paginas de cliente.
    "footer_privacy": {
        "enabled_for": "all",
    },
    # `vcard` (linkFactory/14, decision de Vero 2026-07-27): el boton "Guardar
    # en contactos" — un contact.vcf generado en build con los datos PUBLICOS
    # del negocio (nombre, telefono, web, direccion) y el link de su pagina
    # adentro, que es el punto: nadie guarda el telefono de un servicio al que
    # va cada 3 meses, y el enlace se olvida; el contacto guardado lleva los
    # dos. A diferencia de los bloques de arriba, este AGREGA contenido nuevo,
    # asi que su default es "none": ninguna vertical cambia un byte hasta que
    # declare `blocks: {vcard: {enabled_for: ...}}` en su vertical.yaml (la
    # activacion viaja con la ola del re-export, linkFactory/12). Referencia
    # viva del patron: My Guest (su .vcf se arma igual, campo por campo).
    "vcard": {
        "enabled_for": "none",
    },
}

_GENERAL = ("", "general")


def _normalize_enabled_for(block_id: str, value):
    """Validate and normalize an `enabled_for` value from vertical.yaml."""
    if isinstance(value, str):
        keyword = value.strip().lower()
        if keyword in ("all", "none"):
            return keyword
        raise ValueError(
            f"blocks.{block_id}.enabled_for: valor invalido {value!r} "
            "(usa 'all', 'none' o una lista de business types)."
        )
    if isinstance(value, (list, tuple)):
        types = tuple(str(item).strip().lower() for item in value if str(item).strip())
        if not types:
            raise ValueError(
                f"blocks.{block_id}.enabled_for: la lista esta vacia "
                "(para desactivar el bloque usa 'none')."
            )
        return types
    raise ValueError(
        f"blocks.{block_id}.enabled_for: tipo invalido {type(value).__name__!r} "
        "(usa 'all', 'none' o una lista de business types)."
    )


def merge_blocks(overrides) -> dict:
    """Layer a vertical's `blocks:` overrides on top of the engine registry.

    Only blocks the engine knows can be overridden — an unknown id is almost
    certainly a typo in vertical.yaml, so it fails loudly instead of being
    silently ignored.
    """
    blocks = copy.deepcopy(ENGINE_BLOCKS)
    for block_id, cfg in (overrides or {}).items():
        if block_id not in blocks:
            known = ", ".join(sorted(blocks))
            raise ValueError(
                f"blocks.{block_id}: bloque desconocido para el motor "
                f"(conocidos: {known})."
            )
        if not isinstance(cfg, dict) or "enabled_for" not in cfg:
            raise ValueError(
                f"blocks.{block_id}: cada override debe ser un objeto con "
                "'enabled_for'."
            )
        blocks[block_id]["enabled_for"] = _normalize_enabled_for(
            block_id, cfg["enabled_for"]
        )
    return blocks


def block_enabled(blocks: dict, block_id: str, business_type) -> bool:
    """True if `block_id` may render for a payload of this `business_type`."""
    if block_id not in blocks:
        known = ", ".join(sorted(blocks))
        raise KeyError(
            f"Bloque desconocido: {block_id!r} (conocidos: {known})."
        )
    enabled_for = blocks[block_id]["enabled_for"]
    if enabled_for == "all":
        return True
    if enabled_for == "none":
        return False
    btype = str(business_type or "").strip().lower()
    if btype in _GENERAL:
        return True
    return btype in enabled_for


# --------------------------------------------------------------------------- #
# Sustitución de tokens de plantilla — UNA sola pasada
# --------------------------------------------------------------------------- #
def fill_tokens(template: str, tokens: dict) -> str:
    """Sustituye `{{TOKEN}}` por su valor en UNA pasada.

    Antes esto era un `for token, value: out = out.replace(token, value)`
    secuencial (hallazgo #17 de la auditoría de ataque del 2026-07-23). El
    problema: el valor que entra en la pasada N vuelve a mirarse en la N+1, así
    que un dato del negocio que contenga literalmente `{{PRODUCT_CARDS}}` se
    RE-EXPANDE con el bloque del motor. Hoy no es explotable —lo que se
    reinyecta es HTML que el propio motor ya escapó, no markup del atacante—
    pero es un pie puesto para que el día de mañana sí lo sea, y produce
    páginas mal armadas sin avisar.

    Con una pasada, lo que salga de un token queda tal cual: el texto del
    negocio nunca puede volverse plantilla.

    Los tokens desconocidos se dejan intactos a propósito: quien valida que no
    queden `{{...}}` sin resolver es el llamador (store.py lo hace y falla), y
    tragárselos aquí escondería el error.
    """
    if not tokens:
        return template
    patron = re.compile("|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)))
    return patron.sub(lambda m: str(tokens[m.group(0)]), template)
