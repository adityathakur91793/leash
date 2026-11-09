import { request } from './api';
import { SUPPORTED_CURRENCIES } from './mandates';

const ATTEMPT_STATES = new Set([
  'PREPARED', 'REQUESTING', 'UNKNOWN', 'PROVIDER_CREATED', 'FAILED',
  'RECONCILIATION_REQUIRED',
]);
const ORDER_STATES = new Set([
  'CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'CAPTURING', 'COMPLETED', 'FAILED', 'CANCELLED',
  'RECONCILIATION_REQUIRED',
]);

function assertOrder(data, proposalId) {
  if (!data || typeof data !== 'object' || Array.isArray(data)
      || data.proposal_id !== proposalId || typeof data.order_id !== 'string'
      || !data.order_id || typeof data.approval_url !== 'string'
      || !['CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'COMPLETED'].includes(data.status)
      || !/^[A-Z]{3}$/.test(data.currency)
      || !Number.isFinite(Number(data.total_amount))) {
    throw new Error('Checkout response could not be verified. Refresh before continuing.');
  }
  const url = new URL(data.approval_url);
  if (url.protocol !== 'https:'
      || url.username || url.password || url.port && url.port !== '443' || url.hash
      || !['www.sandbox.paypal.com', 'sandbox.paypal.com'].includes(url.hostname)) {
    throw new Error('Checkout returned an untrusted PayPal approval link.');
  }
  return data;
}

export async function createCheckout(token, proposalId) {
  const returnUrl = new URL(window.location.href);
  returnUrl.search = new URLSearchParams({ checkout: 'return', proposal_id: proposalId });
  returnUrl.hash = '';
  const cancelUrl = new URL(returnUrl);
  cancelUrl.search = new URLSearchParams({ checkout: 'cancel', proposal_id: proposalId });
  const data = await request(`/api/v1/proposals/${encodeURIComponent(proposalId)}/create-order`, {
    method: 'POST', token, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proposal_id: proposalId, return_url: returnUrl.href, cancel_url: cancelUrl.href }),
    expectedStatus: 201,
  });
  return assertOrder(data, proposalId);
}

export async function getCheckoutState(token, proposalId) {
  const data = await request(`/api/v1/proposals/${encodeURIComponent(proposalId)}/checkout`, { token });
  const expectedKeys = [
    'proposal_id', 'status', 'consent_recorded', 'order_id', 'approval_url',
    'currency', 'total_amount', 'payment_status', 'recovery_available',
  ];
  if (!data || typeof data !== 'object' || Array.isArray(data)
      || Object.keys(data).length !== expectedKeys.length
      || expectedKeys.some((key) => !Object.hasOwn(data, key))
      || data.proposal_id !== proposalId || typeof data.consent_recorded !== 'boolean'
      || typeof data.recovery_available !== 'boolean'
      || !SUPPORTED_CURRENCIES.includes(data.currency)
      || !Number.isFinite(Number(data.total_amount))
      || (data.status !== null && !ATTEMPT_STATES.has(data.status) && !ORDER_STATES.has(data.status))
      || (data.payment_status !== null && !ORDER_STATES.has(data.payment_status))
      || (data.order_id !== null && typeof data.order_id !== 'string')
      || (data.approval_url !== null && typeof data.approval_url !== 'string')) {
    throw new Error('Saved checkout state is malformed. Refresh proposal authorization.');
  }
  if (data.approval_url) {
    const url = new URL(data.approval_url);
    if (url.protocol !== 'https:'
        || url.username || url.password || url.port && url.port !== '443' || url.hash
        || !['www.sandbox.paypal.com', 'sandbox.paypal.com'].includes(url.hostname)) {
      throw new Error('Saved checkout has an untrusted PayPal approval link.');
    }
  }
  return data;
}

export async function recoverProposalCheckout(token, proposalId) {
  const result = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/checkout/reconcile`,
    { method: 'POST', token },
  );
  const statuses = new Set([...ATTEMPT_STATES, ...ORDER_STATES]);
  const providerStatuses = new Set([
    'CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'COMPLETED',
  ]);
  if (!result || typeof result !== 'object' || Array.isArray(result)
      || Object.keys(result).length !== 4
      || !['proposal_id', 'status', 'provider_status', 'payment_completed']
        .every((key) => Object.hasOwn(result, key))
      || result.proposal_id !== proposalId || !statuses.has(result.status)
      || (result.provider_status !== null && !providerStatuses.has(result.provider_status))
      || typeof result.payment_completed !== 'boolean'
      || (result.payment_completed && result.provider_status !== 'COMPLETED')) {
    throw new Error('Saved payment status could not be verified. Refresh checkout state.');
  }
  return result;
}

export async function verifyCheckoutReturn(token, proposalId, orderId) {
  const result = await request('/api/v1/payments/return', {
    method: 'POST', token,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proposal_id: proposalId, order_id: orderId }),
  });
  if (!result || result.proposal_id !== proposalId || result.order_id !== orderId
      || typeof result.payment_completed !== 'boolean'
      || !['CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'CAPTURING',
        'COMPLETED', 'RECONCILIATION_REQUIRED'].includes(result.status)) {
    throw new Error('Checkout return could not be verified. Refresh payment status.');
  }
  return result;
}

export async function captureCheckout(token, orderId, proposalId) {
  const result = await request(`/api/v1/payments/${encodeURIComponent(orderId)}/capture`, {
    method: 'POST', token,
  });
  if (!result || result.order_id !== orderId || result.proposal_id !== proposalId
      || result.status !== 'COMPLETED' || typeof result.capture_id !== 'string'
      || !result.capture_id.trim()) {
    throw new Error('Payment is not confirmed by LEASH. Refresh status before retrying.');
  }
  return result;
}

export function navigateToPayPal(approvalUrl) {
  const target = new URL(approvalUrl);
  if (target.protocol !== 'https:'
      || target.username || target.password || target.port && target.port !== '443' || target.hash
      || !['www.sandbox.paypal.com', 'sandbox.paypal.com'].includes(target.hostname)) {
    throw new Error('Checkout returned an untrusted PayPal approval link.');
  }
  window.location.assign(target.href);
}
