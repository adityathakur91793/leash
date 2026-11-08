import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../services/api';
import * as api from '../services/proposals';
import { ApprovalConsentPanel } from '../components/ApprovalConsentPanel';

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
const proposal = {
  proposal_id: 'pr_1', product_name: 'Notebook', merchant: 'shop.example',
  total: '24.50', currency: 'USD', status: 'EVALUATED',
};
const assessment = {
  status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
  created_at: '2030-01-01T00:00:00Z', is_current: true,
};

function policyDecision(decision = 'ALLOW') {
  return {
    decision,
    rules: checks.map((check, index) => {
      const outcome = decision === 'BLOCK' && index === 0 ? 'BLOCK'
        : decision === 'REQUIRE_APPROVAL' && index === 4 ? 'REQUIRE_APPROVAL' : 'PASS';
      const result = {
        PASS: 'This check passed.',
        BLOCK: 'This check blocks the proposal.',
        REQUIRE_APPROVAL: 'This check requires separate user review.',
      }[outcome];
      return {
        rule_id: `P${String(index + 1).padStart(2, '0')}`,
        outcome,
        severity: outcome === 'BLOCK' ? 'BLOCKING'
          : outcome === 'REQUIRE_APPROVAL' ? 'REVIEW' : 'INFORMATIONAL',
        explanation: `${check} ${result}`,
        requires_user_review: outcome === 'REQUIRE_APPROVAL',
      };
    }),
  };
}

function snapshot(id = 'pr_1', decision = 'ALLOW', assessmentValue = assessment) {
  return {
    proposalId: id,
    proposalStatus: decision === 'BLOCK' ? 'REJECTED' : 'EVALUATED',
    policyDecision: policyDecision(decision),
    assessment: assessmentValue,
  };
}

function tokenResult(id = 'pr_1') {
  return {
    proposal_id: id, token: 'never-render-this-token', nonce: 'never-render-this-nonce',
    issued_at: '2030-01-01T00:00:00Z', expires_at: '2030-01-01T00:05:00Z',
  };
}

function consentResult(id = 'pr_1') {
  return {
    proposal_id: id, status: 'APPROVED', policy_decision: 'ALLOW',
    amount: '24.50', currency: 'USD', consented_at: '2030-01-01T00:00:00Z',
    idempotent: false,
  };
}

function props(overrides = {}) {
  const decision = overrides.policyDecision || policyDecision('ALLOW');
  return {
    proposal,
    policyDecision: decision,
    assessment,
    sessionToken: 'session-secret',
    sessionIdentity: 'user-1',
    onSessionExpired: vi.fn(),
    onRefreshAuthorization: vi.fn(),
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});

describe('ApprovalConsentPanel', () => {
  it('keeps deterministic BLOCK from review or purchase consent', () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue(snapshot('pr_1', 'BLOCK'));
    const issue = vi.spyOn(api, 'issueApprovalToken');
    const record = vi.spyOn(api, 'recordPurchaseConsent');
    render(<ApprovalConsentPanel {...props({ policyDecision: policyDecision('BLOCK') })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    return screen.findByText('Current assessment').then(() => {
    expect(screen.getByRole('alert')).toHaveTextContent('Purchase blocked by deterministic policy');
    expect(screen.queryByRole('button', { name: /complete exception review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /purchase consent/i })).not.toBeInTheDocument();
    expect(issue).not.toHaveBeenCalled();
    expect(record).not.toHaveBeenCalled();
    });
  });

  it('completes explicit review, then requires separate purchase confirmation', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => (
      snapshot(id, 'REQUIRE_APPROVAL')
    ));
    vi.spyOn(api, 'issueApprovalToken').mockResolvedValue(tokenResult());
    const consume = vi.spyOn(api, 'consumeApprovalToken').mockResolvedValue({
      success: true, proposal_id: 'pr_1', reason: 'Review completed.',
    });
    const record = vi.spyOn(api, 'recordPurchaseConsent').mockResolvedValue(consentResult());
    render(<ApprovalConsentPanel {...props({
      policyDecision: policyDecision('REQUIRE_APPROVAL'),
    })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');

    fireEvent.click(screen.getByRole('button', { name: /complete exception review/i }));
    expect(await screen.findByText(/Exception review completed/)).toBeInTheDocument();
    expect(screen.getByText(/This is not purchase consent/)).toBeInTheDocument();
    expect(record).not.toHaveBeenCalled();
    expect(consume).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
    const dialog = await screen.findByRole('dialog', { name: 'Confirm purchase consent' });
    expect(dialog).toHaveTextContent('shop.example');
    expect(dialog).toHaveTextContent('Notebook');
    expect(dialog).toHaveTextContent('USD 24.50');
    expect(record).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(
      screen.getByRole('button', { name: /confirm purchase consent/i }),
    ));

    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: /confirm purchase consent/i }));
    await waitFor(() => expect(record).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/LEASH confirmed proposal status: APPROVED/)).toBeInTheDocument();
    expect(record).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText(/No PayPal order was created/)).toBeInTheDocument();
  });

  it('does not enable actions for stale or missing assessments', async () => {
    const refresh = vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue(snapshot());
    const stale = { ...assessment, is_current: false };
    const view = render(<ApprovalConsentPanel {...props({ assessment: stale })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('assessment is stale');
    expect(screen.queryByRole('button', { name: /continue to purchase consent/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    expect(refresh).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText('Current assessment')).toBeInTheDocument());
    view.unmount();
    render(<ApprovalConsentPanel {...props({ assessment: null })} />);
    expect(screen.getByText(/complete current policy and assessment are unavailable/i)).toBeInTheDocument();
  });

  it.each(['MEDIUM', 'HIGH'])('does not create a review bypass for ALLOW with %s risk', async (risk) => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue(snapshot(
      'pr_1', 'ALLOW', { ...assessment, risk_level: risk },
    ));
    render(<ApprovalConsentPanel {...props({
      assessment: { ...assessment, risk_level: risk },
    })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    expect(screen.getByRole('alert')).toHaveTextContent(`ALLOW with ${risk} auditor risk`);
    expect(screen.queryByRole('button', { name: /complete exception review/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /continue to purchase consent/i })).not.toBeInTheDocument();
  });

  it('handles expired sessions without showing authorization success', async () => {
    const expired = vi.fn();
    vi.spyOn(api, 'refreshProposalAuthorization').mockRejectedValue(new ApiError('Session expired.', {
      status: 401, code: 'unauthenticated',
    }));
    render(<ApprovalConsentPanel {...props({ onSessionExpired: expired })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/session has expired/i);
    expect(expired).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/consent recorded/i)).not.toBeInTheDocument();
  });

  it('supports Escape dismissal and restores focus from the confirmation dialog', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => snapshot(id));
    const record = vi.spyOn(api, 'recordPurchaseConsent');
    render(<ApprovalConsentPanel {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    const continueButton = screen.getByRole('button', { name: /continue to purchase consent/i });
    fireEvent.click(continueButton);
    const dialog = await screen.findByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /review purchase details/i }));
    expect(record).not.toHaveBeenCalled();
  });

  it('does not present caller assessment as current until an authoritative refresh succeeds', async () => {
    const pending = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization').mockReturnValue(pending.promise);
    render(<ApprovalConsentPanel {...props()} />);
    expect(screen.getByText('Not current')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    expect(screen.getByText(/Loading assessment data/)).toBeInTheDocument();
    await act(async () => pending.resolve(snapshot()));
    expect(screen.getByText('Current assessment')).toBeInTheDocument();
  });

  it('isolates only the application root and restores its prior inert state', async () => {
    const appRoot = document.createElement('div');
    appRoot.id = 'root';
    document.body.append(appRoot);
    try {
      vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue(snapshot());
      const view = render(<ApprovalConsentPanel {...props()} />);
      fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
      await screen.findByText('Current assessment');
      fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
      const dialog = await screen.findByRole('dialog');
      expect(appRoot.inert).toBe(true);
      const outsideButton = document.createElement('button');
      outsideButton.textContent = 'Outside control';
      document.body.append(outsideButton);
      outsideButton.focus();
      expect(dialog).toContainElement(document.activeElement);
      outsideButton.remove();
      view.unmount();
      expect(appRoot.inert).toBe(false);

      appRoot.inert = true;
      const second = render(<ApprovalConsentPanel {...props()} />);
      fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
      await screen.findByText('Current assessment');
      fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
      await screen.findByRole('dialog');
      second.unmount();
      expect(appRoot.inert).toBe(true);
    } finally {
      appRoot.remove();
    }
  });

  it('ignores Escape while consent is pending and restores focus on dismissal', async () => {
    const pending = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue(snapshot());
    vi.spyOn(api, 'recordPurchaseConsent').mockReturnValue(pending.promise);
    render(<ApprovalConsentPanel {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    const trigger = screen.getByRole('button', { name: /continue to purchase consent/i });
    fireEvent.click(trigger);
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: /confirm purchase consent/i }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await act(async () => pending.resolve(consentResult()));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: /review and purchase consent/i }));
  });

  it('revalidates after a consent failure and requires confirmation again before retry', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => snapshot(id));
    const record = vi.spyOn(api, 'recordPurchaseConsent')
      .mockRejectedValueOnce(new Error('network failure'))
      .mockResolvedValueOnce(consentResult());
    render(<ApprovalConsentPanel {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
    let dialog = await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: /confirm purchase consent/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/authorization request could not be completed/i);
    expect(record).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /recheck authorization/i }));
    await waitFor(() => expect(screen.getByRole('button', { name: /confirm purchase consent/i })).toBeEnabled());
    expect(record).toHaveBeenCalledTimes(1);
    dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /confirm purchase consent/i }));
    expect(await screen.findByText(/LEASH confirmed proposal status: APPROVED/)).toBeInTheDocument();
    expect(record).toHaveBeenCalledTimes(2);
  });

  it('prevents duplicate review requests and ignores an old proposal token response', async () => {
    const pendingToken = deferred();
    const refresh = vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => (
      snapshot(id, 'REQUIRE_APPROVAL')
    ));
    const issue = vi.spyOn(api, 'issueApprovalToken').mockReturnValue(pendingToken.promise);
    const consume = vi.spyOn(api, 'consumeApprovalToken');
    const view = render(<ApprovalConsentPanel {...props({
      policyDecision: policyDecision('REQUIRE_APPROVAL'),
    })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    const reviewButton = screen.getByRole('button', { name: /complete exception review/i });
    fireEvent.click(reviewButton);
    fireEvent.click(reviewButton);
    await waitFor(() => expect(issue).toHaveBeenCalledTimes(1));
    expect(refresh).toHaveBeenCalledTimes(2);
    view.rerender(<ApprovalConsentPanel {...props({
      proposal: { ...proposal, proposal_id: 'pr_2' },
      policyDecision: policyDecision('REQUIRE_APPROVAL'),
    })} />);
    await act(async () => pendingToken.resolve(tokenResult('pr_1')));
    expect(consume).not.toHaveBeenCalled();
    expect(screen.queryByText(/Exception review completed/)).not.toBeInTheDocument();
  });

  it('does not leak approval token or nonce into rendered content or browser storage', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => (
      snapshot(id, 'REQUIRE_APPROVAL')
    ));
    vi.spyOn(api, 'issueApprovalToken').mockResolvedValue(tokenResult());
    vi.spyOn(api, 'consumeApprovalToken').mockResolvedValue({
      success: true, proposal_id: 'pr_1', reason: 'Review completed.',
    });
    const { container } = render(<ApprovalConsentPanel {...props({
      policyDecision: policyDecision('REQUIRE_APPROVAL'),
    })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    fireEvent.click(screen.getByRole('button', { name: /complete exception review/i }));
    await screen.findByText(/Exception review completed/);
    expect(container.textContent).not.toContain('never-render-this-token');
    expect(container.textContent).not.toContain('never-render-this-nonce');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('does not expose token or nonce values echoed by an authorization error', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => (
      snapshot(id, 'REQUIRE_APPROVAL')
    ));
    vi.spyOn(api, 'issueApprovalToken').mockRejectedValue(new Error(
      'Rejected token=never-render-this-token nonce=never-render-this-nonce',
    ));
    const { container } = render(<ApprovalConsentPanel {...props({
      policyDecision: policyDecision('REQUIRE_APPROVAL'),
    })} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    fireEvent.click(screen.getByRole('button', { name: /complete exception review/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/authorization request could not be completed/i);
    expect(container.textContent).not.toContain('never-render-this-token');
    expect(container.textContent).not.toContain('never-render-this-nonce');
  });

  it('does not display success when unmounted during a pending consent request', async () => {
    const pendingConsent = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization').mockImplementation(async (_token, id) => snapshot(id));
    vi.spyOn(api, 'recordPurchaseConsent').mockReturnValue(pendingConsent.promise);
    const view = render(<ApprovalConsentPanel {...props()} />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
    fireEvent.click(await screen.findByRole('button', { name: /confirm purchase consent/i }));
    expect(screen.getByText(/Confirming with the backend/)).toBeInTheDocument();
    view.unmount();
    await act(async () => pendingConsent.resolve(consentResult()));
    expect(screen.queryByText(/LEASH confirmed proposal status/)).not.toBeInTheDocument();
  });
});
