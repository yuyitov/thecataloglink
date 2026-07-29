/**
 * Product-config puro y sin efectos secundarios (testeable con `node --test`,
 * igual que stripe-filter.mjs de la Fase 2.1): namespacing de KV keys por
 * `PRODUCT_ID` y marca usada en los correos. Ambos leen únicamente de `env`
 * — nunca tocan KV ni red — así una vertical nueva solo necesita sus propias
 * env vars en `wrangler.toml` (ver `wrangler.toml.tpl`), sin editar código.
 */

// Un export nuevo siempre declara PRODUCT_ID. Desde ese momento estos valores
// dejan de ser opcionales: caer a los defaults históricos de HMU enviaría
// correos, CORS o enlaces de una marca a otra sin ningún error visible.
//
// El env completamente vacío se conserva como compatibilidad del deploy
// histórico de HMU y de pruebas unitarias puras. No es una escotilla para una
// vertical nueva: export_vertical.py siempre escribe PRODUCT_ID.
export const REQUIRED_VERTICAL_ENV = [
  'PRODUCT_ID',
  'WORKER_NAME',
  'BRAND_NAME',
  'BRAND_TAGLINE',
  'VALID_BRAND_STYLES',
  'GITHUB_REPO',
  'GITHUB_ACTIONS_EVENT',
  'TALLY_FORM_URL_EN',
  'TALLY_FORM_URL_ES',
  'FROM_EMAIL',
  'PUBLIC_BOOK_BASE_URL',
]

export function runtimeConfigErrors(env) {
  const productId = String(env?.PRODUCT_ID ?? '').trim()
  if (!productId) return []
  return REQUIRED_VERTICAL_ENV.filter(
    (name) => String(env?.[name] ?? '').trim() === '',
  )
}

// Todas las KV keys del worker se namespacean por PRODUCT_ID (no por dominio:
// el mismo Cloudflare account puede alojar varios Workers, cada uno con su
// propio KV namespace, pero el prefijo evita ambigüedad si dos verticales
// llegaran a compartir namespace). Sin PRODUCT_ID configurado cae a "hmu"
// (comportamiento histórico, sin romper el deploy actual de HMU Link).
export function kvKey(env, ns, ...parts) {
  const productId = (env.PRODUCT_ID || 'hmu').trim();
  return [`${productId}_${ns}`, ...parts].join(':');
}

// Marca usada en asuntos/cuerpos/footers de los correos — parametrizable por
// vertical vía env; los defaults son los de HMU Link (comportamiento actual).
export function brandName(env) {
  return (env.BRAND_NAME || 'HMU Link').trim();
}

export function brandTagline(env) {
  return (env.BRAND_TAGLINE || 'Service Menus for Small Businesses').trim();
}

export function brandDomain(env) {
  return (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim().replace(/^https?:\/\//, '');
}

// Identidad de ESTE worker (no de la marca pública): usada en /health y en el
// User-Agent que dispatchGitHubAction manda a la API de GitHub — nunca la ve un
// cliente. Antes 'service-menu-worker' estaba hardcodeado en worker.js, así que
// una vertical exportada (p.ej. PawContact) reportaba una identidad ajena.
// Default = mismo literal de siempre (comportamiento actual de HMU intacto).
// export_vertical.py ya resuelve WORKER_NAME (= "<vertical_id>-worker") para el
// `name` de wrangler.toml; declararlo también en [vars] lo expone a env.
export function workerName(env) {
  return (env.WORKER_NAME || 'service-menu-worker').trim();
}

// Metadata key con la que ESTE worker marca en Stripe las Checkout Sessions de
// compra de corrección adicional (/buy-correction) y con la que el webhook /
// stripe-filter.mjs las reconoce de vuelta. Antes era el literal hmu_correction
// en los dos lados — y como la cuenta de Stripe se COMPARTE entre productos,
// una vertical exportada que heredara el literal colisionaba con HMU: cada
// compra de corrección de una la procesaba (también) la otra. Se deriva de
// PRODUCT_ID (mismo namespacing que kvKey): "<PRODUCT_ID>_correction", con
// default hmu_correction si PRODUCT_ID no está configurado — cero cambio de
// comportamiento para el deploy real de HMU. Debe usarse en AMBOS lados
// (crear el checkout y detectarlo), nunca volver al literal (hay test de
// regresión que lo prohíbe fuera de este default).
export function correctionMetadataKey(env) {
  const productId = ((env && env.PRODUCT_ID) || 'hmu').trim();
  return `${productId}_correction`;
}

// Modificaciones GRATIS incluidas en la compra. Estándar de la casa decidido
// por Vero el 2026-07-20: 2 (antes 1). Se configura por vertical en
// vertical.yaml (`legal.free_changes`), que el export vuelca a FREE_CHANGES en
// wrangler.toml — así los Términos publicados y lo que el worker realmente
// entrega salen del MISMO número y no pueden divergir.
export const DEFAULT_FREE_CHANGES = 2;

export function freeChanges(env) {
  const raw = Number.parseInt(String((env && env.FREE_CHANGES) ?? '').trim(), 10);
  if (!Number.isInteger(raw) || raw < 0) return DEFAULT_FREE_CHANGES;
  return Math.min(raw, 10);
}

// Tope del base64 que se acepta para adjuntar inline en un correo (~75 KB de
// PNG). Un QR de segno pesa ~1-3 KB; el tope existe para que un body raro no
// haga que SendGrid rechace el envío entero y el cliente se quede sin entrega.
export const MAX_INLINE_IMAGE_BASE64 = 100000;

// Valida base64 "puro" (sin data: URI, sin saltos de línea) para adjuntos de
// correo. Devuelve la cadena limpia o '' — nunca lanza: el QR es un extra, la
// entrega no puede caerse por él.
export function sanitizeBase64Image(value, maxLen = MAX_INLINE_IMAGE_BASE64) {
  const raw = typeof value === 'string' ? value.trim() : '';
  if (!raw || raw.length > maxLen) return '';
  return /^[A-Za-z0-9+/]+={0,2}$/.test(raw) ? raw : '';
}

// Idioma del correo post-pago a partir de la moneda del checkout de Stripe.
// MXN -> español, USD -> inglés, CUALQUIER OTRA -> null = correo BILINGÜE.
// Antes el motor hacía `currency === 'mxn' ? 'es' : 'en'`, así que un comprador
// en CAD o EUR recibía inglés a secas por descarte; My Guest ya vende en CAD.
// Con null, quien arma el correo manda las dos versiones y el cliente elige
// (mismo criterio que ModaLink, la mejor implementación de la auditoría §7.11).
export function emailLangFromCurrency(currency) {
  const normalized = String(currency || '').trim().toLowerCase();
  if (normalized === 'mxn') return 'es';
  if (normalized === 'usd') return 'en';
  return null;
}

// Sin acentos, minúsculas, todo lo no-alfanumérico -> '_' — MISMA normalización
// que create_tally_forms.py::nk() en Python (regla fija: los dos deben coincidir
// byte a byte o el check-mapping y el worker divergen sobre qué título matchea).
// Vive aquí (no en worker.js) porque languageQuestionAliases() la necesita y este
// módulo es el que se testea con node --test sin tocar KV/red.
export function normalizeKey(key) {
  return String(key || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
}

// Alias de la pregunta explícita de idioma, derivados de BRAND_NAME en vez de
// vivir como literal fijo en tally-field-aliases.json. Antes ese JSON —
// compartido/copiado verbatim a cada vertical por export_vertical.py — traía
// hardcodeado 'hmu_link' ("...your hmu link show first"), así que una vertical
// nueva que SÍ preguntara el idioma explícitamente (p.ej. "your PawContact")
// nunca hubiera matcheado sin editar el JSON a mano. Con BRAND_NAME="HMU Link"
// esto reproduce EXACTO los dos alias históricos (ver test de paridad).
export function languageQuestionAliases(env) {
  const slug = normalizeKey(brandName(env));
  return [
    `which_language_should_your_${slug}_show_first`,
    `en_que_idioma_debe_aparecer_primero_tu_${slug}`
  ];
}

// Idioma del formulario de Tally al que corresponde un form_id, derivado de las
// env vars TALLY_FORM_URL_ES/EN (formato `https://tally.so/r/<FORM_ID>?order_id=`)
// que TODO worker ya tiene. Antes buildPublicPayload hardcodeaba 'MeyDpk' (el
// form ES de HMU) como fallback, así que en cualquier vertical exportada el
// form_id nunca coincidía y TODO caía a inglés (bug real de PawContact: cliente
// llenó el form ES 0QyRRB y su página salió en 'en'). Devuelve 'es'/'en' si el
// form_id es el de ESA vertical, o null si no se reconoce (el caller decide el
// último recurso). Nunca lanza.
export function tallyFormLang(env, formId) {
  const id = String(formId || '').trim();
  if (!id) return null;
  const idFromUrl = (url) => {
    const m = String(url || '').match(/tally\.so\/r\/([A-Za-z0-9]+)/);
    return m ? m[1] : null;
  };
  if (idFromUrl(env && env.TALLY_FORM_URL_ES) === id) return 'es';
  if (idFromUrl(env && env.TALLY_FORM_URL_EN) === id) return 'en';
  return null;
}

// Idioma principal de la vertical: el último recurso cuando no hay ni respuesta
// de idioma ni un form_id reconocible. Sale de `intake.default_language` del
// vertical.yaml (export_vertical.py lo vuelca a esta var). Sin la var → 'en',
// que es el comportamiento histórico y el de HMU/PawContact hoy.
//
// Existe por ModaLink, la única vertical cuyos formularios NO preguntan el
// idioma (tiene uno por idioma) y que además vende en México: con el 'en' fijo,
// un intake cuyo form_id no se reconociera —una URL de Tally reemitida, una var
// mal copiada— publicaría en inglés la página de una clienta que escribió todo
// en español, y el generador la rechazaría por "the English page still looks
// Spanish". Su worker propio ya se defendía de eso adivinando por cuál clave de
// `business_name` venía contestada; esto es esa misma defensa, dicha en la
// config en vez de adivinada en el código.
export function defaultIntakeLanguage(env) {
  const raw = String((env && env.DEFAULT_INTAKE_LANGUAGE) || '').trim().toLowerCase();
  return raw === 'es' || raw === 'en' ? raw : 'en';
}

// Regla completa del idioma por defecto de la página del cliente (decisión de
// Vero, 2026-07-17): (1) respuesta explícita del cliente a la pregunta de
// idioma → esa manda; (2) sin respuesta, el idioma del formulario que llenó
// (tallyFormLang); (3) último recurso → el idioma principal de la vertical
// (defaultIntakeLanguage, 'en' si no lo declara). Vive aquí (no en worker.js)
// para testearse con node --test sin tocar KV/red, igual que el resto del módulo.
export function resolveDefaultLanguage(langRaw, env, formId) {
  const raw = String(langRaw || '').toLowerCase();
  if (raw.includes('espa') || raw.includes('span')) return 'es';
  if (raw.includes('engl') || raw.includes('ingl')) return 'en';
  return tallyFormLang(env, formId) || defaultIntakeLanguage(env);
}

// ─────────────────────────────────────────────────────────────────────────────
// PLANTILLA DE PÁGINA (F6 bloque 2)
//
// El motor renderiza dos plantillas base: `service-menu` (las 5 verticales que
// hoy venden) y `catalog` (The Catalog Link). En Python la fuente de verdad es
// `vertical.yaml::template`, leída por vertical_config.py; el worker no lee YAML,
// así que el export vuelca ese mismo valor a la var TEMPLATE del wrangler.toml.
//
// El default es `service-menu` A PROPÓSITO: los 5 workers ya desplegados NO
// declaran TEMPLATE y deben seguir comportándose byte a byte igual. Un valor
// desconocido también cae al default — el worker nunca debe negarse a procesar
// un intake pagado por una var mal escrita; lo que se ramifica por plantilla es
// qué campos se extraen, y esa decisión falla hacia lo que ya funciona.
export const DEFAULT_TEMPLATE = 'service-menu';
export const KNOWN_TEMPLATES = ['service-menu', 'catalog'];

export function pageTemplate(env) {
  const raw = String((env && env.TEMPLATE) || '').trim().toLowerCase();
  return KNOWN_TEMPLATES.includes(raw) ? raw : DEFAULT_TEMPLATE;
}

// ─────────────────────────────────────────────────────────────────────────────
// BOTÓN DE VENTA DEL CATÁLOGO (F6 bloque 2)
//
// El catálogo no tiene los 8 canales de contacto de service-menu: tiene UN botón
// por producto, y el cliente elige su canal en el claim. Los tres tipos son los
// que reconoce generate_catalog.py::SALE_KINDS — si divergen, el generador
// rechaza el payload de un cliente que YA PAGÓ, así que el nombre exacto importa
// más que la comodidad de escribirlo bonito.
export const SALE_BUTTON_KINDS = ['whatsapp', 'sms', 'tel'];

// Respuesta de la pregunta "¿por dónde te escriben?" -> kind del generador.
// Acepta el texto de la opción en los dos idiomas (normalizado con normalizeKey)
// y también el valor crudo por si el formulario cambia de wording.
const SALE_KIND_ALIASES = {
  whatsapp: 'whatsapp',
  wa: 'whatsapp',
  wpp: 'whatsapp',
  whats: 'whatsapp',
  sms: 'sms',
  mensaje: 'sms',
  mensaje_de_texto: 'sms',
  text: 'sms',
  text_message: 'sms',
  texto: 'sms',
  tel: 'tel',
  telefono: 'tel',
  phone: 'tel',
  call: 'tel',
  llamada: 'tel',
  llamar: 'tel',
  phone_call: 'tel',
  llamada_telefonica: 'tel',
};

// Devuelve el kind normalizado, o '' si la respuesta no se reconoce. Fail-open
// hacia WhatsApp NO: un botón que abre el canal equivocado es peor que uno que
// el gate de intake incompleto detiene con un motivo legible.
export function normalizeSaleKind(value) {
  const key = normalizeKey(value);
  if (!key) return '';
  if (SALE_KIND_ALIASES[key]) return SALE_KIND_ALIASES[key];
  // La opción del formulario suele venir con explicación ("WhatsApp — te
  // escriben al chat"); basta con que empiece por un alias conocido.
  for (const [alias, kind] of Object.entries(SALE_KIND_ALIASES)) {
    if (key.startsWith(`${alias}_`)) return kind;
  }
  return '';
}

// El valor del botón: un teléfono (se conservan los dígitos, más el '+' inicial
// que el cliente suele escribir) o, como escotilla, una URL pública. Igual que
// generate_catalog.py::sale_href, que acepta las dos formas.
export function normalizeSaleValue(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (/^https?:\/\//i.test(raw)) return raw;
  const digits = raw.replace(/\D/g, '');
  return digits || raw;
}

export function emailFooterHtml(env) {
  return `${brandName(env)} — ${brandTagline(env)}`;
}

export function emailFooterText(env) {
  return `${brandName(env)} — ${brandDomain(env)}`;
}

// Origin permitido por CORS. La API es server-to-server (Stripe, Tally, GitHub
// Actions) — ningún navegador la llama — así que se acota al origin del sitio
// en vez de '*'. Se deriva de PUBLIC_BOOK_BASE_URL (el mismo dominio público de
// la vertical), sin barra final. Default = el de HMU (comportamiento actual).
export function corsOrigin(env) {
  return (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim().replace(/\/+$/, '');
}

// Catálogo de estilos por defecto: los 12 de HMU Link. Es el fallback cuando
// VALID_BRAND_STYLES no está configurado, así que el deploy real de HMU no
// cambia aunque no declare la var.
export const DEFAULT_BRAND_STYLES = [
  'black-gold', 'soft-blush', 'charcoal-clean', 'warm-sand',
  'aqua-clean', 'sage-calm', 'electric-slate', 'terracotta-warm',
  'sunny-paws', 'midnight-ink', 'clarity-editorial', 'horizon-teal'
];

// Estilos válidos con los que el worker acepta el intake de Tally, por vertical.
// Vienen de env.VALID_BRAND_STYLES (coma-separado; export_vertical.py lo llena
// desde styles.catalog del vertical.yaml, la fuente de verdad). Ausente o
// vacío/mal escrito → cae a los 12 de HMU (nunca a lista vacía: eso rompería
// todo el intake — el fail-safe correcto aquí es el catálogo por defecto, no
// fail-closed a cero).
export function validBrandStyles(env) {
  const raw = (env.VALID_BRAND_STYLES || '').trim();
  if (!raw) return [...DEFAULT_BRAND_STYLES];
  const parsed = raw.split(',').map((s) => s.trim()).filter(Boolean);
  return parsed.length ? parsed : [...DEFAULT_BRAND_STYLES];
}

// Estilo al que cae un intake cuyo estilo elegido no está en el catálogo.
// 'warm-sand' es el neutro histórico de HMU (y está en el catálogo de PawContact),
// así que se prefiere cuando existe — conservando el comportamiento actual;
// si una vertical no lo incluye, cae a su primer estilo (siempre válido).
export function fallbackBrandStyle(styles) {
  return styles.includes('warm-sand') ? 'warm-sand' : styles[0];
}

// ─────────────────────────────────────────────────────────────────────────────
// PREFILL DE PROSPECTO (Cory → worker → Tally), Fase 2.4
//
// Un prospecto que Cory generó (p. ej. una demo de HMU con caducidad) compra por
// un link rastreado: Cory pone `client_reference_id=<slug>` en el checkout de
// Stripe, así que el MISMO webhook llega aquí con el slug. Con el slug, el worker
// pide a Cory un `prefill.json` PÚBLICO (los datos ya son públicos: están en la
// demo del prospecto) y prellena por URL los campos VISIBLES del formulario de
// Tally, en el idioma del comprador. El cliente ve sus datos ya cargados y puede
// dejarlos, editarlos o borrarlos — su envío es el que genera su HMU Link
// permanente. Todo es fail-open: sin base configurada, sin slug, o si el fetch
// falla, la URL del formulario queda EXACTAMENTE como hoy (order_id + email).
// ─────────────────────────────────────────────────────────────────────────────

// Base pública desde donde el worker lee el prefill del prospecto, por slug:
// `${base}/${slug}/prefill.json`. Sin la var → la función de prefill se apaga
// (comportamiento actual de HMU intacto). Sin barra final.
export function prospectPrefillBase(env) {
  return (env.PROSPECT_PREFILL_BASE_URL || '').trim().replace(/\/+$/, '');
}

// Misma forma exacta de slug que valida el tracker de Cory (stripe.js SLUG_RE):
// solo se confía en un client_reference_id con esta forma. Validarlo ANTES de
// construir la URL de fetch evita path-traversal / SSRF por un valor arbitrario.
export const PROSPECT_SLUG_RE = /^[a-z0-9-]{3,80}-[0-9a-f]{6}$/;

export function prospectSlug(session) {
  const raw = String(session?.client_reference_id || '').trim();
  return PROSPECT_SLUG_RE.test(raw) ? raw : null;
}

// Construye el fragmento de query `&name=value&...` para prellenar campos
// VISIBLES de Tally desde un objeto {name: value}. Puro y testeable:
// - salta valores vacíos / no-string (un campo que Cory no tiene no se prellena);
// - URL-encodea nombre y valor;
// - respeta `maxLen` (largo total del fragmento): agrega pares mientras quepan y
//   descarta el resto — una URL demasiado larga la truncan navegadores/proxies y
//   rompería el link, así que es mejor prellenar de más a de menos sin pasarse.
//   El orden de inserción del objeto define la prioridad (lo más útil primero).
// Nunca lanza: ante cualquier entrada rara devuelve "".
export function buildPrefillQuery(prefill, maxLen = 1500) {
  if (!prefill || typeof prefill !== 'object') return '';
  let out = '';
  for (const [name, value] of Object.entries(prefill)) {
    if (typeof value !== 'string' || value === '') continue;
    if (typeof name !== 'string' || name === '') continue;
    const pair = `&${encodeURIComponent(name)}=${encodeURIComponent(value)}`;
    if (out.length + pair.length > maxLen) continue;
    out += pair;
  }
  return out;
}

// ¿Esta vertical puede mandar al cliente a SU formulario de Tally prellenado
// para pedir una modificación (Ola 1c), o cae al `/correct/` de texto libre?
//
// Es un interruptor explícito y apagado por default a propósito. Tally **no**
// prellena una pregunta visible por su `name`: el valor llega en la URL pero el
// campo se renderiza vacío (verificado el 2026-07-21 con un formulario de
// prueba). Lo que sí funciona —y es como opera el prefill de Cory en HMU— es
// que el dato viaje a un HIDDEN FIELD y que cada pregunta visible lo tome con
// "default answer", cableado a mano en el editor de Tally.
//
// Sin ese cableado, el cliente recibiría un formulario VACÍO y al enviarlo
// borraría todo lo que no reescribiera. El fail-safe original (mínimo de campos
// de prefill) no protege: Tally siempre manda un `name` por campo —derivado del
// título— así que el mapa nunca está vacío y la condición siempre se cumple.
//
// Prender solo cuando los DOS formularios de la vertical estén cableados:
// MODIFICATION_FORM_PREFILL = "1" en su wrangler.toml.
export function modificationFormPrefillEnabled(env) {
  const raw = String((env && env.MODIFICATION_FORM_PREFILL) ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes';
}

// Los prefill.json de Cory usan sus propios nombres (hours, maps_url,
// reviews_url); los formularios creados con `name:` estable usan los del spec
// (opening_hours_text, google_maps_url, google_reviews_url). Este helper
// duplica las claves que difieren para que la MISMA URL prellene los dos
// estilos de formulario — los nombres que un formulario no declara como hidden
// field, Tally simplemente los ignora. Puro y testeable; nunca lanza.
const CORY_PREFILL_ALIASES = {
  hours: 'opening_hours_text',
  maps_url: 'google_maps_url',
  reviews_url: 'google_reviews_url',
};

export function expandProspectPrefill(prefill) {
  if (!prefill || typeof prefill !== 'object') return {};
  const out = { ...prefill };
  for (const [from, to] of Object.entries(CORY_PREFILL_ALIASES)) {
    if (typeof prefill[from] === 'string' && prefill[from] !== '' && !(to in out)) {
      out[to] = prefill[from];
    }
  }
  return out;
}
