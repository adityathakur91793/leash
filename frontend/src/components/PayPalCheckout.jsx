import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader, RefreshCw, ShieldCheck } from 'lucide-react';
import {
  captureCheckout, createCheckout, getCheckoutState, navigateToPayPal,
  recoverProposalCheckout, verifyCheckoutReturn,
} from '../services/payments';

export function PayPalCheckout({ proposalId, consent, sessionToken, onSessionExpired }) {
  const [state, setState] = useState(null);
  const [phase, setPhase] = useState('loading');
  const [error, setError] = useState('');
  const [generation, setGeneration] = useState(0);
  const [capturing, setCapturing] = useState(false);
  const identityRef = useRef({ proposalId, sessionToken });
  const inFlightRef = useRef(false);
  const actionInFlightRef = useRef(false);
  const mountedRef = useRef(true);
  identityRef.current = { proposalId, sessionToken };

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const refresh = useCallback(async () => {
    if (!sessionToken || !proposalId) {
      setPhase('unavailable');
      return;
    }
    setPhase('loading');
    setError('');
    const captured = { proposalId, sessionToken };
    try {
      const current = await getCheckoutState(sessionToken, proposalId);
      if (!mountedRef.current || identityRef.current.proposalId !== captured.proposalId
          || identityRef.current.sessionToken !== captured.sessionToken) return;
      if (current.proposal_id !== proposalId) throw new Error('Checkout state did not match this proposal.');
      setState(current);
      setPhase('ready');
    } catch (failure) {
      if (!mountedRef.current || identityRef.current.proposalId !== captured.proposalId
          || identityRef.current.sessionToken !== captured.sessionToken) return;
      if (failure?.status === 401) onSessionExpired?.();
      if (failure?.status === 404) { setState(null); setPhase('ready'); }
      else {
        setError(failure.message || 'Checkout status is unavailable.');
        setPhase('error');
      }
    }
  }, [onSessionExpired, proposalId, sessionToken]);

  useEffect(() => {
    let active = true;
    const run = async () => {
      if (!sessionToken || !proposalId) return;
      try {
        const current = await getCheckoutState(sessionToken, proposalId);
        if (!active || identityRef.current.proposalId !== proposalId
            || identityRef.current.sessionToken !== sessionToken
            || current.proposal_id !== proposalId) return;
        setState(current);
        setPhase('ready');
      } catch (failure) {
        if (!active) return;
        if (failure?.status === 401) onSessionExpired?.();
        if (failure?.status === 404) { setState(null); setPhase('ready'); }
        else { setError(failure.message || 'Checkout status is unavailable.'); setPhase('error'); }
      }
    };
    setPhase('loading');
    setState(null);
    run();
    return () => { active = false; };
  }, [generation, onSessionExpired, proposalId, sessionToken]);

  const begin = async () => {
    if (inFlightRef.current || !sessionToken || !proposalId) return;
    inFlightRef.current = true;
    const captured = { proposalId, sessionToken };
    setPhase('creating');
    setError('');
    try {
      const order = await createCheckout(sessionToken, proposalId);
      if (!mountedRef.current || identityRef.current.proposalId !== captured.proposalId
          || identityRef.current.sessionToken !== captured.sessionToken) return;
      if (order.proposal_id !== captured.proposalId) throw new Error('Checkout response did not match this proposal.');
      setState({ ...order, payment_status: order.status });
      setPhase('ready');
    } catch (failure) {
      if (!mountedRef.current || identityRef.current.proposalId !== captured.proposalId
          || identityRef.current.sessionToken !== captured.sessionToken) return;
      if (failure?.status === 401) onSessionExpired?.();
      setError(failure.message || 'Checkout could not be started.');
      setPhase('error');
      setGeneration((value) => value + 1);
    } finally {
      inFlightRef.current = false;
    }
  };

  const continueCheckout = () => {
    try { navigateToPayPal(state?.approval_url); }
    catch (failure) { setError(failure.message); setPhase('error'); }
  };

  const capture = async () => {
    if (actionInFlightRef.current || capturing || !state?.order_id) return;
    actionInFlightRef.current = true;
    const capturedIdentity = { proposalId, sessionToken };
    setCapturing(true);
    setError('');
    try {
      const result = await captureCheckout(sessionToken, state.order_id, proposalId);
      if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
          || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
      setState((previous) => ({ ...previous, status: 'COMPLETED', payment_status: 'COMPLETED' }));
      setPhase('ready');
      return result;
    } catch (failure) {
      if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
          || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
      if (failure?.status === 401) onSessionExpired?.();
      setError(failure.message || 'Payment status is not confirmed.');
      setPhase('error');
      setGeneration((value) => value + 1);
    } finally {
      actionInFlightRef.current = false;
      if (mountedRef.current) setCapturing(false);
    }
  };

  const reconcile = async () => {
    if (actionInFlightRef.current) return;
    if (!state?.order_id) {
      if (!state?.recovery_available || !sessionToken) return begin();
      actionInFlightRef.current = true;
      const capturedIdentity = { proposalId, sessionToken };
      setPhase('creating');
      setError('');
      try {
        const result = await recoverProposalCheckout(sessionToken, proposalId);
        if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
            || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
        setState((previous) => ({
          ...previous, status: result.status,
          payment_status: result.payment_completed ? 'COMPLETED' : result.provider_status,
          provider_status: result.provider_status,
        }));
        setPhase('ready');
      } catch (failure) {
        if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
            || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
        if (failure?.status === 401) onSessionExpired?.();
        setError(failure.message || 'Saved payment status could not be verified.');
        setPhase('error');
      } finally {
        actionInFlightRef.current = false;
      }
      return;
    }
    actionInFlightRef.current = true;
    const capturedIdentity = { proposalId, sessionToken };
    setPhase('creating');
    setError('');
    try {
      const result = await verifyCheckoutReturn(
        sessionToken, proposalId, state.order_id,
      );
      if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
          || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
      setState((previous) => ({
        ...previous, status: result.status, payment_status: result.status,
      }));
      setPhase('ready');
    } catch (failure) {
      if (!mountedRef.current || identityRef.current.proposalId !== capturedIdentity.proposalId
          || identityRef.current.sessionToken !== capturedIdentity.sessionToken) return;
      if (failure?.status === 401) onSessionExpired?.();
      setError(failure.message || 'PayPal status could not be reconciled.');
      setPhase('error');
    } finally {
      actionInFlightRef.current = false;
    }
  };

  const consentRecorded = consent?.status === 'APPROVED' || state?.consent_recorded === true;
  const recoveryOnly = !consentRecorded && state?.recovery_available === true;
  const pending = ['UNKNOWN', 'REQUESTING', 'RECONCILIATION_REQUIRED', 'CAPTURING']
    .includes(state?.status);
  const captureNeedsReconciliation = ['RECONCILIATION_REQUIRED', 'CAPTURING']
    .includes(state?.status);
  const attemptRecovery = state?.payment_status == null
    && ['PREPARED', 'REQUESTING', 'UNKNOWN', 'RECONCILIATION_REQUIRED']
      .includes(state?.status);
  const manualReconciliation = state?.status === 'RECONCILIATION_REQUIRED'
    && state?.payment_status == null && !state?.order_id && !state?.recovery_available;
  const failed = ['FAILED', 'CANCELLED'].includes(state?.status)
    || ['FAILED', 'CANCELLED'].includes(state?.payment_status);
  const settled = state?.payment_status === 'COMPLETED';
  if (!consentRecorded && !recoveryOnly && phase !== 'error') return null;
  if (!consentRecorded && !recoveryOnly && !error) return null;
  return <section className="paypal-checkout-card" aria-labelledby="paypal-checkout-heading">
    <div className="checkout-heading"><ShieldCheck size={20} aria-hidden="true" />
      <div><h3 id="paypal-checkout-heading">PayPal Sandbox checkout</h3>
        <p>{recoveryOnly
          ? 'Review the saved PayPal payment status. This check cannot authorize a new capture.'
          : 'Purchase consent is recorded. PayPal approval and payment settlement are separate steps.'}</p></div>
    </div>
    {phase === 'loading' && <p role="status"><Loader className="spinning" size={16} /> Checking saved checkout…</p>}
    {phase === 'creating' && <p role="status"><Loader className="spinning" size={16} /> Preparing secure checkout…</p>}
    {error && <p role="alert" className="checkout-error">{error}</p>}
    {phase === 'ready' && settled && <p role="status">Payment is confirmed by LEASH and PayPal.</p>}
    {phase === 'ready' && failed && <p role="alert" className="checkout-error">
      This checkout is marked {state?.payment_status === 'CANCELLED' || state?.status === 'CANCELLED'
        ? 'cancelled' : 'failed'}. LEASH will not retry or replace it automatically. Contact support to reconcile the order.
    </p>}
    {phase === 'ready' && pending && <div role="status" className="checkout-pending">
      <AlertTriangle size={17} /> {recoveryOnly
        ? 'The saved payment needs verification. This check will not create or capture an order.'
        : 'Checkout creation has an uncertain result. LEASH will reconcile the existing attempt before any retry.'}
    </div>}
    {phase === 'ready' && manualReconciliation && <p role="alert" className="checkout-error">
      This attempt needs operator reconciliation. LEASH will not create another PayPal order.
    </p>}
    {phase === 'ready' && state?.status === 'APPROVED' && !settled
      && <p role="status">PayPal approval was verified. Payment is not yet settled.</p>}
    {phase === 'ready' && recoveryOnly && !settled && state?.payment_status
      && <p role="status">{state.provider_status
        ? `PayPal reports ${state.provider_status}.`
        : `Saved checkout status is ${state.payment_status}.`} LEASH has not confirmed payment settlement.</p>}
    {phase === 'ready' && !consentRecorded && <p>Record purchase consent before starting checkout.</p>}
    <div className="checkout-actions">
      {state?.approval_url && !settled && !failed && state?.status !== 'APPROVED' && !captureNeedsReconciliation && <button type="button" className="button-primary"
        disabled={phase === 'creating' || pending} onClick={continueCheckout}>Continue to PayPal Sandbox</button>}
      {!recoveryOnly && state?.status === 'APPROVED' && !settled && <button type="button" className="button-primary"
        disabled={capturing || phase !== 'ready'} onClick={capture}>
        {capturing ? 'Confirming payment…' : 'Capture payment'}
      </button>}
      {consentRecorded && !manualReconciliation
        && (pending || !state?.order_id || phase === 'error') && <button type="button" className="button-secondary"
        disabled={phase === 'creating' || phase === 'loading'}
        onClick={pending ? (attemptRecovery || !state?.order_id ? begin : reconcile)
          : state?.order_id ? refresh : begin}>
        <RefreshCw size={16} /> {pending ? attemptRecovery || !state?.order_id ? 'Reconcile checkout' : 'Reconcile payment'
          : state?.order_id ? 'Retry safely' : 'Start PayPal checkout'}
      </button>}
      {recoveryOnly && state?.recovery_available && <button type="button" className="button-secondary"
        disabled={phase === 'creating' || phase === 'loading'} onClick={reconcile}>
        <RefreshCw size={16} /> {phase === 'creating' ? 'Checking saved payment…' : 'Verify saved payment status'}
      </button>}
    </div>
  </section>;
}
