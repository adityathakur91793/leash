import { render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';

function response(status, value) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: vi.fn().mockResolvedValue(value == null ? '' : JSON.stringify(value)),
  };
}

describe('App session integration', () => {
  it('disables shopping after an expired session and waits for explicit restart', async () => {
    sessionStorage.setItem('leash.shopping.session', 'expired-token');
    const fetchMock = vi.fn((url) => {
      if (url.endsWith('/health')) return Promise.resolve(response(200, { status: 'healthy' }));
      if (url.endsWith('/api/v1/status')) return Promise.resolve(response(200, { status: 'operational' }));
      if (url.endsWith('/api/v1/sessions/me')) return Promise.resolve(response(401, { detail: 'expired' }));
      return Promise.resolve(response(500, null));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Session ended' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ask LEASH' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Start new session' })).toBeEnabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
  });
});
