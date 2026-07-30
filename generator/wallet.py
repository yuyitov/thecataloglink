"""Pase genérico de Google Wallet — bloque `wallet_google` (linkFactory/18).

POR QUÉ EXISTE
--------------
El mismo insight que trajo el vCard, un paso más lejos: el cliente recurrente
olvida el enlace. El `.vcf` guarda el negocio en la agenda; el pase de wallet lo
guarda en la app que la gente **sí vuelve a abrir**. Decisión 2a de Vero
(2026-07-27, charly/34): el wallet entra al 100% del producto.

Referencia viva (solo lectura): `My Guest/MyGuest/books/scripts/wallet_passes.py`
y `docs/WALLET_PASSES_ENABLE_CHECKLIST.md`. Ahí se midió contra la doc oficial
por qué esto no se puede firmar en el navegador y qué exige Google. Este módulo
es el mismo mecanismo, con los datos derivados del payload del negocio en vez de
los de una propiedad.

LOS DOS INTERRUPTORES — y por qué son DOS y no uno
--------------------------------------------------
1. **El bloque** `wallet_google` en `vertical.yaml` (registro de blocks.py,
   default `none`). Responde a *"¿este producto quiere el pase?"*. Es el mismo
   patrón del vCard.

2. **El interruptor de PUBLICACIÓN**, `LINK_FACTORY_GOOGLE_WALLET_PUBLISH`.
   Responde a *"¿Google ya nos dejó publicar?"*, que **no es una decisión de
   producto sino un hecho externo**, y por eso no vive en el YAML de nadie.

   ⛔ **HOY ESTÁ APAGADO, Y NO SE ENCIENDE HASTA QUE LLEGUE EL CORREO DE
   APROBACIÓN DE GOOGLE.** Estado al 2026-07-30: Issuer "Bridge Company"
   (Merchant ID BCR2DN6DTKVJTFR6) **EN REVISIÓN**. Google permite CREAR y
   PROBAR pases; publicar espera la aprobación. La ficha que espera ese correo
   es `charly/37`.

   La razón de fondo está medida en el checklist de My Guest: **mientras la
   cuenta está en demo mode el pase sale marcado `[TEST ONLY]` y solo lo pueden
   guardar las cuentas admin/developer y las de prueba dadas de alta.** Un
   cliente real toca el botón y **no pasa nada** — no da error, no hace nada.
   Publicarlo hoy sería poner un botón muerto en la página de alguien que ya
   pagó, que es exactamente la clase de fallo silencioso que persigue el PLAN
   CERO REGRESIONES.

   Con este interruptor apagado, `build_google_wallet_url()` devuelve "" pase lo
   que pase, y **ninguna página generada menciona `pay.google.com`** aunque el
   bloque esté encendido en las cinco verticales y las credenciales estén
   puestas. Lo afirma `tests/python/test_wallet_google.py` con mutación.

APPLE WALLET: CERO CÓDIGO, A PROPÓSITO
--------------------------------------
No hay ni una línea de Apple en este módulo y no debe haberla. Está sellado
hasta que Vero pague los **$99 USD/año** del Apple Developer Program — hasta
entonces no hay certificado con qué firmar y cualquier código sería código
muerto que hay que mantener.

- **Dónde está la referencia cuando se desbloquee:** `build_apple_pkpass` y
  `build_apple_pass_json` en `My Guest/MyGuest/books/scripts/wallet_passes.py`
  (escritas, probadas y apagadas), y la receta paso a paso en
  `My Guest/MyGuest/docs/WALLET_PASSES_ENABLE_CHECKLIST.md` §"Prender Apple
  Wallet" — incluye la trampa del content-type: si el servidor manda el
  `.pkpass` como `application/octet-stream`, iOS lo baja como archivo suelto y
  Wallet no lo abre.
- **Qué lo desbloquea:** que Vero pague los $99. Nada más; el mecanismo
  funciona en cuanto hay certificado, sin revisión manual como la de Google.

QUÉ VA DENTRO DEL PASE (y qué nunca)
------------------------------------
Un pase se comparte igual que una tarjeta de contacto, así que lleva **solo lo
que la página ya publica**: nombre del negocio, teléfono público y el enlace de
su página. Misma regla que el `.vcf`. Nada que no esté ya en el HTML.

FIRMA
-----
RS256 con `openssl` por línea de comandos — el mismo camino que My Guest, y por
la misma razón: con el interruptor apagado no cuesta ni una dependencia nueva
de pip en el CI. Nada de esto se ejecuta si el interruptor está apagado.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Largo seguro de un JWT dentro de una URL, según la doc de Google. Pasarse no
# da error: el navegador trunca el link y el botón falla en la mano del cliente
# — el fallo silencioso otra vez. Al pasarse se prefiere NO imprimir el botón.
GOOGLE_JWT_SAFE_LENGTH = 1800

# El interruptor de PUBLICACIÓN. Ver la nota larga del encabezado.
PUBLISH_FLAG = "LINK_FACTORY_GOOGLE_WALLET_PUBLISH"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _warn(mensaje: str) -> None:
    """Avisa fuerte y sigue. La página es el producto; el pase es un extra.

    Una credencial mal puesta NO puede tumbar la generación de la página de un
    cliente que ya pagó — misma regla que My Guest.
    """
    print(f"[wallet] AVISO: {mensaje}", file=sys.stderr)


def publicacion_encendida() -> bool:
    """True solo con un valor afirmativo EXPLÍCITO.

    Nada de "si la variable existe": una variable vacía, un `0` o un `false`
    dejan el interruptor apagado. Encenderlo tiene que ser un acto deliberado.
    """
    return _env(PUBLISH_FLAG).lower() in ("1", "true", "yes", "on")


def _b64url(crudo: bytes) -> str:
    return base64.urlsafe_b64encode(crudo).decode("ascii").rstrip("=")


def _id_suffix(slug) -> str:
    """Los ids de Google Wallet solo aceptan [A-Za-z0-9._-]."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", str(slug or "")).strip("-") or "pase"


def _openssl(args, **kwargs):
    binario = _env("OPENSSL_BIN") or "openssl"
    return subprocess.run([binario, *args], capture_output=True, **kwargs)


def openssl_disponible() -> bool:
    try:
        return _openssl(["version"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def datos_del_pase(payload: dict, page_url: str) -> dict:
    """Los tres datos del pase, DERIVADOS del payload — nunca escritos a mano.

    `phone` cae en `whatsapp` como respaldo (los dos son números y lo que
    importa es el caller-id), igual que `make_vcard`. El catálogo no captura
    `phone`: su número público es el del `sale_button`, y por eso también se
    mira aquí — un solo lugar que sabe leer las tres formas vivas.
    """
    nombre = str(payload.get("business_name", "") or "").strip()
    telefono = ""
    for clave in ("phone", "whatsapp"):
        valor = str(payload.get(clave, "") or "").strip()
        if valor:
            telefono = valor
            break
    if not telefono:
        venta = payload.get("sale_button")
        if isinstance(venta, dict):
            telefono = str(venta.get("value", "") or "").strip()
    return {
        "business_name": nombre,
        "phone": telefono,
        "page_url": str(page_url or "").strip(),
    }


def build_google_wallet_payload(datos: dict, *, issuer_id: str,
                                class_suffix: str, slug: str,
                                brand: str = "") -> dict:
    """El objeto genérico del pase. Solo datos que la página ya publica.

    La clase viaja DENTRO del JWT (`genericClasses`): Google la crea en el
    primer guardado y así no hace falta una llamada previa a la API REST. Es la
    misma forma que usa My Guest.

    OJO con `class_suffix`: en la consola de Vero ya existe la clase
    `bridge_generic_demo` (Activa, vacía) y **no se borra** — se publicará sola
    cuando Google conceda el acceso.
    """
    object_id = f"{issuer_id}.{_id_suffix(slug)}"
    class_id = f"{issuer_id}.{class_suffix}"

    # `cardTitle` es el renglon chico de arriba y `header` el grande. Al
    # principio los dos llevaban el nombre del negocio, repetido. Ahora arriba
    # va la MARCA (HMU Link, ModaLink...) y abajo el negocio, que es como lo
    # hace My Guest y como se lee mejor — y ademas es lo que hizo caber el
    # pase: ver la nota de presupuesto de bytes mas abajo.
    generico = {
        "id": object_id,
        "classId": class_id,
        "state": "ACTIVE",
        "cardTitle": {"defaultValue": {
            "language": "es", "value": brand or datos["business_name"]}},
        "header": {"defaultValue": {"language": "es",
                                    "value": datos["business_name"]}},
    }
    if datos["phone"]:
        generico["subheader"] = {"defaultValue": {"language": "es",
                                                  "value": datos["phone"]}}
    if datos["page_url"]:
        # EL ENLACE VA DOS VECES, Y NINGUNA SOBRA (medido con Vero sobre el
        # pase REAL el 2026-07-30, no supuesto).
        #
        # El primer intento puso el enlace SOLO en el QR, con `alternateText`
        # vacío. Vero abrió el pase en su teléfono y cazó el fallo: **el QR es
        # inútil para el dueño del pase.** Quien lo tiene guardado ya está en
        # su teléfono; para escanear su propio código necesitaría un segundo
        # aparato. El QR sirve para ENSEÑARLE la página a otra persona; para
        # volver uno mismo hace falta un enlace que se pueda tocar. Y el
        # enlace era el punto entero del bloque: el cliente recurrente olvida
        # el link.
        #
        #   1. `alternateText` — el renglón que Google pinta DEBAJO del QR.
        #      Vacío no salía nada. Es el sitio más visible del pase.
        #   2. `linksModuleData` — el enlace TOCABLE de la vista de detalle.
        #      Es el que abre la página de un toque.
        #
        # PRESUPUESTO DE BYTES — por qué no van TRES veces.
        # El arreglo se probó primero con un `textModulesData` extra (el
        # enlace también como texto, por si una versión de la app no pinta el
        # módulo de enlaces). Al medirlo contra los slugs REALES de los
        # clientes vivos, **3 de 7 cruzaban los 1800 caracteres y se quedaban
        # SIN botón** — o sea, la redundancia de más borraba el botón entero
        # justo en los clientes de nombre largo. `textModulesData` costaba 163
        # caracteres; quitarlo devolvió el margen. La regla que queda: antes de
        # agregar un campo al pase, medirlo contra el peor cliente vivo, no
        # contra un demo corto.
        generico["linksModuleData"] = {
            "uris": [{"uri": datos["page_url"], "description": "Abrir"}]
        }
        generico["barcode"] = {"type": "QR_CODE", "value": datos["page_url"],
                               "alternateText": datos["page_url"]}
    return {"genericClasses": [{"id": class_id}], "genericObjects": [generico]}


def _firmar_rs256(entrada: bytes, private_key_pem: str):
    """Firma RS256 con openssl. Devuelve bytes, o None si falla."""
    tmp = tempfile.mkdtemp(prefix="lf-gw-")
    try:
        ruta = os.path.join(tmp, "sa.pem")
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write(private_key_pem)
        r = _openssl(["dgst", "-sha256", "-sign", ruta], input=entrada)
        if r.returncode != 0:
            _warn("openssl no pudo firmar el JWT: "
                  + r.stderr.decode("utf-8", "ignore").strip())
            return None
        return r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_google_wallet_url(payload: dict, page_url: str, *, brand: str = "",
                            ignorar_interruptor: bool = False) -> str:
    """La URL de "Agregar a Google Wallet", o "" si no se puede/no se debe.

    Nunca lanza: la página se genera con o sin botón.

    `ignorar_interruptor` existe SOLO para el camino de prueba en modo test
    (`scripts/probar_wallet_google.py`), que imprime el link en la terminal para
    guardarlo a mano en un Android. **Ningún generador lo pasa** — si algún día
    alguien lo pasa desde el camino de las páginas, el pase `[TEST ONLY]` acaba
    publicado en la página de un cliente. Lo vigila
    test_wallet_google.py::test_ningun_generador_ignora_el_interruptor.
    """
    if not (ignorar_interruptor or publicacion_encendida()):
        return ""

    issuer_id = _env("GOOGLE_WALLET_ISSUER_ID")
    class_suffix = _env("GOOGLE_WALLET_CLASS_SUFFIX") or "bridge_generic_demo"
    cuenta_cruda = _env("GOOGLE_WALLET_SERVICE_ACCOUNT_JSON")

    if not issuer_id or not cuenta_cruda:
        _warn("falta GOOGLE_WALLET_ISSUER_ID o "
              "GOOGLE_WALLET_SERVICE_ACCOUNT_JSON: la página sale sin botón.")
        return ""
    try:
        cuenta = json.loads(cuenta_cruda)
    except ValueError as exc:
        _warn(f"GOOGLE_WALLET_SERVICE_ACCOUNT_JSON no es JSON válido: {exc}")
        return ""

    client_email = str(cuenta.get("client_email") or "").strip()
    private_key = str(cuenta.get("private_key") or "")
    if not client_email or "PRIVATE KEY" not in private_key:
        _warn("la cuenta de servicio no trae client_email o private_key.")
        return ""
    if not openssl_disponible():
        _warn("openssl no está disponible: no se puede firmar el pase.")
        return ""

    datos = datos_del_pase(payload, page_url)
    if not datos["business_name"]:
        _warn("el payload no trae business_name: no se arma ningún pase.")
        return ""

    origen = re.match(r"^https?://[^/]+", datos["page_url"] or "")
    claims = {
        "iss": client_email,
        "aud": "google",
        "typ": "savetowallet",
        "origins": [origen.group(0)] if origen else [],
        "payload": build_google_wallet_payload(
            datos, issuer_id=issuer_id, class_suffix=class_suffix,
            slug=payload.get("public_slug") or datos["business_name"],
            brand=brand),
    }
    cabecera = {"alg": "RS256", "typ": "JWT"}
    entrada = ".".join([
        _b64url(json.dumps(cabecera, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(claims, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")),
    ]).encode("ascii")

    firma = _firmar_rs256(entrada, private_key)
    if not firma:
        return ""
    jwt = entrada.decode("ascii") + "." + _b64url(firma)

    if len(jwt) > GOOGLE_JWT_SAFE_LENGTH:
        _warn(f"el JWT mide {len(jwt)} caracteres y el límite seguro es "
              f"{GOOGLE_JWT_SAFE_LENGTH}. La página sale sin botón en vez de "
              "publicar un link que el navegador puede truncar.")
        return ""
    return f"https://pay.google.com/gp/v/save/{jwt}"
