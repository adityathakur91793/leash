import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import { getActivityOverview } from '../services/activity';

export function useActivityOverview(token, onSessionExpired, sessionIdentity) {
  const [state, setState] = useState({ data: null, loading: false, error: null, identity: null });
  const [refreshRevision, setRefreshRevision] = useState(0);
  const generationRef = useRef(0);
  const tokenRef = useRef(token);
  const expiredRef = useRef(onSessionExpired);
  tokenRef.current = token;
  expiredRef.current = onSessionExpired;

  useEffect(() => {
    const generation = ++generationRef.current;
    if (!token) {
      setState({ data: null, loading: false, error: null, identity: null });
      return () => { generationRef.current += 1; };
    }
    setState({ data: null, loading: true, error: null, identity: sessionIdentity });
    getActivityOverview(token).then((data) => {
      if (tokenRef.current === token && generationRef.current === generation) {
        setState({ data, loading: false, error: null, identity: sessionIdentity });
      }
    }).catch((error) => {
      if (tokenRef.current !== token || generationRef.current !== generation) return;
      if (error instanceof ApiError && error.code === 'unauthenticated') expiredRef.current?.();
      setState({ data: null, loading: false, error, identity: sessionIdentity });
    });
    return () => { generationRef.current += 1; };
  }, [token, sessionIdentity, refreshRevision]);

  const refresh = useCallback(() => setRefreshRevision((value) => value + 1), []);
  const visible = state.identity === sessionIdentity;
  return {
    data: visible ? state.data : null,
    loading: visible ? state.loading : Boolean(token),
    error: visible ? state.error : null,
    refresh,
  };
}
