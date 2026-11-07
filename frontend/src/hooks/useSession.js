import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ApiError,
  SESSION_KEY,
  createSession,
  getSessionInfo,
  revokeSession,
} from '../services/api';

let initializationPromise = null;

function sessionState(status, session = null, error = null, errorCode = null) {
  return {
    status,
    token: session?.token ?? null,
    userId: session?.userId ?? null,
    expiresAt: session?.expiresAt ?? null,
    error,
    errorCode,
  };
}

async function initializeSession() {
  let existingToken;
  try {
    existingToken = sessionStorage.getItem(SESSION_KEY);
  } catch {
    return sessionState('error', null, 'Session storage is unavailable.', 'storage');
  }

  if (existingToken) {
    try {
      const info = await getSessionInfo(existingToken);
      return sessionState('active', {
        token: existingToken,
        userId: info.user_id,
        expiresAt: info.expires_at,
      });
    } catch (error) {
      if (error instanceof ApiError && error.code === 'unauthenticated') {
        return sessionState('expired');
      }
      return sessionState('error', null, error.message, error.code || 'request');
    }
  }

  try {
    const created = await createSession();
    if (!created?.session_token || !created?.expires_at) {
      throw new Error('The service returned incomplete session information.');
    }
    // Persist before releasing the module-level bootstrap promise so a later
    // mount validates this identity instead of creating another one.
    sessionStorage.setItem(SESSION_KEY, created.session_token);
    return sessionState('active', {
      token: created.session_token,
      userId: created.user_id,
      expiresAt: created.expires_at,
    });
  } catch (error) {
    return sessionState('error', null, error.message, error.code || 'request');
  }
}

function getInitialization() {
  if (!initializationPromise) {
    initializationPromise = initializeSession().finally(() => {
      initializationPromise = null;
    });
  }
  return initializationPromise;
}

export function useSession() {
  const [session, setSession] = useState(() => sessionState('loading'));
  const [actionError, setActionError] = useState('');
  const sessionRef = useRef(sessionState('loading'));
  const generationRef = useRef(0);
  const mountedRef = useRef(false);
  const restartPromiseRef = useRef(null);

  const commitSession = useCallback((next) => {
    sessionRef.current = next;
    setSession(next);
  }, []);

  const isCurrent = useCallback((generation) => (
    mountedRef.current && generationRef.current === generation
  ), []);

  const storeActiveSession = useCallback((next) => {
    if (next.status === 'active' && next.token) {
      sessionStorage.setItem(SESSION_KEY, next.token);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const generation = ++generationRef.current;
    getInitialization().then((result) => {
      if (!isCurrent(generation)) return;
      try {
        storeActiveSession(result);
        commitSession(result);
      } catch {
        commitSession(sessionState('error', null, 'Session storage is unavailable.', 'storage'));
      }
    });
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, [commitSession, isCurrent, storeActiveSession]);

  useEffect(() => {
    if (session.status !== 'active' || !session.expiresAt) return undefined;
    const generation = generationRef.current;
    let timer;
    const checkExpiry = () => {
      if (!isCurrent(generation)) return;
      const remaining = Date.parse(session.expiresAt) - Date.now();
      if (!Number.isFinite(remaining) || remaining <= 0) {
        generationRef.current += 1;
        commitSession(sessionState('expired'));
        return;
      }
      timer = window.setTimeout(checkExpiry, Math.min(remaining, 2_000_000_000));
    };
    checkExpiry();
    return () => window.clearTimeout(timer);
  }, [session.status, session.expiresAt, commitSession, isCurrent]);

  const restartSession = useCallback(() => {
    if (restartPromiseRef.current) return restartPromiseRef.current;
    if (!['expired', 'revoked'].includes(sessionRef.current.status)) {
      return Promise.resolve(false);
    }

    const generation = ++generationRef.current;
    setActionError('');
    commitSession(sessionState('loading'));
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      commitSession(sessionState('error', null, 'Session storage is unavailable.', 'storage'));
      return Promise.resolve(false);
    }

    const operation = (async () => {
      try {
        const created = await createSession();
        if (!created?.session_token || !created?.expires_at) {
          throw new Error('The service returned incomplete session information.');
        }
        const next = sessionState('active', {
          token: created.session_token,
          userId: created.user_id,
          expiresAt: created.expires_at,
        });
        if (!isCurrent(generation)) return false;
        storeActiveSession(next);
        commitSession(next);
        return true;
      } catch (error) {
        if (isCurrent(generation)) {
          commitSession(sessionState('error', null, error.message, error.code || 'request'));
        }
        return false;
      }
    })();

    restartPromiseRef.current = operation;
    operation.finally(() => {
      if (restartPromiseRef.current === operation) restartPromiseRef.current = null;
    });
    return operation;
  }, [commitSession, isCurrent, storeActiveSession]);

  const retrySession = useCallback(async () => {
    const generation = ++generationRef.current;
    setActionError('');
    commitSession(sessionState('loading'));
    const result = await getInitialization();
    if (!isCurrent(generation)) return false;
    try {
      storeActiveSession(result);
      commitSession(result);
    } catch {
      commitSession(sessionState('error', null, 'Session storage is unavailable.', 'storage'));
      return false;
    }
    return result.status === 'active';
  }, [commitSession, isCurrent, storeActiveSession]);

  const logout = useCallback(async () => {
    const current = sessionRef.current;
    if (!session.token || current.status !== 'active' || current.token !== session.token) {
      return false;
    }
    const actionGeneration = generationRef.current;
    const actionToken = session.token;
    setActionError('');

    let revokeError = null;
    try {
      await revokeSession(actionToken);
    } catch (error) {
      revokeError = error;
    }

    if (revokeError) {
      if (!isCurrent(actionGeneration)) return false;
      if (revokeError instanceof ApiError && revokeError.code === 'unauthenticated') {
        generationRef.current += 1;
        commitSession(sessionState('expired'));
      }
      setActionError(revokeError.message);
      return false;
    }

    // Clear only the token this request was issued for. A stale revoke must
    // never delete the token from a newer session.
    let storageError = false;
    try {
      if (sessionStorage.getItem(SESSION_KEY) === actionToken) {
        sessionStorage.removeItem(SESSION_KEY);
      }
    } catch {
      storageError = true;
    }
    if (!isCurrent(actionGeneration)) return false;
    generationRef.current += 1;
    commitSession(sessionState('revoked'));
    if (storageError) {
      setActionError('Session ended on the server, but the browser could not clear its saved token.');
    }
    return true;
  }, [session.status, session.token, commitSession, isCurrent]);

  const markExpired = useCallback(() => {
    if (sessionRef.current.status !== 'active') return;
    generationRef.current += 1;
    commitSession(sessionState('expired'));
  }, [commitSession]);

  return { ...session, actionError, logout, restartSession, retrySession, markExpired };
}
