import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  consumeApprovalToken,
  evaluateProposal,
  issueApprovalToken,
  recordPurchaseConsent,
  refreshProposalAuthorization,
  validateAuthorizationSnapshot,
  validatePolicyDecision,
} from '../services/proposals';
import { SUPPORTED_CURRENCIES } from '../services/mandates';

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers({ 'Content-Type': 'application/json' }),
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  };
}

const tokenResponse = {
  proposal_id: 'pr_1', token: 'private-token',
  issued_at: '2030-01-01T00:00:00Z', expires_at: '2030-01-01T00:05:00Z', nonce: 'nonce',
};
const consentResponse = {
  proposal_id: 'pr_1', status: 'APPROVED', policy_decision: 'ALLOW',
  amount: '12.50', currency: 'USD', consented_at: '2030-01-01T00:00:00Z', idempotent: false,
};

function freshPolicy() {
  const checks = [
    'Checks mandate signature integrity.',
    'Checks that the mandate is active and unexpired.',
    "Checks the mandate's total budget limit.",
    'Checks the per-item price limit.',
    'Checks whether the merchant is eligible.',
    'Checks whether the product category is eligible.',
    'Checks item quantity limits.',
    'Checks currency consistency.',
    'Checks live-price policy requirements.',
    'Checks the daily spend limit.',
    'Checks for duplicate or replayed proposals.',
    'Checks proposal data integrity.',
  ];
  const rules = Array.from({ length: 12 }, (_, index) => ({
    rule_id: `P${String(index + 1).padStart(2, '0')}`,
    outcome: 'PASS', severity: 'INFORMATIONAL',
    explanation: `${checks[index]} This check passed.`, requires_user_review: false,
  }));
  return {
    decision: 'ALLOW', rules,
  };
}

function legacyPolicy() {
  const rules = Array.from({ length: 12 }, (_, index) => ({
    rule_id: `P${String(index + 1).padStart(2, '0')}`, rule_name: `Rule ${index + 1}`,
    status: 'PASS', reason: 'Checked.', evidence: {}, evaluated_at: '2030-01-01T00:00:00Z',
  }));
  return {
    proposal_id: 'pr_1', mandate_id: 'man_1', decision: 'ALLOW',
    decision_at: '2030-01-01T00:00:00Z', rules, blocking_rules: [],
    approval_required_rules: [], is_one_time_exception: false, exception_types: [],
  };
}

const freshAssessment = {
  status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
  created_at: '2030-01-01T00:00:00Z', is_current: true,
};

function freshSnapshot(policy = freshPolicy(), assessment = freshAssessment) {
  return {
    proposal_id: 'pr_1', proposal_status: 'EVALUATED',
    policy_decision: policy, assessment,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('proposal authorization API services', () => {
  it('requests one coherent policy and auditor snapshot through the authenticated API', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, freshSnapshot()));
    vi.stubGlobal('fetch', fetchMock);
    await expect(refreshProposalAuthorization('session', 'pr_1')).resolves.toMatchObject({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED',
      policyDecision: { decision: 'ALLOW' }, assessment: freshAssessment,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/proposals/pr_1/authorization-snapshot');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session');
    expect(options.body).toBeUndefined();
  });

  it('accepts only the minimized public policy response from evaluation', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, freshPolicy()));
    vi.stubGlobal('fetch', fetchMock);
    await expect(evaluateProposal('session', 'pr_1')).resolves.toEqual(freshPolicy());
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/proposals/pr_1/evaluate-review');

    const unsafe = freshPolicy();
    unsafe.rules[0].evidence = { content_hash: 'private-value' };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, unsafe)));
    await expect(evaluateProposal('session', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it.each([
    ['impossible policy date', { ...legacyPolicy(), decision_at: '2030-02-30T00:00:00Z' }],
    ['invalid policy offset', { ...legacyPolicy(), decision_at: '2030-01-01T00:00:00+01:99' }],
    ['impossible rule date', {
      ...legacyPolicy(),
      rules: legacyPolicy().rules.map((rule, index) => (
        index === 0 ? { ...rule, evaluated_at: '2030-02-30T00:00:00Z' } : rule
      )),
    }],
  ])('rejects policy timestamps that are syntactically plausible but invalid (%s)', (_label, policy) => {
    expect(() => validatePolicyDecision(policy, 'pr_1')).toThrow(
      expect.objectContaining({ code: 'invalid-response' }),
    );
  });

  it('accepts strict UTC and numeric-offset timestamps in policy and assessment data', () => {
    const policy = {
      ...legacyPolicy(), decision_at: '2030-01-01T02:30:00+02:30',
      rules: legacyPolicy().rules.map((rule) => ({
        ...rule, evaluated_at: '2030-01-01T02:30:00+02:30',
      })),
    };
    expect(() => validatePolicyDecision(policy, 'pr_1')).not.toThrow();
  });

  it('rejects stale assessment and mismatched endpoint snapshot identity', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, freshSnapshot(
      freshPolicy(), { ...freshAssessment, is_current: false },
    ))));
    await expect(refreshProposalAuthorization('session', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, {
      ...freshSnapshot(), proposal_id: 'pr_other',
    })));
    await expect(refreshProposalAuthorization('session', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it.each([
    ['missing finding array', { ...freshAssessment, finding_codes: null }],
    ['invalid affected field', { ...freshAssessment, affected_fields: ['owner_id'] }],
    ['stale assessment', { ...freshAssessment, status: 'STALE', is_current: false }],
    ['failed assessment', { ...freshAssessment, status: 'FAILED', risk_level: 'UNCERTAIN' }],
    ['uncertain assessment', { ...freshAssessment, risk_level: 'UNCERTAIN' }],
    ['invalid assessment timestamp', { ...freshAssessment, created_at: 'later' }],
  ])('rejects malformed or noncurrent authorization assessment (%s)', (_label, value) => {
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: freshPolicy(),
      assessment: value,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));
  });

  it('rejects incomplete policy rules and wrong proposal lifecycle in a snapshot', () => {
    const incompletePolicy = { ...freshPolicy(), rules: freshPolicy().rules.slice(1) };
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: incompletePolicy,
      assessment: freshAssessment,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'SUBMITTED', policyDecision: freshPolicy(),
      assessment: freshAssessment,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));
  });

  it('rejects arbitrary, nested, or extra policy fields in the public contract', () => {
    const withEvidence = structuredClone(freshPolicy());
    withEvidence.rules[0].evidence = {
      content_hash: 'private-hash', nested: { idempotency_key: 'private-key' },
    };
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: withEvidence,
      assessment: freshAssessment,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));

    const withUnknownRuleField = structuredClone(freshPolicy());
    withUnknownRuleField.rules[0].diagnostic = 'private diagnostic';
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: withUnknownRuleField,
      assessment: freshAssessment,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));

    const withRawReason = structuredClone(freshPolicy());
    withRawReason.rules[0].explanation = 'https://internal.example/private-hash';
    expect(() => validateAuthorizationSnapshot({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: withRawReason,
      assessment: freshAssessment,
    }, 'pr_1')).toThrow(expect.objectContaining({ code: 'invalid-response' }));
  });

  it('issues a review token with the exact authenticated request contract', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, tokenResponse));
    vi.stubGlobal('fetch', fetchMock);
    await expect(issueApprovalToken('session', 'pr_1')).resolves.toEqual(tokenResponse);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/proposals/pr_1/approval-token');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session');
    expect(JSON.parse(options.body)).toEqual({ proposal_id: 'pr_1' });
  });

  it.each([
    ['mismatched proposal', { ...tokenResponse, proposal_id: 'pr_2' }],
    ['missing token', { ...tokenResponse, token: '' }],
    ['invalid issue timestamp', { ...tokenResponse, issued_at: 'tomorrow' }],
    ['expiry before issue', { ...tokenResponse, expires_at: '2029-12-31T23:59:00Z' }],
    ['missing nonce', { ...tokenResponse, nonce: '' }],
  ])('rejects malformed token success (%s)', async (_label, body) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, body)));
    await expect(issueApprovalToken('session', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('consumes the token with only the backend contract fields and validates review success', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, {
      success: true, proposal_id: 'pr_1', reason: 'Review completed.',
    }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(consumeApprovalToken('session', 'pr_1', tokenResponse)).resolves.toMatchObject({
      success: true, proposal_id: 'pr_1',
    });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/proposals/pr_1/approve');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session');
    expect(JSON.parse(options.body)).toEqual({
      proposal_id: 'pr_1', token: 'private-token',
      issued_at: tokenResponse.issued_at, nonce: 'nonce',
    });
  });

  it.each([
    ['false success', { success: false, proposal_id: 'pr_1', reason: 'No.' }],
    ['wrong proposal', { success: true, proposal_id: 'pr_2', reason: 'Done.' }],
    ['missing reason', { success: true, proposal_id: 'pr_1', reason: '' }],
  ])('rejects malformed review success (%s)', async (_label, body) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, body)));
    await expect(consumeApprovalToken('session', 'pr_1', tokenResponse)).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('records consent using only explicit confirmation and validates server result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, consentResponse));
    vi.stubGlobal('fetch', fetchMock);
    await expect(recordPurchaseConsent('session', 'pr_1')).resolves.toEqual(consentResponse);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/proposals/pr_1/purchase-consent');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session');
    expect(JSON.parse(options.body)).toEqual({ confirm_purchase: true });
    expect(options.body).not.toContain('amount');
    expect(options.body).not.toContain('policy_decision');
  });

  it.each([
    ['wrong status', { ...consentResponse, status: 'EVALUATED' }],
    ['wrong decision', { ...consentResponse, policy_decision: 'BLOCK' }],
    ['zero amount', { ...consentResponse, amount: '0' }],
    ['invalid currency format', { ...consentResponse, currency: 'US' }],
    ['unsupported currency', { ...consentResponse, currency: 'ZZZ' }],
    ['invalid timestamp', { ...consentResponse, consented_at: 'soon' }],
    ['wrong idempotency type', { ...consentResponse, idempotent: 'false' }],
  ])('rejects malformed consent success (%s)', async (_label, body) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, body)));
    await expect(recordPurchaseConsent('session', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });

  it('accepts every currency supported by the committed backend', async () => {
    for (const currency of SUPPORTED_CURRENCIES) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, {
        ...consentResponse, currency,
      })));
      await expect(recordPurchaseConsent('session', 'pr_1')).resolves.toMatchObject({ currency });
    }
  });
});
