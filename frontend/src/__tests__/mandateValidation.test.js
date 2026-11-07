import { describe, expect, it } from 'vitest';
import { validateMandateDraft } from '../services/mandateValidation';

const validValues = {
  max_total_budget: '0.30', currency: 'USD', allowed_merchants: 'shop.example',
  allowed_categories: 'books, home', max_item_count: '3', max_quantity_per_item: '2',
  max_price_per_item: '0.29', allow_subscriptions: false, require_user_approval: true,
  expires_at: '2030-01-01T00:00',
};

describe('mandate draft validation', () => {
  it('accepts valid permissions and returns only backend-supported fields', () => {
    const { errors, payload } = validateMandateDraft(validValues, new Date('2029-01-01T00:00:00Z'));
    expect(errors).toEqual({});
    expect(payload).toMatchObject({
      max_total_budget: '0.30', currency: 'USD',
      allowed_merchants: ['shop.example'], allowed_categories: ['books', 'home'],
      max_item_count: 3, max_quantity_per_item: 2, max_price_per_item: '0.29',
      allow_subscriptions: false, require_user_approval: true,
    });
    expect(payload.expires_at).toMatch(/Z$/);
    expect(payload).not.toHaveProperty('owner_user_id');
    expect(payload).not.toHaveProperty('signature');
  });

  it('uses exact minor units for price and budget comparison', () => {
    const { errors } = validateMandateDraft({
      ...validValues,
      max_total_budget: '9007199254740992.01',
      max_price_per_item: '9007199254740992.02',
    }, new Date('2029-01-01T00:00:00Z'));
    expect(errors.max_price_per_item).toMatch(/cannot exceed/i);
  });

  it('rejects backend-invalid amounts, merchant strings and item counts', () => {
    const { errors, payload } = validateMandateDraft({
      ...validValues,
      max_total_budget: '1.001',
      max_price_per_item: '-1',
      allowed_merchants: 'https://shop.example/path',
      max_item_count: '1',
      max_quantity_per_item: '2',
      expires_at: '2020-01-01T00:00',
    }, new Date('2029-01-01T00:00:00Z'));
    expect(payload).toBeNull();
    expect(errors.max_total_budget).toBeTruthy();
    expect(errors.max_price_per_item).toBeTruthy();
    expect(errors.allowed_merchants).toBeTruthy();
    expect(errors.max_quantity_per_item).toBeTruthy();
    expect(errors.expires_at).toBeTruthy();
  });
});
