import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { ShoppingAgent } from '../components/ShoppingAgent';

function response(status, value) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: vi.fn().mockResolvedValue(value == null ? '' : JSON.stringify(value)),
  };
}

describe('ShoppingAgent purchase proposals', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('submits a purchase proposal using the selected mandate and catalog metadata', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/api/v1/shopping-agent/mandates')) {
        return Promise.resolve(response(200, [{
          mandate_id: 'mandate-1',
          currency: 'USD',
          max_total_budget: '120.00',
          expires_at: '2030-01-01T00:00:00Z',
        }]));
      }
      if (url.endsWith('/api/v1/shopping-agent/chat')) {
        return Promise.resolve(response(200, {
          message: 'Here is a product match.',
          products: [{
            product_id: 'demo_bottle_001',
            name: 'Reusable Bottle',
            description: 'A durable bottle for outdoor use.',
            description_status: 'SIMULATED',
            merchant_id: 'greenmfg',
            merchant_name: 'Green Mfg',
            category: 'outdoors',
            unit_price: '22.00',
            tax: '0.00',
            shipping: '0.00',
            other_charges: '0.00',
            currency: 'USD',
            price_status: 'SIMULATED',
            availability: 'SIMULATED_AVAILABLE',
            availability_status: 'SIMULATED',
            source: 'LOCAL_SIMULATED_CATALOG',
            is_simulated: true,
          }],
        }));
      }
      if (url.endsWith('/api/v1/proposals')) {
        const payload = JSON.parse(options.body || '{}');
        expect(payload).toMatchObject({
          mandate_id: 'mandate-1',
          product_id: 'demo_bottle_001',
          product_name: 'Reusable Bottle',
          merchant: 'greenmfg',
          category: 'outdoors',
          quantity: 1,
          unit_price: '22.00',
          currency: 'USD',
          tax: '0.00',
          shipping: '0.00',
          other_charges: '0.00',
          product_source: 'LOCAL_SIMULATED_CATALOG',
        });
        return Promise.resolve(response(201, { proposal_id: 'pr_1', status: 'SUBMITTED' }));
      }
      return Promise.resolve(response(500, null));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ShoppingAgent token="session-token" onSessionExpired={vi.fn()} />);

    await screen.findByRole('option', { name: /USD 120\.00/i });
    fireEvent.change(screen.getByLabelText(/purchase mandate/i), { target: { value: 'mandate-1' } });
    fireEvent.change(screen.getByLabelText(/what are you looking for/i), { target: { value: 'water bottle' } });
    fireEvent.click(screen.getByRole('button', { name: /ask leash/i }));

    expect(await screen.findByText('Reusable Bottle')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /propose purchase/i }));

    expect(await screen.findByText(/proposal pr_1 submitted/i)).toBeInTheDocument();
  });

  it('shows an error when proposal submission fails', async () => {
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/api/v1/shopping-agent/mandates')) {
        return Promise.resolve(response(200, [{
          mandate_id: 'mandate-1',
          currency: 'USD',
          max_total_budget: '120.00',
          expires_at: '2030-01-01T00:00:00Z',
        }]));
      }
      if (url.endsWith('/api/v1/shopping-agent/chat')) {
        return Promise.resolve(response(200, {
          message: 'Here is a product match.',
          products: [{
            product_id: 'demo_bottle_001',
            name: 'Reusable Bottle',
            description: 'A durable bottle for outdoor use.',
            description_status: 'SIMULATED',
            merchant_id: 'greenmfg',
            merchant_name: 'Green Mfg',
            category: 'outdoors',
            unit_price: '22.00',
            tax: '0.00',
            shipping: '0.00',
            other_charges: '0.00',
            currency: 'USD',
            price_status: 'SIMULATED',
            availability: 'SIMULATED_AVAILABLE',
            availability_status: 'SIMULATED',
            source: 'LOCAL_SIMULATED_CATALOG',
            is_simulated: true,
          }],
        }));
      }
      if (url.endsWith('/api/v1/proposals')) {
        return Promise.resolve(response(422, { detail: 'Mandate not found.' }));
      }
      return Promise.resolve(response(500, null));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ShoppingAgent token="session-token" onSessionExpired={vi.fn()} />);

    await screen.findByRole('option', { name: /USD 120\.00/i });
    fireEvent.change(screen.getByLabelText(/purchase mandate/i), { target: { value: 'mandate-1' } });
    fireEvent.change(screen.getByLabelText(/what are you looking for/i), { target: { value: 'water bottle' } });
    fireEvent.click(screen.getByRole('button', { name: /ask leash/i }));

    expect(await screen.findByText('Reusable Bottle')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /propose purchase/i }));

    expect(await screen.findByText(/some submitted information needs attention/i)).toBeInTheDocument();
  });
});
