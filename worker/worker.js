/**
 * Service Menu Worker — motor de Link Factory, parametrizado por vertical vía
 * env vars (PRODUCT_ID, BRAND_NAME, …; ver lista más abajo). Los ejemplos y
 * defaults de esta cabecera son los de HMU Link, la vertical de referencia.
 *
 * Stripe → Worker → KV → GitHub Actions → GitHub Pages
 *
 * Flujo:
 * 1. Stripe payment → webhook → create hmu_order in KV
 * 2. Send post-payment email with Tally form link (pre-filled with order_id)
 * 3. Customer fills Tally → webhook → validate order + save submission
 * 4. Dispatch GitHub Actions to generate service menu page
 * 5. GitHub Actions calls /notify → send delivery email (includes a one-time
 *    correction link to https://www.hmulink.com/correct/?t=<token>)
 *
 * Flujo de correcciones (gratuita incluida + adicionales de pago):
 * a. /notify genera un correction_token de un solo uso (KV hmu_correction:*)
 *    y el correo de entrega enlaza la página /correct/ con ese token.
 * b. La página estática /correct/ llama GET /correction-status y luego
 *    POST /correct — el token autentica; no hay formulario de Tally.
 * c. /correct despacha GitHub Actions (is_correction) → apply_correction.py
 *    edita data/clients/<slug>.client.json y regenera la página; Actions llama
 *    /notify con correction_id + correction_status (applied|manual) y aquí se
 *    manda el correo de confirmación (y copia a REPLY_TO_EMAIL).
 * d. Correcciones adicionales ($6 USD / $59 MXN, sección 3/6 de los Términos):
 *    GET /buy-correction?slug=… crea un Stripe Checkout Session (requiere el
 *    secret STRIPE_SECRET_KEY; sin él redirige a la página de contacto). El
 *    webhook detecta la metadata de corrección de ESTA vertical
 *    (correctionMetadataKey: <PRODUCT_ID>_correction=1; para HMU,
 *    hmu_correction=1), acuña un token nuevo y se lo
 *    manda por correo al email del pedido original (nunca al comprador si no
 *    coincide — pagar por la página de otro solo le regala la corrección).
 *
 * Environment variables (non-secret):
 * - PRODUCT_ID = hmu (prefixes every KV key — hmu_order, hmu_correction, …
 *   — so this same worker can run a different vertical against its own KV
 *   namespace without key collisions; defaults to "hmu" if unset)
 * - BRAND_NAME = HMU Link (used in email subjects/bodies/footers)
 * - BRAND_TAGLINE = Service Menus for Small Businesses (email footer line)
 * - GITHUB_REPO = yuyitov/service-menu-app
 * - GITHUB_ACTIONS_EVENT = new-hmu-service-menu
 * - TALLY_FORM_URL_EN = https://tally.so/r/<FORM_ID_EN>?order_id=
 * - TALLY_FORM_URL_ES = https://tally.so/r/<FORM_ID_ES>?order_id=
 *   (además del link de intake, de aquí se deriva el idioma del form que el
 *   cliente llenó — fallback de default_language; ver tallyFormLang)
 * - FROM_EMAIL = HMU Link <hello@hmulink.com>
 * - PUBLIC_BOOK_BASE_URL = https://www.hmulink.com
 *
 * Secrets (in Cloudflare):
 * - STRIPE_WEBHOOK_SECRET
 * - TALLY_SIGNING_SECRET_EN (the EN form has its own signing secret)
 * - TALLY_SIGNING_SECRET_ES (the ES form has its own signing secret)
 * - GITHUB_TOKEN
 * - SENDGRID_API_KEY
 * - NOTIFY_SECRET
 * - STRIPE_SECRET_KEY (opcional — solo lo usa /buy-correction para crear
 *   Checkout Sessions de correcciones adicionales; usar una key RESTRINGIDA
 *   con permiso de escritura solo en Checkout Sessions)
 */

import { classifyStripeEvent } from './stripe-filter.mjs';
import { kvKey, brandName, brandTagline, brandDomain, emailFooterHtml, emailFooterText, corsOrigin, validBrandStyles, fallbackBrandStyle, prospectPrefillBase, prospectSlug, buildPrefillQuery, workerName, normalizeKey, languageQuestionAliases, resolveDefaultLanguage, correctionMetadataKey, emailLangFromCurrency, sanitizeBase64Image, freeChanges, modificationFormPrefillEnabled, expandProspectPrefill, pageTemplate, normalizeSaleKind, normalizeSaleValue, runtimeConfigErrors, SALE_BUTTON_KINDS } from './product-config.mjs';
// Mapa campo-público -> alias de título del intake de Tally, única fuente de
// verdad compartida con create_tally_forms.py --check-mapping (ver el archivo).
// Es config por vertical: export_vertical.py copia este JSON a cada repo, así
// una vertical con otras preguntas edita SUS alias sin tocar este worker.
import RAW_FIELD_ALIASES from './tally-field-aliases.json' with { type: 'json' };
import RAW_PRIMARY_CTA_ALIASES from './primary-cta-aliases.json' with { type: 'json' };

/**
 * Los alias de cada campo, MAS su propio nombre canonico al final.
 *
 * El JSON mapea cada campo del payload a los TITULOS normalizados de la
 * pregunta que lo alimenta (`services_text` -> 'list_your_services_with_prices').
 * Eso funciona cuando Tally manda el titulo, que es lo que pasa con formularios
 * hechos A MANO — como los de HMU.
 *
 * Pero un formulario creado por `scripts/create_tally_forms.py` declara un
 * `name:` ESTABLE por pregunta (justamente para que el prefill no dependa del
 * wording), y entonces Tally manda ese nombre y NO el titulo. Resultado: los
 * alias no casaban con nada y el campo se perdia EN SILENCIO.
 *
 * Se descubrio el 2026-07-26 en la primera prueba final de Vero: una compra
 * real de $1 con un negocio real (Puppy Patch). Ella lleno el formulario
 * completo, sus respuestas llegaron intactas al KV... y de 17 campos el worker
 * solo leyo 6 — y esos 6 por CASUALIDAD, porque su lista de alias ya incluia
 * el nombre. Los otros 11 (servicios, whatsapp, telefono, direccion, horarios,
 * FAQ...) se tiraron, y la generacion murio con "intake has no parseable
 * services". Un cliente que pagara habria recibido una pagina mutilada.
 *
 * La ironia que hay que recordar: HMU se salvaba porque su formulario esta
 * hecho a mano y sin `name`. El bug pegaba justo en los productos cuyo
 * formulario se genero BIEN.
 *
 * El nombre canonico se agrega AL FINAL a proposito: los titulos conservan la
 * prioridad, asi que ningun formulario que hoy funciona cambia de conducta —
 * el nombre solo entra cuando el titulo no encontro nada.
 */
// Ojo con las claves `_comment*`: el JSON lleva notas en texto plano, no listas.
// Sin este guard, `[...alias, campo]` desarma la cadena LETRA POR LETRA y el
// mapa queda con basura. Lo cazo el test de este mismo arreglo — por eso el
// guard es `Array.isArray` y no un `startsWith('_')`: protege de cualquier
// valor que no sea lista, se llame como se llame.
const FIELD_ALIASES = Object.fromEntries(
  Object.entries(RAW_FIELD_ALIASES).map(([campo, alias]) => [
    campo,
    !Array.isArray(alias) || alias.includes(campo) ? alias : [...alias, campo],
  ])
);

/**
 * Los alias de UN campo, tolerando que la vertical no lo pregunte.
 *
 * El mapa es config por vertical (export_vertical.py le copia a cada repo el
 * suyo), y no todas preguntan lo mismo: ModaLink no tiene tipo de negocio, ni
 * FAQ, ni horario unico. Antes se leia `FIELD_ALIASES.faq_text` directo, asi
 * que un campo sin entrada llegaba a `answerAny(a, undefined)` y el worker
 * REVENTABA con el intake de un cliente que ya pago. Sin alias declarados queda
 * solo el nombre canonico —que es lo que la derivacion de arriba agrega igual—
 * y el campo sale vacio, que es la respuesta correcta a "no lo pregunto".
 */
function fieldAliases(campo) {
  const alias = FIELD_ALIASES[campo];
  return Array.isArray(alias) ? alias : [campo];
}

/**
 * Campos OPCIONALES del intake: el motor sabe COMO se extrae cada uno, y la
 * vertical decide SI existe declarandolo en su `tally-field-aliases.json`.
 *
 * Es el principio de arquitectura de Vero (2026-07-26) aplicado al worker: lo
 * distinto de un producto es una OPCION del motor, nunca una copia aparte. Aqui
 * viven los campos del giro de la moda que ModaLink extraia en su propio
 * worker; manana los de Dr Link se suman a la misma tabla.
 *
 * Dos cosas que hacen que esto NO toque a HMU ni a PawContact:
 *
 *  1. Se emiten solo si el mapa de la vertical trae el campo. El de HMU no trae
 *     ninguno de estos, asi que su payload no gana ni una clave (hay prueba).
 *  2. Todo viaja como TEXTO tal cual lo contesto el negocio. El worker no
 *     conoce los catalogos cerrados del giro (`servicios_produccion`,
 *     `cita_previa`, `cash|card|transfer`): quien traduce la etiqueta del
 *     formulario a su clave es `build_client_from_intake.py`, que si lee el
 *     `vertical.yaml`. Duplicar esas listas aqui habria sido una segunda fuente
 *     de verdad que se desincroniza sola.
 */
const OPTIONAL_INTAKE_FIELDS = {
  // Giro (moda hoy; el motor solo ve texto)
  specialty: answerAny,
  specialty_other: answerAny,
  years_experience: answerAny,
  training_text: answerAny,
  delivery_time_text: answerAny,
  deposit_policy_text: answerAny,
  appointment_mode: answerAny,
  home_service: answerAny,
  payment_methods: answerAny,
  invoicing: answerAny,
  photo_rights_confirmed: answerAny,
  // Salud (Dr Link). Son nombres planos a proposito: el worker solo
  // transporta texto; Python arma/valida el contrato y el generador acepta
  // tambien el objeto `credentials` historico del fork.
  country: answerAny,
  profession: answerAny,
  credentials_institution: answerAny,
  cedula_profesional: answerAny,
  cedula_especialidad: answerAny,
  license_state: answerAny,
  license_number: answerAny,
  npi: answerAny,
  board_name: answerAny,
  languages: answerAny,
  telemedicine_offered: answerAny,
  telemedicine_modality: answerAny,
  insurance: answerAny,
  privacy_notice_url: answerAny,
  appointment_policy_text: answerAny,
  emergency_policy_text: answerAny,
  // Directorio publico de la vertical
  directory_state: answerAny,
  directory_opt_in: answerAny,
  // Una red social mas
  pinterest: answerAny,
  // Horario POR SUCURSAL: una respuesta por ubicacion, con la MISMA forma plana
  // que el resto de las ubicaciones (`location_N_*`), para que build_locations()
  // las alinee por indice sin inventar una estructura nueva en el payload.
  location_1_hours: answerAny,
  location_2_hours: answerAny,
  location_3_hours: answerAny,
  // Mini-lookbook (2 a 4 fotos)
  lookbook_urls: (a, alias) => answerFileUrls(a, alias, MAX_LOOKBOOK_URLS),
};

// Tope de fotos del mini-lookbook. Es el mismo numero que el formulario acepta
// y que `MAX_LOOKBOOK_PHOTOS` del generador publica.
const MAX_LOOKBOOK_URLS = 4;

// Los campos opcionales que ESTA vertical declaro, ya extraidos.
function optionalIntakeFields(answers) {
  const out = {};
  for (const [campo, extraer] of Object.entries(OPTIONAL_INTAKE_FIELDS)) {
    if (!Array.isArray(FIELD_ALIASES[campo])) continue;
    out[campo] = extraer(answers, FIELD_ALIASES[campo]);
  }
  return out;
}

// Precio de la corrección adicional — DEBE coincidir con la sección 3 de los
// Términos publicados (public/terms/ y public/es/terminos/): $6 USD / $59 MXN.
// (Auditoría 2026-07: el código cobraba $3/$40, valores viejos anteriores a
// los Términos vigentes — unit_amount va en centavos.)
const CORRECTION_PRICE = {
  usd: { unit_amount: 600, label: '$6 USD' },
  mxn: { unit_amount: 5900, label: '$59 MXN' }
};

const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$/;
// Límites del texto libre de corrección: viaja en client_payload (visible en
// logs públicos de Actions) y se pega en el prompt del LLM — acotarlo.
const CORRECTION_TEXT_MIN = 5;
const CORRECTION_TEXT_MAX = 3000;

// Cloudflare entrega el MISMO `env` (bindings del deploy) en cada request de
// este worker — no varía por request ni por cliente — así que cachearlo a nivel
// módulo deja que corsHeaders()/jsonResponse() deriven el Origin de la marca
// (corsOrigin) sin propagar `env` por los ~74 call-sites de jsonResponse. No hay
// fuga entre requests: todas ven el mismo valor, escribirlo es idempotente.
let _workerEnv = null;

/**
 * Adapta el nombre del binding KV sin renombrar el namespace vivo.
 *
 * El motor usa internamente `SERVICE_MENU_KV`. Una vertical puede conservar
 * su binding histórico (`DR_LINK_KV`, por ejemplo) declarando
 * `KV_BINDING = "DR_LINK_KV"`. Se crea una vista del env con un alias; no se
 * copia ni migra una sola llave. Sin configuración se conserva exactamente el
 * binding histórico del motor.
 */
function bindRuntimeEnv(env) {
  const binding = String(env?.KV_BINDING || 'SERVICE_MENU_KV').trim() || 'SERVICE_MENU_KV';
  const namespace = env?.[binding];
  if (!namespace) {
    throw new Error(`KV binding ${binding} is not available`);
  }
  if (binding === 'SERVICE_MENU_KV') return env;
  return Object.create(env, {
    SERVICE_MENU_KV: { value: namespace, enumerable: true },
  });
}

// Named export (ademas del default que usa Cloudflare) para probar la
// normalizacion del payload de Tally con node --test — en particular que un
// hidden field nunca pise la respuesta visible del mismo nombre. La runtime de
// Workers solo invoca el default export; esto es inerte ahi.
// buildPublicPayload/missingFromIntake se exportan por la misma razón desde que
// el worker sirve DOS plantillas (F6 bloque 2): la rama que elige es lo que
// decide si un cliente que pagó recibe su catálogo o una página de menú de
// servicios, y eso tiene que poder probarse sin desplegar ni tocar KV.
// checkIntakeCompleteness/marca se exportan porque la alerta de intake pagado
// incompleto es un fallo SILENCIOSO: si deja de mandarse, nada visible se rompe
// (el 422 se lo come Tally) y el cliente que pagó se queda sin página. Tiene que
// poder probarse que se manda, que NO se manda cuando el intake está completo, y
// que un reintento del webhook no duplica el correo.
// modificationCopy se exporta porque es una PROMESA al cliente: si el texto y
// el interruptor MODIFICATION_FORM_PREFILL se desincronizan, el correo describe
// una experiencia que la vertical no entrega (regresión R1 del gate de HMU).
export {
  normalizeTallyPayload, buildPublicPayload, missingFromIntake,
  checkIntakeCompleteness, marca, modificationCopy, bindRuntimeEnv,
  // Contrato puro de la compra de correcciones. Se exporta para que una
  // vertical migrada pueda fijar precio, validación de slug y URL sin copiar
  // una implementación paralela del Worker.
  CORRECTION_PRICE, SLUG_RE, buyCorrectionUrl,
  // El correo de ENTREGA: lo que recibe el cliente que pagó. No estaba
  // exportado, así que ni una prueba lo miraba — y es donde vivían las dos
  // notas de Vero de la Fase 2 (el idioma por moneda y la imagen del correo).
  buildDeliveryEmail, buildDeliveryText, DELIVERY_QR_CID
};

export default {
  async fetch(request, env, ctx) {
    try {
      const missingConfig = runtimeConfigErrors(env);
      if (missingConfig.length) {
        return new Response(JSON.stringify({
          ok: false,
          error: 'Worker configuration incomplete',
          missing: missingConfig,
        }), {
          status: 503,
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
          },
        });
      }

      env = bindRuntimeEnv(env);
      _workerEnv = env;
      const url = new URL(request.url);
      const pathname = url.pathname;

      if (request.method === 'OPTIONS') {
        return new Response(null, {
          status: 204,
          headers: corsHeaders()
        });
      }

      const ip = request.headers.get('cf-connecting-ip') || 'unknown';
      const minuteSlot = Math.floor(Date.now() / 60000);

      if (request.method === 'GET' && pathname === '/health') {
        return jsonResponse({ ok: true, worker: workerName(env) });
      }

      if (request.method === 'POST' && pathname === '/stripe/webhook') {
        // Cap junk floods before spending CPU on HMAC verification. Stripe's
        // real volume for this business is far below 60/min per source IP;
        // a 429 just makes Stripe retry with backoff.
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'stripe', ip, minuteSlot), 60, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleStripeWebhook(request, env);
      }

      if (request.method === 'POST' && pathname === '/tally-webhook') {
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'tally', ip, minuteSlot), 10, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleTallyWebhook(request, env);
      }

      if (request.method === 'POST' && pathname === '/alert-generation-failed') {
        return await handleGenerationFailedAlert(request, env);
      }

      if (request.method === 'POST' && pathname === '/email-events') {
        return await handleEmailEvents(request, env);
      }

      if (request.method === 'POST' && pathname === '/notify') {
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'notify', ip, minuteSlot), 20, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleNotify(request, env);
      }

      // Correcciones: consumidos por la página estática /correct/ del sitio.
      if (request.method === 'GET' && pathname === '/correction-status') {
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'corrstatus', ip, minuteSlot), 30, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleCorrectionStatus(url, env);
      }

      if (request.method === 'POST' && pathname === '/correct') {
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'correct', ip, minuteSlot), 10, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleCorrectionRequest(request, env);
      }

      if (request.method === 'GET' && pathname === '/buy-correction') {
        const allowed = await checkRateLimit(env, kvKey(env, 'rl', 'buycorr', ip, minuteSlot), 10, 120);
        if (!allowed) return jsonResponse({ ok: false, error: 'Too many requests' }, 429);
        return await handleBuyCorrection(url, env);
      }

      return jsonResponse({ ok: false, error: 'Not found' }, 404);
    } catch (error) {
      console.error('Worker error:', safeError(error));
      return jsonResponse(
        { ok: false, error: 'Internal worker error' },
        500
      );
    }
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// STRIPE WEBHOOK HANDLER
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Lee un secreto del entorno recortando espacios y saltos de linea.
 *
 * Copiar un signing secret del panel de Stripe, de Tally o de GitHub arrastra
 * con facilidad un `\n` final, y `wrangler secret put` lo guarda tal cual. Un
 * secreto con basura invisible no falla de forma ruidosa: rompe el HMAC y deja
 * el webhook rechazando TODO en silencio (el checkout cobra y el intake nunca
 * llega). NOTIFY_SECRET ya lo recortaba; esto lo vuelve la regla para todos.
 */
function secret(env, name) {
  return (env[name] || '').trim();
}

async function handleStripeWebhook(request, env) {
  const webhookSecret = secret(env, 'STRIPE_WEBHOOK_SECRET');
  if (!webhookSecret) {
    return jsonResponse({ ok: false, error: 'Stripe webhook not configured' }, 500);
  }

  const rawBody = await request.text();
  const signatureHeader = request.headers.get('stripe-signature') || '';

  const valid = await validateStripeSignature(rawBody, signatureHeader, webhookSecret);
  if (!valid) {
    return jsonResponse({ ok: false, error: 'Invalid Stripe signature' }, 400);
  }

  let event;
  try {
    event = JSON.parse(rawBody);
  } catch {
    return jsonResponse({ ok: false, error: 'Invalid JSON' }, 400);
  }

  const type = event?.type;
  const session = event?.data?.object || {};

  // Clasificación de producto — fail-closed (ver engine/worker/stripe-filter.mjs).
  // La cuenta de Stripe se comparte con otros productos (MyGuest, Dr Link, …) y
  // Stripe abanica cada evento firmado aquí; el filtro decide procesar / enrutar
  // a corrección / ignorar, sin tocar KV ni red (por eso es testeable con
  // `node --test`). El manejo de logs e I/O queda en el worker.
  const verdict = classifyStripeEvent(event, env);

  if (verdict.action === 'correction') {
    // Compras de corrección adicional: Checkout Sessions creadas por este mismo
    // worker en /buy-correction, marcadas con metadata (no traen payment_link).
    return await handleCorrectionPurchase(session, env);
  }

  if (verdict.action === 'ignore') {
    switch (verdict.reason) {
      case 'unsupported_type':
        return jsonResponse({ ok: true, ignored: true, type });
      case 'filter_not_configured':
        if (type === 'checkout.session.completed') {
          console.log('stripe payment_link observed (filter not configured):', verdict.observedPaymentLink || '(none)');
        }
        // FALLO SILENCIOSO #1 (el mas probable y mas caro): el allowlist esta
        // vacio -> se IGNORAN todos los pagos. Alerta GLOBAL (una por hora), no
        // por pago: durante el apagon la cuenta compartida fanea eventos de
        // todos los productos y un dedup por-pago seria una tormenta.
        await alertOnce(env, 'filter_not_configured', 3600,
          `${marca(env)} esta IGNORANDO todos los pagos (filtro sin configurar)`, [
            'STRIPE_PAYMENT_LINK_IDS esta vacio/sin configurar en este worker.',
            'El worker responde 200 {ignored} a TODOS los pagos entrantes: el',
            'cliente paga y NUNCA recibe el formulario de intake.',
            '',
            'Revisa STRIPE_PAYMENT_LINK_IDS en wrangler.toml y redespliega.',
          ]);
        return jsonResponse({ ok: true, ignored: true, reason: 'filter_not_configured' });
      case 'unattributable_event_type':
        return jsonResponse({ ok: true, ignored: true, reason: 'unattributable_event_type' });
      case 'other_product':
        // Diagnostic: surface filter mismatches (plink ids are not secrets).
        console.log('ignored other_product — observed plink:', verdict.observedPaymentLink || '(none)', '| expected:', verdict.expectedPaymentLinks.join(','));
        // FALLO SILENCIOSO #2: un payment_link propio que quedo desfasado (p. ej.
        // tras un reprecio) cae aqui y el pago se ignora en silencio. PERO la
        // cuenta de Stripe es COMPARTIDA entre productos: cada venta de un
        // hermano tambien llega como other_product. Por eso solo se alerta de
        // links DESCONOCIDOS -- ni el propio ni en KNOWN_OTHER_PAYMENT_LINKS --
        // que es justo el sintoma de un link propio sin registrar.
        {
          const plink = verdict.observedPaymentLink || '';
          if (plink && !knownOtherPaymentLinks(env).includes(plink)) {
            await alertOnce(env, `other_product:${plink}`, 86400,
              `${marca(env)}: pago con payment_link NO reconocido (link desfasado?)`, [
                'Llego un pago cuyo payment_link NO coincide con el del producto y fue IGNORADO.',
                '',
                `payment_link observado: ${plink}`,
                `esperados: ${verdict.expectedPaymentLinks.join(', ') || '(ninguno)'}`,
                '',
                'Si el link ES del producto, actualiza STRIPE_PAYMENT_LINK_IDS.',
                'Si es de otro producto de la cuenta compartida, agregalo a',
                'KNOWN_OTHER_PAYMENT_LINKS para dejar de recibir este aviso.',
              ]);
          }
        }
        return jsonResponse({ ok: true, ignored: true, reason: 'other_product' });
      default:
        // Fail-closed también ante un motivo inesperado: no procesar.
        return jsonResponse({ ok: true, ignored: true, reason: verdict.reason });
    }
  }

  // verdict.action === 'process'
  const paymentIntentId =
    type === 'checkout.session.completed'
      ? (session.payment_intent || session.id || '')
      : (session.id || '');
  const customerEmail =
    session.customer_email ||
    session.customer_details?.email ||
    session.receipt_email ||
    '';
  const amountTotal = session.amount_total ?? session.amount ?? 0;
  const currency = session.currency || '';

  if (!paymentIntentId) {
    return jsonResponse({ ok: false, error: 'Missing payment_intent id' }, 400);
  }

  const processedKey = kvKey(env, 'processed', paymentIntentId);
  const alreadyProcessed = await env.SERVICE_MENU_KV.get(processedKey).catch(() => null);
  if (alreadyProcessed) {
    return jsonResponse({ ok: true, idempotent: true, paymentIntentId });
  }

  const now = new Date().toISOString();
  const orderId = paymentIntentId;

  // Save order record
  try {
    await env.SERVICE_MENU_KV.put(kvKey(env, 'order', orderId), JSON.stringify({
      order_id: orderId,
      payment_intent_id: paymentIntentId,
      customer_email: customerEmail,
      amount: amountTotal,
      currency,
      stripe_event_type: type,
      // Slug del prospecto cuando la compra llegó por un link rastreado de Cory
      // (client_reference_id, validado contra PROSPECT_SLUG_RE). Es la ÚNICA
      // fuente de verdad de qué vista previa compró este cliente: viene de
      // Stripe, no del formulario, así que no lo puede editar quien tenga la
      // URL. La rama de catálogo de buildPublicPayload lo lee de aquí para que
      // el workflow baje el payload.json correcto. En service-menu es un campo
      // más en KV que nadie consulta todavía; se guarda igual porque el dato ya
      // existía y perderlo obligaría a re-raspar el Instagram del cliente.
      prospect_slug: prospectSlug(session) || '',
      status: 'paid',
      created_at: now
    }));
  } catch (err) {
    console.error('order record save failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to save order record' }, 500);
  }

  if (!customerEmail) {
    await env.SERVICE_MENU_KV.put(processedKey, '1', { expirationTtl: 604800 });
    return jsonResponse({ ok: true, paymentIntentId, hasEmail: false });
  }

  if (!secret(env, 'SENDGRID_API_KEY')) {
    return jsonResponse({ ok: false, error: 'SENDGRID_API_KEY not configured' }, 500);
  }

  const baseFormEN = (env.TALLY_FORM_URL_EN || '').trim();
  const baseFormES = (env.TALLY_FORM_URL_ES || '').trim();

  if (!baseFormEN || !baseFormES) {
    return jsonResponse({ ok: false, error: 'TALLY_FORM_URL not configured' }, 500);
  }

  // Construct form URLs with order_id + customer_email (the two hidden fields
  // worker.js relies on for every vertical).
  let formUrlEN = `${baseFormEN}${encodeURIComponent(orderId)}&customer_email=${encodeURIComponent(customerEmail)}`;
  let formUrlES = `${baseFormES}${encodeURIComponent(orderId)}&customer_email=${encodeURIComponent(customerEmail)}`;

  // Prefill de prospecto (Cory → worker, Fase 2.4): si esta compra llegó por un
  // link rastreado de un prospecto (Cory puso client_reference_id=<slug> en el
  // checkout) y hay una base de prefill configurada, prellena los campos
  // VISIBLES del formulario con los datos que Cory ya recolectó, en cada idioma.
  // El cliente los ve cargados y puede dejarlos, editarlos o borrarlos. Fail-open
  // en cada paso: sin slug / sin base / si el fetch falla, las URLs quedan como
  // arriba (order_id + email) y el post-pago sigue idéntico al de hoy.
  const prefill = await fetchProspectPrefill(env, prospectSlug(session));
  if (prefill) {
    // expandProspectPrefill agrega alias con los nombres del spec (hours ->
    // opening_hours_text, etc.): los formularios creados con `name:` estable
    // usan esos nombres en sus hidden fields, y los viejos (HMU) los de Cory.
    // Mandar ambos sirve a los dos; el tope sube porque van claves duplicadas.
    formUrlEN += buildPrefillQuery(expandProspectPrefill(prefill.en), 2500);
    formUrlES += buildPrefillQuery(expandProspectPrefill(prefill.es), 2500);
  }

  // Reserve the idempotency marker BEFORE sending so a concurrent duplicate
  // (Stripe retry / race) can't double-send the email. If the send fails we
  // clear the marker so Stripe's own retry can try again.
  await env.SERVICE_MENU_KV.put(processedKey, '1', { expirationTtl: 604800 }).catch(() => {});

  // Send post-payment email. Asunto en el idioma del comprador (moneda MXN →
  // español): un asunto en inglés a un cliente mexicano confunde y dispara
  // filtros de spam por incongruencia de idioma.
  // MXN -> es, USD -> en, cualquier otra moneda -> null = correo bilingüe
  // (antes toda moneda != MXN caía a inglés por descarte).
  const emailLang = emailLangFromCurrency(currency);
  const subjectES = `Completa tu página ${brandName(env)} — solo falta un formulario`;
  const subjectEN = `Complete your ${brandName(env)} service menu — one form to go`;
  try {
    await sendEmail({
      env,
      to: customerEmail,
      subject: emailLang === 'es' ? subjectES : emailLang === 'en' ? subjectEN
        : `${subjectES} / ${subjectEN}`,
      html: buildPostPaymentEmail({ formUrlEN, formUrlES, lang: emailLang, env }),
      text: buildPostPaymentText({ formUrlEN, formUrlES, lang: emailLang, env })
    });
  } catch (err) {
    console.error('post-payment email failed:', safeError(err));
    await env.SERVICE_MENU_KV.delete(processedKey).catch(() => {});
    return jsonResponse({ ok: false, error: 'Failed to send post-payment email' }, 500);
  }

  return jsonResponse({ ok: true, paymentIntentId, hasEmail: true });
}

// Lee el prefill.json público del prospecto por slug (Cory lo publica en su
// pages.dev; el dato ya es público — está en la demo del prospecto). Fail-open
// total: sin base configurada, sin slug válido, error de red, timeout, no-200,
// o JSON inválido → devuelve null y el flujo de post-pago sigue idéntico al de
// hoy. Nunca lanza. Devuelve { es: {...}, en: {...} } (objetos por idioma,
// vacíos si faltan) o null.
async function fetchProspectPrefill(env, slug) {
  const base = prospectPrefillBase(env);
  if (!base || !slug) return null;
  try {
    // Un prospecto lento no debe bloquear el correo de post-pago.
    const resp = await fetch(`${base}/${slug}/prefill.json`, {
      signal: AbortSignal.timeout(3000),
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    if (!data || typeof data !== 'object') return null;
    return {
      es: (data.es && typeof data.es === 'object') ? data.es : {},
      en: (data.en && typeof data.en === 'object') ? data.en : {},
    };
  } catch {
    return null;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// TALLY WEBHOOK HANDLER
// ─────────────────────────────────────────────────────────────────────────────

async function handleTallyWebhook(request, env) {
  const rawBody = await request.text().catch(() => null);
  if (rawBody === null) {
    return jsonResponse({ ok: false, error: 'Could not read request body' }, 400);
  }

  const secrets = [secret(env, 'TALLY_SIGNING_SECRET_EN'), secret(env, 'TALLY_SIGNING_SECRET_ES')].filter(Boolean);
  if (secrets.length === 0) {
    return jsonResponse({ ok: false, error: 'Webhook signature validation not configured' }, 500);
  }

  const sigHeader = request.headers.get('tally-signature') || '';
  let valid = false;
  for (const secret of secrets) {
    if (await verifyTallySignature(rawBody, sigHeader, secret)) {
      valid = true;
      break;
    }
  }
  if (!valid) {
    return jsonResponse({ ok: false, error: 'Invalid webhook signature' }, 401);
  }

  let rawPayload;
  try {
    rawPayload = JSON.parse(rawBody);
  } catch {
    return jsonResponse({ ok: false, error: 'Invalid JSON payload' }, 400);
  }

  const normalized = normalizeTallyPayload(rawPayload);

  if (!normalized.submission_id) {
    return jsonResponse({ ok: false, error: 'Missing submission_id' }, 400);
  }

  const now = new Date().toISOString();
  const incomingOrderId = cleanValue(getAnswer(normalized.answers, 'order_id')) || '';

  // Guard: require valid order_id
  if (!incomingOrderId) {
    await env.SERVICE_MENU_KV.put(
      kvKey(env, 'missing_order', normalized.submission_id),
      JSON.stringify({
        submission_id: normalized.submission_id,
        attempted_at: now
      }),
      { expirationTtl: 2592000 }
    ).catch(() => {});
    return jsonResponse({ ok: false, status: 'missing_order_id' }, 403);
  }

  // Validate order exists
  const existingOrder = await env.SERVICE_MENU_KV.get(kvKey(env, 'order', incomingOrderId), { type: 'json' }).catch(() => null);
  if (!existingOrder) {
    await env.SERVICE_MENU_KV.put(
      kvKey(env, 'invalid_order', incomingOrderId, normalized.submission_id),
      JSON.stringify({ order_id: incomingOrderId, submission_id: normalized.submission_id, attempted_at: now }),
      { expirationTtl: 2592000 }
    ).catch(() => {});
    return jsonResponse({ ok: false, status: 'invalid_order_id' }, 403);
  }

  // ¿Es una MODIFICACIÓN de una página ya publicada? (Ola 1c) El formulario
  // llega con los hidden client_slug + correction_token que puso el enlace de
  // modificación. Se valida el token ANTES de nada: sin token válido, un envío
  // con client_slug sería una vía para reescribir la página de otro.
  const modification = await resolveModification(env, normalized, existingOrder);
  if (modification.error) {
    await env.SERVICE_MENU_KV.put(
      kvKey(env, 'invalid_modification', incomingOrderId, normalized.submission_id),
      JSON.stringify({
        order_id: incomingOrderId,
        submission_id: normalized.submission_id,
        reason: modification.error,
        attempted_at: now
      }),
      { expirationTtl: 2592000 }
    ).catch(() => {});
    return jsonResponse({ ok: false, status: 'invalid_modification', reason: modification.error }, 403);
  }

  // Check order state: only allow generation from 'paid' status. Una
  // modificación llega con la orden ya 'delivered', así que se salta este gate
  // (su gate es el token, ya validado arriba).
  const allowedStatuses = ['paid', 'form_sent'];
  if (!modification.token && !allowedStatuses.includes(existingOrder.status)) {
    await env.SERVICE_MENU_KV.put(
      kvKey(env, 'invalid_order_status', incomingOrderId, normalized.submission_id),
      JSON.stringify({ order_id: incomingOrderId, submission_id: normalized.submission_id, blocked_by_status: existingOrder.status, attempted_at: now }),
      { expirationTtl: 2592000 }
    ).catch(() => {});
    return jsonResponse({ ok: false, status: 'invalid_order_status' }, 409);
  }

  // Validate email if form includes it
  const formEmail = (cleanValue(getAnswer(normalized.answers, 'customer_email')) || '').toLowerCase().trim();
  const orderEmail = (existingOrder.customer_email || '').toLowerCase().trim();
  if (formEmail && orderEmail && formEmail !== orderEmail) {
    await env.SERVICE_MENU_KV.put(
      kvKey(env, 'email_mismatch', incomingOrderId, normalized.submission_id),
      JSON.stringify({
        order_id: incomingOrderId,
        submission_id: normalized.submission_id,
        form_email: formEmail,
        order_email: orderEmail,
        attempted_at: now
      }),
      { expirationTtl: 2592000 }
    ).catch(() => {});
    return jsonResponse({ ok: false, status: 'order_email_mismatch' }, 403);
  }

  // Build the sanitized public payload (only approved public fields).
  // Internal answers (contact person, private phone, "keep off page" notes)
  // stay in the KV submission record and are never dispatched.
  const publicPayload = buildPublicPayload(normalized, incomingOrderId, env);

  // Transporte del payload del catálogo, "opción A" (decisión de Vero 07-25):
  // el slug del prospecto sale del REGISTRO DE LA ORDEN, donde lo dejó el
  // client_reference_id del checkout de Stripe — no del formulario. Con él, el
  // workflow baja de Cory el payload.json de la vista previa que el cliente ya
  // vio. Leerlo de un hidden field de Tally sería dejar que cualquiera con la
  // URL reclamara el catálogo de otro negocio.
  if (publicPayload.template === 'catalog') {
    publicPayload.prospect_slug = String(existingOrder.prospect_slug || '').trim();
  }

  // Qué es un intake "completo" depende de la plantilla (ver missingFromIntake).
  // Si falta algo: rastro en KV, CORREO de alerta, y 422.
  const missing = await checkIntakeCompleteness(env, publicPayload, {
    orderId: incomingOrderId,
    submissionId: normalized.submission_id,
    now
  });
  if (missing) {
    return jsonResponse({ ok: false, status: 'incomplete_intake', missing }, 422);
  }

  // En una modificación se CONSERVA el slug de la página existente: si se
  // dejara el derivado del nombre del negocio + submission_id, cada
  // modificación publicaría una página NUEVA y dejaría huérfana la que el
  // cliente ya compartió. Es el riesgo principal de este flujo.
  if (modification.slug) {
    publicPayload.public_slug = modification.slug;
  }
  const slug = publicPayload.public_slug;

  // Save submission to KV (full answers, private fields included — KV only)
  const submissionKey = kvKey(env, 'submission', normalized.submission_id);
  try {
    await env.SERVICE_MENU_KV.put(submissionKey, JSON.stringify({
      submission_id: normalized.submission_id,
      order_id: incomingOrderId,
      customer_email: orderEmail,
      slug,
      answers: normalized.answers,
      // Lo que el cliente escribió, tal cual, keyed por field name de Tally:
      // es lo que reconstruye SU formulario cuando pide una modificación.
      prefill: normalized.prefill,
      // Marca que este envío EDITA una página ya entregada: /notify lo usa para
      // mandar el correo de "tu página fue actualizada" en vez del de entrega.
      is_modification: Boolean(modification.token),
      received_at: now,
      status: 'received'
    }), { expirationTtl: 7776000 }); // 90 days
  } catch (err) {
    console.error('submission save failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to save submission' }, 500);
  }

  // Update order status to intake_received
  try {
    existingOrder.status = 'intake_received';
    existingOrder.submission_id = normalized.submission_id;
    existingOrder.slug = slug;
    existingOrder.updated_at = now;
    await env.SERVICE_MENU_KV.put(kvKey(env, 'order', incomingOrderId), JSON.stringify(existingOrder));
  } catch (err) {
    console.error('order update failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to update order' }, 500);
  }

  // Dispatch GitHub Actions to generate page.
  // The full public payload travels in client_payload (MyGuest pattern):
  // GitHub Actions never needs KV access.
  // OJO: NO incluir order_id — GitHub imprime el env de cada step en los logs
  // públicos del repo. El workflow notifica con submission_id y el worker
  // resuelve el order_id desde KV.
  try {
    await dispatchGitHubAction(env, {
      submission_id: normalized.submission_id,
      slug,
      public_payload: publicPayload
    });
  } catch (err) {
    console.error('github dispatch failed:', safeError(err));
    await env.SERVICE_MENU_KV.put(kvKey(env, 'order', incomingOrderId), JSON.stringify({
      ...existingOrder,
status: 'failed_dispatch',
      updated_at: now
    }));
    // FALLO SILENCIOSO #3: pago + formulario OK, pero el dispatch a GitHub
    // Actions fallo -> el cliente pago y su pagina NO se esta generando. Aqui
    // se responde 500 a proposito (para que Stripe reintente), asi que el
    // dedup por-orden es CRITICO: aunque reintente, sale UNA alerta al dia.
    await alertOnce(env, `failed_dispatch:${incomingOrderId}`, 86400,
      `${marca(env)}: pago cobrado pero la generacion NO arranco`, [
        'El cliente pago y su formulario llego, pero el dispatch a GitHub',
        'Actions fallo: su pagina NO se esta generando.',
        '',
        `orden: ${incomingOrderId}`,
        '',
        'Revisa GITHUB_TOKEN y GITHUB_REPO en el worker.',
      ]);
    return jsonResponse({ ok: false, error: 'Failed to dispatch generation' }, 500);
  }

  // Modificación despachada: recién ahora se quema el token (si el dispatch
  // falla, el cliente puede reintentar con el mismo enlace) y se descuenta una
  // de las incluidas, acuñando la siguiente si le quedan.
  if (modification.token) {
    try {
      modification.record.used_at = now;
      modification.record.submission_id = normalized.submission_id;
      await env.SERVICE_MENU_KV.put(
        kvKey(env, 'correction', modification.token),
        JSON.stringify(modification.record),
        { expirationTtl: 7776000 }
      );
      if (modification.record.paid !== true) {
        await consumeFreeChange(env, slug, modification.record);
      }
    } catch (err) {
      console.error('modification token burn failed:', safeError(err));
    }
  }

  // Update order to 'generating'
  try {
    existingOrder.status = 'generating';
    existingOrder.updated_at = now;
    await env.SERVICE_MENU_KV.put(kvKey(env, 'order', incomingOrderId), JSON.stringify(existingOrder));
  } catch (err) {
    console.error('status update failed:', safeError(err));
  }

  return jsonResponse({
    ok: true,
    submission_id: normalized.submission_id,
    order_id: incomingOrderId,
    slug,
    status: 'generating'
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// DELIVERY EMAIL HANDLER (called by GitHub Actions post-generation)
// ─────────────────────────────────────────────────────────────────────────────

async function handleNotify(request, env) {
  const notifySecret = (env.NOTIFY_SECRET || '').trim();
  if (!notifySecret) {
    return jsonResponse({ ok: false, error: 'NOTIFY_SECRET not configured' }, 500);
  }

  const authHeader = request.headers.get('authorization') || '';
  const provided = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';
  if (!timingSafeEqual(provided, notifySecret)) {
    return jsonResponse({ ok: false, error: 'Unauthorized' }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: 'Invalid JSON' }, 400);
  }

  // Notificación de corrección aplicada (Actions manda correction_id en vez
  // de submission_id) — camino separado del delivery inicial.
  if ((body?.correction_id || '').trim()) {
    return await handleCorrectionNotify(body, env);
  }

  const slug = (body?.slug || '').trim();
  const submissionId = (body?.submission_id || '').trim();
  let orderId = (body?.order_id || '').trim();
  // QR en base64 que manda el workflow (el asset todavía no está publicado
  // cuando se dispara /notify, así que no se puede descargar). Fail-open: si no
  // viene, viene mal formado o viene demasiado grande, el correo sale sin QR —
  // nunca bloquea la entrega, que es lo que el cliente pagó.
  const qrPngBase64 = sanitizeBase64Image(body?.qr_png_base64);
  // Frases rojas que el linter de copy encontró (`legal.copy_linter` de la
  // vertical -> GITHUB_OUTPUT linter_flags -> el workflow). Data-gated: una
  // vertical sin lista no lo manda nunca y su correo no cambia. Warn-only: la
  // página ya está publicada; el correo solo sugiere revisarla.
  const linterFlags = Number(body?.linter_flags) || 0;

  // El workflow ya no conoce el order_id (no viaja en client_payload para no
  // aparecer en logs públicos de Actions); se resuelve desde la submission.
  let submissionRecord = null;
  if (submissionId) {
    submissionRecord = await env.SERVICE_MENU_KV.get(kvKey(env, 'submission', submissionId), { type: 'json' }).catch(() => null);
  }
  if (!orderId) {
    orderId = (submissionRecord?.order_id || '').trim();
  }

  // Este envío EDITÓ una página ya entregada: el correo que toca es el de
  // "tu página fue actualizada", no otra entrega (que además sería idempotente
  // y no mandaría nada — el cliente se quedaría sin aviso).
  if (submissionRecord?.is_modification && slug) {
    return await notifyModificationApplied({ env, slug, orderId, submissionId });
  }

  if (!slug || !orderId) {
    return jsonResponse({ ok: false, error: 'Missing slug or order reference' }, 400);
  }

  // Get order from KV
  const order = await env.SERVICE_MENU_KV.get(kvKey(env, 'order', orderId), { type: 'json' }).catch(() => null);
  if (!order) {
    return jsonResponse({ ok: false, error: 'Order not found' }, 404);
  }

  const customerEmail = order.customer_email || '';
  if (!customerEmail) {
    return jsonResponse({ ok: false, error: 'No customer email on record' }, 400);
  }

  // Check if already delivered
  const deliveryKey = kvKey(env, 'delivery', slug);
  const existingDelivery = await env.SERVICE_MENU_KV.get(deliveryKey, { type: 'json' }).catch(() => null);
  if (existingDelivery && existingDelivery.status === 'delivered') {
    return jsonResponse({ ok: true, slug, idempotent: true, alreadyDelivered: true });
  }

  // Generate correction token
  const correctionToken = generateSecureToken();

  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  const pageUrl = `${baseUrl}/links/${slug}/`;

  // Idioma del cuerpo = idioma del comprador (moneda MXN → español), igual que
  // el asunto. Se guarda también en el token para los correos de corrección.
  // Una moneda que no identifica idioma (CAD, EUR…) cae a inglés: aquí, a
  // diferencia del correo post-pago, no se manda bilingüe porque este correo
  // lleva la página YA generada en un idioma concreto.
  const deliveryLang = emailLangFromCurrency(order.currency) || 'en';

  // Save correction token (la corrección gratuita incluida)
  try {
    await env.SERVICE_MENU_KV.put(kvKey(env, 'correction', correctionToken), JSON.stringify({
      correction_token: correctionToken,
      order_id: orderId,
      slug,
      lang: deliveryLang,
      paid: false,
      created_at: new Date().toISOString(),
      used_at: null
    }), { expirationTtl: 2592000 }); // 30 days
  } catch (err) {
    console.error('correction token save failed:', safeError(err));
  }

  // Save delivery record
  try {
    await env.SERVICE_MENU_KV.put(deliveryKey, JSON.stringify({
      slug,
      order_id: orderId,
      customer_email: customerEmail,
      page_url: pageUrl,
      correction_token: correctionToken,
      // Contador de modificaciones incluidas (FREE_CHANGES <- legal.free_changes).
      // Se congela AQUÍ el total que le tocaba al cliente al comprar: si mañana
      // la vertical cambia el número, quien ya compró conserva lo prometido.
      free_total: freeChanges(env),
      free_used: 0,
      status: 'delivered',
      delivered_at: new Date().toISOString()
    }), { expirationTtl: 7776000 }); // 90 days
  } catch (err) {
    console.error('delivery record save failed:', safeError(err));
  }

  // Send delivery email
  if (!secret(env, 'SENDGRID_API_KEY')) {
    return jsonResponse({ ok: false, error: 'SENDGRID_API_KEY not configured' }, 500);
  }

  // Formulario completo prellenado si la vertical ya lo soporta; si no, la
  // página de texto libre de siempre.
  const correctionUrl = await bestModificationUrl(env, {
    token: correctionToken,
    slug,
    lang: deliveryLang,
    orderId
  });
  try {
    await sendEmail({
      env,
      to: customerEmail,
      subject: deliveryLang === 'es'
        ? `¡Tu página ${brandName(env)} está lista! 🎉`
        : `Your ${brandName(env)} service menu is ready! 🎉`,
      html: buildDeliveryEmail({
        pageUrl,
        slug,
        lang: deliveryLang,
        correctionUrl,
        hasQr: Boolean(qrPngBase64),
        hasLinterFlags: linterFlags > 0,
        env
      }),
      text: buildDeliveryText({ pageUrl, lang: deliveryLang, correctionUrl, hasQr: Boolean(qrPngBase64), env }),
      attachments: qrPngBase64
        ? [{
            content: qrPngBase64,
            type: 'image/png',
            filename: `${(env.PRODUCT_ID || 'hmu').trim()}-qr-${slug}.png`,
            disposition: 'inline',
            content_id: DELIVERY_QR_CID
          }]
        : []
    });
  } catch (err) {
    console.error('delivery email failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to send delivery email' }, 500);
  }

  // Update order status to delivered
  try {
    order.status = 'delivered';
    order.updated_at = new Date().toISOString();
    await env.SERVICE_MENU_KV.put(kvKey(env, 'order', orderId), JSON.stringify(order));
  } catch (err) {
    console.error('order status update failed:', safeError(err));
  }

  return jsonResponse({
    ok: true,
    slug,
    status: 'delivered',
    pageUrl
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// CORRECTIONS — status, request, purchase, notify
// ─────────────────────────────────────────────────────────────────────────────

// Marca una modificación gratis como consumida en el registro de entrega y, si
// al cliente le quedan incluidas, acuña y devuelve el token del SIGUIENTE
// enlace. Devuelve '' cuando ya no quedan (a partir de ahí se compra).
// Nunca lanza: un fallo de contador no puede tumbar una corrección ya aplicada
// — en el peor caso el cliente pide la siguiente por correo.
async function consumeFreeChange(env, slug, tokenRecord) {
  try {
    const deliveryKey = kvKey(env, 'delivery', slug);
    const delivery = await env.SERVICE_MENU_KV.get(deliveryKey, { type: 'json' });
    if (!delivery) return '';
    // Entregas anteriores a esta versión no tienen contador: se asume que la
    // que se acaba de usar era la primera.
    const total = Number.isInteger(delivery.free_total) ? delivery.free_total : freeChanges(env);
    const used = (Number.isInteger(delivery.free_used) ? delivery.free_used : 0) + 1;
    delivery.free_total = total;
    delivery.free_used = used;

    let nextToken = '';
    if (used < total) {
      nextToken = generateSecureToken();
      await env.SERVICE_MENU_KV.put(kvKey(env, 'correction', nextToken), JSON.stringify({
        correction_token: nextToken,
        order_id: tokenRecord.order_id,
        slug,
        lang: tokenRecord.lang === 'es' ? 'es' : 'en',
        paid: false,
        created_at: new Date().toISOString(),
        used_at: null
      }), { expirationTtl: 7776000 }); // 90 days
      delivery.correction_token = nextToken;
    }
    await env.SERVICE_MENU_KV.put(deliveryKey, JSON.stringify(delivery), { expirationTtl: 7776000 });
    return nextToken;
  } catch (err) {
    console.error('free change counter failed:', safeError(err));
    return '';
  }
}

function correctionFormUrl(env, token, lang) {
  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  return `${baseUrl}/correct/?t=${encodeURIComponent(token)}&l=${lang === 'es' ? 'es' : 'en'}`;
}

// Mínimo de campos prellenados para mandar al cliente a SU formulario completo
// en vez de a la página de texto libre. Con menos, el formulario saldría casi
// vacío y enviarlo BORRARÍA su información: en ese caso es mejor el camino
// viejo (/correct/ + IA).
//
// OJO: esta cota NO alcanza como red de seguridad, aunque se diseñó para eso.
// Tally manda un `name` por CADA campo —derivado del título si el spec no lo
// declara— así que el mapa de prefill nunca está vacío y la condición siempre
// se cumple, incluso en una vertical sin cablear. La red real es el interruptor
// MODIFICATION_FORM_PREFILL de arriba (hallazgo del 2026-07-21).
const MIN_PREFILL_FIELDS_FOR_FORM_EDIT = 3;

// Tope del fragmento de prefill: un intake completo es mucho más largo que el
// de un prospecto (1500). 6000 deja la URL dentro de lo que navegadores y
// proxies manejan sin truncar.
const MODIFICATION_PREFILL_MAX = 6000;

/**
 * Enlace de MODIFICACIÓN: el formulario de intake del cliente, prellenado con
 * lo que él mismo escribió, más los hidden que lo convierten en una edición de
 * su página existente (client_slug + correction_token) en vez de una página
 * nueva. Devuelve '' si no hay con qué prellenar — quien llama cae a
 * `correctionFormUrl`.
 */
function modificationFormUrl(env, { token, slug, lang, orderId, customerEmail, prefill }) {
  // Interruptor explícito por vertical: sin el cableado de hidden fields +
  // "default answer" en Tally, el formulario llega VACÍO y enviarlo borraría lo
  // que el cliente no reescriba. Apagado = `/correct/` de texto libre, que
  // funciona. Ver modificationFormPrefillEnabled en product-config.mjs.
  if (!modificationFormPrefillEnabled(env)) return '';
  const base = ((lang === 'es' ? env.TALLY_FORM_URL_ES : env.TALLY_FORM_URL_EN) || '').trim();
  if (!base || !slug || !token) return '';
  const fields = prefill && typeof prefill === 'object' ? Object.keys(prefill).length : 0;
  if (fields < MIN_PREFILL_FIELDS_FOR_FORM_EDIT) return '';

  // Las bases TALLY_FORM_URL_* ya terminan en `?order_id=`.
  let url = `${base}${encodeURIComponent(orderId || '')}`;
  url += `&customer_email=${encodeURIComponent(customerEmail || '')}`;
  url += `&client_slug=${encodeURIComponent(slug)}`;
  url += `&correction_token=${encodeURIComponent(token)}`;
  url += buildPrefillQuery(prefill, MODIFICATION_PREFILL_MAX);
  return url;
}

// Enlace de modificación con caída al camino viejo: formulario completo
// prellenado si se puede, página de texto libre si no.
/**
 * Correo de "tu página fue actualizada" tras una modificación hecha con el
 * formulario completo. Reusa los mismos cuerpos que la corrección por texto
 * libre, e incluye el enlace de la SIGUIENTE modificación si al cliente le
 * quedan incluidas (consumeFreeChange ya la acuñó y la dejó en el registro de
 * entrega). Idempotente por submission_id: Actions puede reintentar.
 */
async function notifyModificationApplied({ env, slug, orderId, submissionId }) {
  const deliveryKey = kvKey(env, 'delivery', slug);
  const delivery = await env.SERVICE_MENU_KV.get(deliveryKey, { type: 'json' }).catch(() => null);
  if (delivery?.last_modification_notified === submissionId) {
    return jsonResponse({ ok: true, idempotent: true, slug });
  }

  const order = orderId
    ? await env.SERVICE_MENU_KV.get(kvKey(env, 'order', orderId), { type: 'json' }).catch(() => null)
    : null;
  const customerEmail = order?.customer_email || delivery?.customer_email || '';
  const lang = emailLangFromCurrency(order?.currency) || 'en';
  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  const pageUrl = `${baseUrl}/links/${slug}/`;

  // Enlace de la siguiente modificación incluida, si quedan.
  let nextCorrectionUrl = '';
  const nextToken = delivery?.correction_token || '';
  if (nextToken) {
    const nextRecord = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction', nextToken), { type: 'json' }).catch(() => null);
    if (nextRecord && !nextRecord.used_at) {
      nextCorrectionUrl = await bestModificationUrl(env, { token: nextToken, slug, lang, orderId });
    }
  }

  if (customerEmail && secret(env, 'SENDGRID_API_KEY')) {
    try {
      await sendEmail({
        env,
        to: customerEmail,
        subject: lang === 'es'
          ? `✅ Tu página ${brandName(env)} fue actualizada`
          : `✅ Your ${brandName(env)} page was updated`,
        html: buildCorrectionAppliedEmail({
          pageUrl, lang, buyUrl: buyCorrectionUrl(env, slug), nextCorrectionUrl, env
        }),
        text: buildCorrectionAppliedText({
          pageUrl, lang, buyUrl: buyCorrectionUrl(env, slug), nextCorrectionUrl, env
        })
      });
    } catch (err) {
      console.error('modification applied email failed:', safeError(err));
      return jsonResponse({ ok: false, error: 'Failed to send modification email' }, 500);
    }
  }

  if (delivery) {
    delivery.last_modification_notified = submissionId;
    delivery.updated_at = new Date().toISOString();
    await env.SERVICE_MENU_KV.put(deliveryKey, JSON.stringify(delivery), { expirationTtl: 7776000 }).catch(() => {});
  }
  return jsonResponse({ ok: true, slug, status: 'modification_notified' });
}

/**
 * ¿Este envío del formulario es una modificación de una página ya publicada?
 * Devuelve {slug, token, record} si el token es válido para ese slug y esa
 * orden, {} si el envío es un intake normal, o {error} si trae credenciales de
 * modificación que no cuadran (se rechaza: nunca se degrada a intake nuevo, que
 * publicaría una página duplicada en silencio).
 */
async function resolveModification(env, normalized, order) {
  const slug = (cleanValue(getAnswer(normalized.answers, 'client_slug')) || '').trim();
  const token = (cleanValue(getAnswer(normalized.answers, 'correction_token')) || '').trim();
  if (!slug && !token) return {};
  if (!slug || !token) return { error: 'incomplete_modification_credentials' };
  if (!SLUG_RE.test(slug) || token.length > 64) return { error: 'malformed_modification_credentials' };

  const record = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction', token), { type: 'json' }).catch(() => null);
  if (!record) return { error: 'unknown_correction_token' };
  if (record.used_at) return { error: 'correction_token_already_used' };
  if (record.slug !== slug) return { error: 'token_slug_mismatch' };
  if (record.order_id && order?.order_id && record.order_id !== order.order_id) {
    return { error: 'token_order_mismatch' };
  }
  return { slug, token, record };
}

async function bestModificationUrl(env, { token, slug, lang, orderId }) {
  try {
    const order = orderId
      ? await env.SERVICE_MENU_KV.get(kvKey(env, 'order', orderId), { type: 'json' })
      : null;
    const submissionId = order?.submission_id || '';
    const submission = submissionId
      ? await env.SERVICE_MENU_KV.get(kvKey(env, 'submission', submissionId), { type: 'json' })
      : null;
    const url = modificationFormUrl(env, {
      token,
      slug,
      lang,
      orderId,
      customerEmail: order?.customer_email || '',
      prefill: submission?.prefill
    });
    if (url) return url;
  } catch (err) {
    console.error('modification url build failed:', safeError(err));
  }
  return correctionFormUrl(env, token, lang);
}

// La compra de corrección adicional pasa por el worker (crea el Checkout
// Session al vuelo); la página /correct/ y los correos enlazan esta URL.
function buyCorrectionUrl(env, slug, fallbackOrigin = '') {
  const workerBase = (env.WORKER_PUBLIC_URL || fallbackOrigin || '').trim().replace(/\/+$/, '');
  if (!workerBase || !SLUG_RE.test(slug)) return '';
  return `${workerBase}/buy-correction?slug=${encodeURIComponent(slug)}`;
}

// GET /correction-status?t=<token> — estado del token para la página /correct/.
// Un token es un secreto de 32 chars aleatorios: responder estado + slug a
// quien lo tenga no filtra nada que el dueño no sepa ya.
async function handleCorrectionStatus(url, env) {
  const token = (url.searchParams.get('t') || '').trim();
  if (!token || token.length > 64) {
    return jsonResponse({ ok: true, state: 'invalid' });
  }
  const record = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction', token), { type: 'json' }).catch(() => null);
  if (!record) {
    return jsonResponse({ ok: true, state: 'invalid' });
  }
  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  if (record.used_at) {
    return jsonResponse({
      ok: true,
      state: 'used',
      buy_url: buyCorrectionUrl(env, record.slug)
    });
  }
  return jsonResponse({
    ok: true,
    state: 'valid',
    slug: record.slug,
    page_url: `${baseUrl}/links/${record.slug}/`
  });
}

// POST /correct — body JSON {token, changes}. El token de un solo uso
// autentica (nace en el correo de entrega o en una compra de corrección).
async function handleCorrectionRequest(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: 'Invalid JSON' }, 400);
  }

  const token = cleanValue(body?.token);
  // El texto viaja en client_payload (logs públicos de Actions) y la página
  // /correct/ lo advierte: solo cambios para la página pública, nada privado.
  const changes = String(body?.changes || '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '')
    .trim();

  if (!token || token.length > 64) {
    return jsonResponse({ ok: false, state: 'invalid' }, 403);
  }
  if (changes.length < CORRECTION_TEXT_MIN) {
    return jsonResponse({ ok: false, error: 'changes_too_short' }, 400);
  }
  if (changes.length > CORRECTION_TEXT_MAX) {
    return jsonResponse({ ok: false, error: 'changes_too_long' }, 400);
  }

  const record = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction', token), { type: 'json' }).catch(() => null);
  if (!record) {
    return jsonResponse({ ok: false, state: 'invalid' }, 403);
  }
  if (record.used_at) {
    return jsonResponse({ ok: false, state: 'used', buy_url: buyCorrectionUrl(env, record.slug) }, 403);
  }

  const slug = record.slug;
  const now = new Date().toISOString();
  const correctionId = `c${generateSecureToken().slice(0, 20)}`;

  // El registro guarda el texto completo (KV, privado); a Actions solo viaja
  // el texto saneado + slug + correction_id — nunca order_id ni el token.
  try {
    await env.SERVICE_MENU_KV.put(kvKey(env, 'correction_request', correctionId), JSON.stringify({
      correction_id: correctionId,
      correction_token: token,
      order_id: record.order_id,
      slug,
      lang: record.lang === 'es' ? 'es' : 'en',
      paid: !!record.paid,
      changes,
      status: 'dispatched',
      created_at: now
    }), { expirationTtl: 7776000 }); // 90 days
  } catch (err) {
    console.error('correction request save failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to save correction' }, 500);
  }

  try {
    await dispatchGitHubAction(env, {
      is_correction: true,
      correction_id: correctionId,
      slug,
      correction_text: changes
    });
  } catch (err) {
    // El token NO se quema si el dispatch falla: el cliente puede reintentar.
    console.error('github dispatch for correction failed:', safeError(err));
    return jsonResponse({ ok: false, error: 'Failed to dispatch correction' }, 500);
  }

  // Quemar el token después del dispatch exitoso. (Ventana de carrera entre
  // dos POST simultáneos con el mismo token: aceptada — el rate limit la acota
  // y el peor caso es una regeneración doble de la misma página.)
  try {
    record.used_at = now;
    record.correction_id = correctionId;
    await env.SERVICE_MENU_KV.put(kvKey(env, 'correction', token), JSON.stringify(record), { expirationTtl: 7776000 });
  } catch (err) {
    console.error('correction token update failed:', safeError(err));
  }

  // Contador de modificaciones GRATIS (estándar de la casa: 2, configurable por
  // vertical con FREE_CHANGES <- legal.free_changes). Antes cada token valía por
  // una y se acababa ahí; ahora, si al cliente le quedan incluidas, se acuña el
  // siguiente enlace aquí y viaja en el correo de "tu página fue actualizada".
  // Solo consumen cupo las gratis: una modificación comprada no descuenta.
  if (record.paid !== true) {
    const nextToken = await consumeFreeChange(env, slug, record);
    if (nextToken) {
      try {
        const requestRecord = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction_request', correctionId), { type: 'json' });
        if (requestRecord) {
          requestRecord.next_correction_token = nextToken;
          await env.SERVICE_MENU_KV.put(kvKey(env, 'correction_request', correctionId), JSON.stringify(requestRecord), { expirationTtl: 7776000 });
        }
      } catch (err) {
        console.error('next correction token save failed:', safeError(err));
      }
    }
  }

  // Copia de auditoría para Verónica — nunca bloquea la respuesta al cliente.
  await notifyAdmin(env, `${brandName(env)} corrección solicitada — ${slug}`, [
    `Corrección ${record.paid ? 'ADICIONAL (pagada)' : 'gratuita'} solicitada.`,
    `Página: ${(env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim()}/links/${slug}/`,
    `correction_id: ${correctionId}`,
    '',
    'Cambios pedidos por el cliente:',
    changes
  ]);

  return jsonResponse({ ok: true, correction_id: correctionId, slug });
}

// GET /buy-correction?slug=<slug> — crea un Stripe Checkout Session para una
// corrección adicional y redirige. Sin STRIPE_SECRET_KEY (o sin registro de
// entrega) redirige a /correct/?buy=unavailable, que ofrece WhatsApp/correo.
// Seguridad: cualquiera puede pagar por cualquier slug, pero el token acuñado
// se envía SOLO al email del pedido original — pagar por la página de un
// tercero únicamente le regala una corrección al dueño.
async function handleBuyCorrection(url, env) {
  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  const unavailableUrl = `${baseUrl}/correct/?buy=unavailable`;

  const slug = (url.searchParams.get('slug') || '').trim().toLowerCase();
  if (!SLUG_RE.test(slug)) {
    return Response.redirect(unavailableUrl, 302);
  }

  const delivery = await env.SERVICE_MENU_KV.get(kvKey(env, 'delivery', slug), { type: 'json' }).catch(() => null);
  const stripeKey = secret(env, 'STRIPE_SECRET_KEY');
  if (!delivery || !stripeKey) {
    return Response.redirect(unavailableUrl, 302);
  }

  const order = delivery.order_id
    ? await env.SERVICE_MENU_KV.get(kvKey(env, 'order', delivery.order_id), { type: 'json' }).catch(() => null)
    : null;
  const currency = (order?.currency || '').toLowerCase() === 'mxn' ? 'mxn' : 'usd';
  const lang = currency === 'mxn' ? 'es' : 'en';
  const price = CORRECTION_PRICE[currency];

  const params = new URLSearchParams();
  params.set('mode', 'payment');
  params.set('line_items[0][quantity]', '1');
  params.set('line_items[0][price_data][currency]', currency);
  params.set('line_items[0][price_data][unit_amount]', String(price.unit_amount));
  params.set(
    'line_items[0][price_data][product_data][name]',
    lang === 'es' ? `${brandName(env)} — Corrección adicional` : `${brandName(env)} — Extra correction`
  );
  // Metadata por vertical (<PRODUCT_ID>_correction) — el webhook la detecta
  // con la MISMA función en stripe-filter.mjs; un literal fijo colisionaría
  // entre verticales en la cuenta compartida de Stripe.
  params.set(`metadata[${correctionMetadataKey(env)}]`, '1');
  params.set('metadata[slug]', slug);
  params.set('success_url', `${baseUrl}/correct/thanks/?l=${lang}`);
  params.set('cancel_url', `${baseUrl}/links/${slug}/`);
  if (delivery.customer_email) {
    params.set('customer_email', delivery.customer_email);
  }

  let session;
  try {
    const resp = await fetch('https://api.stripe.com/v1/checkout/sessions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${stripeKey}`,
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: params.toString()
    });
    if (!resp.ok) {
      console.error('checkout session create failed:', resp.status);
      return Response.redirect(unavailableUrl, 302);
    }
    session = await resp.json();
  } catch (err) {
    console.error('checkout session create error:', safeError(err));
    return Response.redirect(unavailableUrl, 302);
  }

  if (!session?.url) {
    return Response.redirect(unavailableUrl, 302);
  }
  return Response.redirect(session.url, 302);
}

// Webhook: pago de corrección adicional → acuñar token nuevo y mandarlo al
// email del pedido original.
async function handleCorrectionPurchase(session, env) {
  const paymentIntentId = session.payment_intent || session.id || '';
  if (!paymentIntentId) {
    return jsonResponse({ ok: false, error: 'Missing payment_intent id' }, 400);
  }

  const processedKey = kvKey(env, 'processed', paymentIntentId);
  const alreadyProcessed = await env.SERVICE_MENU_KV.get(processedKey).catch(() => null);
  if (alreadyProcessed) {
    return jsonResponse({ ok: true, idempotent: true, paymentIntentId });
  }

  const slug = cleanValue(session.metadata?.slug || '').toLowerCase();
  const buyerEmail = session.customer_email || session.customer_details?.email || '';
  const delivery = SLUG_RE.test(slug)
    ? await env.SERVICE_MENU_KV.get(kvKey(env, 'delivery', slug), { type: 'json' }).catch(() => null)
    : null;
  const order = delivery?.order_id
    ? await env.SERVICE_MENU_KV.get(kvKey(env, 'order', delivery.order_id), { type: 'json' }).catch(() => null)
    : null;
  // SOLO el email del pedido/entrega original recibe el token (nunca el
  // comprador de la sesión si no hay registro — eso sería regalar acceso).
  const ownerEmail = order?.customer_email || delivery?.customer_email || '';
  const currency = (session.currency || order?.currency || 'usd').toLowerCase();
  const lang = currency === 'mxn' ? 'es' : 'en';

  await env.SERVICE_MENU_KV.put(processedKey, '1', { expirationTtl: 604800 }).catch(() => {});

  if (!delivery || !ownerEmail) {
    // Sin registro de entrega no se puede acuñar con seguridad: atender a mano.
    await notifyAdmin(env, `${brandName(env)} corrección pagada SIN registro — atender manual`, [
      'Se pagó una corrección adicional pero no hay registro de entrega en KV.',
      `slug (metadata): ${slug || '(vacío)'}`,
      `email del comprador (Stripe): ${buyerEmail || '(sin email)'}`,
      `payment_intent: ${paymentIntentId}`,
      '',
      'Acción: aplicar la corrección manualmente o reembolsar.'
    ]);
    return jsonResponse({ ok: true, manual: true, paymentIntentId });
  }

  const token = generateSecureToken();
  try {
    await env.SERVICE_MENU_KV.put(kvKey(env, 'correction', token), JSON.stringify({
      correction_token: token,
      order_id: delivery.order_id || '',
      slug,
      lang,
      paid: true,
      created_at: new Date().toISOString(),
      used_at: null
    }), { expirationTtl: 2592000 }); // 30 days
  } catch (err) {
    console.error('paid correction token save failed:', safeError(err));
    await env.SERVICE_MENU_KV.delete(processedKey).catch(() => {});
    return jsonResponse({ ok: false, error: 'Failed to save correction token' }, 500);
  }

  const formUrl = correctionFormUrl(env, token, lang);
  const pageUrl = `${(env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim()}/links/${slug}/`;
  try {
    await sendEmail({
      env,
      to: ownerEmail,
      subject: lang === 'es'
        ? `Tu corrección adicional de ${brandName(env)} — pídela aquí`
        : `Your extra ${brandName(env)} correction — request it here`,
      html: buildCorrectionPurchaseEmail({ formUrl, pageUrl, lang, env }),
      text: buildCorrectionPurchaseText({ formUrl, pageUrl, lang, env })
    });
  } catch (err) {
    console.error('correction purchase email failed:', safeError(err));
    await env.SERVICE_MENU_KV.delete(processedKey).catch(() => {});
    return jsonResponse({ ok: false, error: 'Failed to send correction email' }, 500);
  }

  // Sin copia de admin por la compra en sí: el recibo de Stripe ya registra el
  // pago, y el aviso "<marca> corrección solicitada" (handleCorrectionRequest)
  // llega con el cambio real cuando el comprador lo envía.
  return jsonResponse({ ok: true, paymentIntentId, slug });
}

// /notify con correction_id — Actions terminó de procesar la corrección.
// correction_status: 'applied' (página regenerada) | 'manual' (el LLM no pudo
// aplicarla con seguridad; se atiende a mano).
async function handleCorrectionNotify(body, env) {
  const correctionId = cleanValue(body?.correction_id);
  const status = body?.correction_status === 'applied' ? 'applied' : 'manual';

  const record = await env.SERVICE_MENU_KV.get(kvKey(env, 'correction_request', correctionId), { type: 'json' }).catch(() => null);
  if (!record) {
    return jsonResponse({ ok: false, error: 'Correction request not found' }, 404);
  }
  if (record.notified_at) {
    return jsonResponse({ ok: true, idempotent: true, correction_id: correctionId });
  }

  const order = record.order_id
    ? await env.SERVICE_MENU_KV.get(kvKey(env, 'order', record.order_id), { type: 'json' }).catch(() => null)
    : null;
  const customerEmail = order?.customer_email || '';
  const lang = record.lang === 'es' ? 'es' : 'en';
  const baseUrl = (env.PUBLIC_BOOK_BASE_URL || 'https://www.hmulink.com').trim();
  const pageUrl = `${baseUrl}/links/${record.slug}/`;
  const buyUrl = buyCorrectionUrl(env, record.slug);
  // Si al cliente le quedaban modificaciones incluidas, /correct ya acuñó el
  // siguiente enlace: va en este mismo correo para que no tenga que pedirlo.
  const nextCorrectionUrl = record.next_correction_token
    ? await bestModificationUrl(env, {
        token: record.next_correction_token,
        slug: record.slug,
        lang,
        orderId: record.order_id
      })
    : '';

  if (customerEmail) {
    try {
      if (status === 'applied') {
        await sendEmail({
          env,
          to: customerEmail,
          subject: lang === 'es'
            ? `✅ Tu página ${brandName(env)} fue actualizada`
            : `✅ Your ${brandName(env)} page was updated`,
          html: buildCorrectionAppliedEmail({ pageUrl, lang, buyUrl, nextCorrectionUrl, env }),
          text: buildCorrectionAppliedText({ pageUrl, lang, buyUrl, nextCorrectionUrl, env })
        });
      } else {
        await sendEmail({
          env,
          to: customerEmail,
          subject: lang === 'es'
            ? 'Recibimos tu corrección — la aplicamos en 1 día hábil'
            : 'We received your correction — applying it within 1 business day',
          html: buildCorrectionManualEmail({ pageUrl, lang, env }),
          text: buildCorrectionManualText({ pageUrl, lang, env })
        });
      }
    } catch (err) {
      console.error('correction notify email failed:', safeError(err));
      return jsonResponse({ ok: false, error: 'Failed to send correction email' }, 500);
    }
  }

  if (status === 'manual') {
    await notifyAdmin(env, `⚠️ ${brandName(env)} corrección MANUAL pendiente — ${record.slug}`, [
      'La corrección automática no se pudo aplicar; hay que hacerla a mano.',
      `Página: ${pageUrl}`,
      `correction_id: ${correctionId}`,
      '',
      'Cambios pedidos por el cliente:',
      record.changes || '(sin texto)',
      '',
      'Cómo aplicarla: editar data/clients/' + record.slug + '.client.json,',
      'regenerar con generator/generate_service_menu.py --client y pushear.'
    ]);
  }

  try {
    record.status = status;
    record.notified_at = new Date().toISOString();
    await env.SERVICE_MENU_KV.put(kvKey(env, 'correction_request', correctionId), JSON.stringify(record), { expirationTtl: 7776000 });
  } catch (err) {
    console.error('correction request update failed:', safeError(err));
  }

  return jsonResponse({ ok: true, correction_id: correctionId, status });
}

// Aviso interno a Verónica (REPLY_TO_EMAIL). Nunca lanza: un fallo de correo
// interno no debe romper el flujo del cliente.
async function notifyAdmin(env, subject, lines) {
  const to = (env.REPLY_TO_EMAIL || '').trim();
  if (!to) return;
  const text = lines.join('\n');
  try {
    await sendEmail({
      env,
      to,
      subject,
      html: `<pre style="font-family: monospace; white-space: pre-wrap;">${escapeHtml(text)}</pre>`,
      text
    });
  } catch (err) {
    console.error('admin notify failed:', safeError(err));
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ─────────────────────────────────────────────────────────────────────────────
// UTILITY FUNCTIONS
// ─────────────────────────────────────────────────────────────────────────────

/** Nombre legible del producto para los correos de alerta. Se prefiere
 * BRAND_NAME —que es literalmente la var documentada como «marca usada en
 * asuntos/cuerpos/footers de los correos»— y solo si falta se capitaliza
 * PRODUCT_ID. Con PRODUCT_ID a secas la alerta decía «Hmu»/«Pawcontact»:
 * legible, pero no es como se llama ninguno de los dos productos. Sin ninguna
 * de las dos vars queda «El producto», que es la señal de que a esa vertical
 * le falta declarar su identidad. */
function marca(env) {
  const brand = String((env && env.BRAND_NAME) || '').trim();
  if (brand) return brand;
  const id = String((env && env.PRODUCT_ID) || 'el producto').trim();
  return id.charAt(0).toUpperCase() + id.slice(1);
}

function alertEmail(env) {
  return (env.ALERT_EMAIL || env.REPLY_TO_EMAIL || '').trim();
}

// Payment links de OTROS productos de la cuenta Stripe compartida que ya sabemos
// que no son del producto: NO alertamos por ellos (evita el ruido de cada venta de un
// producto hermano). Mismo formato que STRIPE_PAYMENT_LINK_IDS (coma-separado).
function knownOtherPaymentLinks(env) {
  return (env.KNOWN_OTHER_PAYMENT_LINKS || '')
    .split(',')
    .map((id) => id.trim())
    .filter(Boolean);
}

function sessionEmail(session) {
  return session?.customer_email || session?.customer_details?.email || session?.receipt_email || '';
}

// Manda UN correo de alerta, deduplicado por `dedupKey` durante `ttlSeconds`.
// Reserva la llave hmu_alerted:<dedupKey> ANTES de enviar: si dos reintentos de
// Stripe entran a la vez, solo el primero pasa el chequeo y manda el correo.
// Nunca lanza: una alerta que falla no puede tumbar el flujo del cliente ni el
// 200 que Stripe espera.
async function alertOnce(env, dedupKey, ttlSeconds, subject, lines) {
  try {
    const to = alertEmail(env);
    if (!to) return;
    const key = `hmu_alerted:${dedupKey}`;
    const already = await env.SERVICE_MENU_KV.get(key).catch(() => null);
    if (already) return;
    // Reservar antes de enviar (idempotencia / rate-limit).
    await env.SERVICE_MENU_KV.put(key, '1', { expirationTtl: ttlSeconds }).catch(() => {});
    const text = lines.join('\n');
    await sendEmail({
      env,
      to,
      subject,
      html: `<pre style="font-family: monospace; white-space: pre-wrap;">${escapeHtml(text)}</pre>`,
      text
    });
  } catch (err) {
    console.error('alertOnce failed:', safeError(err));
  }
}

// Qué le falta al intake, en español legible para el correo de alerta. La clave
// es el mismo string que viaja en el 422 `incomplete_intake` (missingFromIntake).
// Un `missing` desconocido cae a la clave cruda: la alerta se manda igual —
// nunca se pierde por un caso que este mapa no previó.
const INCOMPLETE_INTAKE_REASONS = {
  business_name: {
    es: 'falta el nombre del negocio',
    detalle: 'Llegó un formulario de intake sin business_name (campo obligatorio).'
  },
  public_contact: {
    es: 'sin contacto público',
    detalle: 'Llegó un formulario de intake sin ningún medio de contacto público\n(WhatsApp / teléfono / email / reservas / web).'
  },
  sale_button: {
    es: 'sin botón de venta',
    detalle: 'Llegó un formulario de intake sin el botón de venta del catálogo\n(WhatsApp / SMS / teléfono): las tarjetas de producto no llevarían a ningún lado.'
  },
  products: {
    es: 'sin productos',
    detalle: 'Llegó un formulario de intake sin fuente de productos (ni la vista previa\ndel prospector ni la lista escrita por el cliente).'
  }
};

/**
 * ¿El intake trae lo mínimo para publicar? Devuelve '' si sí, o el nombre de lo
 * que falta — y en ese caso deja el rastro en KV y MANDA CORREO.
 *
 * El correo es lo que se había perdido. Sin él, el 422 se lo come Tally, el
 * cliente ya pagó, su página nunca se genera y nadie se entera: el fallo
 * silencioso exacto que el bloque A3 existe para evitar. Estaba en el worker de
 * HMU desde A3 y desapareció al extraer el motor; se repone aquí, así que lo
 * heredan las CINCO verticales y no solo HMU.
 *
 * Nunca lanza: alertOnce se traga sus propios errores y el put de KV va con
 * .catch(). El 422 sale igual aunque la alerta falle.
 */
async function checkIntakeCompleteness(env, publicPayload, { orderId, submissionId, now }) {
  // El nombre del negocio lo exigen las dos plantillas; el resto del mínimo
  // cambia por plantilla (un canal de contacto en service-menu; botón de venta
  // + productos en el catálogo).
  const missing = publicPayload.business_name
    ? missingFromIntake(publicPayload, env)
    : 'business_name';
  if (!missing) return '';
  await recordIncompleteIntake(env, { missing, orderId, submissionId, now });
  return missing;
}

// Rastro en KV + la alerta, deduplicada por submission_id durante 7 días (mismo
// criterio que traía el worker de HMU): Tally reintenta el webhook, y un intake
// incompleto no deja de serlo — un correo por envío, no uno por reintento.
async function recordIncompleteIntake(env, { missing, orderId, submissionId, now }) {
  await env.SERVICE_MENU_KV.put(
    kvKey(env, 'incomplete_intake', orderId, submissionId),
    JSON.stringify({ order_id: orderId, submission_id: submissionId, missing, attempted_at: now }),
    { expirationTtl: 2592000 }
  ).catch(() => {});

  const reason = INCOMPLETE_INTAKE_REASONS[missing];
  const resumen = reason ? reason.es : `falta ${missing}`;
  const detalle = reason
    ? reason.detalle
    : `Llegó un formulario de intake sin ${missing} (campo obligatorio).`;

  await alertOnce(env, `incomplete_intake:${submissionId}`, 604800,
    `⚠️ ${marca(env)}: intake incompleto — ${resumen}`, [
      ...detalle.split('\n'),
      'La página NO se generó y el cliente no recibió nada.',
      '',
      `order_id: ${orderId}`,
      `submission_id: ${submissionId}`,
      `falta: ${missing}`,
      '',
      'Acción: contactar al cliente para completar el dato, o revisar el mapeo',
      'del formulario de Tally si el campo sí venía.'
    ]);
}

// POST /alert-generation-failed — lo llama el step `if: failure()` del workflow
// de generación cuando la generación falla DESPUÉS de que el cliente pagó y
// llenó el formulario (sin esto, un fallo de Actions dejaba la orden colgada y
// nadie se enteraba). Autenticado con NOTIFY_SECRET, el mismo de /notify.
async function handleGenerationFailedAlert(request, env) {
  const notifySecret = (env.NOTIFY_SECRET || '').trim();
  if (!notifySecret) {
    return jsonResponse({ ok: false, error: 'NOTIFY_SECRET not configured' }, 500);
  }
  const authHeader = request.headers.get('authorization') || '';
  const provided = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';
  if (!timingSafeEqual(provided, notifySecret)) {
    return jsonResponse({ ok: false, error: 'Unauthorized' }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    body = {};
  }
  const slug = cleanValue(body?.slug);
  const submissionId = cleanValue(body?.submission_id);
  const correctionId = cleanValue(body?.correction_id);
  const runUrl = cleanValue(body?.run_url);
  const reason = cleanValue(body?.reason) || 'La generación de la página falló en GitHub Actions.';

  // Dedup por el identificador más específico disponible.
  const dedup = `genfail:${correctionId || submissionId || slug || runUrl || 'unknown'}`;
  await alertOnce(env, dedup, 86400,
    `⚠️ HMU: la generación FALLÓ — ${slug || submissionId || 'sin id'}`, [
      'La generación de la página falló DESPUÉS del pago + formulario.',
      'El cliente pagó y llenó el intake, pero su página no se generó.',
      '',
      `slug: ${slug || '(desconocido)'}`,
      submissionId ? `submission_id: ${submissionId}` : '',
      correctionId ? `correction_id: ${correctionId}` : '',
      `motivo: ${reason}`,
      runUrl ? `run de Actions: ${runUrl}` : '',
      '',
      'Acción: revisar el run de Actions y re-disparar la generación.'
    ].filter(Boolean));

  return jsonResponse({ ok: true });
}

const EMAIL_FAILURE_EVENTS = new Set(['bounce', 'dropped', 'blocked', 'spamreport']);

/**
 * Convierte una firma ECDSA de DER (ASN.1) a IEEE P1363 (r||s, 64 bytes).
 *
 * SendGrid firma con OpenSSL y manda la firma en DER. WebCrypto NO acepta DER
 * para ECDSA: `crypto.subtle.verify` exige r||s crudo, y ante DER simplemente
 * devuelve false -- sin excepcion, sin log, sin pista. La version anterior
 * pasaba el DER directo (la variable hasta se llamaba `sigDer`), asi que
 * RECHAZABA CON 401 todos los eventos legitimos de SendGrid. Se detecto
 * firmando un payload con una llave P-256 real y viendo que no lo aceptaba.
 *
 * Nota del formato: DER guarda r y s como enteros con signo, asi que a veces
 * les antepone un 0x00 (si el primer bit es 1) y a veces son mas cortos de 32
 * bytes. Por eso hay que quitar el relleno y volver a alinear a la DERECHA en
 * 32 bytes: truncar o copiar en crudo falla ~1 de cada 2 firmas.
 */
function ecdsaDerToP1363(der) {
  if (der[0] !== 0x30) return null;
  let i = 2;
  if (der[1] & 0x80) i = 2 + (der[1] & 0x7f);   // longitud larga
  const leerEntero = () => {
    if (der[i++] !== 0x02) return null;
    const len = der[i++];
    let v = der.slice(i, i + len);
    i += len;
    while (v.length > 32 && v[0] === 0x00) v = v.slice(1);
    if (v.length > 32 || v.length === 0) return null;
    const out = new Uint8Array(32);
    out.set(v, 32 - v.length);
    return out;
  };
  const r = leerEntero();
  if (!r) return null;
  const s = leerEntero();
  if (!s) return null;
  const sig = new Uint8Array(64);
  sig.set(r, 0);
  sig.set(s, 32);
  return sig;
}

async function verifySendGridSignature(rawBody, signature, timestamp, publicKeyB64) {
  if (!signature || !timestamp || !publicKeyB64) return false;
  try {
    // Ventana anti-replay de 300s, igual que el webhook de Stripe.
    const ts = Number(timestamp);
    if (!Number.isFinite(ts) || Math.abs(Date.now() / 1000 - ts) > 300) return false;
    const keyDer = Uint8Array.from(atob(publicKeyB64), (c) => c.charCodeAt(0));
    const key = await crypto.subtle.importKey(
      'spki', keyDer, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']
    );
    const sig = ecdsaDerToP1363(Uint8Array.from(atob(signature), (c) => c.charCodeAt(0)));
    if (!sig) return false;
    const payload = new TextEncoder().encode(timestamp + rawBody);
    return await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, sig, payload);
  } catch (err) {
    console.error('verifySendGridSignature failed:', safeError(err));
    return false;
  }
}

async function handleEmailEvents(request, env) {
  const publicKey = env.SENDGRID_WEBHOOK_PUBLIC_KEY;
  if (!publicKey) {
    // Fail-closed a proposito: mejor no recibir eventos que aceptarlos sin firma.
    return new Response('email events webhook not configured', { status: 503 });
  }
  const rawBody = await request.text();
  const ok = await verifySendGridSignature(
    rawBody,
    request.headers.get('X-Twilio-Email-Event-Webhook-Signature'),
    request.headers.get('X-Twilio-Email-Event-Webhook-Timestamp'),
    publicKey
  );
  if (!ok) return new Response('invalid signature', { status: 401 });

  let events;
  try {
    events = JSON.parse(rawBody);
  } catch {
    return new Response('invalid json', { status: 400 });
  }
  if (!Array.isArray(events)) return new Response('expected array', { status: 400 });

  let alertados = 0;
  for (const ev of events) {
    const tipo = String(ev?.event || '').toLowerCase();
    if (!EMAIL_FAILURE_EVENTS.has(tipo)) continue;
    const email = String(ev?.email || 'desconocido');
    const motivo = String(ev?.reason || ev?.response || 'sin motivo reportado');
    // Dedup por destinatario+tipo, 24h: SendGrid reintenta el webhook y un
    // mismo rebote puede llegar varias veces.
    await alertOnce(
      env,
      `email_fail:${tipo}:${email}`,
      86400,
      `Un correo de entrega no llego (${tipo})`,
      [
        `Destinatario: ${email}`,
        `Evento: ${tipo}`,
        `Motivo: ${motivo}`,
        '',
        'El cliente pago y su correo NO llego. Revisa la direccion y reenvia a mano.',
      ]
    );
    alertados++;
  }
  return new Response(JSON.stringify({ ok: true, received: events.length, alerted: alertados }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function validateStripeSignature(rawBody, signatureHeader, secret) {
  if (!secret || !signatureHeader) return false;

  try {
    // Stripe header: "t=<ts>,v1=<sig>[,v1=<sig>...]". Parse by key, not by
    // position, and collect every v1 (Stripe may send more than one).
    let timestamp = '';
    const v1Signatures = [];
    for (const part of signatureHeader.split(',')) {
      const idx = part.indexOf('=');
      if (idx === -1) continue;
      const k = part.slice(0, idx).trim();
      const v = part.slice(idx + 1).trim();
      if (k === 't') timestamp = v;
      else if (k === 'v1') v1Signatures.push(v);
    }
    if (!timestamp || v1Signatures.length === 0) return false;

    // Reject signatures outside a 5-minute tolerance to bound replay of a
    // captured, validly-signed request. Stripe re-signs with a fresh timestamp
    // on every delivery (including manual "Resend"), so this never blocks
    // legitimate deliveries.
    const ts = Number(timestamp);
    if (!Number.isFinite(ts) || Math.abs(Date.now() / 1000 - ts) > 300) return false;

    const signedContent = `${timestamp}.${rawBody}`;
    const key = await crypto.subtle.importKey(
      'raw',
      new TextEncoder().encode(secret),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );
    const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(signedContent));
    const expectedSignature = Array.from(new Uint8Array(sig))
      .map(b => b.toString(16).padStart(2, '0'))
      .join('');

    return v1Signatures.some((s) => timingSafeEqual(s, expectedSignature));
  } catch {
    return false;
  }
}

async function verifyTallySignature(rawBody, signatureHeader, secret) {
  if (!secret || !signatureHeader) return false;

  try {
    let normalizedBody;
    try {
      normalizedBody = JSON.stringify(JSON.parse(rawBody));
    } catch {
      normalizedBody = rawBody;
    }

    const key = await crypto.subtle.importKey(
      'raw',
      new TextEncoder().encode(secret),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );
    const sig = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(normalizedBody));
    const expectedBase64 = btoa(String.fromCharCode(...new Uint8Array(sig)));
    return timingSafeEqual(expectedBase64, signatureHeader.trim());
  } catch {
    return false;
  }
}

async function dispatchGitHubAction(env, payload) {
  const githubToken = secret(env, 'GITHUB_TOKEN');
  if (!githubToken || !env.GITHUB_REPO) {
    throw new Error('GITHUB_TOKEN or GITHUB_REPO not configured');
  }

  const repo = env.GITHUB_REPO;
  const eventType = env.GITHUB_ACTIONS_EVENT || 'new-hmu-service-menu';

  const response = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${githubToken}`,
      'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json',
      // GitHub API rechaza con 403 cualquier request sin User-Agent.
      'User-Agent': workerName(env)
    },
    body: JSON.stringify({
      event_type: eventType,
      client_payload: payload
    })
  });

  if (!response.ok) {
    throw new Error(`GitHub dispatch failed: ${response.status} ${response.statusText}`);
  }
}

/**
 * Limitador de ritmo BEST-EFFORT. No es un control de seguridad.
 *
 * Decision formal del hallazgo #5 de la auditoria del 2026-07-23, que ofrecia
 * dos salidas: migrarlo a Durable Object o aceptarlo documentado. Se acepta
 * documentado, y estas son las razones:
 *
 *  1. NO es la barrera. Cada ruta que pasa por aqui tiene un segundo candado
 *     REAL detras: firma HMAC en /stripe/webhook y /tally-webhook, token de
 *     256 bits en /correct y /correction-status, Bearer en /notify. Si este
 *     limitador desapareciera, nadie entraria: solo llegaria mas ruido.
 *  2. Falla-ABIERTO a proposito. Si KV tiene un hipo, el .catch() deja pasar.
 *     Fallar-cerrado seria PEOR: bloquearia webhooks de Stripe legitimos, es
 *     decir, pagos de clientes reales que no se procesarian.
 *  3. Es por IP y no es atomico (lee-luego-escribe), asi que quien rote IPs lo
 *     esquiva y varias peticiones simultaneas pueden contar de menos. Ambas
 *     cosas se ACEPTAN: mitigarlas de verdad exigiria un Durable Object
 *     -- binding nuevo, migracion y redeploy de 5 workers -- para endurecer
 *     algo que no es lo que detiene a nadie.
 *
 * Si algun dia una ruta que GASTA dinero queda sin segundo candado, esta
 * decision se revisa: ahi si haria falta un limitador de verdad.
 */
async function checkRateLimit(env, key, limit, ttl) {
  const current = await env.SERVICE_MENU_KV.get(key, { type: 'json' }).catch(() => null);
  const count = (current?.count || 0) + 1;

  if (count > limit) return false;

  await env.SERVICE_MENU_KV.put(key, JSON.stringify({ count }), { expirationTtl: ttl }).catch(() => {});
  return true;
}

async function sendEmail({ env, to, subject, html, text, attachments }) {
  const sendgridKey = secret(env, 'SENDGRID_API_KEY');
  if (!sendgridKey) {
    throw new Error('SENDGRID_API_KEY not configured');
  }

  const fromRaw = env.FROM_EMAIL || `${brandName(env)} <hello@hmulink.com>`;
  const fromEmail = fromRaw.split('<').pop().replace('>', '').trim();
  // Display name del remitente: sin él, los clientes ven "hello" a secas y
  // los filtros de spam desconfían más.
  const fromName = fromRaw.includes('<') ? fromRaw.split('<')[0].trim() : brandName(env);
  // hello@hmulink.com no es un buzón real: las respuestas del cliente
  // (incluida la solicitud de corrección gratuita) deben llegar a un correo
  // monitoreado, no rebotar.
  const replyTo = (env.REPLY_TO_EMAIL || '').trim();

  const body = {
    personalizations: [
      {
        to: [{ email: to }],
        subject
      }
    ],
    from: fromName ? { email: fromEmail, name: fromName } : { email: fromEmail },
    // Multipart text/plain + text/html mejora deliverability (HTML-only es
    // señal clásica de spam). SendGrid exige text/plain ANTES de text/html.
    content: [
      ...(text ? [{ type: 'text/plain', value: text }] : []),
      { type: 'text/html', value: html }
    ]
  };
  if (replyTo) {
    body.reply_to = { email: replyTo };
  }
  // Adjuntos (hoy: el QR de la entrega, inline vía content_id). Solo se agrega
  // la clave si hay algo: SendGrid rechaza `attachments: []`.
  if (Array.isArray(attachments) && attachments.length) {
    body.attachments = attachments;
  }

  const response = await fetch('https://api.sendgrid.com/v3/mail/send', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${sendgridKey}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    throw new Error(`Email send failed: ${response.status}`);
  }

  // SendGrid responds 202 with an empty body — nothing to parse.
  return { ok: true, status: response.status };
}

// Normalización de payload Tally — copiada del patrón probado de MyGuest.
// Tally envía data.fields[] con {key, label, type, value}; los hidden fields
// prefilled por URL llegan como fields con label "order_id". El objeto answers
// se indexa por key, label, title e id, más versiones normalizadas de cada uno.
function normalizeTallyPayload(payload) {
  const submissionId =
    payload?.id ||
    payload?.submission_id ||
    payload?.submissionId ||
    payload?.responseId ||
    payload?.data?.id ||
    payload?.data?.submission_id ||
    payload?.data?.submissionId ||
    payload?.data?.responseId ||
    null;

  const answers = {};

  copyTopLevelAnswers(answers, payload);

  if (payload?.data?.hiddenFields && typeof payload.data.hiddenFields === 'object' && !Array.isArray(payload.data.hiddenFields)) {
    copyAnswerObject(answers, payload.data.hiddenFields);
  }

  const fields =
    Array.isArray(payload?.data?.fields) ? payload.data.fields :
    Array.isArray(payload?.fields) ? payload.fields :
    [];

  // `prefill` guarda SOLO {field name de Tally -> texto}, que es justo lo que
  // se puede volver a meter en el formulario por URL (Ola 1c). `answers` mezcla
  // labels, ids y claves normalizadas — sirve para leer el intake, pero no para
  // reconstruirlo. Los nombres son estables porque tally_form.yaml los declara
  // (`name:` por pregunta); si una vertical aún no los declara, este mapa sale
  // casi vacío y el flujo cae solo al camino viejo (/correct/ de texto libre).
  const prefill = {};

  for (const field of fields) {
    const value = extractTallyFieldValue(field);
    // Desde el cableado de prefill (2026-07-21) cada campo de texto tiene un
    // HIDDEN FIELD con su MISMO nombre (asi Tally muestra el valor de la URL
    // como default answer). En el submit llegan LOS DOS con ese nombre: el
    // hidden trae lo que decia la URL y el visible lo que el cliente dejo al
    // enviar. El visible SIEMPRE manda — un hidden nunca pisa una respuesta ya
    // escrita (si el cliente edito el campo, honrar el hidden publicaria el
    // valor viejo) y jamas entra al mapa de prefill.
    const isHidden = field?.type === 'HIDDEN_FIELDS';
    const keys = [field?.key, field?.name, field?.label, field?.title, field?.id].filter(Boolean);
    for (const key of keys) {
      for (const k of [String(key), normalizeKey(String(key))]) {
        if (isHidden && k in answers && cleanValue(answers[k]) !== '') continue;
        answers[k] = value;
      }
    }
    const name = typeof field?.name === 'string' ? field.name.trim() : '';
    if (!isHidden && name && typeof value === 'string' && value !== '') {
      prefill[name] = value;
    }
  }

  return {
    submission_id: String(submissionId || ''),
    form_id: String(payload?.data?.formId || payload?.formId || ''),
    answers,
    prefill
  };
}

function copyAnswerObject(target, source) {
  for (const [key, value] of Object.entries(source)) {
    target[key] = value;
    target[normalizeKey(key)] = value;
  }
}

function copyTopLevelAnswers(target, source) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return;
  const reservedKeys = new Set([
    'id', 'submission_id', 'submissionId', 'responseId',
    'submitted_at', 'submittedAt', 'createdAt', 'data', 'answers', 'fields'
  ]);
  for (const [key, value] of Object.entries(source)) {
    if (reservedKeys.has(key)) continue;
    target[key] = value;
    target[normalizeKey(key)] = value;
  }
}

// Tally Multiple Choice manda IDs de opción; aquí se convierten al texto real.
function extractTallyFieldValue(field) {
  if (!field || typeof field !== 'object') return field;

  const rawValue =
    'value' in field ? field.value :
    'answer' in field ? field.answer :
    'answers' in field ? field.answers :
    'text' in field ? field.text :
    null;

  if (Array.isArray(rawValue) && Array.isArray(field.options)) {
    const selectedTexts = rawValue
      .map((selectedId) => {
        const option = field.options.find((opt) => opt.id === selectedId);
        return option ? option.text : selectedId;
      })
      .filter(Boolean);
    if (selectedTexts.length === 1) return selectedTexts[0];
    return selectedTexts;
  }

  if (typeof rawValue === 'string' && Array.isArray(field.options)) {
    const option = field.options.find((opt) => opt.id === rawValue);
    return option ? option.text : rawValue;
  }

  return rawValue;
}

function getAnswer(answers, key) {
  if (!answers || typeof answers !== 'object') return undefined;
  if (key in answers) return answers[key];
  const normalizedKey = normalizeKey(key);
  if (normalizedKey in answers) return answers[normalizedKey];
  return undefined;
}

function answerAny(answers, keys) {
  for (const k of keys) {
    const v = getAnswer(answers, k);
    if (Array.isArray(v)) {
      const joined = v.map((x) => cleanValue(x)).filter(Boolean).join(', ');
      if (joined) return joined;
      continue;
    }
    const cleaned = cleanValue(v);
    if (cleaned) return cleaned;
  }
  return '';
}

// Fallback for a handful of business-type fields (class schedule / tour details /
// pet notes) whose exact Tally wording varies across the ES and EN forms, so an
// exhaustive alias list is fragile. Each entry in tokenSets is a list of tokens
// that must ALL appear in a normalized answer key; the first matching, non-empty
// answer wins. Tokens are chosen to be specific enough to avoid false positives
// (e.g. ['class','schedule'] won't match the plain business-hours question).
function answerContains(answers, tokenSets) {
  if (!answers || typeof answers !== 'object') return '';
  for (const tokens of tokenSets) {
    for (const key of Object.keys(answers)) {
      const nk = normalizeKey(key);
      if (!tokens.every((t) => nk.includes(t))) continue;
      const v = answers[key];
      if (Array.isArray(v)) {
        const joined = v.map((x) => cleanValue(x)).filter(Boolean).join(', ');
        if (joined) return joined;
        continue;
      }
      const cleaned = cleanValue(v);
      if (cleaned) return cleaned;
    }
  }
  return '';
}

// Tally FILE_UPLOAD fields arrive as an array of { url, name, mimeType, ... }.
// Only the first file is used for single-file fields (logo / main photo).
function answerFileUrl(answers, keys) {
  for (const k of keys) {
    const v = getAnswer(answers, k);
    if (Array.isArray(v) && v.length > 0) {
      const first = v[0];
      if (first && typeof first === 'object' && typeof first.url === 'string') return first.url;
      if (typeof first === 'string' && first.trim()) return first.trim();
    }
  }
  return '';
}

function answerFileUrls(answers, keys, limit = 6) {
  for (const k of keys) {
    const v = getAnswer(answers, k);
    if (!Array.isArray(v) || v.length === 0) continue;
    return v
      .map((item) => {
        if (item && typeof item === 'object' && typeof item.url === 'string') return item.url.trim();
        if (typeof item === 'string') return item.trim();
        return '';
      })
      .filter(Boolean)
      .slice(0, limit);
  }
  return [];
}

function slugify(text) {
  return String(text || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/g, '');
}

// El boton que el negocio eligio destacar. La tabla NO vive aqui: vive en
// primary-cta-aliases.json, y la comparten este worker y el generador
// (engine/generator/primary_cta.py). Estuvo copiada en las tres y divergio —
// 3 de las 5 opciones del formulario de PawContact caian a WhatsApp en
// silencio (auditoria 2026-07-26). Mismo guard `Array.isArray` que
// FIELD_ALIASES: el JSON lleva notas `_comment*` que no son listas.
const PRIMARY_CTA_ALIASES = Object.fromEntries(
  Object.entries(RAW_PRIMARY_CTA_ALIASES).flatMap(([kind, aliases]) =>
    Array.isArray(aliases) ? aliases.map((alias) => [alias, kind]) : []
  )
);

function normalizePrimaryCta(value) {
  return PRIMARY_CTA_ALIASES[normalizeKey(value)] || '';
}

// Lo que TODA página necesita, sea del tipo que sea: estilo, idioma, nombre del
// negocio y slug público. Sale de aquí (y no duplicado en cada rama) porque las
// cuatro derivaciones son transformaciones delicadas — el estilo cae a un
// fallback marcado, el idioma tiene una regla de precedencia de tres niveles, y
// el slug es lo que hace que una modificación no publique una página nueva. Una
// segunda copia de esto es una copia que se queda atrás.
function intakeIdentity(normalized, orderId, env) {
  const a = normalized.answers;

  // El separador entre el NOMBRE del estilo y su descripción es la raya (— o –),
  // nunca el guion normal: un nombre de estilo LO LLEVA DENTRO ('sunny-paws',
  // 'warm-sand'), porque el catálogo de cada vertical son slugs. Incluir '-' aquí
  // (como estaba hasta el 2026-07-25) cortaba 'sunny-paws — alegre y jugueton' en
  // 'sunny', que no existe en ninguna vertical: PawContact entero caía a
  // fallbackBrandStyle y sus 4 opciones entregaban la MISMA página warm-sand.
  // Si alguna vertical vuelve a usar guion como separador,
  // `create_tally_forms.py --check-mapping` lo caza antes del deploy.
  const styleRaw = answerAny(a, fieldAliases('pick_your_style'));
  const styleName = styleRaw.split(/[—–]/)[0] || '';
  let brandStyle = slugify(styleName);
  let styleUnmapped = false;
  const validStyles = validBrandStyles(env || {});
  if (!validStyles.includes(brandStyle)) {
    styleUnmapped = true;
    brandStyle = fallbackBrandStyle(validStyles);
  }

  // fieldAliases('default_language') solo trae alias genéricos (sin marca); los
  // dos alias específicos de marca ("...your <BRAND> show first") se derivan de
  // BRAND_NAME en tiempo de ejecución (languageQuestionAliases, Fase 2.3/2.9 —
  // antes 'hmu_link' vivía hardcodeado en el JSON compartido con cada vertical).
  // Regla del idioma (ronda 3 de deuda del worker): respuesta explícita del
  // cliente > idioma del formulario que llenó (form_id vs TALLY_FORM_URL_ES/EN,
  // ver tallyFormLang) > 'en'. Antes el fallback comparaba contra 'MeyDpk' (el
  // form ES de HMU, hardcodeado), así que toda vertical exportada caía a inglés.
  const langAliases = [...fieldAliases('default_language'), ...languageQuestionAliases(env)];
  const langRaw = answerAny(a, langAliases);
  const defaultLanguage = resolveDefaultLanguage(langRaw, env, normalized.form_id);

  const businessName = answerAny(a, fieldAliases('business_name'));
  const suffix = String(normalized.submission_id || orderId || '')
    .toLowerCase().replace(/[^a-z0-9]/g, '').slice(-7) || 'x0';
  const publicSlug = `${slugify(businessName) || 'hmu-page'}-${suffix}`;

  return { brandStyle, styleUnmapped, defaultLanguage, businessName, publicSlug };
}

// Mapea las respuestas del intake (formularios EN/ES de la vertical) al
// payload público que consume el generador. SOLO campos públicos aprobados:
// los datos internos (nombre de contacto, teléfono privado, notas "keep off")
// se quedan en KV y nunca se despachan a GitHub Actions.
//
// Los alias de título por campo (FIELD_ALIASES) viven en tally-field-aliases.json
// — única fuente de verdad, compartida con create_tally_forms.py --check-mapping.
// Aquí solo quedan las transformaciones no-triviales (estilo, idioma, slug, CTA,
// archivos, fallbacks answerContains). `env` aporta el catálogo de estilos válido
// de la vertical (validBrandStyles); sin él, cae a los 12 de HMU.
function buildPublicPayload(normalized, orderId, env) {
  // La plantilla decide QUÉ campos se extraen. `service-menu` es el default y su
  // rama es la de siempre, sin un byte de cambio: los 5 workers desplegados no
  // declaran TEMPLATE, así que caen aquí igual que ayer.
  if (pageTemplate(env) === 'catalog') {
    return buildCatalogPublicPayload(normalized, orderId, env);
  }
  const a = normalized.answers;
  const { brandStyle, styleUnmapped, defaultLanguage, businessName, publicSlug } =
    intakeIdentity(normalized, orderId, env);

  return {
    public_slug: publicSlug,
    default_language: defaultLanguage,
    brand_style: brandStyle,
    style_unmapped: styleUnmapped,
    business_name: businessName,
    business_type: answerAny(a, fieldAliases('business_type')),
    short_description: answerAny(a, fieldAliases('short_description')),
    whatsapp: answerAny(a, fieldAliases('whatsapp')),
    phone: answerAny(a, fieldAliases('phone')),
    public_email: answerAny(a, fieldAliases('public_email')),
    instagram: answerAny(a, fieldAliases('instagram')),
    facebook: answerAny(a, fieldAliases('facebook')),
    tiktok: answerAny(a, fieldAliases('tiktok')),
    website: answerAny(a, fieldAliases('website')),
    other_public_link: answerAny(a, fieldAliases('other_public_link')),
    delivery_pickup_links_text: answerAny(a, fieldAliases('delivery_pickup_links_text')),
    portfolio_link: answerAny(a, fieldAliases('portfolio_link')),
    booking_url: answerAny(a, fieldAliases('booking_url')),
    primary_cta: normalizePrimaryCta(answerAny(a, fieldAliases('primary_cta'))),
    google_maps_url: answerAny(a, fieldAliases('google_maps_url')),
    google_reviews_url: answerAny(a, fieldAliases('google_reviews_url')),
    address: answerAny(a, fieldAliases('address')),
    location_1_notes: answerAny(a, fieldAliases('location_1_notes')),
    service_area_text: answerAny(a, fieldAliases('service_area_text')),
    client_care_text: answerAny(a, fieldAliases('client_care_text')),
    reservations_text: answerAny(a, fieldAliases('reservations_text')),
    class_schedule_text: answerAny(a, fieldAliases('class_schedule_text'))
      || answerContains(a, [['class', 'schedule'], ['class', 'timetable'], ['horario', 'clase']]),
    tour_details_text: answerAny(a, fieldAliases('tour_details_text'))
      || answerContains(a, [['tour'], ['experience', 'detail'], ['experiencia', 'detalle']]),
    pet_notes_text: answerAny(a, fieldAliases('pet_notes_text'))
      || answerContains(a, [['pet'], ['mascota']]),
    opening_hours_text: answerAny(a, fieldAliases('opening_hours_text')),
    service_categories_text: answerAny(a, fieldAliases('service_categories_text')),
    price_display: answerAny(a, fieldAliases('price_display')),
    services_text: answerAny(a, fieldAliases('services_text')),
    faq_text: answerAny(a, fieldAliases('faq_text')),
    featured_text: answerAny(a, fieldAliases('featured_text')),
    policies_text: answerAny(a, fieldAliases('policies_text')),
    logo_url: answerFileUrl(a, fieldAliases('logo_url')),
    image_url: answerFileUrl(a, fieldAliases('image_url')),
    gallery_image_urls: answerFileUrls(a, fieldAliases('gallery_image_urls'), 5),
    location_1_name: answerAny(a, fieldAliases('location_1_name')),
    location_2_name: answerAny(a, fieldAliases('location_2_name')),
    location_2_address: answerAny(a, fieldAliases('location_2_address')),
    location_2_maps_url: answerAny(a, fieldAliases('location_2_maps_url')),
    location_2_notes: answerAny(a, fieldAliases('location_2_notes')),
    location_3_name: answerAny(a, fieldAliases('location_3_name')),
    location_3_address: answerAny(a, fieldAliases('location_3_address')),
    location_3_maps_url: answerAny(a, fieldAliases('location_3_maps_url')),
    location_3_notes: answerAny(a, fieldAliases('location_3_notes')),
    additional_locations_text: answerAny(a, fieldAliases('additional_locations_text')),
    // Lo que ESTA vertical declare de mas (ver OPTIONAL_INTAKE_FIELDS). La
    // tabla es CERRADA y sus nombres no se cruzan con ninguno de arriba, asi
    // que el spread no puede pisar un campo del motor por mucho que una
    // vertical escriba lo que sea en su mapa de alias.
    ...optionalIntakeFields(a)
  };
}

// Rama de la plantilla `catalog` (The Catalog Link, F6 bloque 2).
//
// Un catálogo NO es un menú de servicios con otra hoja de estilos: no tiene
// ocho canales de contacto, ni horarios, ni sucursales, ni FAQ — tiene
// PRODUCTOS y UN botón de venta. Extraer aquí los 47 campos de service-menu
// llenaría el payload de cadenas vacías y, peor, dejaría al generador del
// catálogo sin lo único que le importa. Por eso es una rama y no una suma de
// campos opcionales.
//
// El formulario del claim tiene DOS caminos (decisión de Vero del 07-24) y este
// payload sirve a los dos:
//   - PRELLENADO: el negocio ya vio su vista previa. Solo confirma y elige su
//     botón de venta. Sus ~20 productos con fotos NO viajan por el formulario
//     (no caben en un prefill de Tally, que va por query string): viven en Cory
//     y el workflow los baja por slug — transporte "opción A", decidido el
//     07-25. Por eso este payload lleva `prospect_slug` y no `products`.
//   - ORGÁNICO: llegó por la tienda sin vista previa y captura lo suyo. Ahí sí
//     viajan `products_text` (una línea por producto) y las fotos que subió a
//     Tally, que build_client_from_intake parsea igual que hace hoy con
//     `services_text`.
//
// `prospect_slug` NO se lee del formulario a propósito: se rellena en el
// call-site desde el registro de la orden, donde lo puso el
// `client_reference_id` del checkout de Stripe. Un hidden field de Tally lo
// puede editar cualquiera con la URL, y con eso se reclamaría el catálogo de
// otro negocio.
function buildCatalogPublicPayload(normalized, orderId, env) {
  const a = normalized.answers;
  const { brandStyle, styleUnmapped, defaultLanguage, businessName, publicSlug } =
    intakeIdentity(normalized, orderId, env);

  const saleKind = normalizeSaleKind(answerAny(a, fieldAliases('sale_button_kind')));
  const saleValue = normalizeSaleValue(answerAny(a, fieldAliases('sale_button_value')));

  return {
    template: 'catalog',
    public_slug: publicSlug,
    default_language: defaultLanguage,
    brand_style: brandStyle,
    style_unmapped: styleUnmapped,
    business_name: businessName,
    short_description: answerAny(a, fieldAliases('short_description')),
    address: answerAny(a, fieldAliases('address')),
    // El generador exige sale_button.kind válido y un value que produzca enlace;
    // se manda lo que haya (aunque venga incompleto) para que el gate de intake
    // del call-site pueda decir exactamente qué faltó, en vez de que el
    // generador falle después con un payload que ya nadie puede inspeccionar.
    sale_button: { kind: saleKind, value: saleValue },
    catalog_mode: answerAny(a, fieldAliases('catalog_mode')),
    products_text: answerAny(a, fieldAliases('products_text')),
    // 20 = el tope de productos por catálogo que fijó F2.
    product_image_urls: answerFileUrls(a, fieldAliases('product_image_urls'), 20),
    logo_url: answerFileUrl(a, fieldAliases('logo_url')),
    prospect_slug: '',
    // Lo que ESTA vertical declare de mas (ver OPTIONAL_INTAKE_FIELDS), igual
    // que la rama de service-menu de arriba.
    //
    // POR QUE LA RAMA CERRADA SE ABRE JUSTO AQUI (linkFactory/17, 2026-07-27).
    // La lista de arriba es cerrada porque un catalogo no tiene los 47 campos de
    // un menu de servicios, no porque no pueda crecer. Y una pregunta REAL del
    // formulario vivo del catalogo se estaba cayendo por esta puerta:
    // `photo_rights_confirmed` — el consentimiento de derechos de imagen del
    // camino ORGANICO, el unico donde el negocio sube SUS fotos. Se contesta, y
    // hasta hoy vivia SOLO en el blob crudo del intake, con expirationTtl de 90
    // dias, mientras que la pagina publicada con esas fotos es PERMANENTE. La
    // unica prueba de un consentimiento no puede expirar antes que el uso que
    // ampara (mismo dictamen de pawcontact/6).
    //
    // Se resuelve con el mecanismo que el motor YA tiene en vez de con un campo
    // a mano, y eso NO le agrega ni una clave a nadie: `optionalIntakeFields`
    // solo emite lo que el `tally-field-aliases.json` de la vertical declara, y
    // el mapa GENERICO no declara ninguno de los campos de esa tabla (hay
    // prueba: campos-de-vertical.test.mjs). Quien gana el campo es `catalog`,
    // que desde hoy lleva mapa propio con esa sola entrada de mas.
    //
    // Que llegue al payload es la MITAD del camino: sin `intake.fields` en el
    // vertical.yaml, `build_client_from_intake` lo tira aqui mismo y el
    // consentimiento no queda en el client.json, que es lo unico permanente.
    // Son SIEMPRE dos declaraciones, no una (leccion de linkFactory/16).
    ...optionalIntakeFields(a)
  };
}

// ¿Este intake trae lo mínimo para publicar? Cambia por plantilla: en
// service-menu el mínimo es UN canal de contacto (la página es una tarjeta de
// contacto); en el catálogo es el botón de venta (sin él, las tarjetas de
// producto no llevan a ningún lado) MÁS una fuente de productos — la vista
// previa de Cory (prospect_slug) o lo que el cliente escribió.
//
// Devuelve '' si está completo, o el nombre de lo que falta (el mismo string
// que ya viajaba en el 422 `incomplete_intake`).
function missingFromIntake(payload, env) {
  if (pageTemplate(env) !== 'catalog') {
    const hasContact = payload.whatsapp || payload.phone || payload.public_email
      || payload.booking_url || payload.website;
    return hasContact ? '' : 'public_contact';
  }
  const sale = payload.sale_button || {};
  if (!SALE_BUTTON_KINDS.includes(sale.kind) || !sale.value) return 'sale_button';
  if (!payload.prospect_slug && !payload.products_text) return 'products';
  return '';
}

function cleanValue(val) {
  if (!val) return '';
  return String(val).trim();
}

function generateSecureToken() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  let token = '';
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  for (let i = 0; i < array.length; i++) {
    token += chars[array[i] % chars.length];
  }
  return token;
}

function timingSafeEqual(a, b) {
  if (!a || !b) return false;
  if (a.length !== b.length) return false;

  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

function safeError(error) {
  if (!error) return '(unknown error)';
  return error.message || String(error);
}

function corsHeaders() {
  // This API is server-to-server only (Stripe, Tally, GitHub Actions) — no
  // browser calls it, so no wildcard is needed. Scope to the site origin
  // instead of '*' to shrink the surface. El origin sale de la config de la
  // vertical (corsOrigin lee PUBLIC_BOOK_BASE_URL); `env` viene del caché de
  // módulo (_workerEnv), así los call-sites de jsonResponse no cambian.
  return {
    'Access-Control-Allow-Origin': corsOrigin(_workerEnv || {}),
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      ...corsHeaders()
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// EMAIL TEMPLATES
// ─────────────────────────────────────────────────────────────────────────────

// El cuerpo va en el idioma del comprador (lang derivado de la moneda). Un asunto
// en inglés con cuerpo en español (o viceversa) confunde y dispara filtros de spam
// por incongruencia de idioma — el asunto ya se localiza, así que el cuerpo también.
// El correo enlaza AMBOS formularios; el del idioma del comprador va primero.
// Qué versiones del correo post-pago se mandan: una sola cuando la moneda
// identifica el idioma (MXN/USD), las dos cuando no (emailLangFromCurrency
// devuelve null). Inglés primero en el caso bilingüe: si la moneda no es ni MXN
// ni USD, el comprador es de fuera y el inglés es la apuesta más segura.
function postPaymentLangs(lang) {
  return lang === 'es' || lang === 'en' ? [lang] : ['en', 'es'];
}

function postPaymentCopy(lang, { btnES, btnEN, env }) {
  const es = lang !== 'en';
  const t = es
    ? {
        heading: '¡Tu página de servicios está casi lista!',
        greeting: 'Hola,',
        intro: `Gracias por tu compra en ${brandName(env)}. Solo falta un paso: completa tu formulario de información para que generemos tu página.`,
        cta: '📋 Completa tu información:',
        or: 'o',
        firstBtn: btnES,
        secondBtn: btnEN,
        closing: 'Una vez que completes el formulario, generaremos tu página y recibirás un link para compartir con tus clientes.'
      }
    : {
        heading: 'Your service page is almost ready!',
        greeting: 'Hi,',
        intro: `Thanks for your purchase at ${brandName(env)}. Just one step left: fill out your info form so we can generate your page.`,
        cta: '📋 Complete your information:',
        or: 'or',
        firstBtn: btnEN,
        secondBtn: btnES,
        closing: "Once you complete the form, we'll generate your page and you'll get a link to share with your customers."
      };
  return t;
}

function buildPostPaymentEmail({ formUrlEN, formUrlES, lang, env }) {
  const btnES = `<a href="${formUrlES}" style="display: inline-block; background: #f478b0; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 500;">Abrir Formulario (Español)</a>`;
  const btnEN = `<a href="${formUrlEN}" style="display: inline-block; background: #00a0b5; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 500;">Open Form (English)</a>`;
  const sections = postPaymentLangs(lang).map((l) => {
    const t = postPaymentCopy(l, { btnES, btnEN, env });
    return `
    <h2 style="margin-top: 0; color: #1a1a1a;">${t.heading}</h2>
    <p>${t.greeting}</p>
    <p>${t.intro}</p>

    <div style="margin: 30px 0;">
      <p style="color: #666; font-size: 14px; margin-bottom: 10px;">${t.cta}</p>
      ${t.firstBtn}
      <p style="margin: 15px 0; color: #999;">${t.or}</p>
      ${t.secondBtn}
    </div>

    <p style="color: #666; font-size: 14px;">${t.closing}</p>`;
  });
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.6; color: #333; background: #f5f5f5; margin: 0; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
${sections.join('\n    <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">\n')}

    <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
    <p style="color: #999; font-size: 12px; margin: 0;">${emailFooterHtml(env)}</p>
  </div>
</body>
</html>
  `;
}

// Versión text/plain del correo post-pago (multipart mejora deliverability).
function buildPostPaymentText({ formUrlEN, formUrlES, lang, env }) {
  const lineES = `Completa tu formulario (Español): ${formUrlES}`;
  const lineEN = `Open your form (English): ${formUrlEN}`;
  const block = (l) => (l === 'es'
    ? [
        '¡Tu página de servicios está casi lista!',
        '',
        `Gracias por tu compra en ${brandName(env)}. Solo falta un paso: completa tu formulario de información para que generemos tu página.`,
        '',
        lineES,
        lineEN,
        '',
        'Una vez que completes el formulario, generaremos tu página y recibirás un link para compartir con tus clientes.'
      ]
    : [
        'Your service page is almost ready!',
        '',
        `Thanks for your purchase at ${brandName(env)}. Just one step left: fill out your info form so we can generate your page.`,
        '',
        lineEN,
        lineES,
        '',
        "Once you complete the form, we'll generate your page and you'll get a link to share with your customers."
      ]);
  const body = postPaymentLangs(lang).map((l) => block(l).join('\n')).join('\n\n---\n\n');
  return [body, '', emailFooterText(env)].join('\n');
}

// content_id del QR que se adjunta inline en el correo de entrega: el HTML lo
// referencia como `cid:<esto>` y SendGrid resuelve el adjunto.
const DELIVERY_QR_CID = 'delivery-qr';

/**
 * Cómo se le describe al cliente su modificación incluida. Depende del MISMO
 * interruptor que decide a dónde apunta el enlace (modificationFormPrefillEnabled):
 *
 *   - encendido  → el enlace abre SU formulario de Tally ya prellenado, así que
 *                  el correo puede prometer "verás tu información actual".
 *   - apagado    → el enlace abre la página /correct/, donde el cliente escribe
 *                  en texto libre qué quiere cambiar.
 *
 * Antes el texto prometía SIEMPRE el formulario prellenado. Con el interruptor
 * apagado —que es el caso de toda vertical que no haya cableado sus dos
 * formularios de Tally— eso le promete a cada cliente nuevo algo que no recibe.
 * Es la regresión R1 del gate de HMU (2026-07-26): sus dos formularios vivos no
 * traen los hidden fields client_slug/correction_token ni un `name` estable por
 * pregunta, así que el prefill no puede funcionar ahí; lo que se corrige es el
 * copy, no el interruptor.
 */
function modificationCopy(env, es) {
  const prefill = modificationFormPrefillEnabled(env);
  if (es) {
    return prefill
      ? {
          open: 'verás tu información actual y editas solo lo que quieras cambiar',
          openHere: 'Ábrela aquí y edita solo lo que quieras cambiar:'
        }
      : {
          open: 'nos escribes ahí qué quieres cambiar y nosotros lo aplicamos',
          openHere: 'Ábrela aquí y escríbenos qué quieres cambiar:'
        };
  }
  return prefill
    ? {
        open: 'you will see your current information and edit only what you want to change',
        openHere: 'Open it here and edit only what you want to change:'
      }
    : {
        open: 'you tell us there what you want changed and we apply it',
        openHere: 'Open it here and tell us what you want changed:'
      };
}

// La corrección gratuita se pide con el botón (flujo automatizado /correct/)
// o respondiendo al correo (reply_to va al buzón real) — ambas vías valen.
function buildDeliveryEmail({ pageUrl, slug, lang, correctionUrl, hasQr, hasLinterFlags, env }) {
  const es = lang !== 'en';
  // Aviso del linter de copy (`legal.copy_linter`). Solo aparece si la vertical
  // tiene lista Y el intake disparó alguna frase, así que el correo de una
  // vertical sin lista no cambia ni un byte. No dice CUÁL frase a propósito: el
  // correo va al cliente, y el objetivo es que revise su texto, no señalarlo.
  const linterBlock = hasLinterFlags
    ? `
    <div style="margin: 26px 0; padding: 18px 20px; background: #fdeaea; border-left: 4px solid #c0392b; border-radius: 6px;">
      <p style="margin: 0 0 8px 0; color: #211a17; font-weight: 700;">${es ? 'Revisa el texto de tu página' : 'Please review your page copy'}</p>
      <p style="margin: 0; color: #6b5f58; font-size: 14px;">${es
        ? 'Detectamos frases en tu información que podrían aludir a marcas de terceros (por ejemplo, «réplica de» o «inspirado en [marca]»). Tu página ya está publicada; te recomendamos revisarla y, si hace falta, usar tu modificación incluida.'
        : 'We flagged some phrasing in your submission that may reference third-party brands (for example, "replica of" or "inspired by [brand]"). Your page is already published; please review it and, if needed, use your included modification.'}</p>
    </div>`
    : '';
  // Cuántas modificaciones gratis incluye la compra (FREE_CHANGES <-
  // legal.free_changes): el correo dice el MISMO número que los Términos.
  const free = freeChanges(env);
  // El MISMO QR, publicado junto a la página del cliente. El correo lo adjunta
  // inline (`cid:`) porque es lo que se ve sin hacer nada; este enlace es el
  // camino que queda cuando la imagen NO se ve, que es la mitad que faltaba:
  // un cliente de correo con las imágenes apagadas —el default de muchos—
  // pinta un recuadro GRIS donde va el QR, y hasta hoy ese recuadro no decía
  // qué era (alt="QR") ni llevaba a ninguna parte. Verificado 2026-07-27 por
  // HTTP: `<página>/qr.png` responde 200 image/png en los clientes reales de
  // PawContact y ModaLink.
  const qrUrl = `${String(pageUrl || '').replace(/\/+$/, '')}/qr.png`;
  const qrCopy = es
    ? {
        title: '📲 Tu código QR',
        alt: 'Código QR de tu página',
        body: 'Imprímelo o pégalo donde tus clientes puedan escanearlo. También va adjunto como imagen PNG en este correo.',
        link: 'Descargar el QR (PNG)'
      }
    : {
        title: '📲 Your QR code',
        alt: 'QR code for your page',
        body: 'Print it or put it anywhere your customers can scan it. It is also attached to this email as a PNG image.',
        link: 'Download the QR (PNG)'
      };
  const qrBlock = hasQr
    ? `
    <div style="margin: 30px 0; text-align: center;">
      <p style="margin: 0 0 12px 0; color: #333; font-weight: 500;">${qrCopy.title}</p>
      <a href="${qrUrl}" style="text-decoration: none;"><img src="cid:${DELIVERY_QR_CID}" alt="${qrCopy.alt}" width="180" height="180" style="display: inline-block; border: 1px solid #eee; border-radius: 8px;"></a>
      <p style="margin: 12px 0 0 0; color: #888; font-size: 12px;">${qrCopy.body}</p>
      <p style="margin: 6px 0 0 0; font-size: 12px;"><a href="${qrUrl}" style="color: #00a0b5;">${qrCopy.link}</a></p>
    </div>`
    : '';
  const t = es
    ? {
        heading: '¡Tu página de servicios está lista! 🎉',
        greeting: 'Hola,',
        intro: `Tu página de servicios en ${brandName(env)} ha sido generada exitosamente. Aquí está tu link público:`,
        label: 'Tu página pública:',
        note: 'Nota: tu página puede tardar unos minutos en estar activa mientras se publica.',
        stepsTitle: 'Próximos pasos:',
        steps: [
          'Abre tu página y verifica que todo se vea correcto',
          'Descarga el código QR desde tu página para compartir fácilmente',
          'Comparte el link en tu bio de Instagram, WhatsApp, tarjetas de visita, etc.'
        ],
        correctionTitle: free === 1
          ? '✏️ Incluido: 1 modificación gratis'
          : `✏️ Incluido: ${free} modificaciones gratis`,
        correctionBody: `Si necesitas cambiar tu información (horarios, servicios, precios, etc.), tu compra incluye ${free === 1 ? 'una modificación' : `${free} modificaciones`} sin costo. Ábrela con este botón: ${modificationCopy(env, true).open}.`,
        correctionBtn: 'Usar mi modificación incluida',
        correctionAlt: 'También puedes simplemente responder a este correo con los cambios que quieras. Para cambiar fotos o logo, responde a este correo adjuntando los archivos.'
      }
    : {
        heading: 'Your service page is ready! 🎉',
        greeting: 'Hi,',
        intro: `Your ${brandName(env)} service page has been generated successfully. Here is your public link:`,
        label: 'Your public page:',
        note: 'Note: your page may take a few minutes to go live while it publishes.',
        stepsTitle: 'Next steps:',
        steps: [
          'Open your page and check that everything looks right',
          'Download the QR code from your page to share it easily',
          'Share the link in your Instagram bio, WhatsApp, business cards, etc.'
        ],
        correctionTitle: free === 1
          ? '✏️ Included: 1 free modification'
          : `✏️ Included: ${free} free modifications`,
        correctionBody: `If you need to change your info (hours, services, prices, etc.), your purchase includes ${free === 1 ? 'one free modification' : `${free} free modifications`}. Open it with this button: ${modificationCopy(env, false).open}.`,
        correctionBtn: 'Use my included modification',
        correctionAlt: 'You can also simply reply to this email with the changes you want. To change photos or your logo, reply to this email with the files attached.'
      };
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.6; color: #333; background: #f5f5f5; margin: 0; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
    <h2 style="margin-top: 0; color: #1a1a1a;">${t.heading}</h2>
    <p>${t.greeting}</p>
    <p>${t.intro}</p>

    <div style="margin: 30px 0; padding: 20px; background: #f0f8ff; border-left: 4px solid #00a0b5; border-radius: 4px;">
      <p style="margin: 0 0 10px 0; color: #666; font-size: 12px;">${t.label}</p>
      <a href="${pageUrl}" style="display: inline-block; color: #00a0b5; font-size: 18px; font-weight: 500; text-decoration: none; word-break: break-all;">${pageUrl}</a>
    </div>

    <p style="color: #999; font-size: 13px;">${t.note}</p>
${linterBlock}${qrBlock}
    <h3 style="color: #1a1a1a; margin-top: 30px;">${t.stepsTitle}</h3>
    <ul style="color: #666;">
      <li>${t.steps[0]}</li>
      <li>${t.steps[1]}</li>
      <li>${t.steps[2]}</li>
    </ul>

    <div style="margin: 30px 0; padding: 20px; background: #fff9e6; border-left: 4px solid #ffa934; border-radius: 4px;">
      <p style="margin: 0 0 15px 0; color: #333; font-weight: 500;">${t.correctionTitle}</p>
      <p style="margin: 0 0 15px 0; color: #666; font-size: 14px;">${t.correctionBody}</p>
      <a href="${correctionUrl}" style="display: inline-block; background: #ffa934; color: #4a2c00; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600;">${t.correctionBtn}</a>
      <p style="margin: 15px 0 0 0; color: #888; font-size: 12px;">${t.correctionAlt}</p>
    </div>

    <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
    <p style="color: #999; font-size: 12px; margin: 0;">${emailFooterHtml(env)}</p>
  </div>
</body>
</html>
  `;
}

// Versión text/plain del correo de entrega.
function buildDeliveryText({ pageUrl, lang, correctionUrl, hasQr, env }) {
  const es = lang !== 'en';
  const free = freeChanges(env);
  // El mismo enlace de respaldo que el HTML (ver `qrUrl` en buildDeliveryEmail):
  // esta versión la lee quien tiene el correo en texto plano, y sin el enlace
  // "va adjunto" era todo lo que tenía.
  const qrUrl = `${String(pageUrl || '').replace(/\/+$/, '')}/qr.png`;
  if (es) {
    return [
      '¡Tu página de servicios está lista!',
      '',
      `Tu página pública: ${pageUrl}`,
      '',
      'Nota: tu página puede tardar unos minutos en estar activa mientras se publica.',
      '',
      'Próximos pasos:',
      '- Abre tu página y verifica que todo se vea correcto',
      hasQr
      ? `- Tu código QR va adjunto en este correo (PNG), listo para imprimir. También lo puedes bajar aquí: ${qrUrl}`
      : '- Descarga el código QR desde tu página para compartir fácilmente',
      '- Comparte el link en tu bio de Instagram, WhatsApp, tarjetas de visita, etc.',
      '',
      `Incluido: ${free === 1 ? 'una modificación' : `${free} modificaciones`} sin costo. ${modificationCopy(env, true).openHere}`,
      correctionUrl,
      'También puedes responder a este correo con los cambios (fotos o logo: adjúntalos en tu respuesta).',
      '',
      emailFooterText(env)
    ].join('\n');
  }
  return [
    'Your service page is ready!',
    '',
    `Your public page: ${pageUrl}`,
    '',
    'Note: your page may take a few minutes to go live while it publishes.',
    '',
    'Next steps:',
    '- Open your page and check that everything looks right',
    hasQr
      ? `- Your QR code is attached to this email (PNG), ready to print. You can also download it here: ${qrUrl}`
      : '- Download the QR code from your page to share it easily',
    '- Share the link in your Instagram bio, WhatsApp, business cards, etc.',
    '',
    `Included: ${free === 1 ? 'one free modification' : `${free} free modifications`}. ${modificationCopy(env, false).openHere}`,
    correctionUrl,
    'You can also reply to this email with the changes (photos or logo: attach the files in your reply).',
    '',
    emailFooterText(env)
  ].join('\n');
}

// ─── Correos del flujo de correcciones ───────────────────────────────────────

// Cascarón compartido de los correos de corrección (mismo look del resto).
function correctionEmailShell(innerHtml, env) {
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.6; color: #333; background: #f5f5f5; margin: 0; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
    ${innerHtml}
    <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
    <p style="color: #999; font-size: 12px; margin: 0;">${emailFooterHtml(env)}</p>
  </div>
</body>
</html>
  `;
}

// Corrección aplicada: confirma y ofrece la corrección adicional de pago
// (precio de la sección 3 de los Términos).
function buildCorrectionAppliedEmail({ pageUrl, lang, buyUrl, nextCorrectionUrl, env }) {
  const es = lang !== 'en';
  const priceLabel = es ? CORRECTION_PRICE.mxn.label : CORRECTION_PRICE.usd.label;
  const t = es
    ? {
        heading: '✅ Tu página fue actualizada',
        intro: 'Aplicamos los cambios que pediste. Revisa tu página (puede tardar unos minutos en reflejarse):',
        buyTitle: '¿Necesitas otro cambio?',
        buyBody: `Puedes comprar una corrección adicional por ${priceLabel}:`,
        buyBtn: 'Comprar corrección adicional',
        alt: 'Si algo no quedó como esperabas, responde a este correo y lo revisamos sin costo.',
        nextTitle: '✏️ Todavía te queda una modificación incluida',
        nextBody: `Cuando la necesites, ábrela con este enlace: ${modificationCopy(env, true).open}.`,
        nextBtn: 'Usar mi modificación incluida'
      }
    : {
        heading: '✅ Your page was updated',
        intro: 'We applied the changes you requested. Check your page (it may take a few minutes to refresh):',
        buyTitle: 'Need another change?',
        buyBody: `You can buy an extra correction for ${priceLabel}:`,
        buyBtn: 'Buy an extra correction',
        alt: 'If something didn\'t come out as expected, reply to this email and we\'ll review it at no cost.',
        nextTitle: '✏️ You still have one included modification',
        nextBody: `Whenever you need it, open this link: ${modificationCopy(env, false).open}.`,
        nextBtn: 'Use my included modification'
      };
  // Si al cliente le quedan modificaciones incluidas se le ofrece ESA, no la de
  // pago: cobrarle teniendo una gratis disponible sería, como mínimo, mala fe.
  const nextBlock = nextCorrectionUrl
    ? `
    <div style="margin: 30px 0; padding: 20px; background: #fff9e6; border-left: 4px solid #ffa934; border-radius: 4px;">
      <p style="margin: 0 0 10px 0; color: #333; font-weight: 500;">${t.nextTitle}</p>
      <p style="margin: 0 0 15px 0; color: #666; font-size: 14px;">${t.nextBody}</p>
      <a href="${nextCorrectionUrl}" style="display: inline-block; background: #ffa934; color: #4a2c00; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600;">${t.nextBtn}</a>
    </div>`
    : '';
  const buyBlock = (!nextCorrectionUrl && buyUrl)
    ? `
    <div style="margin: 30px 0; padding: 20px; background: #f0f8ff; border-left: 4px solid #00a0b5; border-radius: 4px;">
      <p style="margin: 0 0 10px 0; color: #333; font-weight: 500;">${t.buyTitle}</p>
      <p style="margin: 0 0 15px 0; color: #666; font-size: 14px;">${t.buyBody}</p>
      <a href="${buyUrl}" style="display: inline-block; background: #00a0b5; color: #fff; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600;">${t.buyBtn}</a>
    </div>`
    : '';
  return correctionEmailShell(`
    <h2 style="margin-top: 0; color: #1a1a1a;">${t.heading}</h2>
    <p>${t.intro}</p>
    <p><a href="${pageUrl}" style="color: #00a0b5; font-size: 18px; font-weight: 500; word-break: break-all;">${pageUrl}</a></p>
    ${nextBlock}${buyBlock}
    <p style="color: #666; font-size: 14px;">${t.alt}</p>
  `, env);
}

function buildCorrectionAppliedText({ pageUrl, lang, buyUrl, nextCorrectionUrl, env }) {
  const es = lang !== 'en';
  const priceLabel = es ? CORRECTION_PRICE.mxn.label : CORRECTION_PRICE.usd.label;
  const lines = es
    ? [
        'Tu página fue actualizada.',
        '',
        `Revisa tu página (puede tardar unos minutos en reflejarse): ${pageUrl}`,
        '',
        ...(nextCorrectionUrl
          ? [`Todavía te queda una modificación incluida. ${modificationCopy(env, true).openHere} ${nextCorrectionUrl}`, '']
          : buyUrl ? [`¿Necesitas otro cambio? Compra una corrección adicional por ${priceLabel}: ${buyUrl}`, ''] : []),
        'Si algo no quedó como esperabas, responde a este correo y lo revisamos sin costo.',
        '',
        emailFooterText(env)
      ]
    : [
        'Your page was updated.',
        '',
        `Check your page (it may take a few minutes to refresh): ${pageUrl}`,
        '',
        ...(nextCorrectionUrl
          ? [`You still have one included modification. ${modificationCopy(env, false).openHere} ${nextCorrectionUrl}`, '']
          : buyUrl ? [`Need another change? Buy an extra correction for ${priceLabel}: ${buyUrl}`, ''] : []),
        'If something didn\'t come out as expected, reply to this email and we\'ll review it at no cost.',
        '',
        emailFooterText(env)
      ];
  return lines.join('\n');
}

// La corrección no se pudo aplicar automáticamente: promesa honesta de 1 día
// hábil, sin pedirle nada más al cliente (Verónica ya recibió el detalle).
function buildCorrectionManualEmail({ pageUrl, lang, env }) {
  const es = lang !== 'en';
  const t = es
    ? {
        heading: 'Recibimos tu corrección ✏️',
        body: 'Estamos aplicando tus cambios personalmente para que queden perfectos. Tu página quedará actualizada dentro de 1 día hábil — no necesitas hacer nada más.',
        label: 'Tu página:'
      }
    : {
        heading: 'We received your correction ✏️',
        body: 'We\'re applying your changes personally to make sure they come out right. Your page will be updated within 1 business day — nothing else is needed from you.',
        label: 'Your page:'
      };
  return correctionEmailShell(`
    <h2 style="margin-top: 0; color: #1a1a1a;">${t.heading}</h2>
    <p>${t.body}</p>
    <p style="color: #666; font-size: 12px; margin-bottom: 4px;">${t.label}</p>
    <p style="margin-top: 0;"><a href="${pageUrl}" style="color: #00a0b5; word-break: break-all;">${pageUrl}</a></p>
  `, env);
}

function buildCorrectionManualText({ pageUrl, lang, env }) {
  const es = lang !== 'en';
  return (es
    ? [
        'Recibimos tu corrección.',
        '',
        'Estamos aplicando tus cambios personalmente. Tu página quedará actualizada dentro de 1 día hábil — no necesitas hacer nada más.',
        '',
        `Tu página: ${pageUrl}`,
        '',
        emailFooterText(env)
      ]
    : [
        'We received your correction.',
        '',
        'We\'re applying your changes personally. Your page will be updated within 1 business day — nothing else is needed from you.',
        '',
        `Your page: ${pageUrl}`,
        '',
        emailFooterText(env)
      ]).join('\n');
}

// Compra de corrección adicional: entrega el enlace con el token nuevo.
function buildCorrectionPurchaseEmail({ formUrl, pageUrl, lang, env }) {
  const es = lang !== 'en';
  const t = es
    ? {
        heading: '¡Gracias por tu compra! ✏️',
        intro: 'Ya puedes pedir tu corrección adicional. Usa este botón y descríbenos los cambios que quieres en tu página:',
        btn: 'Pedir mi corrección',
        note: 'El enlace es de un solo uso y vence en 30 días. Para cambiar fotos o logo, responde a este correo adjuntando los archivos.',
        label: 'Tu página:'
      }
    : {
        heading: 'Thanks for your purchase! ✏️',
        intro: 'You can now request your extra correction. Use this button and describe the changes you want on your page:',
        btn: 'Request my correction',
        note: 'The link is single-use and expires in 30 days. To change photos or your logo, reply to this email with the files attached.',
        label: 'Your page:'
      };
  return correctionEmailShell(`
    <h2 style="margin-top: 0; color: #1a1a1a;">${t.heading}</h2>
    <p>${t.intro}</p>
    <p style="margin: 25px 0;"><a href="${formUrl}" style="display: inline-block; background: #ffa934; color: #4a2c00; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600;">${t.btn}</a></p>
    <p style="color: #888; font-size: 13px;">${t.note}</p>
    <p style="color: #666; font-size: 12px; margin-bottom: 4px;">${t.label}</p>
    <p style="margin-top: 0;"><a href="${pageUrl}" style="color: #00a0b5; word-break: break-all;">${pageUrl}</a></p>
  `, env);
}

function buildCorrectionPurchaseText({ formUrl, pageUrl, lang, env }) {
  const es = lang !== 'en';
  return (es
    ? [
        '¡Gracias por tu compra!',
        '',
        'Ya puedes pedir tu corrección adicional. Abre este enlace y descríbenos los cambios que quieres:',
        formUrl,
        '',
        'El enlace es de un solo uso y vence en 30 días. Para cambiar fotos o logo, responde a este correo adjuntando los archivos.',
        '',
        `Tu página: ${pageUrl}`,
        '',
        emailFooterText(env)
      ]
    : [
        'Thanks for your purchase!',
        '',
        'You can now request your extra correction. Open this link and describe the changes you want:',
        formUrl,
        '',
        'The link is single-use and expires in 30 days. To change photos or your logo, reply to this email with the files attached.',
        '',
        `Your page: ${pageUrl}`,
        '',
        emailFooterText(env)
      ]).join('\n');
}
