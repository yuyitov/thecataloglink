#!/usr/bin/env python3
"""El botón que el negocio eligió destacar — una sola tabla, para todos.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
La tabla de alias estaba COPIADA TRES VECES (`worker.js`,
`build_client_from_intake.py`, `generate_service_menu.py`) y las copias
divergieron. La auditoría del 2026-07-26 midió el resultado en PawContact: de
las 5 opciones del formulario, 3 se caían EN SILENCIO y el negocio recibía
WhatsApp en su lugar.

  - "Booking link"      el worker normalizaba a `booking_link`, y la tabla
                        solo tenía `booking`.
  - "Reservas / agenda" normalizaba a `reservas_agenda`, y la tabla solo
                        tenía `reservas`.
  - "Instagram"         pasaba el worker y moría aquí: `build_client_from_intake`
                        no tenía `instagram` mientras la copia de
                        `generate_service_menu` sí.
  - (una cuarta, que nadie había visto) `generate_service_menu` no quitaba
                        acentos, así que "Llamada telefónica" tampoco le
                        casaba. En producción quedaba tapada porque
                        `build_client` ya había normalizado antes.

Es un fallo que no genera reporte: un negocio que pide Instagram y recibe
WhatsApp asume que así es el producto. Arreglar los valores sin arreglar la
duplicación es garantizar que vuelva a pasar; por eso la tabla ahora es DATO
(`engine/worker/primary-cta-aliases.json`) y no código repetido.

DÓNDE VIVE LA FUENTE ÚNICA, Y POR QUÉ AHÍ
-----------------------------------------
En `worker/primary-cta-aliases.json`, al lado de `tally-field-aliases.json`,
que es su hermano: los dos son el vocabulario del intake. El worker la importa
con el mismo patrón ya probado (`import ... with { type: 'json' }`, que esbuild
incrusta en el bundle) y el generador la lee desde aquí. La ruta
`<padre>/worker/` es la MISMA en los dos árboles que existen:

    fábrica:  engine/generator/primary_cta.py  ->  engine/worker/...json
    export:   generator/primary_cta.py         ->  worker/...json

Si el archivo falta, esto REVIENTA al importarse, a propósito. Una tabla vacía
sería exactamente el bug que este archivo existe para matar: todo caería al
primer canal disponible y la página saldría publicada, verde y equivocada.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ALIASES_PATH = Path(__file__).resolve().parent.parent / "worker" / "primary-cta-aliases.json"

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _load(path: Path = ALIASES_PATH) -> dict[str, str]:
    """La tabla, invertida a {respuesta normalizada -> canal}.

    Las claves `_comment*` llevan notas en texto plano, no listas: se saltan
    igual que en `worker.js` (guard por tipo, no por el nombre de la clave).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        alias: kind
        for kind, aliases in raw.items()
        if isinstance(aliases, list)
        for alias in aliases
    }


PRIMARY_CTA_ALIASES = _load()
# Los canales que la tabla sabe producir (para quien quiera barrerlos todos).
PRIMARY_CTA_KINDS = tuple(dict.fromkeys(PRIMARY_CTA_ALIASES.values()))


def normalize_answer(value) -> str:
    """`normalizeKey` de product-config.mjs, en Python.

    Sin acentos porque el formulario los tiene ("Llamada telefónica") y el
    worker ya normalizaba así; que una de las copias no lo hiciera era una de
    las cuatro divergencias.
    """
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return _NON_ALNUM_RE.sub("_", text.strip().lower()).strip("_")


def normalize_primary_cta(value) -> str | None:
    """El canal que pidió el negocio, o None si su respuesta no es una opción."""
    normalized = normalize_answer(value)
    if not normalized:
        return None
    return PRIMARY_CTA_ALIASES.get(normalized)
