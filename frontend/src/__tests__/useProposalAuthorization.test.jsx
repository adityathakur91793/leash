import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../services/api';
import * as api from '../services/proposals';
import { useProposalAuthorization } from '../hooks/useProposalAuthorization';

const assessment = {
  status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
  created_at: '2030-01-01T00:00:00Z', is_current: true,
};
const tokenResult = (id, token) => ({
  proposal_id: id, token, nonce: 'nonce',
  issued_at: '2030-01-01T00:00:00Z', expires_at: '2030-01-01T00:05:00Z',
});
const consentResult = (id) => ({
  proposal_id: id, status: 'APPROVED', policy_decision: 'ALLOW', amount: '12.50',
  currency: 'USD', consented_at: '2030-01-01T00:00:00Z', idempotent: false,
});

function policyDecision(outcome, id) {
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
  const rules = Array.from({ length: 12 }, (_, i) => ({
    rule_id: `P${String(i + 1).padStart(2, '0')}`,
    outcome: outcome === 'BLOCK' && i === 0 ? 'BLOCK'
      : outcome === 'REQUIRE_APPROVAL' && i === 3 ? 'REQUIRE_APPROVAL' : 'PASS',
    severity: outcome === 'BLOCK' && i === 0 ? 'BLOCKING'
      : outcome === 'REQUIRE_APPROVAL' && i === 3 ? 'REVIEW' : 'INFORMATIONAL',
    explanation: `${checks[i]} ${outcome === 'BLOCK' && i === 0
      ? 'This check blocks the proposal.'
      : outcome === 'REQUIRE_APPROVAL' && i === 3
        ? 'This check requires separate user review.' : 'This check passed.'}`,
    requires_user_review: outcome === 'REQUIRE_APPROVAL' && i === 3,
  }));
  return { decision: outcome, rules };
}

function freshSnapshot({ id = 'pr_1', outcome = 'ALLOW', risk = 'LOW', status = 'VALID', current = true } = {}) {
  return {
    proposalId: id,
    proposalStatus: outcome === 'BLOCK' ? 'REJECTED' : 'EVALUATED',
    policyDecision: policyDecision(outcome, id),
    assessment: { ...assessment, risk_level: risk, status, is_current: current },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function mount(options = {}) {
  const { id = 'pr_1', token = 'session-one', identity = 'session-one',
    outcome = 'ALLOW', onSessionExpired = vi.fn() } = options;
  const refresh = vi.spyOn(api, 'refreshProposalAuthorization')
    .mockImplementation((_token, proposalId) => Promise.resolve(freshSnapshot({
      id: proposalId, outcome,
    })));
  const hook = renderHook(({ proposalId, sessionToken, sessionIdentity }) => (
    useProposalAuthorization({
      sessionToken, sessionIdentity, proposalId,
      policyDecision: policyDecision(outcome, proposalId),
      assessment,
      onSessionExpired,
    })
  ), { initialProps: { proposalId: id, sessionToken: token, sessionIdentity: identity } });
  return { ...hook, refresh, onSessionExpired };
}

afterEach(() => vi.restoreAllMocks());

describe('useProposalAuthorization', () => {
  it('uses fresh ALLOW/LOW state and records consent only after a separate submit action', async () => {
    const issue = vi.spyOn(api, 'issueApprovalToken');
    const record = vi.spyOn(api, 'recordPurchaseConsent').mockResolvedValue(consentResult('pr_1'));
    const hook = mount();
    await act(async () => { expect(await hook.result.current.preparePurchaseConfirmation()).toBe(true); });
    expect(hook.result.current.phase).toBe('awaiting_purchase_confirmation');
    await act(async () => { expect(await hook.result.current.submitPurchaseConsent()).toBe(true); });
    expect(hook.refresh).toHaveBeenCalledTimes(2);
    expect(issue).not.toHaveBeenCalled();
    expect(record).toHaveBeenCalledWith('session-one', 'pr_1');
    expect(hook.result.current.consent).toEqual(consentResult('pr_1'));
  });

  it('requires REQUIRE_APPROVAL review before confirmation and never consents automatically', async () => {
    vi.spyOn(api, 'issueApprovalToken').mockResolvedValue(tokenResult('pr_1', 'secret-one'));
    const consume = vi.spyOn(api, 'consumeApprovalToken').mockResolvedValue({
      success: true, proposal_id: 'pr_1', reason: 'Review completed.',
    });
    const record = vi.spyOn(api, 'recordPurchaseConsent');
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(true); });
    expect(consume).toHaveBeenCalledWith('session-one', 'pr_1', tokenResult('pr_1', 'secret-one'));
    expect(hook.result.current.phase).toBe('review_completed');
    expect(record).not.toHaveBeenCalled();
    await act(async () => { expect(await hook.result.current.preparePurchaseConfirmation()).toBe(true); });
    expect(hook.result.current.phase).toBe('awaiting_purchase_confirmation');
    expect(record).not.toHaveBeenCalled();
  });

  it('fails closed for BLOCK, ALLOW medium/high, stale, failed, uncertain, and missing assessments', async () => {
    const issue = vi.spyOn(api, 'issueApprovalToken');
    const record = vi.spyOn(api, 'recordPurchaseConsent');
    const cases = [
      { outcome: 'BLOCK' }, { outcome: 'ALLOW', risk: 'MEDIUM' },
      { outcome: 'ALLOW', risk: 'HIGH' },
      { outcome: 'ALLOW', status: 'STALE', current: false },
      { outcome: 'ALLOW', status: 'FAILED', risk: 'UNCERTAIN', current: false },
      { outcome: 'ALLOW', risk: 'UNCERTAIN' },
    ];
    for (const options of cases) {
      const hook = mount(options);
      hook.refresh.mockResolvedValueOnce(freshSnapshot(options));
      await act(async () => { expect(await hook.result.current.preparePurchaseConfirmation()).toBe(false); });
      expect(hook.result.current.phase).toBe('error');
      hook.unmount();
    }
    const missing = mount();
    missing.refresh.mockResolvedValueOnce({ ...freshSnapshot(), assessment: null });
    await act(async () => { expect(await missing.result.current.preparePurchaseConfirmation()).toBe(false); });
    expect(issue).not.toHaveBeenCalled();
    expect(record).not.toHaveBeenCalled();
  });

  it('rejects malformed or mismatched fresh snapshots and does not retain a previous ready phase', async () => {
    const hook = mount();
    await act(async () => { await hook.result.current.preparePurchaseConfirmation(); });
    expect(hook.result.current.phase).toBe('awaiting_purchase_confirmation');
    hook.refresh.mockResolvedValueOnce({
      ...freshSnapshot(), assessment: { status: 'VALID', risk_level: 'LOW', is_current: true },
    });
    await act(async () => { expect(await hook.result.current.submitPurchaseConsent()).toBe(false); });
    expect(hook.result.current.phase).toBe('error');
    expect(hook.result.current.consent).toBeNull();
    hook.refresh.mockResolvedValueOnce(freshSnapshot({ id: 'pr_other' }));
    await act(async () => { expect(await hook.result.current.preparePurchaseConfirmation()).toBe(false); });
    expect(hook.result.current.phase).toBe('error');
  });

  it('prevents duplicate actions while authoritative refresh is pending', async () => {
    const pending = deferred();
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    hook.refresh.mockReturnValueOnce(pending.promise);
    let task;
    await act(async () => {
      task = hook.result.current.startExceptionReview();
      expect(await hook.result.current.startExceptionReview()).toBe(false);
    });
    expect(hook.refresh).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(freshSnapshot({ outcome: 'REQUIRE_APPROVAL' })));
    await act(async () => { await task; });
  });

  it('ignores stale success and failure after proposal switching', async () => {
    const pending = deferred();
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    hook.refresh.mockReturnValueOnce(pending.promise);
    const issue = vi.spyOn(api, 'issueApprovalToken');
    let task;
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    hook.rerender({ proposalId: 'pr_2', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    await act(async () => pending.resolve(freshSnapshot({ outcome: 'REQUIRE_APPROVAL' })));
    await act(async () => { await task; });
    expect(issue).not.toHaveBeenCalled();
    expect(hook.result.current.phase).toBe('idle');

    const staleFailure = deferred();
    hook.refresh.mockReturnValueOnce(staleFailure.promise);
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    hook.rerender({ proposalId: 'pr_3', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    await act(async () => staleFailure.reject(new Error('stale failure')));
    await act(async () => { await task; });
    expect(hook.result.current.phase).toBe('idle');
    expect(hook.result.current.error).toBeNull();
  });

  it('invalidates refresh after session change and handles session expiry errors', async () => {
    const oldRefresh = deferred();
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    hook.refresh.mockReturnValueOnce(oldRefresh.promise);
    const issue = vi.spyOn(api, 'issueApprovalToken');
    let task;
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    hook.rerender({ proposalId: 'pr_1', sessionToken: 'session-two', sessionIdentity: 'session-two' });
    await act(async () => oldRefresh.resolve(freshSnapshot({ outcome: 'REQUIRE_APPROVAL' })));
    await act(async () => { await task; });
    expect(issue).not.toHaveBeenCalled();
    expect(hook.result.current.phase).toBe('idle');

    hook.refresh.mockRejectedValueOnce(new ApiError('Session expired.', {
      status: 401, code: 'unauthenticated',
    }));
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(false); });
    expect(hook.onSessionExpired).toHaveBeenCalledTimes(1);
    expect(hook.result.current.phase).toBe('error');
  });

  it('does not reuse token after failed consumption and ignores identity changes during issue/consume', async () => {
    const issuePending = deferred();
    const issue = vi.spyOn(api, 'issueApprovalToken').mockReturnValue(issuePending.promise);
    const consume = vi.spyOn(api, 'consumeApprovalToken');
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    let task;
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    hook.rerender({ proposalId: 'pr_2', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    await act(async () => issuePending.resolve(tokenResult('pr_1', 'old-secret')));
    await act(async () => { await task; });
    expect(consume).not.toHaveBeenCalled();
    expect(hook.result.current.phase).toBe('idle');

    hook.rerender({ proposalId: 'pr_1', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    issue.mockResolvedValue(tokenResult('pr_1', 'new-secret'));
    const consumePending = deferred();
    consume.mockReturnValueOnce(consumePending.promise);
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    expect(consume).toHaveBeenCalledTimes(1);
    hook.rerender({ proposalId: 'pr_2', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    await act(async () => consumePending.reject(new Error('old consume failed')));
    await act(async () => { await task; });
    expect(hook.result.current.phase).toBe('idle');
    expect(hook.result.current.error).toBeNull();
  });

  it('uses a newly issued token after consumption failure', async () => {
    const firstToken = tokenResult('pr_1', 'first-transient-token');
    const secondToken = tokenResult('pr_1', 'second-transient-token');
    const issue = vi.spyOn(api, 'issueApprovalToken')
      .mockResolvedValueOnce(firstToken).mockResolvedValueOnce(secondToken);
    const consume = vi.spyOn(api, 'consumeApprovalToken')
      .mockRejectedValueOnce(new Error('consume failed'))
      .mockResolvedValueOnce({ success: true, proposal_id: 'pr_1', reason: 'Done.' });
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(false); });
    expect(hook.result.current.phase).toBe('error');
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(true); });
    expect(issue).toHaveBeenCalledTimes(2);
    expect(consume.mock.calls.map(([,, supplied]) => supplied.token)).toEqual([
      'first-transient-token', 'second-transient-token',
    ]);
    expect(hook.result.current.phase).toBe('review_completed');
  });

  it('prevents duplicate actions during token issue, token consume, and consent requests', async () => {
    const issuePending = deferred();
    const consumePending = deferred();
    const consentPending = deferred();
    const issue = vi.spyOn(api, 'issueApprovalToken').mockReturnValue(issuePending.promise);
    const consume = vi.spyOn(api, 'consumeApprovalToken').mockReturnValue(consumePending.promise);
    const record = vi.spyOn(api, 'recordPurchaseConsent').mockReturnValue(consentPending.promise);
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    let reviewTask;
    await act(async () => { reviewTask = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    expect(issue).toHaveBeenCalledTimes(1);
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(false); });
    await act(async () => issuePending.resolve(tokenResult('pr_1', 'once')));
    await act(async () => { await Promise.resolve(); });
    expect(consume).toHaveBeenCalledTimes(1);
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(false); });
    await act(async () => consumePending.resolve({
      success: true, proposal_id: 'pr_1', reason: 'Done.',
    }));
    await act(async () => { await reviewTask; });
    await act(async () => { await hook.result.current.preparePurchaseConfirmation(); });
    let consentTask;
    await act(async () => { consentTask = hook.result.current.submitPurchaseConsent(); });
    await act(async () => { await Promise.resolve(); });
    expect(record).toHaveBeenCalledTimes(1);
    await act(async () => { expect(await hook.result.current.submitPurchaseConsent()).toBe(false); });
    await act(async () => consentPending.resolve(consentResult('pr_1')));
    await act(async () => { await consentTask; });
  });

  it('does not consume an issued token after the session identity changes', async () => {
    const issuePending = deferred();
    const issue = vi.spyOn(api, 'issueApprovalToken').mockReturnValue(issuePending.promise);
    const consume = vi.spyOn(api, 'consumeApprovalToken');
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    let task;
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    expect(issue).toHaveBeenCalledTimes(1);
    hook.rerender({ proposalId: 'pr_1', sessionToken: 'session-two', sessionIdentity: 'session-two' });
    await act(async () => issuePending.resolve(tokenResult('pr_1', 'old-session-token')));
    await act(async () => { await task; });
    expect(consume).not.toHaveBeenCalled();
    expect(hook.result.current.phase).toBe('idle');
  });

  it('ignores consent result after proposal switching and does not expose server status early', async () => {
    const pending = deferred();
    const record = vi.spyOn(api, 'recordPurchaseConsent').mockReturnValue(pending.promise);
    const hook = mount();
    await act(async () => { await hook.result.current.preparePurchaseConfirmation(); });
    let task;
    await act(async () => { task = hook.result.current.submitPurchaseConsent(); });
    expect(hook.result.current.phase).toBe('recording_purchase_consent');
    expect(hook.result.current.consent).toBeNull();
    hook.rerender({ proposalId: 'pr_2', sessionToken: 'session-one', sessionIdentity: 'session-one' });
    await act(async () => pending.resolve(consentResult('pr_1')));
    await act(async () => { await task; });
    expect(record).toHaveBeenCalledTimes(1);
    expect(hook.result.current.phase).toBe('idle');
    expect(hook.result.current.consent).toBeNull();
  });

  it('ignores completions after unmount during refresh, issue, consume, and consent', async () => {
    const refreshPending = deferred();
    let hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    hook.refresh.mockReturnValueOnce(refreshPending.promise);
    const issue = vi.spyOn(api, 'issueApprovalToken');
    let task;
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    hook.unmount();
    await act(async () => refreshPending.resolve(freshSnapshot({ outcome: 'REQUIRE_APPROVAL' })));
    await act(async () => { await task; });
    expect(issue).not.toHaveBeenCalled();

    const issuePending = deferred();
    hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    vi.spyOn(api, 'issueApprovalToken').mockReturnValue(issuePending.promise);
    const consume = vi.spyOn(api, 'consumeApprovalToken');
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    hook.unmount();
    await act(async () => issuePending.resolve(tokenResult('pr_1', 'unmounted-secret')));
    await act(async () => { await task; });
    expect(consume).not.toHaveBeenCalled();

    const consumePending = deferred();
    hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    vi.spyOn(api, 'issueApprovalToken').mockResolvedValue(tokenResult('pr_1', 'one-shot'));
    vi.spyOn(api, 'consumeApprovalToken').mockReturnValue(consumePending.promise);
    await act(async () => { task = hook.result.current.startExceptionReview(); });
    await act(async () => { await Promise.resolve(); });
    hook.unmount();
    await act(async () => consumePending.resolve({ success: true, proposal_id: 'pr_1', reason: 'Done.' }));
    await act(async () => { await task; });

    const consentPending = deferred();
    hook = mount();
    vi.spyOn(api, 'recordPurchaseConsent').mockReturnValue(consentPending.promise);
    await act(async () => { await hook.result.current.preparePurchaseConfirmation(); });
    await act(async () => { task = hook.result.current.submitPurchaseConsent(); });
    hook.unmount();
    await act(async () => consentPending.resolve(consentResult('pr_1')));
    await act(async () => { await task; });
  });

  it('keeps token material out of browser storage and errors', async () => {
    vi.spyOn(api, 'issueApprovalToken').mockResolvedValue(tokenResult('pr_1', 'no-storage-secret'));
    vi.spyOn(api, 'consumeApprovalToken').mockRejectedValue(new ApiError('Review denied.', {
      status: 400, code: 'http',
    }));
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const hook = mount({ outcome: 'REQUIRE_APPROVAL' });
    await act(async () => { expect(await hook.result.current.startExceptionReview()).toBe(false); });
    expect(setItem).not.toHaveBeenCalled();
    expect(hook.result.current.error).not.toContain('no-storage-secret');
    expect(hook.result.current.phase).toBe('error');
  });
});
