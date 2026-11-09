import { request } from './api';

const PAYMENT_STATUSES = [
  'CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'CAPTURING', 'COMPLETED',
  'FAILED', 'CANCELLED', 'RECONCILIATION_REQUIRED',
];
const MANDATE_STATUSES = ['DRAFT', 'CONFIRMED', 'SIGNED', 'ACTIVE', 'EXPIRED', 'REVOKED'];
const PROPOSAL_STATUSES = ['SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED', 'REJECTED', 'WITHDRAWN'];
const UNRESOLVED_ATTEMPT_STATUSES = ['UNKNOWN', 'PROVIDER_CREATED', 'RECONCILIATION_REQUIRED'];
const EVENT_TYPES = new Set([
  'MANDATE_CREATED', 'MANDATE_CONFIRMED', 'MANDATE_SIGNED', 'MANDATE_ACTIVATED',
  'MANDATE_REVOKED', 'MANDATE_EXPIRED', 'PROPOSAL_CREATED', 'PROPOSAL_EVALUATED',
  'PROPOSAL_APPROVED', 'PURCHASE_CONSENT_RECORDED', 'PROPOSAL_REJECTED',
  'EXCEPTION_APPROVED', 'REPLAY_ATTEMPT_BLOCKED', 'AUTHORIZATION_BLOCKED',
  'PAYMENT_ORDER_CREATED', 'PAYMENT_ORDER_ATTEMPT_PREPARED',
  'PAYMENT_ORDER_ATTEMPT_UNKNOWN', 'PAYMENT_ORDER_ATTEMPT_RECONCILED',
  'PAYMENT_APPROVAL_OBSERVED', 'PAYMENT_CAPTURE_ATTEMPTED', 'PAYMENT_CAPTURED',
  'PAYMENT_FAILED', 'PAYMENT_RECONCILED', 'INTENT_ASSESSMENT_COMPLETED',
  'INTENT_ASSESSMENT_FAILED',
]);
const INTEGRITY_NOTE = 'This checks audit-log hash-chain integrity only; it does not prove that underlying business events were correct.';

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function isActivityEvent(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && exactKeys(value, ['timestamp', 'event_type', 'entity_type', 'actor', 'summary', 'display_name', 'amount', 'currency'])
    && typeof value.timestamp === 'string' && Number.isFinite(Date.parse(value.timestamp))
    && EVENT_TYPES.has(value.event_type)
    && ['MANDATE', 'PROPOSAL', 'PAYMENT'].includes(value.entity_type)
    && ['USER', 'AGENT', 'SYSTEM', 'ADMIN', 'POLICY_ENGINE'].includes(value.actor)
    && typeof value.summary === 'string' && value.summary.length > 0 && value.summary.length <= 100
    && typeof value.display_name === 'string' && value.display_name.length > 0 && value.display_name.length <= 256
    && (value.amount === null || (typeof value.amount === 'string' && /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value.amount)))
    && (value.currency === null || /^[A-Z]{3}$/.test(value.currency));
}

export function validateActivityOverview(value) {
  const statusKeys = PAYMENT_STATUSES;
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !exactKeys(value, ['items', 'total', 'has_more', 'mandate_status_counts', 'proposal_status_counts', 'payment_status_counts', 'unresolved_checkout_attempt_count', 'unresolved_checkout_attempt_status_counts', 'confirmed_settlement_count', 'confirmed_settlement_totals', 'hash_chain_valid', 'integrity_note'])
      || !Array.isArray(value.items) || value.items.length > 100 || !value.items.every(isActivityEvent)
      || !Number.isInteger(value.total) || value.total < value.items.length
      || typeof value.has_more !== 'boolean' || value.has_more !== (value.total > value.items.length)
      || !value.mandate_status_counts || typeof value.mandate_status_counts !== 'object'
      || Array.isArray(value.mandate_status_counts) || !exactKeys(value.mandate_status_counts, MANDATE_STATUSES)
      || MANDATE_STATUSES.some((status) => !Number.isInteger(value.mandate_status_counts[status]) || value.mandate_status_counts[status] < 0)
      || !value.proposal_status_counts || typeof value.proposal_status_counts !== 'object'
      || Array.isArray(value.proposal_status_counts) || !exactKeys(value.proposal_status_counts, PROPOSAL_STATUSES)
      || PROPOSAL_STATUSES.some((status) => !Number.isInteger(value.proposal_status_counts[status]) || value.proposal_status_counts[status] < 0)
      || !value.payment_status_counts || typeof value.payment_status_counts !== 'object'
      || Array.isArray(value.payment_status_counts)
      || !exactKeys(value.payment_status_counts, statusKeys)
      || statusKeys.some((status) => !Number.isInteger(value.payment_status_counts[status]) || value.payment_status_counts[status] < 0)
      || !value.unresolved_checkout_attempt_status_counts
      || typeof value.unresolved_checkout_attempt_status_counts !== 'object'
      || Array.isArray(value.unresolved_checkout_attempt_status_counts)
      || !exactKeys(value.unresolved_checkout_attempt_status_counts, UNRESOLVED_ATTEMPT_STATUSES)
      || UNRESOLVED_ATTEMPT_STATUSES.some((status) => !Number.isInteger(value.unresolved_checkout_attempt_status_counts[status]) || value.unresolved_checkout_attempt_status_counts[status] < 0)
      || !Number.isInteger(value.unresolved_checkout_attempt_count)
      || value.unresolved_checkout_attempt_count < 0
      || value.unresolved_checkout_attempt_count !== UNRESOLVED_ATTEMPT_STATUSES.reduce((sum, status) => sum + value.unresolved_checkout_attempt_status_counts[status], 0)
      || !Number.isInteger(value.confirmed_settlement_count) || value.confirmed_settlement_count < 0
      || !Array.isArray(value.confirmed_settlement_totals)
      || !value.confirmed_settlement_totals.every((item) => item && typeof item === 'object'
        && exactKeys(item, ['currency', 'amount']) && /^[A-Z]{3}$/.test(item.currency)
        && typeof item.amount === 'string' && /^(?:0|[1-9]\d*)(?:\.\d{2})$/.test(item.amount))
      || typeof value.hash_chain_valid !== 'boolean' || value.integrity_note !== INTEGRITY_NOTE) {
    throw new Error('The service returned an invalid activity overview. Refresh to try again.');
  }
  return value;
}

export async function getActivityOverview(token) {
  return validateActivityOverview(await request('/api/v1/activity/overview', { token }));
}
