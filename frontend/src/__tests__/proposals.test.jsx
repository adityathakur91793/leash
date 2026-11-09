import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { useProposals } from '../hooks/useProposals';
import { ProposalInbox } from '../components/ProposalInbox';
import { ApiError } from '../services/api';
import * as api from '../services/proposals';

const proposal = { proposal_id: 'pr_1', mandate_id: 'man_1', product_id: 'p1', product_name: '<b>Unsafe</b>', merchant: 'shop.example', category: 'books', quantity: 2, unit_price: '4.00', currency: 'USD', tax: '0.00', shipping: '0.00', other_charges: '0.00', subtotal: '8.00', total: '8.00', product_source: 'https://shop.example/p1', status: 'SUBMITTED', created_at: '2030-01-01T00:00:00Z', fingerprint: 'hidden', content_hash: 'hidden' };

const publicChecks = [
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

function rule(index, outcome = 'PASS') {
  const explanation = {
    PASS: 'This check passed.',
    BLOCK: 'This check blocks the proposal.',
    REQUIRE_APPROVAL: 'This check requires separate user review.',
    NOT_APPLICABLE: 'This check does not apply to this proposal.',
  }[outcome];
  return {
    rule_id: `P${String(index).padStart(2, '0')}`,
    outcome,
    severity: outcome === 'BLOCK' ? 'BLOCKING'
      : outcome === 'REQUIRE_APPROVAL' ? 'REVIEW' : 'INFORMATIONAL',
    explanation: `${publicChecks[index - 1]} ${explanation}`,
    requires_user_review: outcome === 'REQUIRE_APPROVAL',
  };
}

function policyDecision(overrides = {}) {
  const decision = overrides.decision || 'ALLOW';
  const rules = overrides.rules || Array.from({ length: 12 }, (_, index) => (
    decision === 'BLOCK' && index === 0 ? rule(index + 1, 'BLOCK')
      : decision === 'REQUIRE_APPROVAL' && index === 3
        ? rule(index + 1, 'REQUIRE_APPROVAL') : rule(index + 1)
  ));
  return {
    decision, rules,
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function Harness({ onState }) {
  const state = useProposals('session-token', vi.fn());
  onState?.(state);
  return <ProposalInbox proposals={state} />;
}

function AuthorizationLifecycleHarness({ sessionToken = 'session-one', sessionIdentity = 'user-one' }) {
  const [selectedId, setSelectedId] = useState('pr_1');
  const [selectionRevision, setSelectionRevision] = useState(0);
  const selectedProposal = { ...proposal, proposal_id: selectedId, product_name: `Product ${selectedId}` };
  const assessment = {
    status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
    created_at: '2030-01-01T00:00:00Z', is_current: true,
  };
  const state = {
    items: [proposal, { ...proposal, proposal_id: 'pr_2', product_name: 'Product pr_2' }],
    selectedId, selectionRevision, detail: { ...selectedProposal, status: 'EVALUATED' },
    decision: policyDecision(), loading: false, detailLoading: false, evaluating: false,
    error: null,
    intentAssessment: { assessment, loaded: true, loading: false, unavailable: false, error: null },
    refresh: vi.fn(), evaluate: vi.fn(),
    select: (id) => { setSelectedId(id); setSelectionRevision((value) => value + 1); },
  };
  return <ProposalInbox proposals={state} sessionToken={sessionToken} sessionIdentity={sessionIdentity} />;
}

describe('proposal inbox', () => {
  it('invalidates a snapshot when the same proposal is reopened and requires a fresh read', async () => {
    const first = deferred();
    const second = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization')
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const view = render(<AuthorizationLifecycleHarness />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await act(async () => first.resolve({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED',
      policyDecision: policyDecision(),
      assessment: { status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
        created_at: '2030-01-01T00:00:00Z', is_current: true },
    }));
    expect(screen.getByText('Current assessment')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Unsafe/ }));
    expect(screen.getByText('Not current')).toBeInTheDocument();
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    await act(async () => second.resolve({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED',
      policyDecision: policyDecision(),
      assessment: { status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
        created_at: '2030-01-01T00:00:00Z', is_current: true },
    }));
    expect(screen.getByText('Current assessment')).toBeInTheDocument();
    view.unmount();
  });

  it('does not restore a previous session snapshot from an in-flight refresh', async () => {
    const pending = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization').mockReturnValue(pending.promise);
    const view = render(<AuthorizationLifecycleHarness />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    view.rerender(<AuthorizationLifecycleHarness sessionToken="session-two" sessionIdentity="user-two" />);
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    await act(async () => pending.resolve({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: policyDecision(),
      assessment: { status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
        created_at: '2030-01-01T00:00:00Z', is_current: true },
    }));
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    expect(screen.getByText('Not current')).toBeInTheDocument();
    view.unmount();
  });

  it('clears cached authorization display when the session expires', async () => {
    vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: policyDecision(),
      assessment: { status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
        created_at: '2030-01-01T00:00:00Z', is_current: true },
    });
    const view = render(<AuthorizationLifecycleHarness />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    view.rerender(<AuthorizationLifecycleHarness sessionToken={null} sessionIdentity="user-one" />);
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    expect(screen.getByText('Not current')).toBeInTheDocument();
  });

  it('ignores proposal A refresh when switching to proposal B', async () => {
    const pending = deferred();
    vi.spyOn(api, 'refreshProposalAuthorization').mockReturnValue(pending.promise);
    render(<AuthorizationLifecycleHarness />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    fireEvent.click(screen.getByRole('button', { name: /Product pr_2/ }));
    expect(screen.getByText('Not current')).toBeInTheDocument();
    await act(async () => pending.resolve({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED', policyDecision: policyDecision(),
      assessment: { status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
        created_at: '2030-01-01T00:00:00Z', is_current: true },
    }));
    expect(screen.queryByText('Current assessment')).not.toBeInTheDocument();
    expect(screen.getByText('Not current')).toBeInTheDocument();
  });
  it('loads owned proposals, selects details, and renders untrusted text without HTML execution', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    expect(await screen.findByText('<b>Unsafe</b>')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unsafe' })).not.toBeInTheDocument();
    expect(screen.queryByText(/proposal API does not expose persisted auditor/i)).not.toBeInTheDocument();
    expect(screen.queryByText('hidden')).not.toBeInTheDocument();
  });

  it('explains that consent, PayPal approval, and verified settlement are separate', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    expect(await screen.findByText(/purchase consent records your choice/i)).toBeInTheDocument();
    expect(screen.getByText(/payPal checkout and buyer approval happen separately/i)).toBeInTheDocument();
    expect(screen.getByText(/payment is complete only after leash verifies settlement with paypal/i)).toBeInTheDocument();
  });

  it('keeps policy outcome backend supplied and renders BLOCK as blocking', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({
      decision: 'BLOCK',
    }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect((await screen.findAllByText('BLOCK')).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Blocking rules: P01')).toBeInTheDocument();
    expect(screen.getByText('P01')).toBeInTheDocument();
    expect(screen.getByText(/Checks mandate signature integrity\. This check blocks the proposal\./)).toBeInTheDocument();
    expect(screen.getByText(/not user approval/i)).toBeInTheDocument();
  });

  it('updates proposal status only after the consent response is confirmed by the backend', async () => {
    const freshAssessment = {
      status: 'VALID', risk_level: 'LOW', finding_codes: [], affected_fields: [],
      created_at: '2030-01-01T00:00:00Z', is_current: true,
    };
    vi.spyOn(api, 'refreshProposalAuthorization').mockResolvedValue({
      proposalId: 'pr_1', proposalStatus: 'EVALUATED',
      policyDecision: policyDecision(), assessment: freshAssessment,
    });
    vi.spyOn(api, 'recordPurchaseConsent').mockResolvedValue({
      proposal_id: 'pr_1', status: 'APPROVED', policy_decision: 'ALLOW',
      amount: '8.00', currency: 'USD', consented_at: '2030-01-01T00:00:00Z',
      idempotent: false,
    });
    render(<ProposalInbox
      proposals={{
        items: [proposal], selectedId: 'pr_1', detail: { ...proposal, status: 'EVALUATED' },
        decision: policyDecision(), loading: false, detailLoading: false, evaluating: false,
        error: null, intentAssessment: {
          assessment: freshAssessment, loaded: true, loading: false, unavailable: false,
          error: null, refresh: vi.fn(),
        },
        refresh: vi.fn(), select: vi.fn(), evaluate: vi.fn(),
      }}
      sessionToken="session-token"
      sessionIdentity="user-1"
      onSessionExpired={vi.fn()}
    />);
    fireEvent.click(screen.getByRole('button', { name: /refresh policy and assessment/i }));
    await screen.findByText('Current assessment');
    fireEvent.click(screen.getByRole('button', { name: /continue to purchase consent/i }));
    fireEvent.click(await screen.findByRole('button', { name: /confirm purchase consent/i }));
    expect(await screen.findByText(/LEASH confirmed proposal status: APPROVED/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('APPROVED')).toBeInTheDocument());
  });

  it('shows empty state and prevents stale detail from replacing a newer selection', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal, { ...proposal, proposal_id: 'pr_2', product_name: 'Second' }]);
    let resolveFirst;
    vi.spyOn(api, 'getProposal').mockImplementation((_, id) => id === 'pr_1' ? new Promise((resolve) => { resolveFirst = resolve; }) : Promise.resolve({ ...proposal, proposal_id: 'pr_2', product_name: 'Second' }));
    const view = render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(screen.getByRole('button', { name: /Second/ }));
    expect(await screen.findByRole('heading', { name: 'Second' })).toBeInTheDocument();
    await act(async () => resolveFirst(proposal));
    expect(screen.getByRole('heading', { name: 'Second' })).toBeInTheDocument();
    view.unmount();
  });

  it('refreshes authoritative detail and inbox after evaluation', async () => {
    const updated = { ...proposal, status: 'EVALUATED' };
    vi.spyOn(api, 'listProposals').mockResolvedValueOnce([proposal]).mockResolvedValueOnce([updated]);
    vi.spyOn(api, 'getProposal').mockResolvedValueOnce(proposal).mockResolvedValueOnce(updated);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({
      decision: 'REQUIRE_APPROVAL',
    }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect((await screen.findAllByText('EVALUATED')).length).toBeGreaterThanOrEqual(2);
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
    expect(api.getProposal).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/not user approval/i)).toBeInTheDocument();
  });

  it('shows a contract error instead of an empty state for malformed list responses', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue({ items: [] });
    render(<Harness />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid proposal list/i);
    expect(screen.queryByText('No proposals found')).not.toBeInTheDocument();
  });

  it.each([
    ['null entry', [null]],
    ['primitive entry', ['proposal']],
    ['missing proposal ID', [{ ...proposal, proposal_id: '' }]],
    ['unknown lifecycle status', [{ ...proposal, status: 'UNKNOWN' }]],
  ])('rejects a malformed list entry (%s) rather than showing partial data', async (_label, value) => {
    vi.spyOn(api, 'listProposals').mockResolvedValue(value);
    render(<Harness />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid proposal list/i);
    expect(screen.queryByText('No proposals found')).not.toBeInTheDocument();
    expect(screen.queryByText('Unsafe')).not.toBeInTheDocument();
  });

  it('rejects malformed proposal detail before rendering it', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue({ ...proposal, total: null });
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid proposal detail/i);
    expect(screen.queryByText('No proposals found')).not.toBeInTheDocument();
  });

  it('does not surface a failed detail request after selection moved to another proposal', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal, { ...proposal, proposal_id: 'pr_2', product_name: 'Second' }]);
    let rejectFirst;
    vi.spyOn(api, 'getProposal').mockImplementation((_, id) => id === 'pr_1'
      ? new Promise((_, reject) => { rejectFirst = reject; })
      : Promise.resolve({ ...proposal, proposal_id: 'pr_2', product_name: 'Second' }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(screen.getByRole('button', { name: /Second/ }));
    expect(await screen.findByRole('heading', { name: 'Second' })).toBeInTheDocument();
    await act(async () => rejectFirst(new Error('stale request failed')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Second' })).toBeInTheDocument();
  });

  it('does not surface a failed evaluation after selection moved to another proposal', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal, { ...proposal, proposal_id: 'pr_2', product_name: 'Second' }]);
    vi.spyOn(api, 'getProposal').mockImplementation((_, id) => Promise.resolve(
      id === 'pr_1' ? proposal : { ...proposal, proposal_id: 'pr_2', product_name: 'Second' },
    ));
    let rejectEvaluation;
    vi.spyOn(api, 'evaluateProposal').mockImplementation(() => new Promise((_, reject) => {
      rejectEvaluation = reject;
    }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    await waitFor(() => expect(api.evaluateProposal).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /Second/ }));
    expect(await screen.findByRole('heading', { name: 'Second' })).toBeInTheDocument();
    await act(async () => rejectEvaluation(new Error('stale evaluation failed')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Second' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /evaluate with backend/i })).toBeEnabled();
  });

  it('labels REQUIRE_APPROVAL as review required without presenting it as approval', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({ decision: 'REQUIRE_APPROVAL' }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect(await screen.findByText('Review required')).toBeInTheDocument();
    expect(screen.queryByText('REQUIRE_APPROVAL')).not.toBeInTheDocument();
    expect(screen.getByText(/review required is not approval/i)).toBeInTheDocument();
  });

  it('rejects an unknown evaluation decision without crashing or displaying a decision', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({ decision: 'MAYBE' }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid authorization policy decision/i);
    expect(screen.queryByText('MAYBE')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '<b>Unsafe</b>' })).toBeInTheDocument();
  });

  it('rejects malformed evaluation rules without storing the decision', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({ rules: [{ rule_id: 'P01' }] }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid authorization policy decision/i);
    expect(screen.queryByText('ALLOW')).not.toBeInTheDocument();
  });

  it('ignores an older list error after a newer refresh succeeds', async () => {
    let rejectFirst;
    let resolveSecond;
    let state;
    let call = 0;
    vi.spyOn(api, 'listProposals').mockImplementation(() => {
      call += 1;
      if (call === 1) return new Promise((_, reject) => { rejectFirst = reject; });
      return new Promise((resolve) => { resolveSecond = resolve; });
    });
    render(<Harness onState={(value) => { state = value; }} />);
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(1));

    await act(async () => { void state.refresh(); });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
    await act(async () => resolveSecond([{ ...proposal, product_name: 'Newer proposal' }]));
    expect(await screen.findByText('Newer proposal')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /refresh/i })).toBeEnabled();

    await act(async () => rejectFirst(new Error('old refresh failed')));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText('Newer proposal')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('keeps newer manual-refresh data when an older evaluation refresh succeeds', async () => {
    const evaluationList = deferred();
    const manualList = deferred();
    let state;
    let listCalls = 0;
    vi.spyOn(api, 'listProposals').mockImplementation(() => {
      listCalls += 1;
      if (listCalls === 1) return Promise.resolve([proposal]);
      return listCalls === 2 ? evaluationList.promise : manualList.promise;
    });
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision());
    render(<Harness onState={(value) => { state = value; }} />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    await screen.findByRole('heading', { name: '<b>Unsafe</b>' });
    fireEvent.click(screen.getByRole('button', { name: /evaluate with backend/i }));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));

    await act(async () => { void state.refresh(); });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(3));
    await act(async () => manualList.resolve([{ ...proposal, product_name: 'Newer manual result' }]));
    expect(await screen.findByText('Newer manual result')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    await act(async () => evaluationList.resolve([{ ...proposal, product_name: 'Older evaluation result' }]));
    expect(screen.getByText('Newer manual result')).toBeInTheDocument();
    expect(screen.queryByText('Older evaluation result')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('ignores a failed evaluation refresh after a newer manual refresh succeeds', async () => {
    const evaluationList = deferred();
    const manualList = deferred();
    let state;
    let listCalls = 0;
    vi.spyOn(api, 'listProposals').mockImplementation(() => {
      listCalls += 1;
      if (listCalls === 1) return Promise.resolve([proposal]);
      return listCalls === 2 ? evaluationList.promise : manualList.promise;
    });
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision());
    render(<Harness onState={(value) => { state = value; }} />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    await screen.findByRole('heading', { name: '<b>Unsafe</b>' });
    fireEvent.click(screen.getByRole('button', { name: /evaluate with backend/i }));
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));

    await act(async () => { void state.refresh(); });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(3));
    await act(async () => manualList.resolve([{ ...proposal, product_name: 'Newer manual result' }]));
    expect(await screen.findByText('Newer manual result')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    await act(async () => evaluationList.reject(new Error('stale evaluation list failed')));
    expect(screen.getByText('Newer manual result')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('ignores a pending list success after the hook unmounts', async () => {
    const initialRequest = deferred();
    const manualRequest = deferred();
    let renderCount = 0;
    vi.spyOn(api, 'listProposals')
      .mockImplementationOnce(() => initialRequest.promise)
      .mockImplementationOnce(() => manualRequest.promise);
    const hook = renderHook(({ token }) => {
      renderCount += 1;
      return useProposals(token, vi.fn());
    }, { initialProps: { token: null } });

    hook.rerender({ token: 'active-session' });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(1));
    const refreshResult = hook.result.current.refresh();
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
    hook.unmount();
    const rendersAtUnmount = renderCount;

    await act(async () => {
      initialRequest.resolve([proposal]);
      manualRequest.resolve([{ ...proposal, product_name: 'Late list result' }]);
    });
    await expect(refreshResult).resolves.toEqual({ status: 'stale' });
    expect(renderCount).toBe(rendersAtUnmount);
  });

  it('does not expire an unmounted session when a pending list request returns 401', async () => {
    const initialRequest = deferred();
    const manualRequest = deferred();
    const onSessionExpired = vi.fn();
    let renderCount = 0;
    vi.spyOn(api, 'listProposals')
      .mockImplementationOnce(() => initialRequest.promise)
      .mockImplementationOnce(() => manualRequest.promise);
    const hook = renderHook(({ token }) => {
      renderCount += 1;
      return useProposals(token, onSessionExpired);
    }, { initialProps: { token: null } });

    hook.rerender({ token: 'active-session' });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(1));
    const refreshResult = hook.result.current.refresh();
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(2));
    hook.unmount();
    const rendersAtUnmount = renderCount;

    await act(async () => {
      initialRequest.reject(new ApiError('Unauthenticated', { status: 401, code: 'unauthenticated' }));
      manualRequest.reject(new ApiError('Unauthenticated', { status: 401, code: 'unauthenticated' }));
    });
    await expect(refreshResult).resolves.toEqual({ status: 'stale' });
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(renderCount).toBe(rendersAtUnmount);
  });

  it('expires a mounted session for a current list 401 response', async () => {
    const request = deferred();
    const onSessionExpired = vi.fn();
    vi.spyOn(api, 'listProposals').mockImplementationOnce(() => request.promise);
    const hook = renderHook(({ token }) => useProposals(token, onSessionExpired), {
      initialProps: { token: null },
    });

    hook.rerender({ token: 'active-session' });
    await waitFor(() => expect(api.listProposals).toHaveBeenCalledTimes(1));
    await act(async () => {
      request.reject(new ApiError('Unauthenticated', { status: 401, code: 'unauthenticated' }));
    });
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(hook.result.current.error).toMatchObject({ status: 401, code: 'unauthenticated' });
  });
});
