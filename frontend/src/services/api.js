export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
export const SESSION_KEY = 'leash.shopping.session';

const SAFE_MESSAGES = {
  401: 'Your session has ended. Start a new session to continue.',
  403: 'You do not have permission to perform this action.',
  422: 'Some submitted information needs attention. Review it and try again.',
};

export class ApiError extends Error {
  constructor(message, { status = null, code = 'request' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function messageForStatus(status) {
  if (SAFE_MESSAGES[status]) return SAFE_MESSAGES[status];
  if (status >= 500) return 'The service is temporarily unavailable. Please try again shortly.';
  return 'The request could not be completed. Please try again.';
}

async function readResponse(response) {
  if (response.status === 204) return null;
  const body = await response.text();
  if (!body.trim()) return null;
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

export async function request(path, options = {}) {
  const { token, headers: suppliedHeaders, ...fetchOptions } = options;
  const headers = new Headers(suppliedHeaders || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...fetchOptions, headers });
  } catch {
    throw new ApiError('Unable to reach LEASH. Check your connection and try again.', {
      code: 'network',
    });
  }

  let data;
  try {
    data = await readResponse(response);
  } catch {
    data = null;
  }

  if (!response.ok) {
    const code = response.status === 401 ? 'unauthenticated'
      : response.status === 403 ? 'forbidden'
        : response.status === 422 ? 'validation'
          : response.status >= 500 ? 'server' : 'http';
    throw new ApiError(messageForStatus(response.status), {
      status: response.status,
      code,
    });
  }
  return data;
}

export async function checkBackendHealth() {
  return request('/health');
}

export async function getSystemStatus() {
  return request('/api/v1/status');
}

export async function createSession() {
  return request('/api/v1/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
}

export async function getSessionInfo(token) {
  return request('/api/v1/sessions/me', { token });
}

export async function revokeSession(token) {
  return request('/api/v1/sessions/me', { method: 'DELETE', token });
}

// Kept for callers of the former helper without silently creating a new identity.
export async function getShoppingSession() {
  const token = sessionStorage.getItem(SESSION_KEY);
  if (!token) {
    throw new ApiError('No active session. Start a session before using shopping.', {
      code: 'missing-session',
    });
  }
  return token;
}

export async function listShoppingMandates(token) {
  return request('/api/v1/shopping-agent/mandates', { token });
}

export async function sendShoppingMessage(token, message, mandateId) {
  return request('/api/v1/shopping-agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    token,
    body: JSON.stringify({ message, mandate_id: mandateId || null }),
  });
}
