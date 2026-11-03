import { useState, useEffect } from 'react';
import { checkBackendHealth, getSystemStatus } from '../services/api';

export function useBackendStatus() {
  const [health, setHealth] = useState(null);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    async function checkConnection() {
      setLoading(true);
      setError(null);
      try {
        const [healthData, statusData] = await Promise.all([
          checkBackendHealth(),
          getSystemStatus(),
        ]);
        setHealth(healthData);
        setStatus(statusData);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    }
    checkConnection();
  }, []);

  return { health, status, loading, error };
}
