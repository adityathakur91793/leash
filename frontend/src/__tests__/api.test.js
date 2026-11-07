import { describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  getShoppingSession,
  listShoppingMandates,
  request,
  revokeSession,
  sendShoppingMessage,
} from '../services/api';

function response(status, body = '', contentType = 'application/json') {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers({ 'Content-Type': contentType }),
    text: vi.fn().mockResolvedValue(body),
  };
}

describe('shared API client', () => {
  it('attaches a bearer token and handles empty 204 responses', async () => {
    const noContent = response(204);
    const fetchMock = vi.fn().mockResolvedValue(noContent);
    vi.stubGlobal('fetch', fetchMock);

    await expect(revokeSession('secret-token')).resolves.toBeNull();
    const [, options] = fetchMock.mock.calls[0];
    expect(options.headers.get('Authorization')).toBe('Bearer secret-token');
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/sessions/me');
    expect(noContent.text).not.toHaveBeenCalled();
  });

  it('returns decoded JSON and tolerates empty or non-JSON success bodies', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response(200, '{"ok":true}'))
      .mockResolvedValueOnce(response(200, ''))
      .mockResolvedValueOnce(response(200, 'upstream html', 'text/html')));

    await expect(request('/json')).resolves.toEqual({ ok: true });
    await expect(request('/empty')).resolves.toBeNull();
    await expect(request('/html')).resolves.toBeNull();
  });

  it('returns safe errors for JSON and non-JSON error responses', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response(422, '{"detail":"bad field"}'))
      .mockResolvedValueOnce(response(502, '<html>secret backend trace</html>', 'text/html')));

    await expect(request('/invalid')).rejects.toMatchObject({
      name: 'ApiError', status: 422, code: 'validation',
      message: 'Some submitted information needs attention. Review it and try again.',
    });
    await expect(request('/failed')).rejects.toMatchObject({
      name: 'ApiError', status: 502, code: 'server',
      message: 'The service is temporarily unavailable. Please try again shortly.',
    });
  });

  it('distinguishes authentication from authorization failures', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(response(401, '{"detail":"expired"}'))
      .mockResolvedValueOnce(response(403, '{"detail":"forbidden"}')));

    await expect(request('/private')).rejects.toMatchObject({ status: 401, code: 'unauthenticated' });
    await expect(request('/private')).rejects.toMatchObject({ status: 403, code: 'forbidden' });
  });

  it('converts network failures into a safe API error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('socket details')));
    await expect(request('/offline')).rejects.toMatchObject({
      name: 'ApiError', status: null, code: 'network',
      message: 'Unable to reach LEASH. Check your connection and try again.',
    });
  });

  it('keeps existing shopping requests compatible with the shared client', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, '[{"mandate_id":"m1"}]'))
      .mockResolvedValueOnce(response(200, '{"message":"Found items","products":[]}'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(listShoppingMandates('shop-token')).resolves.toEqual([{ mandate_id: 'm1' }]);
    await expect(sendShoppingMessage('shop-token', 'find a bag', '')).resolves.toEqual({
      message: 'Found items', products: [],
    });
    expect(fetchMock.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer shop-token');
    expect(fetchMock.mock.calls[1][1].headers.get('Authorization')).toBe('Bearer shop-token');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ message: 'find a bag', mandate_id: null });
  });

  it('does not silently create a replacement session through the legacy helper', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(getShoppingSession()).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
