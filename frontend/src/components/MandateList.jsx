import { Clock3, RefreshCw, ShieldCheck, Plus } from 'lucide-react';

function expiryLabel(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleDateString() : 'Expiry unavailable';
}

export function MandateList({ mandates, selectedId, loading, error, onSelect, onRefresh, onCreate }) {
  return (
    <section className="mandate-card mandate-list" aria-labelledby="mandate-list-title">
      <div className="list-heading">
        <div>
          <div className="section-eyebrow"><ShieldCheck size={16} /> Your permissions</div>
          <h2 id="mandate-list-title">Mandates</h2>
          <p className="muted-copy">Showing up to the 100 most recent mandates.</p>
        </div>
        <div className="list-actions">
          <button type="button" className="icon-button" onClick={onRefresh} disabled={loading} aria-label="Refresh mandates">
            <RefreshCw size={16} className={loading ? 'spinning' : ''} />
          </button>
          <button type="button" className="button-primary" onClick={onCreate}><Plus size={16} /> New mandate</button>
        </div>
      </div>

      {error && <p className="inline-error" role="alert">{error.message || 'Unable to load mandates.'}</p>}
      {loading && mandates.length === 0 && <div className="empty-state" role="status"><RefreshCw className="spinning" size={20} /><p>Loading your mandates…</p></div>}
      {!loading && !error && mandates.length === 0 && (
        <div className="empty-state">
          <span className="empty-icon"><ShieldCheck size={24} /></span>
          <h3>No mandates yet</h3>
          <p>Create a permission set to define what an agent may propose.</p>
          <button type="button" className="button-primary" onClick={onCreate}><Plus size={16} /> Create your first mandate</button>
        </div>
      )}
      {mandates.length > 0 && (
        <ul className="mandate-rows">
          {mandates.map((mandate) => (
            <li key={mandate.mandate_id}>
              <button type="button" className={`mandate-row ${selectedId === mandate.mandate_id ? 'is-selected' : ''}`} onClick={() => onSelect(mandate.mandate_id)} aria-current={selectedId === mandate.mandate_id ? 'true' : undefined}>
                <span className="mandate-row-main">
                  <span className="mandate-row-title">{mandate.currency} {mandate.max_total_budget}</span>
                  <span className="mandate-row-summary">{mandate.allowed_merchants?.slice(0, 2).join(', ') || 'Merchant limits unavailable'}{mandate.allowed_merchants?.length > 2 ? ` +${mandate.allowed_merchants.length - 2}` : ''}</span>
                  <span className="mandate-row-expiry"><Clock3 size={13} /> Expires {expiryLabel(mandate.expires_at)}</span>
                </span>
                <span className={`status-pill status-${String(mandate.status || 'unknown').toLowerCase()}`}>{mandate.status || 'Unknown'}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
