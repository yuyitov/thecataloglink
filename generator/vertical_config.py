"""Loader for this vertical's brand/domain/styles/strings/blocks config.

Standalone export (produced by link-factory/scripts/export_vertical.py):
unlike the factory's engine, there is exactly one vertical in this repo, so
this reads `vertical.yaml` at the repo root directly instead of resolving
`verticals/<id>/vertical.yaml` via the `LINK_FACTORY_VERTICAL` env var. To
change this vertical's config, edit it in link-factory and re-export — do
not hand-edit this file or vertical.yaml here, they will be overwritten.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from blocks import merge_blocks
from strings_base import BASE_STRINGS

REPO_ROOT = Path(__file__).resolve().parent.parent
STYLES_DIR = Path(__file__).resolve().parent / "styles"
VERTICAL_YAML = REPO_ROOT / "vertical.yaml"


class VerticalConfigError(ValueError):
    """Raised when vertical.yaml is missing required fields or has an invalid shape."""


def load_vertical() -> dict:
    if not VERTICAL_YAML.exists():
        raise VerticalConfigError("No existe vertical.yaml en la raiz del repo.")
    with VERTICAL_YAML.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise VerticalConfigError("vertical.yaml esta vacio o no es un objeto YAML.")
    missing = [k for k in ("id", "brand_name", "domain") if not str(raw.get(k, "")).strip()]
    if missing:
        raise VerticalConfigError(f"vertical.yaml: faltan campos obligatorios: {', '.join(missing)}.")
    domain = str(raw["domain"]).strip()
    if not domain.lower().startswith(("http://", "https://")):
        raise VerticalConfigError(
            f"vertical.yaml: domain debe empezar con http:// o https:// (recibido: {domain!r})."
        )
    catalog = (raw.get("styles") or {}).get("catalog")
    if not isinstance(catalog, list) or not catalog:
        raise VerticalConfigError("vertical.yaml: falta styles.catalog (lista no vacia de estilos).")
    unknown = [name for name in catalog if not (STYLES_DIR / f"{name}.css").exists()]
    if unknown:
        raise VerticalConfigError(f"vertical.yaml: styles.catalog referencia estilos sin CSS: {unknown}.")
    return raw


def build_strings(brand_name: str, overrides: dict) -> dict:
    strings = copy.deepcopy(BASE_STRINGS)
    for lang, values in strings.items():
        for key, value in values.items():
            values[key] = value.format(brand_name=brand_name)
    for lang, lang_overrides in (overrides or {}).items():
        strings.setdefault(lang, {}).update(lang_overrides or {})
    return strings


# House legal policy (Vero, 2026-07-20): one refund window for every business,
# `hello@<domain>` as the standard mailbox, CFDI on request. Mirrors
# engine/generator/vertical_config.py — keep both in sync when it changes.
LEGAL_DEFAULTS = {
    "refund_days": 7,
    "free_changes": 2,
    "support_email": "",
    "responsable": "",
    "domicilio_fiscal": "",
    "jurisdiction": "Estados Unidos Mexicanos",
    "updated": "",
    "cfdi": True,
    "disclaimers": [],
    "disclaimers_style": "inline",
    "copy_linter": {},
}

# Espejo de engine/generator/vertical_config.py::SCHEMA_DEFAULTS. Aqui solo se
# CONSTRUYEN los valores: quien valida la forma del yaml es la fabrica, antes de
# exportar, asi que un export siempre sale de una config ya validada.
SCHEMA_DEFAULTS = {
    "hours_source": "opening_hours_text",
    "gallery_source": "gallery_images",
    "demo_mode": "bilingual",
    "locations_min": 0,
    "locations_max": 0,
    "require_true": [],
}


def build_schema(raw: dict) -> dict:
    schema = copy.deepcopy(SCHEMA_DEFAULTS)
    schema.update({k: v for k, v in (raw.get("schema") or {}).items() if v is not None})
    return schema


def apex_domain(domain: str) -> str:
    host = str(domain).split("//", 1)[-1].strip("/").split("/", 1)[0]
    return host[4:] if host.lower().startswith("www.") else host


def build_legal(raw: dict) -> dict:
    legal = copy.deepcopy(LEGAL_DEFAULTS)
    legal.update({k: v for k, v in (raw.get("legal") or {}).items() if v is not None})
    if not str(legal["support_email"]).strip():
        legal["support_email"] = f"hello@{apex_domain(raw['domain'])}"
    return legal


VERTICAL = load_vertical()
BRAND_NAME = VERTICAL["brand_name"]
DOMAIN = VERTICAL["domain"].rstrip("/")
STYLES_CATALOG = tuple(VERTICAL["styles"]["catalog"])
# Plantilla base del engine con la que renderiza esta vertical
# (`service-menu` | `catalog`). generate_catalog.py la importa; el generador de
# service-menu no la usa, pero se expone siempre para que las dos plantillas
# vean el mismo modulo de config.
TEMPLATE = VERTICAL.get("template") or "service-menu"
# Que generador renderiza cada plantilla. Espejo de
# engine/generator/vertical_config.py::GENERATOR_FOR_TEMPLATE, que es la fuente
# de verdad en la fabrica; aqui viaja porque build_client_from_intake.py --el
# camino de un cliente que YA PAGO-- lo importa para no llamar al generador
# equivocado. Sin esta linea el repo exportado ni siquiera importa ese modulo.
GENERATOR_FOR_TEMPLATE = {
    "service-menu": "generate_service_menu",
    "catalog": "generate_catalog",
}
STRINGS = build_strings(BRAND_NAME, VERTICAL.get("strings_overrides"))
BLOCKS = merge_blocks(VERTICAL.get("blocks"))
LEGAL = build_legal(VERTICAL)
CATALOGS = copy.deepcopy(VERTICAL.get("catalogs") or {})
SCHEMA = build_schema(VERTICAL)
# Espejo de INTAKE_DEFAULTS de la fabrica.
INTAKE_DEFAULTS = {"default_language": "", "fields": {}}
INTAKE = {**copy.deepcopy(INTAKE_DEFAULTS), **copy.deepcopy(VERTICAL.get("intake") or {})}
TEMPLATE_COMMENT_OVERRIDES = copy.deepcopy(VERTICAL.get("template_comments") or {})
# Directorio publico de la vertical (engine/generator/directory.py). Apagado si
# el yaml no lo declara. Espejo de DIRECTORY_DEFAULTS de la fabrica.
DIRECTORY_DEFAULTS = {
    "enabled": False,
    "paths": {},
    "hreflang": {},
    "default_language": "es",
    "group_by": "",
    "filter_by": "",
    "fallback_category": "",
    "strings": {},
}
DIRECTORY = {**copy.deepcopy(DIRECTORY_DEFAULTS), **copy.deepcopy(VERTICAL.get("directory") or {})}
