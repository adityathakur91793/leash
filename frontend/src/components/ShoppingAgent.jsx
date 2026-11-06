import { useEffect, useState } from 'react';
import { listShoppingMandates, getShoppingSession, sendShoppingMessage } from '../services/api';

export function ShoppingAgent() {
  const [token, setToken] = useState('');
  const [mandates, setMandates] = useState([]);
  const [mandateId, setMandateId] = useState('');
  const [message, setMessage] = useState('');
  const [conversation, setConversation] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let mounted = true;
    getShoppingSession()
      .then((sessionToken) => Promise.all([sessionToken, listShoppingMandates(sessionToken)]))
      .then(([sessionToken, owned]) => {
        if (!mounted) return;
        setToken(sessionToken);
        setMandates(owned);
      })
      .catch((err) => mounted && setError(err.message));
    return () => { mounted = false; };
  }, []);

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
      setError(err.message);
      setMessage(sent);
    } finally {
      setBusy(false);
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
      {mandates.length === 0 && <p className="agent-note">No owned active mandate is available. You can still search the simulated catalogue.</p>}
      <form className="agent-form" onSubmit={submit}>
        <label htmlFor="shopping-message">What are you looking for?</label>
        <textarea id="shopping-message" value={message} maxLength={1500} onChange={(event) => setMessage(event.target.value)} placeholder="For example: find an outdoor bottle under $30" />
        <button type="submit" disabled={busy || !token}>{busy ? 'Working…' : 'Ask LEASH'}</button>
      </form>
      {error && <p role="alert" className="agent-error">{error}</p>}
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
