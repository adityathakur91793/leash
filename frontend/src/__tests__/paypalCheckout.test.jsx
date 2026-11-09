import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as payments from '../services/payments';
import { PayPalCheckout } from '../components/PayPalCheckout';

const props = {
  proposalId: 'proposal-1', sessionToken: 'session-secret',
  consent: { status: 'APPROVED' }, onSessionExpired: vi.fn(),
};

afterEach(() => vi.restoreAllMocks());

describe('PayPalCheckout', () => {
  it('creates checkout only after consent and navigates only to verified Sandbox host', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true, status: null, recovery_available: false,
    });
    vi.spyOn(payments, 'createCheckout').mockResolvedValue({
      proposal_id: 'proposal-1', order_id: 'order-1', status: 'CREATED',
      currency: 'USD', total_amount: '18.50',
      approval_url: 'https://www.sandbox.paypal.com/checkoutnow?token=order-1',
    });
    const navigate = vi.spyOn(payments, 'navigateToPayPal').mockImplementation(() => {});
    render(<PayPalCheckout {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: /start paypal checkout/i }));
    await waitFor(() => expect(payments.createCheckout).toHaveBeenCalledTimes(1));
    expect(payments.createCheckout).toHaveBeenCalledWith('session-secret', 'proposal-1');
    fireEvent.click(screen.getByRole('button', { name: /continue to paypal sandbox/i }));
    expect(navigate).toHaveBeenCalledWith(
      'https://www.sandbox.paypal.com/checkoutnow?token=order-1',
    );
    expect(document.body).not.toHaveTextContent('session-secret');
  });

  it('never creates checkout when no consent is present', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: false, status: null, recovery_available: false,
    });
    const create = vi.spyOn(payments, 'createCheckout');
    const { container } = render(<PayPalCheckout {...props} consent={null} />);
    await act(async () => {});
    expect(create).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps an ambiguous attempt pending and reconciles through the persisted attempt', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true, status: 'UNKNOWN', recovery_available: false,
    });
    const create = vi.spyOn(payments, 'createCheckout');
    render(<PayPalCheckout {...props} />);
    expect(await screen.findByText(/uncertain result/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /reconcile checkout/i }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith('session-secret', 'proposal-1');
  });

  it('recovers an anchored provider order through create-order reconciliation', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true, status: 'UNKNOWN',
      order_id: 'provider-order-1', approval_url: null, payment_status: null,
      recovery_available: true,
    });
    vi.spyOn(payments, 'createCheckout').mockResolvedValue({
      proposal_id: 'proposal-1', order_id: 'provider-order-1', status: 'APPROVED',
      currency: 'USD', total_amount: '18.50', approval_url: null,
    });
    const verify = vi.spyOn(payments, 'verifyCheckoutReturn');
    render(<PayPalCheckout {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: /reconcile checkout/i }));
    await waitFor(() => expect(payments.createCheckout).toHaveBeenCalledTimes(1));
    expect(payments.createCheckout).toHaveBeenCalledWith('session-secret', 'proposal-1');
    expect(verify).not.toHaveBeenCalled();
  });

  it('shows settlement only after validated capture response', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true, status: 'APPROVED',
      order_id: 'order-1', payment_status: 'APPROVED', approval_url: null, recovery_available: true,
    });
    vi.spyOn(payments, 'captureCheckout').mockResolvedValue({
      order_id: 'order-1', proposal_id: 'proposal-1', status: 'COMPLETED',
      capture_id: 'capture-1',
    });
    render(<PayPalCheckout {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: /capture payment/i }));
    expect(await screen.findByText(/payment is confirmed by leash and paypal/i)).toBeInTheDocument();
    expect(payments.captureCheckout).toHaveBeenCalledWith(
      'session-secret', 'order-1', 'proposal-1',
    );
  });

  it('uses server reconciliation before retrying an uncertain capture', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true,
      status: 'RECONCILIATION_REQUIRED', order_id: 'order-1',
      payment_status: 'RECONCILIATION_REQUIRED', approval_url: null, recovery_available: true,
    });
    vi.spyOn(payments, 'verifyCheckoutReturn').mockResolvedValue({
      proposal_id: 'proposal-1', order_id: 'order-1',
      status: 'APPROVED', payment_completed: false,
    });
    const capture = vi.spyOn(payments, 'captureCheckout').mockResolvedValue({
      order_id: 'order-1', proposal_id: 'proposal-1', status: 'COMPLETED',
      capture_id: 'real-capture',
    });
    render(<PayPalCheckout {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: /reconcile payment/i }));
    expect(await screen.findByRole('button', { name: /capture payment/i })).toBeInTheDocument();
    expect(capture).not.toHaveBeenCalled();
    expect(payments.verifyCheckoutReturn).toHaveBeenCalledWith(
      'session-secret', 'proposal-1', 'order-1',
    );
  });

  it('shows terminal checkout failure without offering another approval or capture', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: true, status: 'FAILED',
      order_id: 'order-1', payment_status: 'FAILED',
      approval_url: 'https://www.sandbox.paypal.com/checkoutnow?token=order-1',
      recovery_available: true,
    });
    render(<PayPalCheckout {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/marked failed/i);
    expect(screen.queryByRole('button', { name: /continue to paypal sandbox/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /capture payment/i })).not.toBeInTheDocument();
  });

  it('lets an owner verify saved settlement after consent is no longer current', async () => {
    vi.spyOn(payments, 'getCheckoutState').mockResolvedValue({
      proposal_id: 'proposal-1', consent_recorded: false,
      status: 'RECONCILIATION_REQUIRED', payment_status: 'RECONCILIATION_REQUIRED',
      order_id: null, approval_url: null, currency: 'USD', total_amount: '18.50',
      recovery_available: true,
    });
    vi.spyOn(payments, 'recoverProposalCheckout').mockResolvedValue({
      proposal_id: 'proposal-1', status: 'COMPLETED',
      provider_status: 'COMPLETED', payment_completed: true,
    });
    const capture = vi.spyOn(payments, 'captureCheckout');
    render(<PayPalCheckout {...props} consent={null} />);
    fireEvent.click(await screen.findByRole('button', { name: /verify saved payment status/i }));
    expect(await screen.findByText(/payment is confirmed by leash and paypal/i)).toBeInTheDocument();
    expect(payments.recoverProposalCheckout).toHaveBeenCalledWith('session-secret', 'proposal-1');
    expect(capture).not.toHaveBeenCalled();
    expect(document.body).not.toHaveTextContent(/provider-order|capture-id|session-secret/);
  });
});
