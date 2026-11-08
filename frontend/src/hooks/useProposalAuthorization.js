import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import {
  consumeApprovalToken,
  issueApprovalToken,
  refreshProposalAuthorization,
  recordPurchaseConsent,
  validateAuthorizationSnapshot,
} from '../services/proposals';

const IDLE = (identity) => ({
  identity, phase: 'idle', error: null, consent: null, snapshot: null,
  snapshotState: 'idle',
});

function currentAssessment(snapshot, proposalId) {
  const decision = snapshot?.policyDecision;
  const assessment = snapshot?.assessment;
  return snapshot?.proposalId === proposalId
    && decision?.decision !== undefined
    && decision.decision !== 'BLOCK'
    && assessment?.status === 'VALID'
    && assessment.is_current === true
    && ['LOW', 'MEDIUM', 'HIGH'].includes(assessment.risk_level);
}

function allowedToProceed(snapshot, proposalId, reviewCompleted) {
  if (!currentAssessment(snapshot, proposalId)) return false;
  if (snapshot.policyDecision.decision === 'ALLOW') {
    return snapshot.assessment.risk_level === 'LOW';
  }
  return snapshot.policyDecision.decision === 'REQUIRE_APPROVAL' && reviewCompleted;
}

function safeError(error) {
  if (error instanceof ApiError && error.code === 'unauthenticated') {
    return 'Your session has expired. Start a new session to continue.';
  }
  return 'The authorization request could not be completed. Refresh the proposal and try again.';
}

/**
 * Own transient approval and consent workflow state for one session and proposal.
 * Each action obtains a validated, coherent snapshot from the authenticated backend
 * authorization-snapshot endpoint before requesting review or purchase consent.
 */
export function useProposalAuthorization({
  sessionToken,
  sessionIdentity = sessionToken,
  proposalId,
  policyDecision = null,
  assessment = null,
  onSessionExpired,
}) {
  const identity = JSON.stringify([sessionIdentity ?? null, sessionToken ?? null, proposalId ?? null]);
  const identityRef = useRef(identity);
  const generationRef = useRef(0);
  const mountedRef = useRef(false);
  const inFlightRef = useRef(false);
  const approvalRef = useRef(null);
  const reviewCompletedRef = useRef(false);
  const latestRef = useRef(null);
  const workflowRef = useRef(IDLE(identity));
  const [workflow, setWorkflow] = useState(() => IDLE(identity));

  latestRef.current = {
    sessionToken, sessionIdentity, proposalId, policyDecision, assessment, onSessionExpired,
  };

  if (identityRef.current !== identity) {
    identityRef.current = identity;
    generationRef.current += 1;
    inFlightRef.current = false;
    approvalRef.current = null;
    reviewCompletedRef.current = false;
    workflowRef.current = IDLE(identity);
  }

  const commit = useCallback((next, expectedIdentity) => {
    if (!mountedRef.current || identityRef.current !== expectedIdentity) return;
    workflowRef.current = { identity: expectedIdentity, ...next };
    setWorkflow(workflowRef.current);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      inFlightRef.current = false;
      approvalRef.current = null;
      reviewCompletedRef.current = false;
    };
  }, []);

  const failClosed = useCallback((message, expectedIdentity, snapshot = null) => {
    commit({
      phase: 'error', error: message, consent: null, snapshot,
      snapshotState: snapshot ? 'fresh' : 'unavailable',
    }, expectedIdentity);
    return false;
  }, [commit]);

  const getFreshSnapshot = useCallback(async (captured, requestGeneration) => {
    if (!captured.sessionToken || !captured.proposalId) {
      throw new ApiError('Current proposal authorization data is unavailable.', {
        code: 'authorization-unavailable',
      });
    }
    const snapshot = validateAuthorizationSnapshot(
      await refreshProposalAuthorization(captured.sessionToken, captured.proposalId),
      captured.proposalId,
    );
    if (identityRef.current !== captured.identity
        || generationRef.current !== requestGeneration || !mountedRef.current) return null;
    return snapshot;
  }, []);

  const reportFailure = useCallback((error, captured) => {
    if (identityRef.current !== captured.identity || !mountedRef.current) return;
    if (error instanceof ApiError && error.code === 'unauthenticated') {
      latestRef.current.onSessionExpired?.();
    }
    commit({
      phase: 'error', error: safeError(error), consent: null,
      snapshot: null, snapshotState: 'unavailable',
    }, captured.identity);
  }, [commit]);

  const startExceptionReview = useCallback(async () => {
    const current = latestRef.current;
    const captured = {
      identity: identityRef.current,
      sessionToken: current.sessionToken,
      proposalId: current.proposalId,
    };
    const phase = workflowRef.current.identity === captured.identity
      ? workflowRef.current.phase : 'idle';
    if (inFlightRef.current || !captured.sessionToken || !captured.proposalId
        || !['idle', 'error'].includes(phase) || reviewCompletedRef.current) return false;

    inFlightRef.current = true;
    const requestGeneration = ++generationRef.current;
    reviewCompletedRef.current = false;
    approvalRef.current = null;
    commit({
      phase: 'issuing_review_token', error: null, consent: null,
      snapshot: null, snapshotState: 'loading',
    }, captured.identity);
    try {
      const snapshot = await getFreshSnapshot(captured, requestGeneration);
      if (!snapshot || identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration) return false;
      commit({
        phase: 'issuing_review_token', error: null, consent: null, snapshot,
        snapshotState: 'fresh',
      }, captured.identity);
      if (!currentAssessment(snapshot, captured.proposalId)
          || snapshot.policyDecision.decision !== 'REQUIRE_APPROVAL') {
        return failClosed(
          'A current assessment and a REQUIRE_APPROVAL policy decision are required for exception review.',
          captured.identity,
          snapshot,
        );
      }

      const approval = await issueApprovalToken(captured.sessionToken, captured.proposalId);
      if (identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration || !mountedRef.current) return false;
      approvalRef.current = approval;
      commit({
        phase: 'consuming_review_token', error: null, consent: null, snapshot,
        snapshotState: 'fresh',
      }, captured.identity);

      const oneTimeApproval = approvalRef.current;
      approvalRef.current = null;
      try {
        const result = await consumeApprovalToken(
          captured.sessionToken, captured.proposalId, oneTimeApproval,
        );
        if (identityRef.current !== captured.identity
            || generationRef.current !== requestGeneration || !mountedRef.current) return false;
        reviewCompletedRef.current = true;
        commit({
          phase: 'review_completed', error: null, consent: null,
          review: result, snapshot, snapshotState: 'fresh',
        }, captured.identity);
        return true;
      } catch (error) {
        approvalRef.current = null;
        reviewCompletedRef.current = false;
        reportFailure(error, captured);
        return false;
      }
    } catch (error) {
      approvalRef.current = null;
      reviewCompletedRef.current = false;
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) reportFailure(error, captured);
      return false;
    } finally {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) inFlightRef.current = false;
    }
  }, [commit, failClosed, getFreshSnapshot, reportFailure]);

  const preparePurchaseConfirmation = useCallback(async () => {
    const current = latestRef.current;
    const captured = {
      identity: identityRef.current,
      sessionToken: current.sessionToken,
      proposalId: current.proposalId,
    };
    if (inFlightRef.current || !captured.sessionToken || !captured.proposalId) return false;
    const phase = workflowRef.current.identity === captured.identity
      ? workflowRef.current.phase : 'idle';
    const reviewed = reviewCompletedRef.current && phase === 'review_completed';
    if (phase !== 'idle' && !reviewed && phase !== 'error') return false;
    inFlightRef.current = true;
    const requestGeneration = ++generationRef.current;
    try {
      const snapshot = await getFreshSnapshot(captured, requestGeneration);
      if (!snapshot || identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration) return false;
      if (!allowedToProceed(snapshot, captured.proposalId, reviewCompletedRef.current)) {
        return failClosed('Current policy and assessment checks do not permit purchase consent.', captured.identity, snapshot);
      }
      if (snapshot.policyDecision.decision === 'REQUIRE_APPROVAL'
          && !reviewCompletedRef.current) {
        return failClosed('Complete exception review before purchase confirmation.', captured.identity, snapshot);
      }
      commit({
        phase: 'awaiting_purchase_confirmation', error: null, consent: null, snapshot,
        snapshotState: 'fresh',
      }, captured.identity);
      return true;
    } catch (error) {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) reportFailure(error, captured);
      return false;
    } finally {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) inFlightRef.current = false;
    }
  }, [commit, failClosed, getFreshSnapshot, reportFailure]);

  const submitPurchaseConsent = useCallback(async () => {
    const current = latestRef.current;
    const captured = {
      identity: identityRef.current,
      sessionToken: current.sessionToken,
      proposalId: current.proposalId,
    };
    if (inFlightRef.current || !captured.sessionToken || !captured.proposalId
        || workflowRef.current.identity !== captured.identity
        || workflowRef.current.phase !== 'awaiting_purchase_confirmation') return false;

    inFlightRef.current = true;
    const requestGeneration = ++generationRef.current;
    commit({
      phase: 'recording_purchase_consent', error: null, consent: null,
      snapshot: null, snapshotState: 'loading',
    }, captured.identity);
    try {
      const snapshot = await getFreshSnapshot(captured, requestGeneration);
      if (!snapshot || identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration) return false;
      commit({
        phase: 'recording_purchase_consent', error: null, consent: null, snapshot,
        snapshotState: 'fresh',
      }, captured.identity);
      if (!allowedToProceed(snapshot, captured.proposalId, reviewCompletedRef.current)) {
        return failClosed('Current policy and assessment checks do not permit purchase consent.', captured.identity, snapshot);
      }
      const result = await recordPurchaseConsent(captured.sessionToken, captured.proposalId);
      if (identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration || !mountedRef.current) return false;
      commit({
        phase: 'consent_completed', error: null, consent: result, snapshot,
        snapshotState: 'fresh',
      }, captured.identity);
      return true;
    } catch (error) {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) reportFailure(error, captured);
      return false;
    } finally {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) inFlightRef.current = false;
    }
  }, [commit, failClosed, getFreshSnapshot, reportFailure]);

  const refreshAuthorization = useCallback(async () => {
    const current = latestRef.current;
    const captured = {
      identity: identityRef.current,
      sessionToken: current.sessionToken,
      proposalId: current.proposalId,
    };
    if (inFlightRef.current || !captured.sessionToken || !captured.proposalId) return false;

    inFlightRef.current = true;
    const requestGeneration = ++generationRef.current;
    commit({
      phase: 'refreshing_authorization', error: null, consent: null,
      snapshot: null, snapshotState: 'loading',
    }, captured.identity);
    try {
      const snapshot = await getFreshSnapshot(captured, requestGeneration);
      if (!snapshot || identityRef.current !== captured.identity
          || generationRef.current !== requestGeneration) return false;
      commit({
        phase: reviewCompletedRef.current ? 'review_completed' : 'idle',
        error: null, consent: null, snapshot, snapshotState: 'fresh',
      }, captured.identity);
      return true;
    } catch (error) {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) reportFailure(error, captured);
      return false;
    } finally {
      if (identityRef.current === captured.identity
          && generationRef.current === requestGeneration) inFlightRef.current = false;
    }
  }, [commit, getFreshSnapshot, reportFailure]);

  const reset = useCallback(() => {
    if (inFlightRef.current) return;
    generationRef.current += 1;
    inFlightRef.current = false;
    approvalRef.current = null;
    reviewCompletedRef.current = false;
    commit(IDLE(identityRef.current), identityRef.current);
  }, [commit]);

  const visibleWorkflow = workflow.identity === identity ? workflow : IDLE(identity);
  return {
    ...visibleWorkflow,
    policyDecision,
    assessment,
    authoritativeSnapshot: visibleWorkflow.snapshot,
    startExceptionReview,
    preparePurchaseConfirmation,
    submitPurchaseConsent,
    refreshAuthorization,
    reset,
  };
}
