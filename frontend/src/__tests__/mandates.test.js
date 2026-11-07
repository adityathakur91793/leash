import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../services/api';
import {
  activateMandate,
  confirmMandate,
  createMandate,
  getMandate,
  listMandates,
  MANDATE_LIST_LIMIT,
  MANDATE_LIST_LIMITATION,
  revokeMandate,
  signMandate,
  verifyMandate,
} from '../services/mandates';

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: vi.fn().mockResolvedValue(body === undefined ? '' : JSON.stringify(body)),
  };
}

function fullMandate(overrides = {}) {
  return {
    mandate_id: 'm1', schema_version: '1.0', status: 'DRAFT',
    max_total_budget: '25.00', currency: 'USD',
    allowed_merchants: ['shop.example'], allowed_categories: ['books'],
    max_item_count: 1, max_quantity_per_item: 1, max_price_per_item: null,
    allow_subscriptions: false, require_user_approval: true,
    expires_at: '2030-01-01T00:00:00Z', created_at: '2029-01-01T00:00:00Z',
    signature: null, signed_at: null, signature_algorithm: null, owner_user_id: 'owner',
    ...overrides,
  };
}

describe('mandate API service', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('uses the exact authenticated list, detail, and verification routes', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(200, []));
    await listMandates('session-token');
    await getMandate('session-token', 'mandate /1');
    await verifyMandate('session-token', 'mandate /1');

    expect(MANDATE_LIST_LIMIT).toBe(100);
    expect(MANDATE_LIST_LIMITATION).toMatch(/100.*pagination/i);
    expect(fetchMock.mock.calls.map(([url, options]) => [url, options.method || 'GET'])).toEqual([
      ['http://localhost:8000/api/v1/mandates?limit=100', 'GET'],
      ['http://localhost:8000/api/v1/mandates/mandate%20%2F1', 'GET'],
      ['http://localhost:8000/api/v1/mandates/mandate%20%2F1/verify', 'GET'],
    ]);
    for (const [, options] of fetchMock.mock.calls) {
      expect(options.headers.get('Authorization')).toBe('Bearer session-token');
    }
  });

  it('sends only supported create fields, never owner or server-managed identity', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(201, fullMandate()));
    const payload = {
      max_total_budget: '25.00',
      currency: 'USD',
      allowed_merchants: ['shop.example'],
      allowed_categories: ['books'],
      expires_at: '2030-01-01T00:00:00Z',
      owner_user_id: 'attacker',
      user_id: 'attacker',
      signature: 'secret',
      mandate_id: 'forged',
    };
    const created = await createMandate('session-token', payload);
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8000/api/v1/mandates');
    expect(options.method).toBe('POST');
    expect(options.headers.get('Authorization')).toBe('Bearer session-token');
    expect(JSON.parse(options.body)).toEqual({
      max_total_budget: '25.00',
      currency: 'USD',
      allowed_merchants: ['shop.example'],
      allowed_categories: ['books'],
      expires_at: '2030-01-01T00:00:00Z',
    });
    expect(created).toMatchObject({ mandate_id: 'm1' });
  });

  it('requires the exact 201 status and a complete backend Mandate response for create', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({ allowed_categories: undefined })));
    await expect(createMandate('t', {})).rejects.toMatchObject({ code: 'invalid-response', status: 201 });
    fetchMock.mockResolvedValueOnce(response(200, fullMandate()));
    await expect(createMandate('t', {})).rejects.toMatchObject({
      code: 'unexpected-success-status', status: 200,
    });
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({ created_at: 'invalid' })));
    await expect(createMandate('t', {})).rejects.toMatchObject({ code: 'invalid-response' });
  });

  it('rejects invalid monetary precision and numeric formats without rounding', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    const invalidValues = ['1.001', '0x10', 'Infinity', 'NaN', '0', '-1'];
    for (const amount of invalidValues) {
      fetchMock.mockResolvedValueOnce(response(201, fullMandate({ max_total_budget: amount })));
      await expect(createMandate('t', {})).rejects.toMatchObject({ code: 'invalid-response' });
    }
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({ max_total_budget: '1' })));
    await expect(createMandate('t', {})).resolves.toMatchObject({ max_total_budget: '1' });
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({ max_total_budget: '1.25' })));
    await expect(createMandate('t', {})).resolves.toMatchObject({ max_total_budget: '1.25' });
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({
      max_total_budget: '9007199254740992.01',
      max_price_per_item: '9007199254740992.02',
    })));
    await expect(createMandate('t', {})).rejects.toMatchObject({ code: 'invalid-response' });
    fetchMock.mockResolvedValueOnce(response(201, fullMandate({
      max_total_budget: '10.00', max_price_per_item: '10.01',
    })));
    await expect(createMandate('t', {})).rejects.toMatchObject({ code: 'invalid-response' });
  });

  it('calls lifecycle transitions as separate POST requests', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(200, { mandate_id: 'm1' }));
    await confirmMandate('t', 'm1');
    await signMandate('t', 'm1');
    await activateMandate('t', 'm1');
    await revokeMandate('t', 'm1');
    expect(fetchMock.mock.calls.map(([url, options]) => [url, options.method, options.body])).toEqual([
      ['http://localhost:8000/api/v1/mandates/m1/confirm', 'POST', undefined],
      ['http://localhost:8000/api/v1/mandates/m1/sign', 'POST', undefined],
      ['http://localhost:8000/api/v1/mandates/m1/activate', 'POST', undefined],
      ['http://localhost:8000/api/v1/mandates/m1/revoke', 'POST', undefined],
    ]);
  });

  it('preserves distinct 401/403 handling and safe 422 details without leaking 5xx bodies', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    fetchMock.mockResolvedValueOnce(response(401, { detail: 'internal secret' }));
    await expect(listMandates('t')).rejects.toMatchObject({ code: 'unauthenticated', status: 401 });
    fetchMock.mockResolvedValueOnce(response(403, { detail: 'owner secret' }));
    await expect(listMandates('t')).rejects.toMatchObject({ code: 'forbidden', status: 403 });
    fetchMock.mockResolvedValueOnce(response(422, { detail: [{ loc: ['body', 'expires_at'], msg: 'invalid date', type: 'value_error', input: 'private', ctx: { secret: 'x' } }] }));
    let validation;
    try { await createMandate('t', {}); } catch (error) { validation = error; }
    expect(validation).toBeInstanceOf(ApiError);
    expect(validation.details).toEqual([{ loc: ['body', 'expires_at'], msg: 'invalid date', type: 'value_error' }]);
    expect(JSON.stringify(validation)).not.toContain('private');
    fetchMock.mockResolvedValueOnce(response(500, { detail: 'database password' }));
    let serverError;
    try { await listMandates('t'); } catch (error) { serverError = error; }
    expect(serverError.message).not.toContain('database password');
    expect(serverError.details).toBeNull();
  });
});
