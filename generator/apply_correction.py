#!/usr/bin/env python3
"""Apply a customer-requested correction to an already-published client page.

Runs inside GitHub Actions when the service-menu-worker dispatches a
repository_dispatch event with is_correction=true (the customer used the
one-time correction link from their delivery email → /correct/ page →
worker POST /correct).

Input:  env CORRECTION_PAYLOAD — JSON string:
        { is_correction: true, correction_id, slug, correction_text }
Output: data/clients/<slug>.client.json  (updated in place when applied)
        public/links/<slug>/             (regenerated when applied)
        GITHUB_OUTPUT slug=<slug> correction_status=applied|manual

The correction text is free-form (Spanish or English). gpt-4o-mini edits the
existing client JSON; the result is validated strictly against the original
before anything is written. ANY problem — missing OPENAI_API_KEY, API failure,
schema drift, no-op edit — downgrades to correction_status=manual and leaves
the page untouched: the workflow still notifies the worker, which emails the
customer an honest "we'll apply it by hand within 1 business day" and sends
Verónica the details. This script must therefore never exit non-zero for
content problems; only for infrastructure bugs (missing env, bad JSON payload).

Security notes:
- correction_text comes from the customer and is public-page content by
  definition (the /correct/ page warns not to include private info).
- Image fields (logo_url, primary_image_url, gallery_images) are pinned to
  their original values — photo changes go through email, not this flow.
- public_slug is pinned; a correction can never move or clone a page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "data" / "clients"
LINKS_DIR = REPO_ROOT / "public" / "links"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")

VALID_BRAND_STYLES = [
    "black-gold", "soft-blush", "charcoal-clean", "warm-sand",
    "aqua-clean", "sage-calm", "electric-slate", "terracotta-warm",
    "sunny-paws", "midnight-ink", "clarity-editorial", "horizon-teal",
]

# Campos que una corrección de texto NUNCA puede tocar (se restauran del
# original sin avisar): identidad de la página e imágenes.
PINNED_TOP_LEVEL = ("public_slug", "logo_url", "primary_image_url", "gallery_images")

# Claves opcionales que el LLM puede AGREGAR si el cliente lo pide
# (p.ej. "agrega un paquete destacado" / "agrega mi segunda sucursal").
OPTIONAL_TOP_LEVEL = {"locations"}
OPTIONAL_CONTENT_KEYS = {"featured_package"}

MAX_STR = 600          # tope defensivo por string (el generador ya trunca fino)
MAX_LIST = 60          # tope defensivo por lista
MAX_CORRECTION_TEXT = 3000


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def write_output(slug: str, status: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"slug={slug}\n")
            f.write(f"correction_status={status}\n")
    print(f"correction_status={status}")


def clamp(value, depth: int = 0):
    """Defensive recursive clamp on the LLM output: bounded strings/lists."""
    if depth > 8:
        return None
    if isinstance(value, str):
        return value[:MAX_STR]
    if isinstance(value, list):
        return [clamp(v, depth + 1) for v in value[:MAX_LIST]]
    if isinstance(value, dict):
        return {str(k)[:80]: clamp(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def llm_edit(original: dict, correction_text: str) -> dict | None:
    """Ask gpt-4o-mini for the full updated client JSON. None on any failure."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("WARN: OPENAI_API_KEY missing — falling back to manual", file=sys.stderr)
        return None

    prompt = (
        "You maintain the JSON that renders a small business's public service "
        "page. The page has two language versions: content.es (Spanish) and "
        "content.en (English) — they must stay in sync, so apply every change "
        "to BOTH blocks, translating the new text naturally into each language. "
        "Keep prices, numbers, phone numbers and proper nouns unchanged unless "
        "the customer asks to change them.\n\n"
        "Apply ONLY the changes the customer requests below (the request may be "
        "written in Spanish or English). Do not rewrite, reformat or 'improve' "
        "anything they did not ask about. If a change affects only PART of a "
        "combined value — e.g. changing Saturday inside 'Lunes a sábado de "
        "10:00 a 20:00' — split the value so only the requested part changes "
        "and everything else keeps its current data (correct: 'Lunes a viernes "
        "de 10:00 a 20:00. Sábado de 10:00 a 14:00'). Service entries and "
        "'featured_package' use separate 'name' and optional 'description' fields "
        "(services also have a 'category'); if you edit one whose 'name' still holds "
        "a combined 'Category — Name — Description' (or 'Name — Description') string "
        "from an older page, split it so the short name stays in 'name', the detail "
        "moves to 'description', and any leading section name goes in 'category'. "
        "Keep the exact same JSON structure "
        "and keys; you may add 'featured_package' inside a content block or a "
        "'locations' array at the top level only if the customer explicitly "
        "asks for that. Never change public_slug, logo_url, primary_image_url "
        "or gallery_images. If part of the request cannot be done by editing "
        "this JSON (photo/logo changes, new pages), skip that part silently.\n\n"
        "Return ONLY the complete updated JSON object.\n\n"
        f"CURRENT JSON:\n{json.dumps(original, ensure_ascii=False)}\n\n"
        f"CUSTOMER REQUEST:\n{correction_text}"
    )
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        updated = json.loads(content)
        return updated if isinstance(updated, dict) else None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as e:
        print(f"WARN: correction LLM call failed ({e})", file=sys.stderr)
        return None


def _types_compatible(orig, new) -> bool:
    """New value must keep the original's rough type. None↔str is allowed
    (nullable text/url fields); everything else must match container type."""
    if orig is None or new is None:
        return new is None or isinstance(new, str)
    if isinstance(orig, bool) or isinstance(new, bool):
        return isinstance(orig, bool) and isinstance(new, bool)
    for typ in (str, list, dict, (int, float)):
        if isinstance(orig, typ):
            return isinstance(new, typ)
    return False


def _validate_services(services) -> bool:
    if not isinstance(services, list) or not services:
        return False
    for svc in services:
        if not isinstance(svc, dict):
            return False
        if not isinstance(svc.get("category"), str) or not svc["category"].strip():
            return False
        if not isinstance(svc.get("name"), str) or not svc["name"].strip():
            return False
        if "price_label" in svc and not isinstance(svc["price_label"], str):
            return False
    return True


def _validate_content_block(orig_block: dict, new_block) -> bool:
    if not isinstance(new_block, dict):
        return False
    allowed = set(orig_block.keys()) | OPTIONAL_CONTENT_KEYS
    if not set(new_block.keys()) <= allowed:
        print(f"WARN: content block has unexpected keys: {set(new_block.keys()) - allowed}", file=sys.stderr)
        return False
    # Toda clave original debe seguir presente con tipo compatible.
    for key, orig_val in orig_block.items():
        if key not in new_block:
            return False
        if not _types_compatible(orig_val, new_block[key]):
            print(f"WARN: content key {key} changed type", file=sys.stderr)
            return False
    if not _validate_services(new_block.get("services")):
        return False
    fp = new_block.get("featured_package")
    if fp is not None and (not isinstance(fp, dict) or not isinstance(fp.get("name"), str)):
        return False
    return True


def validate_and_sanitize(original: dict, updated: dict) -> dict | None:
    """Validate the LLM output against the original schema; pin protected
    fields. Returns the sanitized dict, or None if it can't be trusted."""
    if not isinstance(updated, dict):
        return None
    updated = clamp(updated)

    allowed_top = set(original.keys()) | OPTIONAL_TOP_LEVEL
    if not set(updated.keys()) <= allowed_top:
        print(f"WARN: unexpected top-level keys: {set(updated.keys()) - allowed_top}", file=sys.stderr)
        return None
    for key, orig_val in original.items():
        if key not in updated:
            print(f"WARN: missing top-level key: {key}", file=sys.stderr)
            return None
        if key != "content" and not _types_compatible(orig_val, updated[key]):
            print(f"WARN: top-level key {key} changed type", file=sys.stderr)
            return None

    # Campos inmutables: se restauran del original, pase lo que pase.
    for key in PINNED_TOP_LEVEL:
        if key in original:
            updated[key] = original[key]

    if updated.get("brand_style") not in VALID_BRAND_STYLES:
        updated["brand_style"] = original.get("brand_style", "warm-sand")
    if updated.get("default_language") not in ("es", "en"):
        updated["default_language"] = original.get("default_language", "es")

    content = updated.get("content")
    orig_content = original.get("content") or {}
    if not isinstance(content, dict) or set(content.keys()) != set(orig_content.keys()):
        return None
    for lang, orig_block in orig_content.items():
        if not _validate_content_block(orig_block, content.get(lang)):
            return None

    locations = updated.get("locations")
    if locations is not None:
        if not isinstance(locations, list):
            return None
        for loc in locations:
            if not isinstance(loc, dict) or not all(isinstance(v, str) for v in loc.values()):
                return None

    # La página debe conservar al menos un contacto público (misma regla que
    # el build inicial) — una corrección no puede dejarla sin botones.
    if not (
        updated.get("whatsapp")
        or updated.get("phone")
        or updated.get("public_email")
        or updated.get("booking_url")
        or updated.get("website")
        or updated.get("tiktok")
        or updated.get("other_public_link")
        or updated.get("delivery_pickup_links")
        or updated.get("portfolio_link")
    ):
        print("WARN: correction would remove every public contact", file=sys.stderr)
        return None

    return updated


def main() -> int:
    raw = os.environ.get("CORRECTION_PAYLOAD", "")
    if not raw:
        fail("CORRECTION_PAYLOAD env var is empty")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"CORRECTION_PAYLOAD is not valid JSON: {e}")

    slug = str(event.get("slug", "")).strip()
    correction_text = str(event.get("correction_text", "")).strip()[:MAX_CORRECTION_TEXT]

    if not slug or not SLUG_RE.match(slug):
        fail("invalid or missing slug")
    if not correction_text:
        fail("missing correction_text")

    client_path = CLIENTS_DIR / f"{slug}.client.json"
    if not client_path.exists():
        # Página vieja sin JSON versionado — se atiende a mano.
        print(f"WARN: {client_path} not found — manual", file=sys.stderr)
        write_output(slug, "manual")
        return 0

    original = json.loads(client_path.read_text(encoding="utf-8"))

    updated = llm_edit(original, correction_text)
    if updated is None:
        write_output(slug, "manual")
        return 0

    sanitized = validate_and_sanitize(original, updated)
    if sanitized is None:
        print("WARN: LLM output failed validation — manual", file=sys.stderr)
        write_output(slug, "manual")
        return 0

    if sanitized == original:
        # El modelo no cambió nada: mejor prometer aplicación manual que
        # mandarle al cliente un "quedó actualizada" falso.
        print("WARN: LLM produced a no-op edit — manual", file=sys.stderr)
        write_output(slug, "manual")
        return 0

    client_path.write_text(
        json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"client JSON updated: {client_path.relative_to(REPO_ROOT)}")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "generator" / "generate_service_menu.py"), "--client", str(client_path)],
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        # Restaurar JSON *y* la página generada a medias: si quedaran sucios,
        # el paso de commit del workflow publicaría una página rota.
        client_path.write_text(
            json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "checkout", "--", f"public/links/{slug}", f"data/clients/{slug}.client.json"],
            cwd=REPO_ROOT,
        )
        print(f"WARN: generator failed ({result.returncode}) — original restored, manual", file=sys.stderr)
        write_output(slug, "manual")
        return 0

    index_html = LINKS_DIR / slug / "index.html"
    if not index_html.exists():
        print(f"WARN: expected output missing: {index_html} — manual", file=sys.stderr)
        write_output(slug, "manual")
        return 0

    write_output(slug, "applied")
    print(f"correction applied: public/links/{slug}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
