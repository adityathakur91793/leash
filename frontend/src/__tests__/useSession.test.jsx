import { act, renderHook, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SESSION_KEY } from '../services/api';
import { useSession } from '../hooks/useSession';

function response(status, value = null) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: vi.fn().mockResolvedValue(value == null ? '' : JSON.stringify(value)),
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

async function flushMicrotasks() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

const sessionInfo = {
  user_id: 'usr_1',
  created_at: '2026-11-07T00:00:00Z',
  expires_at: '2030-11-07T00:00:00Z',
  is_active: true,
};

const createdSession = {
  session_token: 'new-token',
  user_id: 'usr_2',
  created_at: '2026-11-07T00:00:00Z',
  expires_at: '2030-11-07T00:00:00Z',
};

describe('useSession', () => {
  beforeEach(() => sessionStorage.clear());

  it('validates an existing token through /sessions/me', async () => {
    sessionStorage.setItem(SESSION_KEY, 'saved-token');
    const fetchMock = vi.fn().mockResolvedValue(response(200, sessionInfo));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());

    await waitFor(() => expect(result.current.status).toBe('active'));
    expect(result.current.token).toBe('saved-token');
    expect(result.current.expiresAt).toBe(sessionInfo.expires_at);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/sessions/me');
    expect(fetchMock.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer saved-token');
  });

  it('creates a session when no token exists and avoids StrictMode duplicates', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(201, createdSession));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession(), { wrapper: StrictMode });

    await waitFor(() => expect(result.current.status).toBe('active'));
    expect(result.current.token).toBe('new-token');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('new-token');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/sessions');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });

  it('does not silently replace a session rejected as expired or revoked', async () => {
    sessionStorage.setItem(SESSION_KEY, 'old-token');
    const fetchMock = vi.fn().mockResolvedValue(response(401, { detail: 'expired' }));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());

    await waitFor(() => expect(result.current.status).toBe('expired'));
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('old-token');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/sessions/me');
  });

  it('preserves authorization denial as an error, not an expired session', async () => {
    sessionStorage.setItem(SESSION_KEY, 'saved-token');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(403, { detail: 'denied' })));
    const { result } = renderHook(() => useSession());

    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.error).toContain('permission');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('saved-token');
  });

  it('retries validation for an existing session without creating a replacement', async () => {
    sessionStorage.setItem(SESSION_KEY, 'saved-token');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(403, { detail: 'denied' }))
      .mockResolvedValueOnce(response(200, sessionInfo));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('error'));

    await act(async () => { await result.current.retrySession(); });
    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('saved-token');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.every(([url]) => url.includes('/sessions/me'))).toBe(true);
  });

  it('starts a replacement session only after explicit restart', async () => {
    sessionStorage.setItem(SESSION_KEY, 'old-token');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(401, { detail: 'expired' }))
      .mockResolvedValueOnce(response(201, createdSession));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('expired'));

    await act(async () => { await result.current.restartSession(); });
    expect(result.current.status).toBe('active');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('new-token');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('clears stored token only after successful revocation', async () => {
    sessionStorage.setItem(SESSION_KEY, 'saved-token');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, sessionInfo))
      .mockResolvedValueOnce(response(204));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('active'));

    let revoked;
    await act(async () => { revoked = await result.current.logout(); });
    expect(revoked).toBe(true);
    expect(result.current.status).toBe('revoked');
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(fetchMock.mock.calls[1][0]).toContain('/sessions/me');
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE');
  });

  it('does not claim logout succeeded when revocation fails', async () => {
    sessionStorage.setItem(SESSION_KEY, 'saved-token');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, sessionInfo))
      .mockResolvedValueOnce(response(503, { detail: 'internal detail' }));
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('active'));

    let revoked;
    await act(async () => { revoked = await result.current.logout(); });
    expect(revoked).toBe(false);
    expect(result.current.status).toBe('active');
    expect(result.current.actionError).toContain('temporarily unavailable');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('saved-token');
  });

  it('ignores stale logout success after expiry and explicit restart', async () => {
    sessionStorage.setItem(SESSION_KEY, 'old-token');
    const pendingRevoke = deferred();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/sessions/me') && !options.method) {
        return Promise.resolve(response(200, sessionInfo));
      }
      if (options.method === 'DELETE') return pendingRevoke.promise;
      if (options.method === 'POST') return Promise.resolve(response(201, {
        ...createdSession, session_token: 'replacement-token',
      }));
      return Promise.resolve(response(500));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('active'));

    let logoutPromise;
    act(() => { logoutPromise = result.current.logout(); });
    act(() => { result.current.markExpired(); });
    await act(async () => { await result.current.restartSession(); });
    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('replacement-token');

    await act(async () => {
      pendingRevoke.resolve(response(204));
      await logoutPromise;
    });
    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('replacement-token');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('replacement-token');
  });

  it('deduplicates concurrent restart calls while creation is pending', async () => {
    sessionStorage.setItem(SESSION_KEY, 'expired-token');
    const pendingCreate = deferred();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/sessions/me')) return Promise.resolve(response(401, { detail: 'expired' }));
      if (options.method === 'POST') return pendingCreate.promise;
      return Promise.resolve(response(500));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('expired'));

    let firstRestart;
    let secondRestart;
    act(() => {
      firstRestart = result.current.restartSession();
      secondRestart = result.current.restartSession();
    });
    expect(secondRestart).toBe(firstRestart);
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);

    await act(async () => {
      pendingCreate.resolve(response(201, {
        ...createdSession, session_token: 'one-restart-token',
      }));
      await firstRestart;
    });
    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('one-restart-token');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('one-restart-token');
  });

  it('ignores a stale expiry timer after a newer session starts', async () => {
    sessionStorage.setItem(SESSION_KEY, 'old-token');
    const pendingValidation = deferred();
    let mockedNow = Date.now();
    vi.spyOn(Date, 'now').mockImplementation(() => mockedNow);
    const oldExpiry = new Date(mockedNow + 60_000).toISOString();
    const replacementExpiry = new Date(mockedNow + 120_000).toISOString();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/sessions/me')) return pendingValidation.promise;
      if (options.method === 'POST') return Promise.resolve(response(201, {
        ...createdSession,
        session_token: 'new-timer-session',
        expires_at: replacementExpiry,
      }));
      return Promise.resolve(response(500));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    const callbacks = [];
    vi.spyOn(window, 'setTimeout').mockImplementation((callback) => {
      callbacks.push(callback);
      return callbacks.length;
    });
    vi.spyOn(window, 'clearTimeout').mockImplementation(() => {});

    await act(async () => {
      pendingValidation.resolve(response(200, { ...sessionInfo, expires_at: oldExpiry }));
      await flushMicrotasks();
    });
    expect(result.current.status).toBe('active');
    const staleExpiryCallback = callbacks[0];
    expect(staleExpiryCallback).toBeTypeOf('function');

    await act(async () => { result.current.markExpired(); });
    await act(async () => { await result.current.restartSession(); });
    expect(result.current.token).toBe('new-timer-session');
    mockedNow = Date.parse(oldExpiry) + 1;
    expect(Date.parse(replacementExpiry)).toBeGreaterThan(mockedNow);
    act(() => { staleExpiryCallback(); });

    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('new-timer-session');
    expect(result.current.userId).toBe(createdSession.user_id);
    expect(result.current.expiresAt).toBe(replacementExpiry);
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('new-timer-session');
  });

  it('ignores stale logout failure after a newer session starts', async () => {
    sessionStorage.setItem(SESSION_KEY, 'old-token');
    const pendingRevoke = deferred();
    const fetchMock = vi.fn((url, options = {}) => {
      if (url.endsWith('/sessions/me') && !options.method) {
        return Promise.resolve(response(200, sessionInfo));
      }
      if (options.method === 'DELETE') return pendingRevoke.promise;
      if (options.method === 'POST') return Promise.resolve(response(201, {
        ...createdSession, session_token: 'newer-token',
      }));
      return Promise.resolve(response(500));
    });
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.status).toBe('active'));

    let logoutPromise;
    act(() => { logoutPromise = result.current.logout(); });
    act(() => { result.current.markExpired(); });
    await act(async () => { await result.current.restartSession(); });
    await act(async () => {
      pendingRevoke.reject(new TypeError('connection lost'));
      await logoutPromise;
    });

    expect(result.current.status).toBe('active');
    expect(result.current.token).toBe('newer-token');
    expect(result.current.actionError).toBe('');
    expect(sessionStorage.getItem(SESSION_KEY)).toBe('newer-token');
  });
});
