import { ApiError, request } from './api';

export const MANDATE_LIST_LIMIT = 100;
export const MANDATE_LIST_LIMITATION =
  'The backend returns at most the 100 most recently created mandates and does not provide pagination.';

const CREATE_FIELDS = [
  'max_total_budget',
  'currency',
  'allowed_merchants',
  'allowed_categories',
  'max_item_count',
  'max_quantity_per_item',
  'max_price_per_item',
  'allow_subscriptions',
  'require_user_approval',
  'expires_at',
];

const STATUSES = new Set(['DRAFT', 'CONFIRMED', 'SIGNED', 'ACTIVE', 'EXPIRED', 'REVOKED']);
const CURRENCIES = new Set(['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY']);

function isDate(value) {
  return typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value));
}

function decimalParts(value) {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  if (typeof value === 'number' && !Number.isFinite(value)) return null;
  const text = String(value);
  if (text.length > 128) return null;
  const match = /^\+?(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text);
  if (!match || (!match[1] && !match[2])) return null;
  const fraction = match[2] || '';
  const exponent = BigInt(match[3] || '0');
  const digits = `${match[1] || '0'}${fraction}`;
  const coefficient = BigInt(digits);
  const scale = BigInt(fraction.length) - exponent;
  if (coefficient <= 0n || scale > 2n) return null;
  return { coefficient, scale };
}

function isDecimal(value, { nullable = false } = {}) {
  if (nullable && value === null) return true;
  return decimalParts(value) !== null;
}

function decimalGreaterThan(left, right) {
  const leftParts = decimalParts(left);
  const rightParts = decimalParts(right);
  if (!leftParts || !rightParts) return false;
  const leftDigits = leftParts.coefficient.toString();
  const rightDigits = rightParts.coefficient.toString();
  const leftOrder = BigInt(leftDigits.length) - leftParts.scale;
  const rightOrder = BigInt(rightDigits.length) - rightParts.scale;
  if (leftOrder !== rightOrder) return leftOrder > rightOrder;
  const width = Math.max(leftDigits.length, rightDigits.length);
  const normalizedLeft = BigInt(leftDigits.padEnd(width, '0'));
  const normalizedRight = BigInt(rightDigits.padEnd(width, '0'));
  return normalizedLeft > normalizedRight;
}

// FastAPI returns the full Mandate response model (including defaults and nullable fields).
function isCompleteMandate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const strings = ['mandate_id', 'schema_version'];
  if (strings.some((field) => typeof value[field] !== 'string' || !value[field].trim())) return false;
  if (!STATUSES.has(value.status) || !CURRENCIES.has(value.currency)) return false;
  if (!isDecimal(value.max_total_budget)) return false;
  if (!Array.isArray(value.allowed_merchants) || !value.allowed_merchants.length
    || value.allowed_merchants.some((item) => typeof item !== 'string' || !item.trim())) return false;
  if (!Array.isArray(value.allowed_categories) || !value.allowed_categories.length
    || value.allowed_categories.some((item) => typeof item !== 'string' || !item.trim())) return false;
  if (!Number.isInteger(value.max_item_count) || value.max_item_count < 1
    || !Number.isInteger(value.max_quantity_per_item) || value.max_quantity_per_item < 1
    || value.max_quantity_per_item > value.max_item_count) return false;
  if (!isDecimal(value.max_price_per_item, { nullable: true })) return false;
  if (value.max_price_per_item !== null
    && decimalGreaterThan(value.max_price_per_item, value.max_total_budget)) return false;
  if (typeof value.allow_subscriptions !== 'boolean'
    || typeof value.require_user_approval !== 'boolean') return false;
  if (!isDate(value.expires_at) || !isDate(value.created_at)) return false;
  if (!['signature', 'signature_algorithm', 'owner_user_id'].every((field) => (
    value[field] === null || typeof value[field] === 'string'
  ))) return false;
  return value.signed_at === null || isDate(value.signed_at);
}

function invalidMandateResponse() {
  return new ApiError('The service returned an incomplete mandate response. The create outcome is unknown.', {
    status: 201,
    code: 'invalid-response',
  });
}

function encodeMandateId(mandateId) {
  return encodeURIComponent(mandateId);
}

function createPayload(payload) {
  const allowedPayload = {};
  for (const field of CREATE_FIELDS) {
    if (Object.hasOwn(payload, field) && payload[field] !== undefined) {
      allowedPayload[field] = payload[field];
    }
  }
  return allowedPayload;
}

export function listMandates(token) {
  return request(`/api/v1/mandates?limit=${MANDATE_LIST_LIMIT}`, { token });
}

export function getMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}`, { token });
}

export function createMandate(token, payload) {
  return request('/api/v1/mandates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    token,
    body: JSON.stringify(createPayload(payload)),
    expectedStatus: 201,
  }).then((result) => {
    if (!isCompleteMandate(result)) throw invalidMandateResponse();
    return result;
  });
}

export function confirmMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}/confirm`, {
    method: 'POST',
    token,
  });
}

export function signMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}/sign`, {
    method: 'POST',
    token,
  });
}

export function activateMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}/activate`, {
    method: 'POST',
    token,
  });
}

export function revokeMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}/revoke`, {
    method: 'POST',
    token,
  });
}

export function verifyMandate(token, mandateId) {
  return request(`/api/v1/mandates/${encodeMandateId(mandateId)}/verify`, { token });
}
