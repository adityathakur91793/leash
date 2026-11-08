import { StrictMode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../services/api';
import {
  getIntentAssessment,
  validateIntentAssessmentResponse,
} from '../services/proposals';
import { useIntentAssessment } from '../hooks/useIntentAssessment';
import { useProposals } from '../hooks/useProposals';
import { ProposalInbox } from '../components/ProposalInbox';
import * as proposalApi from '../services/proposals';

const currentAssessment = {
  status: 'VALID',
  risk_level: 'LOW',
  finding_codes: ['BUDGET_TENSION'],
  affected_fields: ['mandate_budget'],
  created_at: '2030-01-01T00:00:00+00:00',
  is_current: true,
};

const proposal = {
  proposal_id: 'pr_1', mandate_id: 'man_1', product_id: 'p1',
  product_name: 'Notebook', merchant: 'shop.example', category: 'books',
  quantity: 1, unit_price: '4.00', currency: 'USD', tax: '0.00',
  shipping: '0.00', other_charges: '0.00', subtotal: '4.00', total: '4.00',
  product_source: 'https://shop.example/p1', status: 'SUBMITTED',
  created_at: '2030-01-01T00:00:00Z', fingerprint: 'hidden', content_hash: 'hidden',
};

function payload(proposalId = 'pr_1', assessment = currentAssessment) {
  return { proposal_id: proposalId, assessment };
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

function httpResponse(status, body, contentType = 'application/json') {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers({ 'Content-Type': contentType }),
    text: vi.fn().mockResolvedValue(body),
  };
}

describe('Intent Auditor assessment service contract', () => {
  it('validates and returns the minimized current, stale, failed, and null contracts', () => {
    expect(validateIntentAssessmentResponse(payload(), 'pr_1')).toEqual(currentAssessment);
    expect(validateIntentAssessmentResponse(payload('pr_1', {
      ...currentAssessment, status: 'STALE', risk_level: 'UNCERTAIN', is_current: false,
    }), 'pr_1')).toMatchObject({ status: 'STALE', is_current: false });
    expect(validateIntentAssessmentResponse(payload('pr_1', {
      ...currentAssessment, status: 'FAILED', risk_level: 'UNCERTAIN', is_current: false,
    }), 'pr_1')).toMatchObject({ status: 'FAILED', risk_level: 'UNCERTAIN' });
    expect(validateIntentAssessmentResponse(payload('pr_1', null), 'pr_1')).toBeNull();
  });

  it.each([
    ['wrong proposal ID', { ...payload(), proposal_id: 'pr_2' }],
    ['missing top-level property', { proposal_id: 'pr_1' }],
    ['unexpected internal field', { ...payload(), assessment_id: 'internal-id' }],
    ['invalid status', payload('pr_1', { ...currentAssessment, status: 'UNKNOWN' })],
    ['invalid risk enum', payload('pr_1', { ...currentAssessment, risk_level: 'CRITICAL' })],
    ['invalid finding code', payload('pr_1', { ...currentAssessment, finding_codes: ['MADE_UP'] })],
    ['non-array findings', payload('pr_1', { ...currentAssessment, finding_codes: null })],
    ['invalid affected field', payload('pr_1', { ...currentAssessment, affected_fields: ['owner_id'] })],
    ['non-array affected fields', payload('pr_1', { ...currentAssessment, affected_fields: {} })],
    ['invalid calendar date', payload('pr_1', { ...currentAssessment, created_at: '2030-02-30T00:00:00Z' })],
    ['timestamp without timezone', payload('pr_1', { ...currentAssessment, created_at: '2030-01-01T00:00:00' })],
    ['non-boolean freshness', payload('pr_1', { ...currentAssessment, is_current: 'true' })],
    ['unexpected assessment field', payload('pr_1', { ...currentAssessment, mandate_id: 'man_1' })],
    ['malformed top-level response', null],
  ])('rejects %s as an invalid response', (_label, value) => {
    expect(() => validateIntentAssessmentResponse(value, 'pr_1')).toThrow(
      expect.objectContaining({ code: 'invalid-response' }),
    );
  });

  it('uses the shared authenticated client and safely encodes the selected ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(httpResponse(
      200, JSON.stringify(payload('pr_1', currentAssessment)),
    ));
    vi.stubGlobal('fetch', fetchMock);
    await expect(getIntentAssessment('private-session', 'pr_1')).resolves.toEqual(currentAssessment);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/proposals/pr_1/auditor-assessment');
    expect(fetchMock.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer private-session');

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(httpResponse(
      200, JSON.stringify(payload('pr%2Fa', currentAssessment)),
    )));
    await expect(getIntentAssessment('private-session', 'pr/a')).rejects.toMatchObject({
      code: 'invalid-response',
    });
    expect(fetch.mock.calls[0][0]).toContain('/pr%2Fa/auditor-assessment');
  });

  it('preserves distinct 401 and 403 errors and rejects non-JSON success bodies', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(httpResponse(401, '{"detail":"expired"}'))
      .mockResolvedValueOnce(httpResponse(403, '{"detail":"forbidden"}'))
      .mockResolvedValueOnce(httpResponse(200, '<html>not json</html>', 'text/html')));
    await expect(getIntentAssessment('token', 'pr_1')).rejects.toMatchObject({
      status: 401, code: 'unauthenticated',
    });
    await expect(getIntentAssessment('token', 'pr_1')).rejects.toMatchObject({
      status: 403, code: 'forbidden',
    });
    await expect(getIntentAssessment('token', 'pr_1')).rejects.toMatchObject({
      code: 'invalid-response',
    });
  });
});

describe('useIntentAssessment', () => {
  it('does not request an assessment without an active token and selected proposal', () => {
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment').mockResolvedValue(null);
    const hook = renderHook(({ token, id }) => useIntentAssessment(token, vi.fn(), id), {
      initialProps: { token: null, id: 'pr_1' },
    });
    expect(getAssessment).not.toHaveBeenCalled();
    expect(hook.result.current.loading).toBe(false);
    hook.rerender({ token: 'active-token', id: null });
    expect(getAssessment).not.toHaveBeenCalled();
  });

  it('loads a valid current assessment and exposes null as unavailable without interpreting why', async () => {
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockResolvedValueOnce(currentAssessment)
      .mockResolvedValueOnce(null);
    const hook = renderHook(({ id }) => useIntentAssessment('session-token', vi.fn(), id), {
      initialProps: { id: 'pr_1' },
    });
    await waitFor(() => expect(hook.result.current.assessment).toEqual(currentAssessment));
    expect(hook.result.current.unavailable).toBe(false);

    hook.rerender({ id: 'pr_2' });
    await waitFor(() => expect(hook.result.current.loaded).toBe(true));
    expect(hook.result.current.assessment).toBeNull();
    expect(hook.result.current.unavailable).toBe(true);
    expect(getAssessment).toHaveBeenCalledTimes(2);
  });

  it('retains valid STALE and FAILED/UNCERTAIN records as auditor data, separate from policy', async () => {
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockResolvedValueOnce({ ...currentAssessment, status: 'STALE', is_current: false })
      .mockResolvedValueOnce({
        ...currentAssessment, status: 'FAILED', risk_level: 'UNCERTAIN', is_current: false,
      });
    const hook = renderHook(({ id }) => useIntentAssessment('token', vi.fn(), id), {
      initialProps: { id: 'pr_1' },
    });
    await waitFor(() => expect(hook.result.current.assessment?.status).toBe('STALE'));
    expect(hook.result.current.assessment.is_current).toBe(false);
    hook.rerender({ id: 'pr_2' });
    await waitFor(() => expect(hook.result.current.assessment?.status).toBe('FAILED'));
    expect(hook.result.current.assessment.risk_level).toBe('UNCERTAIN');
  });

  it('ignores a stale success when selection moves from proposal A to B', async () => {
    const requestA = deferred();
    const requestB = deferred();
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(requestA.promise).mockReturnValueOnce(requestB.promise);
    const hook = renderHook(({ id }) => useIntentAssessment('token', vi.fn(), id), {
      initialProps: { id: 'pr_1' },
    });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(1));
    hook.rerender({ id: 'pr_2' });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(2));
    await act(async () => requestB.resolve({ ...currentAssessment, risk_level: 'HIGH' }));
    expect(hook.result.current.assessment.risk_level).toBe('HIGH');
    await act(async () => requestA.resolve(currentAssessment));
    expect(hook.result.current.assessment.risk_level).toBe('HIGH');
  });

  it('ignores stale failures and stale 401s after selection or session changes', async () => {
    const requestA = deferred();
    const requestB = deferred();
    const onSessionExpired = vi.fn();
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(requestA.promise).mockReturnValueOnce(requestB.promise);
    const hook = renderHook(({ id, token }) => useIntentAssessment(token, onSessionExpired, id), {
      initialProps: { id: 'pr_1', token: 'old-token' },
    });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(1));
    hook.rerender({ id: 'pr_2', token: 'new-token' });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(2));
    await act(async () => requestB.resolve(currentAssessment));
    await act(async () => requestA.reject(new ApiError('Unauthenticated', {
      status: 401, code: 'unauthenticated',
    })));
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(hook.result.current.assessment).toEqual(currentAssessment);
    expect(hook.result.current.error).toBeNull();
  });

  it('does not expire the session for a stale 401 after selection changes', async () => {
    const requestA = deferred();
    const requestB = deferred();
    const onSessionExpired = vi.fn();
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(requestA.promise).mockReturnValueOnce(requestB.promise);
    const hook = renderHook(({ id }) => useIntentAssessment('same-token', onSessionExpired, id), {
      initialProps: { id: 'pr_1' },
    });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(1));
    hook.rerender({ id: 'pr_2' });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(2));
    await act(async () => requestB.resolve(currentAssessment));
    await act(async () => requestA.reject(new ApiError('Unauthenticated', {
      status: 401, code: 'unauthenticated',
    })));
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(hook.result.current.assessment).toEqual(currentAssessment);
  });

  it('ignores an old failure after an explicit session generation changes', async () => {
    const oldRequest = deferred();
    const freshRequest = deferred();
    const onSessionExpired = vi.fn();
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(freshRequest.promise);
    const hook = renderHook(({ generation }) => useIntentAssessment(
      'same-token', onSessionExpired, 'pr_1', 0, generation,
    ), { initialProps: { generation: 1 } });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(1));
    hook.rerender({ generation: 2 });
    await waitFor(() => expect(proposalApi.getIntentAssessment).toHaveBeenCalledTimes(2));
    await act(async () => freshRequest.resolve(currentAssessment));
    await act(async () => oldRequest.reject(new ApiError('Unauthenticated', {
      status: 401, code: 'unauthenticated',
    })));
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(hook.result.current.assessment).toEqual(currentAssessment);
  });

  it('does not call session expiry after a pending 401 resolves following unmount', async () => {
    const request = deferred();
    const onSessionExpired = vi.fn();
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment').mockReturnValue(request.promise);
    let renderCount = 0;
    const hook = renderHook(() => {
      renderCount += 1;
      return useIntentAssessment('token', onSessionExpired, 'pr_1');
    });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(1));
    const refreshAfterUnmount = hook.result.current.refresh;
    hook.unmount();
    const rendersAtUnmount = renderCount;
    await act(async () => request.reject(new ApiError('Unauthenticated', {
      status: 401, code: 'unauthenticated',
    })));
    await expect(refreshAfterUnmount()).resolves.toEqual({ status: 'idle' });
    expect(onSessionExpired).not.toHaveBeenCalled();
    expect(getAssessment).toHaveBeenCalledTimes(1);
    expect(renderCount).toBe(rendersAtUnmount);
  });

  it('expires the active session for a current 401 but not for a 403', async () => {
    const onSessionExpired = vi.fn();
    vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockRejectedValueOnce(new ApiError('Unauthenticated', { status: 401, code: 'unauthenticated' }))
      .mockRejectedValueOnce(new ApiError('Forbidden', { status: 403, code: 'forbidden' }));
    const hook = renderHook(({ id }) => useIntentAssessment('token', onSessionExpired, id), {
      initialProps: { id: 'pr_1' },
    });
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    hook.rerender({ id: 'pr_2' });
    await waitFor(() => expect(hook.result.current.error?.status).toBe(403));
    expect(onSessionExpired).toHaveBeenCalledTimes(1);
  });

  it('manual refresh generations ignore an older success and reject stale loading state', async () => {
    const first = deferred();
    const second = deferred();
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    let state;
    const hook = renderHook(() => {
      state = useIntentAssessment('token', vi.fn(), 'pr_1');
      return state;
    });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(1));
    act(() => { void state.refresh(); });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve({ ...currentAssessment, risk_level: 'HIGH' }));
    expect(hook.result.current.loading).toBe(false);
    await act(async () => first.resolve(currentAssessment));
    expect(hook.result.current.assessment.risk_level).toBe('HIGH');
    expect(hook.result.current.loading).toBe(false);
  });

  it('manual refresh generations ignore an older failure after newer data succeeds', async () => {
    const first = deferred();
    const second = deferred();
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    let state;
    const hook = renderHook(() => {
      state = useIntentAssessment('token', vi.fn(), 'pr_1');
      return state;
    });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(1));
    act(() => { void state.refresh(); });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve(currentAssessment));
    await act(async () => first.reject(new Error('stale assessment refresh failed')));
    expect(hook.result.current.assessment).toEqual(currentAssessment);
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.loading).toBe(false);
  });

  it('refreshes assessment after a successful proposal evaluation', async () => {
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockResolvedValueOnce(null).mockResolvedValueOnce(currentAssessment);
    vi.spyOn(proposalApi, 'listProposals').mockResolvedValue([proposal]);
    vi.spyOn(proposalApi, 'getProposal').mockResolvedValue(proposal);
    vi.spyOn(proposalApi, 'evaluateProposal').mockResolvedValue({
      proposal_id: 'pr_1', mandate_id: 'man_1', decision: 'ALLOW',
      decision_at: '2030-01-01T00:00:00Z', rules: [], blocking_rules: [],
      approval_required_rules: [], is_one_time_exception: false, exception_types: [],
    });
    const onSessionExpired = vi.fn();
    const hook = renderHook(() => {
      const proposals = useProposals('token', onSessionExpired);
      const assessment = useIntentAssessment(
        'token', onSessionExpired, proposals.selectedId, proposals.evaluationRevision,
      );
      return { proposals, assessment };
    });
    await waitFor(() => expect(hook.result.current.proposals.items).toHaveLength(1));
    act(() => hook.result.current.proposals.select('pr_1'));
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(hook.result.current.assessment.loaded).toBe(true));
    await act(async () => { await hook.result.current.proposals.evaluate(); });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(hook.result.current.assessment.assessment).toEqual(currentAssessment));
  });

  it('keeps the rendered assessment attached to the selected proposal during A-to-B races', async () => {
    const requestA = deferred();
    const listRequest = deferred();
    const proposalB = { ...proposal, proposal_id: 'pr_2', product_name: 'Second proposal' };
    const assessmentB = {
      ...currentAssessment,
      risk_level: 'HIGH',
      finding_codes: ['MERCHANT_MISMATCH'],
      affected_fields: ['merchant'],
    };
    const listProposals = vi.spyOn(proposalApi, 'listProposals').mockReturnValue(listRequest.promise);
    vi.spyOn(proposalApi, 'getProposal').mockImplementation((_, id) => (
      Promise.resolve(id === 'pr_1' ? proposal : proposalB)
    ));
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment').mockImplementation((_, id) => (
      id === 'pr_1' ? requestA.promise : Promise.resolve(assessmentB)
    ));

    function ReviewHarness() {
      const proposals = useProposals('session-token', vi.fn());
      const assessment = useIntentAssessment(
        'session-token', vi.fn(), proposals.selectedId, proposals.evaluationRevision,
      );
      return <ProposalInbox proposals={{ ...proposals, intentAssessment: assessment }} />;
    }

    render(<ReviewHarness />);
    await waitFor(() => expect(listProposals).toHaveBeenCalledTimes(1));
    await act(async () => listRequest.resolve([proposal, proposalB]));
    fireEvent.click(screen.getByRole('button', { name: /Notebook/ }));
    await screen.findByRole('heading', { name: 'Notebook' });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: /Second proposal/ }));
    await screen.findByRole('heading', { name: 'Second proposal' });
    expect(await screen.findByText('Risk level: High')).toBeInTheDocument();
    expect(screen.getByText('MERCHANT_MISMATCH')).toBeInTheDocument();
    expect(screen.queryByText('BUDGET_TENSION')).not.toBeInTheDocument();

    await act(async () => requestA.resolve(currentAssessment));
    expect(screen.getByRole('heading', { name: 'Second proposal' })).toBeInTheDocument();
    expect(screen.getByText('MERCHANT_MISMATCH')).toBeInTheDocument();
    expect(screen.queryByText('BUDGET_TENSION')).not.toBeInTheDocument();
    expect(getAssessment).toHaveBeenCalledTimes(2);
  });

  it('ignores the first StrictMode effect lifecycle response', async () => {
    const first = deferred();
    const second = deferred();
    const getAssessment = vi.spyOn(proposalApi, 'getIntentAssessment')
      .mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const hook = renderHook(() => useIntentAssessment('token', vi.fn(), 'pr_1'), {
      wrapper: StrictMode,
    });
    await waitFor(() => expect(getAssessment).toHaveBeenCalledTimes(2));
    await act(async () => first.resolve({ ...currentAssessment, risk_level: 'HIGH' }));
    expect(hook.result.current.assessment).toBeNull();
    await act(async () => second.resolve(currentAssessment));
    expect(hook.result.current.assessment).toEqual(currentAssessment);
  });
});
