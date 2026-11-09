import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as payments from '../services/payments';
import { CheckoutReturn } from '../components/CheckoutReturn';

function setReturnUrl(query) {
  window.history.replaceState({ preserved: true }, '', `/app?${query}`);
}

function expectReturnParametersRemoved() {
  const url = new URL(window.location.href);
  for (const key of ['token', 'PayerID', 'checkout', 'proposal_id']) {
    expect(url.searchParams.has(key), `${key} should be removed`).toBe(false);
  }
  expect(window.history.state).toEqual({ preserved: true });
}

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, '', '/');
});

describe('CheckoutReturn URL cleanup', () => {
  it('cleans provider parameters on successful verified return', async () => {
    setReturnUrl('checkout=return&proposal_id=proposal-1&token=provider-order&PayerID=payer-1&keep=1');
    vi.spyOn(payments, 'verifyCheckoutReturn').mockResolvedValue({
      proposal_id: 'proposal-1', order_id: 'provider-order',
      status: 'APPROVED', payment_completed: false,
    });
    render(<CheckoutReturn sessionToken="session" />);
    expect(await screen.findByText(/buyer approval was verified/i)).toBeInTheDocument();
    expect(payments.verifyCheckoutReturn).toHaveBeenCalledWith(
      'session', 'proposal-1', 'provider-order',
    );
    expectReturnParametersRemoved();
    expect(window.location.search).toContain('keep=1');
    expect(document.body).not.toHaveTextContent(/provider-order|payer-1/);
  });

  it('cleans all return parameters on cancellation', () => {
    setReturnUrl('checkout=cancel&proposal_id=proposal-1&token=provider-order&PayerID=payer-1');
    render(<CheckoutReturn sessionToken="session" />);
    expect(screen.getByText(/has not recorded a payment/i)).toBeInTheDocument();
    expectReturnParametersRemoved();
  });

  it('cleans all return parameters when the session is missing', () => {
    setReturnUrl('checkout=return&proposal_id=proposal-1&token=provider-order&PayerID=payer-1');
    render(<CheckoutReturn sessionToken={null} />);
    expect(screen.getByText(/start a session to verify/i)).toBeInTheDocument();
    expectReturnParametersRemoved();
  });

  it('cleans all return parameters for an incomplete return', () => {
    setReturnUrl('checkout=return&token=provider-order&PayerID=payer-1');
    const verify = vi.spyOn(payments, 'verifyCheckoutReturn');
    render(<CheckoutReturn sessionToken="session" />);
    expect(screen.getByText(/details are incomplete/i)).toBeInTheDocument();
    expect(verify).not.toHaveBeenCalled();
    expectReturnParametersRemoved();
  });

  it('cleans parameters before displaying a backend verification error', async () => {
    setReturnUrl('checkout=return&proposal_id=proposal-1&token=provider-order&PayerID=payer-1');
    vi.spyOn(payments, 'verifyCheckoutReturn').mockRejectedValue(new Error('Verification unavailable'));
    render(<CheckoutReturn sessionToken="session" />);
    expect(await screen.findByText(/could not be verified/i)).toBeInTheDocument();
    await waitFor(expectReturnParametersRemoved);
    expect(document.body).not.toHaveTextContent(/provider-order|payer-1/);
  });

  it('cleans malformed return parameters without displaying them', () => {
    setReturnUrl('checkout=unexpected&token=provider-order&PayerID=payer-1&proposal_id=proposal-1');
    render(<CheckoutReturn sessionToken="session" />);
    expect(screen.getByText(/could not be recognized/i)).toBeInTheDocument();
    expectReturnParametersRemoved();
    expect(document.body).not.toHaveTextContent(/provider-order|payer-1/);
  });
});
