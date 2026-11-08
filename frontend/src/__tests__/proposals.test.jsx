import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useProposals } from '../hooks/useProposals';
import { ProposalInbox } from '../components/ProposalInbox';
import { ApiError } from '../services/api';
import * as api from '../services/proposals';

const proposal = { proposal_id: 'pr_1', mandate_id: 'man_1', product_id: 'p1', product_name: '<b>Unsafe</b>', merchant: 'shop.example', category: 'books', quantity: 2, unit_price: '4.00', currency: 'USD', tax: '0.00', shipping: '0.00', other_charges: '0.00', subtotal: '8.00', total: '8.00', product_source: 'https://shop.example/p1', status: 'SUBMITTED', created_at: '2030-01-01T00:00:00Z', fingerprint: 'hidden', content_hash: 'hidden' };

function rule(overrides = {}) {
  return {
    rule_id: 'P01', rule_name: 'Mandate status', status: 'PASS',
    reason: 'The mandate is active.', evidence: {}, evaluated_at: '2030-01-01T00:00:00Z',
    ...overrides,
  };
}

function policyDecision(overrides = {}) {
  return {
    proposal_id: 'pr_1', mandate_id: 'man_1', decision: 'ALLOW',
    decision_at: '2030-01-01T00:00:00Z', rules: [rule()], blocking_rules: [],
    approval_required_rules: [], is_one_time_exception: false, exception_types: [],
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

describe('proposal inbox', () => {
  it('loads owned proposals, selects details, and renders untrusted text without HTML execution', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    expect(await screen.findByText('<b>Unsafe</b>')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unsafe' })).not.toBeInTheDocument();
    expect(screen.getByText('The assessment data is not displayed in this screen yet.')).toBeInTheDocument();
    expect(screen.queryByText('hidden')).not.toBeInTheDocument();
  });

  it('keeps policy outcome backend supplied and renders BLOCK as blocking', async () => {
    vi.spyOn(api, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(api, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(api, 'evaluateProposal').mockResolvedValue(policyDecision({
      decision: 'BLOCK', rules: [rule({ status: 'BLOCK', reason: 'Outside policy.' })],
      blocking_rules: ['P01'],
    }));
    render(<Harness />);
    fireEvent.click(await screen.findByRole('button', { name: /Unsafe/ }));
    fireEvent.click(await screen.findByRole('button', { name: /evaluate with backend/i }));
    expect((await screen.findAllByText('BLOCK')).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Blocking rules: P01')).toBeInTheDocument();
    expect(screen.getByText('P01: Mandate status')).toBeInTheDocument();
    expect(screen.getByText(/not user approval/i)).toBeInTheDocument();
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
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid policy evaluation/i);
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
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid policy evaluation/i);
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
