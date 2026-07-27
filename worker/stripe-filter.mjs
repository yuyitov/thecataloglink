/**
 * Filtro de producto de Stripe — fail-closed.
 *
 * La cuenta de Stripe se comparte con otros productos (MyGuest, Dr Link, …) y
 * Stripe abanica CADA evento firmado a este endpoint. Este módulo decide, de
 * forma pura (sin KV ni red), qué hacer con un webhook ya verificado:
 *
 *   - procesarlo como pedido de la vertical,
 *   - enrutarlo al flujo de compra de corrección, o
 *   - ignorarlo (con un motivo concreto para logs/diagnóstico).
 *
 * Regla de la casa: **FAIL CLOSED**. Si el allowlist de payment links no está
 * configurado o viene mal escrito (deploy a medias, rotación de keys), NO se
 * procesa ningún pago — procesar un pago ajeno mandaría el formulario de intake
 * al cliente equivocado. Ante la duda, se ignora.
 *
 * El caller (worker.js) YA verificó la firma de Stripe y parseó el JSON; aquí
 * solo se clasifica. Mantener este módulo puro es lo que permite los tests
 * `node --test` sin montar un Worker ni un KV.
 */

import { correctionMetadataKey } from './product-config.mjs';

// Solo estos dos tipos de evento pueden corresponder a una venta. Únicamente
// `checkout.session.completed` acarrea `payment_link` (atribuible a un
// producto); `payment_intent.succeeded` no se puede atribuir, así que con el
// filtro configurado se ignora.
export const SUPPORTED_EVENT_TYPES = Object.freeze([
  'checkout.session.completed',
  'payment_intent.succeeded',
]);

/**
 * Parsea el env var del allowlist de payment links.
 *
 * Acepta uno o más ids separados por coma (p. ej. el link USD $39 y el link MXN
 * $699 de la misma vertical), recortando espacios y descartando vacíos. Un valor
 * ausente, vacío o compuesto solo de comas/espacios devuelve `[]` — y `[]`
 * dispara el fail-closed aguas arriba.
 *
 * @param {string|undefined|null} rawValue  env.STRIPE_PAYMENT_LINK_ID
 * @returns {string[]}
 */
export function parsePaymentLinkAllowlist(rawValue) {
  return (rawValue || '')
    .split(',')
    .map((id) => id.trim())
    .filter(Boolean);
}

/**
 * Clasifica un evento de Stripe ya verificado.
 *
 * @param {object} event  El evento parseado de Stripe (firma ya validada).
 * @param {object} env    El entorno del Worker (se lee STRIPE_PAYMENT_LINK_ID).
 * @returns {{action: 'process'|'correction'|'ignore', reason?: string, type?: string,
 *            session?: object, observedPaymentLink?: string, expectedPaymentLinks?: string[]}}
 *
 * Motivos de `ignore`:
 *   - `unsupported_type`         El tipo de evento no es una venta.
 *   - `filter_not_configured`    Allowlist vacío → FAIL CLOSED, no se procesa nada.
 *   - `unattributable_event_type` Evento sin `payment_link` (no atribuible) con el filtro activo.
 *   - `other_product`            El `payment_link` no está en el allowlist (otro producto).
 */
export function classifyStripeEvent(event, env) {
  const type = event?.type;

  if (!SUPPORTED_EVENT_TYPES.includes(type)) {
    return { action: 'ignore', reason: 'unsupported_type', type };
  }

  const session = event?.data?.object || {};

  // Compras de corrección adicional: son Checkout Sessions creadas por este
  // mismo worker en /buy-correction (no payment links), marcadas con metadata.
  // Se atienden ANTES del filtro de payment_link — no traen payment_link.
  // La key es por vertical (correctionMetadataKey: <PRODUCT_ID>_correction,
  // default hmu_correction) — la cuenta de Stripe se comparte, así que un
  // literal fijo haría que dos verticales se procesaran las correcciones
  // mutuamente (bug real HMU↔PawContact, ronda 4 de deuda del worker).
  if (type === 'checkout.session.completed' && session?.metadata?.[correctionMetadataKey(env)] === '1') {
    return { action: 'correction', type, session };
  }

  const expectedPaymentLinks = parsePaymentLinkAllowlist(env?.STRIPE_PAYMENT_LINK_ID);
  const observedPaymentLink = session.payment_link || '';

  // FAIL CLOSED: sin allowlist (o mal escrito) no se procesa ningún pago. Se
  // devuelve el payment_link observado para que el caller lo pueda loguear y
  // capturar uno nuevo, pero nunca se crea un pedido ni se manda correo sin match.
  if (expectedPaymentLinks.length === 0) {
    return { action: 'ignore', reason: 'filter_not_configured', type, observedPaymentLink };
  }

  if (type !== 'checkout.session.completed') {
    return { action: 'ignore', reason: 'unattributable_event_type', type };
  }

  if (!expectedPaymentLinks.includes(observedPaymentLink)) {
    return {
      action: 'ignore',
      reason: 'other_product',
      type,
      observedPaymentLink,
      expectedPaymentLinks,
    };
  }

  return { action: 'process', type, session, observedPaymentLink };
}
