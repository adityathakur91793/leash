import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle, CheckCircle2, Loader, LockKeyhole, ShieldAlert, ShieldCheck,
} from 'lucide-react';
import { IntentAssessmentCard } from './IntentAssessmentCard';
import { useProposalAuthorization } from '../hooks/useProposalAuthorization';

const money = (amount, currency) => `${currency || 'Currency unavailable'} ${amount ?? 'Amount unavailable'}`;

const inertRoots = new WeakMap();

function acquireApplicationInertness(root) {
  let state = inertRoots.get(root);
  if (!state) {
    state = { count: 0, wasInert: Boolean(root.inert) };
    inertRoots.set(root, state);
  }
  state.count += 1;
  root.inert = true;
  return () => {
    const current = inertRoots.get(root);
    if (!current) return;
    current.count -= 1;
    if (current.count === 0) {
      root.inert = current.wasInert;
      inertRoots.delete(root);
    }
  };
}

function ConfirmationDialog({ proposal, busy, error, retrying, onCancel, onConfirm }) {
  const dialogRef = useRef(null);
  const confirmRef = useRef(null);
  const cancelRef = useRef(null);

  useEffect(() => {
    confirmRef.current?.focus();
    const appRoot = document.getElementById('root');
    const releaseInertness = appRoot ? acquireApplicationInertness(appRoot) : () => {};
    const handleKeyDown = (event) => {
      if (event.key === 'Escape' && !busy) {
        event.preventDefault();
        onCancel();
      }
      if (event.key !== 'Tab') return;
      const first = confirmRef.current;
      const last = cancelRef.current;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    const keepFocusInside = (event) => {
      if (dialogRef.current && !dialogRef.current.contains(event.target)) {
        confirmRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('focusin', keepFocusInside);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.removeEventListener('focusin', keepFocusInside);
      releaseInertness();
    };
  }, [busy, onCancel]);

  return createPortal((
    <div className="approval-dialog-backdrop">
      <section
        className="approval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="purchase-confirmation-title"
        aria-describedby="purchase-confirmation-description"
        ref={dialogRef}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) {
            event.preventDefault();
            event.stopPropagation();
            onCancel();
          }
        }}
      >
        <div className="approval-dialog-icon" aria-hidden="true"><LockKeyhole size={22} /></div>
        <h2 id="purchase-confirmation-title">Confirm purchase consent</h2>
        <p id="purchase-confirmation-description">
          This records your consent for this purchase. It does not create a PayPal order or make a payment.
        </p>
        <dl className="approval-confirmation-facts">
          <div><dt>Merchant</dt><dd>{proposal.merchant || 'Merchant unavailable'}</dd></div>
          <div><dt>Product</dt><dd>{proposal.product_name || 'Product unavailable'}</dd></div>
          <div><dt>Amount</dt><dd>{money(proposal.total, proposal.currency)}</dd></div>
        </dl>
        {error && <p className="approval-inline-error" role="alert">{error}</p>}
        <div className="approval-dialog-actions">
          <button ref={cancelRef} type="button" className="button-secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button ref={confirmRef} type="button" className="button-primary" onClick={onConfirm} disabled={busy}>
            {busy ? <><Loader className="spinning" size={16} aria-hidden="true" /> Recording consent…</>
              : retrying ? 'Recheck authorization' : 'Confirm purchase consent'}
          </button>
        </div>
      </section>
    </div>
  ), document.body);
}

export function ApprovalConsentPanel({
  proposal,
  policyDecision,
  assessment,
  assessmentState,
  sessionToken,
  sessionIdentity,
  onSessionExpired,
  onAuthorizationSnapshot,
  onConsentRecorded,
}) {
  const authorization = useProposalAuthorization({
    sessionToken,
    sessionIdentity,
    proposalId: proposal?.proposal_id,
    policyDecision,
    assessment,
    onSessionExpired,
  });
  const usingAuthoritativeSnapshot = authorization.snapshotState === 'fresh';
  const usingCallerData = authorization.snapshotState === 'idle';
  const visiblePolicyDecision = usingAuthoritativeSnapshot
    ? authorization.authoritativeSnapshot.policyDecision
    : usingCallerData ? policyDecision : null;
  const visibleAssessment = usingAuthoritativeSnapshot
    ? authorization.authoritativeSnapshot.assessment
    : usingCallerData && assessment ? { ...assessment, is_current: false } : null;
  const displayedAssessmentState = usingAuthoritativeSnapshot
    ? {
      assessment: visibleAssessment, error: null, loaded: true, loading: false,
      unavailable: visibleAssessment === null,
      refresh: () => authorization.refreshAuthorization(),
    }
    : authorization.snapshotState === 'loading'
      ? {
        assessment: null, error: null, loaded: false, loading: true,
        unavailable: false, refresh: () => authorization.refreshAuthorization(),
      }
      : authorization.snapshotState === 'unavailable'
        ? {
          assessment: null, error: null, loaded: true, loading: false,
          unavailable: true, refresh: () => authorization.refreshAuthorization(),
        }
        : usingCallerData && visibleAssessment ? {
          assessment: visibleAssessment, error: null, loaded: true, loading: false,
          unavailable: false, refresh: () => authorization.refreshAuthorization(),
        } : assessmentState ? {
          ...assessmentState,
          assessment: visibleAssessment,
          refresh: () => authorization.refreshAuthorization(),
        } : null;
  const [dialogOpen, setDialogOpen] = useState(false);
  const consentButtonRef = useRef(null);
  const dialogWasOpen = useRef(false);
  const panelHeadingRef = useRef(null);
  const notifiedConsentRef = useRef(null);
  const lastNotifiedSnapshotRef = useRef(null);
  const blocked = visiblePolicyDecision?.decision === 'BLOCK';
  const validAssessment = visibleAssessment?.status === 'VALID'
    && visibleAssessment?.is_current === true
    && ['LOW', 'MEDIUM', 'HIGH'].includes(visibleAssessment?.risk_level);
  const unavailable = !visiblePolicyDecision || !visibleAssessment;
  const busy = [
    'refreshing_authorization', 'issuing_review_token', 'consuming_review_token',
    'recording_purchase_consent',
  ]
    .includes(authorization.phase);
  const reviewRequired = visiblePolicyDecision?.decision === 'REQUIRE_APPROVAL';
  const reviewableLifecycle = ['SUBMITTED', 'EVALUATED'].includes(proposal?.status);
  const allowLow = visiblePolicyDecision?.decision === 'ALLOW'
    && visibleAssessment?.risk_level === 'LOW' && validAssessment && reviewableLifecycle;
  const reviewComplete = authorization.phase === 'review_completed';
  const canPrepareConsent = allowLow
    || (reviewRequired && reviewComplete && validAssessment && reviewableLifecycle);

  useEffect(() => {
    if (!dialogOpen && dialogWasOpen.current) {
      if (authorization.phase === 'consent_completed') panelHeadingRef.current?.focus();
      else consentButtonRef.current?.focus();
    }
    dialogWasOpen.current = dialogOpen;
  }, [authorization.phase, dialogOpen]);

  useEffect(() => {
    if (authorization.phase === 'consent_completed' && authorization.consent
        && notifiedConsentRef.current !== authorization.consent) {
      notifiedConsentRef.current = authorization.consent;
      onConsentRecorded?.(authorization.consent);
    } else if (!authorization.consent) {
      notifiedConsentRef.current = null;
    }
  }, [authorization.consent, authorization.phase, onConsentRecorded]);

  useEffect(() => {
    if (authorization.snapshotState === 'fresh' && authorization.authoritativeSnapshot
        && lastNotifiedSnapshotRef.current !== authorization.authoritativeSnapshot) {
      lastNotifiedSnapshotRef.current = authorization.authoritativeSnapshot;
      onAuthorizationSnapshot?.(authorization.authoritativeSnapshot);
    }
  }, [authorization.authoritativeSnapshot, authorization.snapshotState, onAuthorizationSnapshot]);

  const prepareConsent = async () => {
    const ready = await authorization.preparePurchaseConfirmation();
    if (ready) setDialogOpen(true);
  };

  const submitConsent = async () => {
    const accepted = await authorization.submitPurchaseConsent();
    if (accepted) setDialogOpen(false);
  };

  const cancelDialog = () => {
    setDialogOpen(false);
  };

  const confirmDialog = async () => {
    if (authorization.phase === 'error') {
      await authorization.preparePurchaseConfirmation();
      return;
    }
    await submitConsent();
  };

  return (
    <section className="approval-consent-panel" aria-labelledby="approval-consent-title">
      <header className="approval-panel-heading">
        <span className="approval-panel-icon" aria-hidden="true"><ShieldCheck size={20} /></span>
        <div>
          <p className="section-eyebrow">Authorization workflow</p>
          <h3 id="approval-consent-title" ref={panelHeadingRef} tabIndex={-1}>Review and purchase consent</h3>
        </div>
      </header>

      <ol className="approval-steps">
        <li className={reviewComplete ? 'approval-step is-complete' : 'approval-step is-current'}>
          <span className="approval-step-number">1</span>
          <div>
            <strong>Review authorization</strong>
            <p>Policy and auditor information is advisory context. The backend rechecks current authorization for every action.</p>
          </div>
        </li>
        <li className={`approval-step ${reviewComplete || allowLow ? 'is-current' : ''}`}>
          <span className="approval-step-number">2</span>
          <div>
            <strong>Record purchase consent</strong>
            <p>Purchase consent is separate from exception review and does not make a payment.</p>
          </div>
        </li>
      </ol>

      {displayedAssessmentState && (
        <IntentAssessmentCard
          assessmentState={displayedAssessmentState}
          policyDecision={visiblePolicyDecision}
        />
      )}

      {unavailable && !blocked && (
        <div className="approval-state approval-state-warning" role="note">
          <AlertTriangle size={18} aria-hidden="true" />
          <p>A complete current policy and assessment are unavailable. Authorization actions are disabled until authoritative data is refreshed.</p>
        </div>
      )}
      {blocked && (
        <div className="approval-state approval-state-blocked" role="alert">
          <ShieldAlert size={18} aria-hidden="true" />
          <p><strong>Purchase blocked by deterministic policy.</strong> Exception review and purchase consent are unavailable.</p>
        </div>
      )}
      {!blocked && !reviewableLifecycle && (
        <div className="approval-state approval-state-warning" role="note">
          <AlertTriangle size={18} aria-hidden="true" />
          <p>This proposal is no longer in a reviewable state. Refresh its details before taking another authorization action.</p>
        </div>
      )}
      {!blocked && visiblePolicyDecision?.decision === 'ALLOW' && validAssessment
        && visibleAssessment.risk_level !== 'LOW' && (
          <div className="approval-state approval-state-warning" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <p>This proposal cannot currently proceed: ALLOW with {visibleAssessment.risk_level} auditor risk is held for safety. No review bypass is available.</p>
          </div>
      )}
      {!blocked && !unavailable && !validAssessment && (
        <div className="approval-state approval-state-warning" role="alert">
          <AlertTriangle size={18} aria-hidden="true" />
          <p>The assessment is stale, failed, uncertain, or otherwise noncurrent. Authorization actions are unavailable until refreshed.</p>
        </div>
      )}
      {!blocked && reviewRequired && validAssessment && !reviewComplete && (
        <div className="approval-review-callout">
          <p><strong>Explicit exception review is required.</strong> Completing review does not record purchase consent.</p>
          <button
            type="button"
            className="button-primary"
            onClick={() => { void authorization.startExceptionReview(); }}
            disabled={!sessionToken || busy || !validAssessment || !reviewableLifecycle}
          >
            {busy && ['issuing_review_token', 'consuming_review_token'].includes(authorization.phase)
              ? <><Loader className="spinning" size={16} aria-hidden="true" /> Completing review…</>
              : 'Complete exception review'}
          </button>
        </div>
      )}
      {reviewComplete && (
        <div className="approval-state approval-state-reviewed" role="status">
          <CheckCircle2 size={18} aria-hidden="true" />
          <p><strong>Exception review completed.</strong> This is not purchase consent. Review the purchase details and confirm separately.</p>
        </div>
      )}

      {authorization.error && !dialogOpen && (
        <p className="approval-inline-error" role="alert">{authorization.error}</p>
      )}
      {authorization.phase === 'consent_completed' && authorization.consent && (
        <div className="approval-state approval-state-reviewed" role="status">
          <CheckCircle2 size={18} aria-hidden="true" />
          <p><strong>LEASH confirmed proposal status: {authorization.consent.status}.</strong> Purchase consent was recorded. No PayPal order was created and no payment was made.</p>
        </div>
      )}

      {(!canPrepareConsent || authorization.phase === 'error' || blocked) && (
        <button
          type="button"
          className="button-secondary"
          onClick={() => { void authorization.refreshAuthorization(); }}
          disabled={busy || !sessionToken}
        >
          Refresh policy and assessment
        </button>
      )}
      {!blocked && canPrepareConsent && authorization.phase !== 'consent_completed' && !dialogOpen && (
        <button
          ref={consentButtonRef}
          type="button"
          className="button-primary approval-consent-trigger"
          onClick={() => {
            if (authorization.phase === 'awaiting_purchase_confirmation') setDialogOpen(true);
            else void prepareConsent();
          }}
          disabled={!sessionToken || busy}
        >
          {authorization.phase === 'awaiting_purchase_confirmation'
            ? 'Review purchase details'
            : 'Continue to purchase consent'}
        </button>
      )}
      {authorization.phase === 'recording_purchase_consent' && (
        <p className="approval-pending" role="status"><Loader className="spinning" size={16} aria-hidden="true" /> Confirming with the backend…</p>
      )}
      {dialogOpen && authorization.phase !== 'consent_completed' && (
        <ConfirmationDialog
          proposal={proposal}
          busy={authorization.phase === 'recording_purchase_consent'}
          error={authorization.error}
          retrying={authorization.phase === 'error'}
          onCancel={cancelDialog}
          onConfirm={() => { void confirmDialog(); }}
        />
      )}
    </section>
  );
}
