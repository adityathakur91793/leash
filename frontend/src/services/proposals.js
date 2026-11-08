import { ApiError, request } from './api';
import { SUPPORTED_CURRENCIES } from './mandates';

const PROPOSAL_STATUSES = new Set([
  'SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED', 'REJECTED', 'WITHDRAWN',
]);
const DECISIONS = new Set(['ALLOW', 'BLOCK', 'REQUIRE_APPROVAL']);
const RULE_STATUSES = new Set(['PASS', 'BLOCK', 'REQUIRE_APPROVAL', 'NOT_APPLICABLE']);
const ASSESSMENT_STATUSES = new Set(['VALID', 'FAILED', 'STALE']);
const ASSESSMENT_RISK_LEVELS = new Set(['LOW', 'MEDIUM', 'HIGH', 'UNCERTAIN']);
const ASSESSMENT_FINDINGS = new Set([
  'AMBIGUOUS_INTENT', 'BUDGET_TENSION', 'CATEGORY_MISMATCH', 'CURRENCY_INCONSISTENCY',
  'MERCHANT_MISMATCH', 'PRICE_INCONSISTENCY', 'QUANTITY_INCONSISTENCY',
  'SUBSCRIPTION_CONCERN', 'INSUFFICIENT_CONTEXT',
]);
const ASSESSMENT_FIELDS = new Set([
  'product_name', 'merchant', 'category', 'quantity', 'unit_price', 'total', 'currency',
  'mandate_budget', 'allowed_merchants', 'allowed_categories', 'item_limits', 'subscriptions',
]);
const POLICY_RULE_IDS = new Set(Array.from({ length: 12 }, (_, index) => (
  `P${String(index + 1).padStart(2, '0')}`
)));
const PUBLIC_RULE_OUTCOMES = new Set(['PASS', 'BLOCK', 'REQUIRE_APPROVAL', 'NOT_APPLICABLE']);
const PUBLIC_RULE_SEVERITIES = new Set(['INFORMATIONAL', 'REVIEW', 'BLOCKING']);
const PUBLIC_RULE_CHECKS = {
  P01: 'Checks mandate signature integrity.',
  P02: 'Checks that the mandate is active and unexpired.',
  P03: "Checks the mandate's total budget limit.",
  P04: 'Checks the per-item price limit.',
  P05: 'Checks whether the merchant is eligible.',
  P06: 'Checks whether the product category is eligible.',
  P07: 'Checks item quantity limits.',
  P08: 'Checks currency consistency.',
  P09: 'Checks live-price policy requirements.',
  P10: 'Checks the daily spend limit.',
  P11: 'Checks for duplicate or replayed proposals.',
  P12: 'Checks proposal data integrity.',
};
const PUBLIC_OUTCOME_TEXT = {
  PASS: 'This check passed.',
  BLOCK: 'This check blocks the proposal.',
  REQUIRE_APPROVAL: 'This check requires separate user review.',
  NOT_APPLICABLE: 'This check does not apply to this proposal.',
};
const MONEY_PATTERN = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;
const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const TEXT_PATTERN = /^[^\u0000-\u001f\u007f]+$/;
const ISO_TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/i;

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isText(value, maxLength = 2048) {
  return typeof value === 'string' && value.length > 0
    && value.length <= maxLength && TEXT_PATTERN.test(value);
}

function isId(value) {
  return typeof value === 'string' && ID_PATTERN.test(value);
}

function isTimestamp(value) {
  return typeof value === 'string'
    && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    && Number.isFinite(Date.parse(value));
}

function isStrictTimestamp(value) {
  if (typeof value !== 'string') return false;
  const match = ISO_TIMESTAMP_PATTERN.exec(value);
  if (!match) return false;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText,
    , offsetHourText, offsetMinuteText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const validDate = year >= 1 && month >= 1 && month <= 12
    && day >= 1 && day <= daysInMonth[month - 1];
  const validTime = Number(hourText) <= 23 && Number(minuteText) <= 59
    && Number(secondText) <= 59;
  const validOffset = offsetHourText === undefined
    || (Number(offsetHourText) <= 23 && Number(offsetMinuteText) <= 59);
  return validDate && validTime && validOffset && Number.isFinite(Date.parse(value));
}

function hasExactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function isMoney(value, { positive = false } = {}) {
  return typeof value === 'string' && MONEY_PATTERN.test(value)
    && (!positive || /[1-9]/.test(value));
}

function invalidResponse(kind) {
  throw new ApiError(`The service returned an invalid ${kind} response.`, {
    code: 'invalid-response',
  });
}

function isProposalSummary(value) {
  return isRecord(value)
    && isId(value.proposal_id)
    && PROPOSAL_STATUSES.has(value.status)
    && isText(value.product_name, 256)
    && isText(value.merchant, 256)
    && isMoney(value.total)
    && typeof value.currency === 'string' && /^[A-Z]{3}$/.test(value.currency)
    && isTimestamp(value.created_at);
}

export function validateProposalList(value) {
  if (!Array.isArray(value) || !value.every(isProposalSummary)) {
    invalidResponse('proposal list');
  }
  return value;
}

export function validateProposalDetail(value, expectedProposalId) {
  const valid = isProposalSummary(value)
    && value.proposal_id === expectedProposalId
    && isId(value.mandate_id)
    && Number.isInteger(value.quantity) && value.quantity >= 1 && value.quantity <= 10_000
    && isMoney(value.unit_price, { positive: true })
    && isMoney(value.subtotal);
  if (!valid) invalidResponse('proposal detail');
  return value;
}

function isRule(value) {
  return isRecord(value)
    && isText(value.rule_id, 32)
    && isText(value.rule_name, 128)
    && RULE_STATUSES.has(value.status)
    && typeof value.reason === 'string'
    && isRecord(value.evidence)
    && isStrictTimestamp(value.evaluated_at);
}

export function validatePolicyDecision(value, expectedProposalId, expectedMandateId) {
  const valid = isRecord(value)
    && isId(value.proposal_id) && value.proposal_id === expectedProposalId
    && isId(value.mandate_id)
    && (expectedMandateId === undefined || value.mandate_id === expectedMandateId)
    && DECISIONS.has(value.decision)
    && isStrictTimestamp(value.decision_at)
    && Array.isArray(value.rules) && value.rules.every(isRule)
    && Array.isArray(value.blocking_rules) && value.blocking_rules.every((item) => isText(item, 32))
    && Array.isArray(value.approval_required_rules)
    && value.approval_required_rules.every((item) => isText(item, 32))
    && typeof value.is_one_time_exception === 'boolean'
    && Array.isArray(value.exception_types)
    && value.exception_types.every((item) => isText(item, 64));
  if (!valid) invalidResponse('policy evaluation');
  return value;
}

export function validatePublicPolicyDecision(value) {
  if (!isRecord(value) || !hasExactKeys(value, ['decision', 'rules'])
      || !DECISIONS.has(value.decision) || !Array.isArray(value.rules)
      || value.rules.length !== POLICY_RULE_IDS.size) {
    invalidResponse('authorization policy decision');
  }

  const expectedRuleKeys = [
    'rule_id', 'outcome', 'severity', 'explanation', 'requires_user_review',
  ];
  const validRules = value.rules.every((rule) => {
    if (!isRecord(rule) || !hasExactKeys(rule, expectedRuleKeys)
        || !POLICY_RULE_IDS.has(rule.rule_id)
        || !PUBLIC_RULE_OUTCOMES.has(rule.outcome)
        || !PUBLIC_RULE_SEVERITIES.has(rule.severity)
        || typeof rule.requires_user_review !== 'boolean') return false;
    const expectedSeverity = rule.outcome === 'BLOCK' ? 'BLOCKING'
      : rule.outcome === 'REQUIRE_APPROVAL' ? 'REVIEW' : 'INFORMATIONAL';
    return rule.severity === expectedSeverity
      && rule.requires_user_review === (rule.outcome === 'REQUIRE_APPROVAL')
      && rule.explanation === `${PUBLIC_RULE_CHECKS[rule.rule_id]} ${PUBLIC_OUTCOME_TEXT[rule.outcome]}`;
  });
  if (!validRules) invalidResponse('authorization policy decision');
  const ids = value.rules.map((rule) => rule.rule_id);
  const outcomes = new Set(value.rules.map((rule) => rule.outcome));
  const coherentDecision = outcomes.has('BLOCK')
    ? value.decision === 'BLOCK'
    : outcomes.has('REQUIRE_APPROVAL')
      ? value.decision === 'REQUIRE_APPROVAL'
      : value.decision !== 'BLOCK';
  if (new Set(ids).size !== POLICY_RULE_IDS.size || !coherentDecision) {
    invalidResponse('authorization policy decision');
  }
  return value;
}

export function validateIntentAssessmentResponse(value, expectedProposalId) {
  const validTopLevel = isRecord(value)
    && hasExactKeys(value, ['proposal_id', 'assessment'])
    && isId(value.proposal_id)
    && value.proposal_id === expectedProposalId
    && (value.assessment === null || (
      isRecord(value.assessment)
      && hasExactKeys(value.assessment, [
        'status', 'risk_level', 'finding_codes', 'affected_fields', 'created_at', 'is_current',
      ])
      && ASSESSMENT_STATUSES.has(value.assessment.status)
      && ASSESSMENT_RISK_LEVELS.has(value.assessment.risk_level)
      && Array.isArray(value.assessment.finding_codes)
      && value.assessment.finding_codes.every((code) => ASSESSMENT_FINDINGS.has(code))
      && Array.isArray(value.assessment.affected_fields)
      && value.assessment.affected_fields.every((field) => ASSESSMENT_FIELDS.has(field))
      && isStrictTimestamp(value.assessment.created_at)
      && typeof value.assessment.is_current === 'boolean'
    ));
  if (!validTopLevel) invalidResponse('Intent Auditor assessment');
  return value.assessment;
}

export function listProposals(token, limit = 100) {
  return request(`/api/v1/proposals?limit=${Math.min(100, Math.max(1, limit))}`, { token });
}

export function getProposal(token, proposalId) {
  return request(`/api/v1/proposals/${encodeURIComponent(proposalId)}`, { token });
}

export function evaluateProposal(token, proposalId) {
  return request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/evaluate-review`,
    { method: 'POST', token },
  ).then(validatePublicPolicyDecision);
}

export async function getIntentAssessment(token, proposalId) {
  const response = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/auditor-assessment`,
    { token },
  );
  return validateIntentAssessmentResponse(response, proposalId);
}

/** Request one backend-produced policy and auditor snapshot. */
export async function refreshProposalAuthorization(token, proposalId) {
  const response = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/authorization-snapshot`,
    { method: 'POST', token },
  );
  if (!isRecord(response) || !hasExactKeys(response, [
    'proposal_id', 'proposal_status', 'policy_decision', 'assessment',
  ])) {
    invalidResponse('authorization snapshot');
  }
  return validateAuthorizationSnapshot({
    proposalId: response.proposal_id,
    proposalStatus: response.proposal_status,
    policyDecision: response.policy_decision,
    assessment: response.assessment,
  }, proposalId);
}

export function validateAuthorizationSnapshot(value, expectedProposalId) {
  const valid = isRecord(value)
    && hasExactKeys(value, ['proposalId', 'proposalStatus', 'policyDecision', 'assessment'])
    && value.proposalId === expectedProposalId
    && PROPOSAL_STATUSES.has(value.proposalStatus)
    && validatePublicPolicyDecision(value.policyDecision)
    && ((value.assessment === null && value.policyDecision.decision === 'BLOCK')
      || (value.assessment !== null
        && validateIntentAssessmentResponse({
          proposal_id: expectedProposalId,
          assessment: value.assessment,
        }, expectedProposalId) !== undefined
        && (value.policyDecision.decision === 'BLOCK'
          || (value.assessment.status === 'VALID'
            && value.assessment.is_current === true
            && ['LOW', 'MEDIUM', 'HIGH'].includes(value.assessment.risk_level)))));
  const lifecycleMatches = isRecord(value)
    && (value.policyDecision?.decision === 'BLOCK'
      ? value.proposalStatus === 'REJECTED'
      : value.proposalStatus === 'EVALUATED');
  if (!valid || !lifecycleMatches) invalidResponse('authorization snapshot');
  return value;
}

function validateApprovalTokenResponse(value, expectedProposalId) {
  const valid = isRecord(value)
    && hasExactKeys(value, ['proposal_id', 'token', 'issued_at', 'expires_at', 'nonce'])
    && value.proposal_id === expectedProposalId
    && isText(value.token, 200)
    && isStrictTimestamp(value.issued_at)
    && isStrictTimestamp(value.expires_at)
    && Date.parse(value.expires_at) > Date.parse(value.issued_at)
    && isText(value.nonce, 200);
  if (!valid) invalidResponse('approval token');
  return value;
}

export async function issueApprovalToken(token, proposalId) {
  const response = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/approval-token`,
    {
      method: 'POST',
      token,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_id: proposalId }),
    },
  );
  return validateApprovalTokenResponse(response, proposalId);
}

export async function consumeApprovalToken(token, proposalId, approval) {
  const response = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/approve`,
    {
      method: 'POST',
      token,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        proposal_id: proposalId,
        token: approval.token,
        issued_at: approval.issued_at,
        nonce: approval.nonce,
      }),
    },
  );
  const valid = isRecord(response)
    && hasExactKeys(response, ['success', 'proposal_id', 'reason'])
    && response.success === true
    && response.proposal_id === proposalId
    && isText(response.reason);
  if (!valid) invalidResponse('approval review');
  return response;
}

function isPositiveAmount(value) {
  if (typeof value === 'number') return Number.isFinite(value) && value > 0;
  return typeof value === 'string'
    && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value)
    && /[1-9]/.test(value);
}

export async function recordPurchaseConsent(token, proposalId) {
  const response = await request(
    `/api/v1/proposals/${encodeURIComponent(proposalId)}/purchase-consent`,
    {
      method: 'POST',
      token,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm_purchase: true }),
    },
  );
  const valid = isRecord(response)
    && hasExactKeys(response, [
      'proposal_id', 'status', 'policy_decision', 'amount', 'currency', 'consented_at', 'idempotent',
    ])
    && response.proposal_id === proposalId
    && response.status === 'APPROVED'
    && ['ALLOW', 'REQUIRE_APPROVAL'].includes(response.policy_decision)
    && isPositiveAmount(response.amount)
    && SUPPORTED_CURRENCIES.includes(response.currency)
    && isStrictTimestamp(response.consented_at)
    && typeof response.idempotent === 'boolean';
  if (!valid) invalidResponse('purchase consent');
  return response;
}
