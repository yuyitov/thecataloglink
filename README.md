# The Catalog Link — Service Menu App

> Exportado de la Link Factory (`scripts/export_vertical.py`, vertical
> `catalog`). No edites `generator/`, `worker/worker.js`,
> `worker/product-config.mjs` ni `worker/stripe-filter.mjs` a mano: son el
> **motor compartido**. Para traer un fix del motor a este repo, corré EN LA
> FÁBRICA (link-factory):
>
> ```powershell
> python scripts/export_vertical.py catalog --engine-only --output "<ruta-a-este-repo>"
> ```
>
> `--engine-only` actualiza **solo** los archivos de motor (con backup de lo que
> cambie en `.engine-only-backup/`) y **nunca** toca lo hecho a mano:
> `worker/wrangler.toml` (secrets, plinks), `vertical.yaml`, `data/` (clientes
> reales), `public/` (tienda, legales, links de clientes), `.github/` ni este
> README. **NUNCA corras un export completo (`--force`) sobre este repo**: borra
> el repo entero y con él la tienda, los legales y los clientes reales.
>
> La **tienda** (landing ES/EN, los 4 legales, `/correct/`, `CNAME` y
> `.github/workflows/pages.yml`) la GENERA la fábrica desde `store.yaml` + la
> sección `legal:` de `vertical.yaml`. Para bajarle mejoras de plantilla a este
> repo, con backup de lo que pise:
>
> ```powershell
> python scripts/export_vertical.py catalog --store-only --output "<ruta-a-este-repo>"
> ```
>
> Lo que quieras conservar de una edición a mano subilo a `store.yaml` en la
> fábrica, no al HTML publicado: el siguiente `--store-only` lo vuelve a pisar.
>
> `vertical.yaml`, `data/`, `worker/wrangler.toml` y los workflows son propios de
> este repo (versionalos con sus propios cambios, p. ej. el dominio real o los
> ids de infra de Etapa B).

## Layout

- `generator/` — motor Python (idéntico al de la fábrica; rutas aplanadas a
  este repo, sin `verticals/<id>/` ni `LINK_FACTORY_VERTICAL`).
- `vertical.yaml` — marca, dominio, estilos, strings, bloques.
- `data/demos/*.json` — payloads de demo. `data/clients/*.json` — clientes
  reales (vacío hasta el primer intake).
- `public/demos/`, `public/links/` — HTML generado (regenéralo con el
  comando de abajo; no lo edites a mano).
- `public/index.html`, `public/es/`, `public/{terms,privacy}/`,
  `public/es/{terminos,privacidad}/`, `public/correct/`, `public/CNAME` — la
  **tienda**, generada por la fábrica (`--store-only` para actualizarla).
- `store.yaml` no viaja a este repo: vive en la fábrica, junto al resto de la
  configuración de la vertical.
- `worker/` — Cloudflare Worker. `wrangler.toml` ya tiene `WORKER_NAME`,
  `PRODUCT_ID`, `BRAND_NAME`, `VALID_BRAND_STYLES` (desde `styles.catalog`) y
  `GITHUB_ACTIONS_EVENT`; el resto de los placeholders `{...}` (KV namespace,
  Payment Link de Stripe, URLs de los forms de Tally, dominio del worker, etc.)
  los llena Etapa B. `tally-field-aliases.json` es la copia editable del mapeo
  de intake de esta vertical (validala con `create_tally_forms.py
  --check-mapping` antes de crear los forms de Tally). Es MOTOR: `--engine-only`
  lo actualiza, pero si lo personalizaste te deja tu versión en
  `.engine-only-backup/` y avisa — compará el diff antes de commitear.
- `.github/workflows/generate-catalog-page.yml` — genera la página al
  recibir el evento `new-catalog-service-menu` (lo dispara el Worker
  tras un pago validado).

## Cómo regenerar

```powershell
pip install -r requirements.txt
python generator/generate_catalog.py
```

## Antes de desplegar infra real

Este export cubre la Etapa A del runbook (local, gratis, sin aprobación).
Para pasar a producción real (repo de GitHub propio, dominio, Cloudflare
Worker + KV, Stripe, Tally, SendGrid) seguí `docs/RUNBOOK_LANZAMIENTO.md`
§ Etapa B en el repo de la fábrica — nada de eso se hizo automáticamente.
