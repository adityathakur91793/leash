import { afterEach, describe, expect, it, vi } from 'vitest';
import { getCheckoutState, recoverProposalCheckout } from '../services/payments';

function response(status, body) {
  return {
    status, ok: status >= 200 && status < 300,
    headers: new Headers({ 'Content-Type': 'application/json' }),
    text: vi.fn().mockResolvedValue(JSON.stringify(body)),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('owner-scoped checkout recovery service', () => {
  it('posts only the proposal path and authenticated session, with no browser order ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, {
      proposal_id: 'proposal-1', status: 'COMPLETED',
      provider_status: 'COMPLETED', payment_completed: true,
    }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(recoverProposalCheckout('session-private', 'proposal-1')).resolves.toMatchObject({
      payment_completed: true,
    });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toContain('/api/v1/proposals/proposal-1/checkout/reconcile');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session-private');
    expect(options.body).toBeUndefined();
    expect(url).not.toContain('order');
  });

  it('rejects inconsistent or unexpected recovery responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, {
      proposal_id: 'proposal-1', status: 'COMPLETED',
      provider_status: 'APPROVED', payment_completed: true,
    })));
    await expect(recoverProposalCheckout('session', 'proposal-1')).rejects.toThrow(/could not be verified/i);
  });

  it('requires the explicit recovery flag in checkout state responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(200, {
      proposal_id: 'proposal-1', status: 'RECONCILIATION_REQUIRED',
      consent_recorded: false, order_id: null, approval_url: null,
      currency: 'USD', total_amount: '10.00',
      payment_status: 'RECONCILIATION_REQUIRED', recovery_available: true,
    })));
    await expect(getCheckoutState('session', 'proposal-1')).resolves.toMatchObject({
      recovery_available: true, order_id: null,
    });
  });
});
