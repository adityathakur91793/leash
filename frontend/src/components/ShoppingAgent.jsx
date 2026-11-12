import { useEffect, useState } from 'react';
import { ApiError, listShoppingMandates, sendShoppingMessage } from '../services/api';
import { createProposal } from '../services/proposals';

export function ShoppingAgent({ token, onSessionExpired }) {
  const [mandates, setMandates] = useState([]);
  const [mandateId, setMandateId] = useState('');
  const [message, setMessage] = useState('');
  const [conversation, setConversation] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [proposalFeedback, setProposalFeedback] = useState(null);
  const [submittingProductId, setSubmittingProductId] = useState(null);
  const [loadingMandates, setLoadingMandates] = useState(false);

  useEffect(() => {
    let mounted = true;
    if (!token) {
      setMandates([]);
      setMandateId('');
      return () => { mounted = false; };
    }
    setError('');
    setMandates([]);
    setLoadingMandates(true);
    listShoppingMandates(token)
      .then((owned) => {
        if (mounted) setMandates(owned || []);
      })
      .catch((err) => {
        if (!mounted) return;
        setError(err.message);
        if (err instanceof ApiError && err.code === 'unauthenticated') onSessionExpired();
      })
      .finally(() => mounted && setLoadingMandates(false));
    return () => { mounted = false; };
  }, [token, onSessionExpired]);

  async function submit(event) {
    event.preventDefault();
    if (!message.trim() || !token || busy) return;
    setBusy(true);
    setError('');
    const sent = message.trim();
    setMessage('');
    try {
      const result = await sendShoppingMessage(token, sent, mandateId);
      setConversation((items) => [...items, { sent, result }]);
    } catch (err) {
      setError(err.message || 'The request could not be completed. Please try again.');
      if (err instanceof ApiError && err.code === 'unauthenticated') onSessionExpired();
      setMessage(sent);
    } finally {
      setBusy(false);
    }
  }

  function canSubmitProduct(product) {
    return Boolean(
      mandateId
      && token
      && product
      && product.availability === 'SIMULATED_AVAILABLE'
      && product.price_status === 'SIMULATED'
      && product.unit_price != null
      && product.currency
      && product.merchant_id
      && product.category
    );
  }

  async function submitPurchaseProposal(product) {
    if (!canSubmitProduct(product) || !token || submittingProductId === product.product_id) return;
    setSubmittingProductId(product.product_id);
    setProposalFeedback(null);
    setError('');
    try {
      const payload = {
        mandate_id: mandateId,
        product_id: product.product_id,
        product_name: product.name,
        merchant: product.merchant_id,
        category: product.category,
        quantity: 1,
        unit_price: String(product.unit_price),
        currency: product.currency,
        tax: String(product.tax ?? '0.00'),
        shipping: String(product.shipping ?? '0.00'),
        other_charges: String(product.other_charges ?? '0.00'),
        product_source: product.source ?? `https://catalog.leash.invalid/products/${product.product_id}`,
      };
      const result = await createProposal(token, payload);
      const successMessage = result?.proposal_id
        ? `Proposal ${result.proposal_id} submitted for review under the active mandate.`
        : 'Proposal submitted for review under the active mandate.';
      setProposalFeedback({ type: 'success', message: successMessage });
      window.dispatchEvent(new CustomEvent('leash:proposal-list-refresh'));
    } catch (err) {
      const message = err.message || 'The purchase proposal could not be submitted.';
      setProposalFeedback({ type: 'error', message });
      if (err instanceof ApiError && err.code === 'unauthenticated') onSessionExpired();
    } finally {
      setSubmittingProductId(null);
    }
  }

  return (
    <section className="shopping-agent" aria-labelledby="shopping-title">
      <div className="shopping-heading">
        <div>
          <h2 id="shopping-title">AI Shopping Agent</h2>
          <p>AI proposes. LEASH policy evaluates. You review separately.</p>
        </div>
        <span className="simulated-badge">SIMULATED CATALOGUE</span>
      </div>
      <label className="mandate-select">
        Purchase mandate
        <select value={mandateId} onChange={(event) => setMandateId(event.target.value)}>
          <option value="">Browse only — no proposal</option>
          {mandates.map((mandate) => (
            <option key={mandate.mandate_id} value={mandate.mandate_id}>
              {mandate.currency} {mandate.max_total_budget} · expires {new Date(mandate.expires_at).toLocaleDateString()}
            </option>
          ))}
        </select>
      </label>
      {loadingMandates && <p className="agent-note" role="status">Loading your active mandates…</p>}
      {!loadingMandates && mandates.length === 0 && <p className="agent-note">No owned active mandate is available. You can still search the simulated catalogue.</p>}
      <form className="agent-form" onSubmit={submit}>
        <label htmlFor="shopping-message">What are you looking for?</label>
        <textarea id="shopping-message" value={message} maxLength={1500} onChange={(event) => setMessage(event.target.value)} placeholder="For example: find an outdoor bottle under $30" />
        <button type="submit" disabled={busy || !token}>{busy ? 'Working…' : 'Ask LEASH'}</button>
      </form>
      {error && <p role="alert" className="agent-error">{error}</p>}
      {proposalFeedback && (
        <p role={proposalFeedback.type === 'error' ? 'alert' : 'status'} className={proposalFeedback.type === 'error' ? 'agent-error' : 'agent-success'}>{proposalFeedback.message}</p>
      )}
      <div className="agent-conversation" aria-live="polite">
        {conversation.map(({ sent, result }, index) => (
          <article className="agent-turn" key={`${index}-${sent}`}>
            <p className="user-message">{sent}</p>
            <p>{result.message}</p>
            {result.products?.map((product) => (
              <div className="product-card" key={product.product_id}>
                <div className="product-title-row">
                  <strong>{product.name}</strong>
                  <span className="simulated-badge">CATALOGUE FIXTURE</span>
                </div>
                <p>Description ({product.description_status.toLowerCase()}): {product.description || 'unavailable or withheld.'}</p>
                <dl>
                  <div><dt>Price ({product.price_status.toLowerCase()})</dt><dd>{product.unit_price == null ? 'Unavailable' : `${product.currency} ${product.unit_price} (simulated)`}</dd></div>
                  <div><dt>Merchant</dt><dd>{product.merchant_name} (simulated catalogue)</dd></div>
                  <div><dt>Availability ({product.availability_status.toLowerCase()})</dt><dd>{product.availability.replaceAll('_', ' ').toLowerCase()}</dd></div>
                </dl>
                <button
                  type="button"
                  className="button-primary"
                  disabled={!canSubmitProduct(product) || !token || submittingProductId === product.product_id}
                  onClick={() => void submitPurchaseProposal(product)}
                >
                  {submittingProductId === product.product_id ? 'Submitting…' : 'Propose purchase'}
                </button>
              </div>
            ))}
            {result.proposal && (
              <div className="proposal-outcome">
                <h3>Proposal outcome</h3>
                <p>Proposal: <strong>{result.proposal.status}</strong></p>
                <p>Policy: <strong>{result.proposal.policy_decision.decision}</strong></p>
                <p>This is not a payment. Review and approval remain separate.</p>
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
