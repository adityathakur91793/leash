import { useEffect, useState } from 'react';
import { Loader } from 'lucide-react';
import { verifyCheckoutReturn } from '../services/payments';

export function CheckoutReturn({ sessionToken, onSessionExpired }) {
  const [state, setState] = useState(null);
  useEffect(() => {
    const url = new URL(window.location.href);
    const action = url.searchParams.get('checkout');
    const hasProviderReturnParameters = url.searchParams.has('token')
      || url.searchParams.has('PayerID');
    if (action === null && !hasProviderReturnParameters) return undefined;
    const proposalId = url.searchParams.get('proposal_id');
    const orderId = url.searchParams.get('token');
    for (const key of ['token', 'PayerID', 'checkout', 'proposal_id']) {
      url.searchParams.delete(key);
    }
    window.history.replaceState(
      window.history.state, '', url.pathname + url.search + url.hash,
    );
    let active = true;
    if (action === 'cancel') {
      setState({ kind: 'cancel', message: 'You returned from PayPal. LEASH has not recorded a payment.' });
    } else if (action !== 'return') {
      setState({ kind: 'error', message: 'Checkout return could not be recognized. Check the proposal inbox.' });
    } else if (!sessionToken) {
      setState({ kind: 'error', message: 'Start a session to verify this checkout return.' });
    } else if (!proposalId || !orderId) {
      setState({ kind: 'error', message: 'Checkout return details are incomplete. Check the proposal inbox.' });
    } else {
      setState({ kind: 'loading' });
      verifyCheckoutReturn(sessionToken, proposalId, orderId).then((result) => {
        if (!active) return;
        setState({
          kind: result.payment_completed ? 'complete' : 'approved',
          message: result.payment_completed
            ? 'PayPal confirmed settlement. LEASH recorded the completed payment.'
            : result.status === 'APPROVED'
              ? 'PayPal buyer approval was verified. Payment has not been captured.'
              : 'PayPal return was checked. Payment is not confirmed; continue in the proposal inbox.',
        });
      }).catch((failure) => {
        if (!active) return;
        if (failure?.status === 401) onSessionExpired?.();
        setState({
          kind: 'error',
          message: 'PayPal return could not be verified. Check payment status in the proposal inbox.',
        });
      });
    }
    return () => { active = false; };
  }, [onSessionExpired, sessionToken]);

  if (!state) return null;
  return <section className={`checkout-return checkout-return-${state.kind}`} aria-live="polite" role="status">
    {state.kind === 'loading' ? <><Loader className="spinning" size={16} /> Verifying checkout with LEASH and PayPal…</> : state.message}
  </section>;
}
