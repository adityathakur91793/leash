import { describe, expect, it, vi } from 'vitest';
import { getActivityOverview, validateActivityOverview } from '../services/activity';
import { DashboardOverview } from '../components/DashboardOverview';
import { ActivityAudit } from '../components/ActivityAudit';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import * as activityService from '../services/activity';
import { useActivityOverview } from '../hooks/useActivityOverview';

const overview = {
  items: [{ timestamp: '2026-11-08T10:00:00Z', event_type: 'PAYMENT_CAPTURED',
    entity_type: 'PAYMENT', actor: 'SYSTEM', summary: 'Payment settlement confirmed',
    display_name: 'Notebook', amount: '16.00', currency: 'USD' }],
  total: 1, has_more: false,
  mandate_status_counts: { DRAFT: 0, CONFIRMED: 0, SIGNED: 0, ACTIVE: 1, EXPIRED: 0, REVOKED: 0 },
  proposal_status_counts: { SUBMITTED: 0, EVALUATED: 0, APPROVED: 0, SETTLED: 1, REJECTED: 0, WITHDRAWN: 0 },
  confirmed_settlement_count: 1,
  unresolved_checkout_attempt_count: 0,
  unresolved_checkout_attempt_status_counts: { UNKNOWN: 0, PROVIDER_CREATED: 0, RECONCILIATION_REQUIRED: 0 },
  payment_status_counts: { CREATED: 0, PAYER_ACTION_REQUIRED: 0, APPROVED: 0, CAPTURING: 0,
    COMPLETED: 1, FAILED: 0, CANCELLED: 0, RECONCILIATION_REQUIRED: 0 },
  confirmed_settlement_totals: [{ currency: 'USD', amount: '16.00' }],
  hash_chain_valid: true,
  integrity_note: 'This checks audit-log hash-chain integrity only; it does not prove that underlying business events were correct.',
};

function response(value) {
  return { ok: true, status: 200, text: async () => JSON.stringify(value) };
}

describe('activity service and screens', () => {
  it('uses the authenticated shared endpoint and accepts the strict public projection', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(overview));
    vi.stubGlobal('fetch', fetchMock);
    await expect(getActivityOverview('private-session')).resolves.toEqual(overview);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/activity/overview');
    expect(fetchMock.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer private-session');
    expect(JSON.stringify(overview)).not.toContain('entry_hash');
  });

  it('rejects unexpected audit fields and malformed settlement totals', () => {
    expect(() => validateActivityOverview({ ...overview, global_payload: { key: 'secret' } })).toThrow(/invalid activity overview/i);
    expect(() => validateActivityOverview({ ...overview, confirmed_settlement_totals: [{ currency: 'USD', amount: '16.0' }] })).toThrow(/invalid activity overview/i);
    expect(() => validateActivityOverview({ ...overview, items: [{ ...overview.items[0], sequence_number: 4 }] })).toThrow(/invalid activity overview/i);
    expect(() => validateActivityOverview({ ...overview, unresolved_checkout_attempt_count: 2 })).toThrow(/invalid activity overview/i);
  });

  it('shows confirmed settlements and makes their audit integrity limitation clear', () => {
    const navigate = vi.fn();
    render(<DashboardOverview mandates={[{ status: 'ACTIVE' }]} proposals={[]} mandatesLoading={false} proposalsLoading={false} dataErrors={[]} activity={{ data: overview, loading: false, error: null }} onRefresh={vi.fn()} onNavigate={navigate} />);
    expect(screen.getByText('Confirmed settlements')).toBeInTheDocument();
    expect(screen.getByText('USD 16.00')).toBeInTheDocument();
    expect(screen.getByText(/not business correctness/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /view activity/i }));
    expect(navigate).toHaveBeenCalledWith('activity');
  });

  it('shows a real zero only when the authoritative overview reports zero', () => {
    const zeroOverview = {
      ...overview,
      confirmed_settlement_count: 0,
      confirmed_settlement_totals: [],
    };
    render(<DashboardOverview mandates={[]} proposals={[]} mandatesLoading={false} proposalsLoading={false} dataErrors={[]} activity={{ data: zeroOverview, loading: false, error: null }} onRefresh={vi.fn()} onNavigate={vi.fn()} />);
    expect(screen.getByText('Confirmed settlements').parentElement.querySelector('strong')).toHaveTextContent('0');
    expect(screen.getByText('No confirmed payments')).toBeInTheDocument();
  });

  it('shows settlement data as unavailable after an activity request failure', () => {
    render(<DashboardOverview mandates={[]} proposals={[]} mandatesLoading={false} proposalsLoading={false} dataErrors={[]} activity={{ data: null, loading: false, error: new Error('offline') }} onRefresh={vi.fn()} onNavigate={vi.fn()} />);
    expect(screen.getByText('Settlement data unavailable')).toBeInTheDocument();
    expect(screen.queryByText('No confirmed payments')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/unavailable/i);
  });

  it('shows settlement data as unavailable when the authoritative overview is missing or incomplete', () => {
    render(<DashboardOverview mandates={[]} proposals={[]} mandatesLoading={false} proposalsLoading={false} dataErrors={[]} activity={{ data: {}, loading: false, error: null }} onRefresh={vi.fn()} onNavigate={vi.fn()} />);
    expect(screen.getByText('Settlement data unavailable')).toBeInTheDocument();
    expect(screen.queryByText('No confirmed payments')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/account data is unavailable/i);
  });

  it('renders a read-only owner activity event without exposing identifiers or hashes', () => {
    const { container } = render(<ActivityAudit activity={{ data: overview, loading: false, error: null }} onRetry={vi.fn()} />);
    expect(screen.getByText('Audit hash chain verified')).toBeInTheDocument();
    expect(screen.getByText('Payment settlement confirmed')).toBeInTheDocument();
    expect(screen.getByText('Notebook · payment')).toBeInTheDocument();
    expect(container.textContent).not.toContain('entry_hash');
  });

  it('does not let a previous session response replace the current overview', async () => {
    let resolveOld;
    let resolveNew;
    vi.spyOn(activityService, 'getActivityOverview')
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockReturnValueOnce(new Promise((resolve) => { resolveNew = resolve; }));
    const { result, rerender } = renderHook(
      ({ token, identity }) => useActivityOverview(token, vi.fn(), identity),
      { initialProps: { token: 'session-one', identity: 'owner-one' } },
    );
    rerender({ token: 'session-two', identity: 'owner-two' });
    await act(async () => { resolveNew(overview); });
    expect(result.current.data).toEqual(overview);
    await act(async () => { resolveOld({ ...overview, total: 99, has_more: true }); });
    expect(result.current.data).toEqual(overview);
  });
});
