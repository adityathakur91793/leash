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

export { API_BASE_URL };
