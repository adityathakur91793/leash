const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export async function checkBackendHealth() {
  const response = await fetch(`${API_BASE_URL}/health`);
  if (!response.ok) {
    throw new Error(`Backend health check failed: ${response.status}`);
  }
  return response.json();
}

export async function getSystemStatus() {
  const response = await fetch(`${API_BASE_URL}/api/v1/status`);
  if (!response.ok) {
    throw new Error(`Status check failed: ${response.status}`);
  }
  return response.json();
}

const SESSION_KEY = 'leash.shopping.session';

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    throw new Error(data?.detail || `Request failed: ${response.status}`);
  }
  return data;
}

export async function getShoppingSession() {
  let token = sessionStorage.getItem(SESSION_KEY);
  if (!token) {
    const session = await request('/api/v1/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    token = session.session_token;
    sessionStorage.setItem(SESSION_KEY, token);
  }
  return token;
}

export async function listShoppingMandates(token) {
  return request('/api/v1/shopping-agent/mandates', {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function sendShoppingMessage(token, message, mandateId) {
  return request('/api/v1/shopping-agent/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ message, mandate_id: mandateId || null }),
  });
}

export { API_BASE_URL };
