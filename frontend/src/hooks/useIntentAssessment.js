import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import { getIntentAssessment } from '../services/proposals';

const PROPOSAL_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

function emptyState(identity = 0) {
  return { identity, assessment: null, loaded: false, loading: false, error: null };
}

export function useIntentAssessment(
  token,
  onSessionExpired,
  proposalId,
  evaluationRevision = 0,
  sessionGeneration = 0,
) {
  const [storedState, setStoredState] = useState(() => emptyState());
  const identityRef = useRef({ token, proposalId, sessionGeneration, id: 0 });
  const requestIdRef = useRef(0);
  const lifecycleIdRef = useRef(0);
  const lifecycleActiveRef = useRef(false);
  const expiredRef = useRef(onSessionExpired);
  expiredRef.current = onSessionExpired;

  // Advance the identity synchronously during render so a stale response is
  // rejected even in the interval before React runs the selection/session effect.
  const identity = identityRef.current;
  if (identity.token !== token || identity.proposalId !== proposalId
      || identity.sessionGeneration !== sessionGeneration) {
    identityRef.current = {
      token, proposalId, sessionGeneration, id: identity.id + 1,
    };
  }
  const currentIdentity = identityRef.current.id;
  const hasSelection = typeof token === 'string' && token.length > 0
    && typeof proposalId === 'string' && PROPOSAL_ID_PATTERN.test(proposalId);

  useEffect(() => {
    lifecycleActiveRef.current = true;
    const lifecycle = ++lifecycleIdRef.current;
    return () => {
      if (lifecycleIdRef.current === lifecycle) {
        lifecycleActiveRef.current = false;
        lifecycleIdRef.current += 1;
        requestIdRef.current += 1;
      }
    };
  }, []);

  const refresh = useCallback(async () => {
    const capturedIdentity = identityRef.current;
    const capturedToken = token;
    const capturedProposalId = proposalId;
    if (!lifecycleActiveRef.current || !hasSelection
        || capturedIdentity.id !== currentIdentity) return { status: 'idle' };

    const requestId = ++requestIdRef.current;
    const lifecycle = lifecycleIdRef.current;
    const isCurrent = () => requestId === requestIdRef.current
      && capturedIdentity.id === identityRef.current.id
      && capturedToken === identityRef.current.token
      && capturedProposalId === identityRef.current.proposalId
      && sessionGeneration === identityRef.current.sessionGeneration
      && lifecycle === lifecycleIdRef.current;

    setStoredState({
      identity: capturedIdentity.id,
      assessment: null,
      loaded: false,
      loading: true,
      error: null,
    });
    try {
      const assessment = await getIntentAssessment(capturedToken, capturedProposalId);
      if (!isCurrent()) return { status: 'stale' };
      setStoredState({
        identity: capturedIdentity.id,
        assessment,
        loaded: true,
        loading: false,
        error: null,
      });
      return { status: 'success' };
    } catch (error) {
      if (!isCurrent()) return { status: 'stale' };
      setStoredState({
        identity: capturedIdentity.id,
        assessment: null,
        loaded: false,
        loading: false,
        error,
      });
      if (error instanceof ApiError && error.code === 'unauthenticated') {
        expiredRef.current?.();
      }
      return { status: 'error' };
    }
  }, [currentIdentity, hasSelection, proposalId, sessionGeneration, token]);

  useEffect(() => {
    if (!hasSelection) {
      requestIdRef.current += 1;
      setStoredState(emptyState(currentIdentity));
      return undefined;
    }
    void refresh();
    return () => { requestIdRef.current += 1; };
  }, [currentIdentity, evaluationRevision, hasSelection, refresh]);

  const visibleState = storedState.identity === currentIdentity
    ? storedState
    : emptyState(currentIdentity);

  // A null record means retrieval has no persisted assessment to return; it
  // does not establish whether the auditor ran or was bypassed.
  return {
    assessment: visibleState.assessment,
    loading: visibleState.loading,
    error: visibleState.error,
    unavailable: visibleState.loaded && visibleState.assessment === null,
    loaded: visibleState.loaded,
    refresh,
  };
}
