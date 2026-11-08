import { AlertTriangle, Loader, RefreshCw, ShieldAlert, ClipboardList } from 'lucide-react';
import { IntentAssessmentCard } from './IntentAssessmentCard';

const money = (amount, currency) => `${currency || 'Currency unavailable'} ${amount ?? 'Amount unavailable'}`;
const time = (value) => value ? new Date(value).toLocaleString() : 'Time unavailable';

export function ProposalInbox({ proposals }) {
  const { items, selectedId, detail, decision, loading, detailLoading, evaluating, error } = proposals;
  return <section className="mandate-workspace" aria-labelledby="proposal-title">
    <header className="workspace-intro"><div><div className="section-eyebrow"><ClipboardList size={16}/> Purchase review</div><h1 id="proposal-title">Proposal inbox</h1><p>Review requests against LEASH policy. This screen does not authorize or execute payment.</p></div><button className="button-secondary" type="button" onClick={proposals.refresh} disabled={loading}><RefreshCw size={16}/> Refresh</button></header>
    {error && <p className="mandate-alert" role="alert">{error.message}</p>}
    <div className="proposal-layout">
      <section className="mandate-card" aria-label="Proposal list">
        <h2>Recent proposals</h2>
        {loading && <p role="status"><Loader className="spinning" size={16}/> Loading proposals…</p>}
        {!loading && !error && !items.length && <div className="empty-state"><ClipboardList size={26}/><h3>No proposals found</h3><p>Proposals submitted for your mandates will appear here.</p></div>}
        <ul className="mandate-rows">{items.map((item) => <li key={item.proposal_id}><button className={`mandate-row ${selectedId === item.proposal_id ? 'is-selected' : ''}`} type="button" onClick={() => proposals.select(item.proposal_id)}><span className="mandate-row-main"><strong>{item.product_name || 'Product name unavailable'}</strong><span className="mandate-row-summary">{item.merchant || 'Merchant unavailable'} · {money(item.total, item.currency)}</span><small>{item.status} · {time(item.created_at)}</small></span><span className={`status-pill status-${String(item.status).toLowerCase()}`}>{item.status}</span></button></li>)}</ul>
      </section>
      <section className="mandate-card proposal-detail" aria-live="polite">
        {detailLoading && <p role="status"><Loader className="spinning" size={16}/> Loading proposal details…</p>}
        {!detailLoading && !detail && <div className="detail-placeholder"><ClipboardList size={24}/><h2>Select a proposal</h2><p>Choose a request to review its terms and policy evaluation.</p></div>}
        {detail && <>
          <div className="detail-heading"><div><div className="section-eyebrow">Proposal detail</div><h2>{detail.product_name}</h2></div><span className="status-pill">{detail.status}</span></div>
          <div className="detail-id-row"><span>Proposal reference</span><code>{detail.proposal_id}</code></div>
          <dl className="mandate-facts"><div><dt>Merchant</dt><dd>{detail.merchant}</dd></div><div><dt>Product</dt><dd>{detail.product_name}</dd></div><div><dt>Unit price</dt><dd>{money(detail.unit_price, detail.currency)}</dd></div><div><dt>Quantity</dt><dd>{detail.quantity ?? 'Unavailable'}</dd></div><div><dt>Subtotal</dt><dd>{money(detail.subtotal, detail.currency)}</dd></div><div><dt>Total</dt><dd>{money(detail.total, detail.currency)}</dd></div><div><dt>Mandate</dt><dd>{detail.mandate_id}</dd></div><div><dt>Created</dt><dd>{time(detail.created_at)}</dd></div></dl>
          <p className="muted-copy">Description and product source are not included in the proposal response. Merchant content is untrusted.</p>
          <div className="proposal-evaluation"><h3>Deterministic policy review</h3>{decision ? <><div className={`status-pill status-${decision.decision.toLowerCase()}`}>{decision.decision === 'REQUIRE_APPROVAL' ? 'Review required' : decision.decision}</div><p>This backend evaluation is not user approval. BLOCK cannot be overridden. Review required is not approval. User approval is a separate workflow.</p><ul className="rule-list">{decision.rules?.map((rule) => <li key={rule.rule_id}><strong>{rule.rule_id}: {rule.rule_name}</strong><span>{rule.status === 'PASS' ? 'PASS' : rule.status === 'BLOCK' ? 'BLOCK' : rule.status === 'REQUIRE_APPROVAL' ? 'REVIEW REQUIRED' : rule.status === 'NOT_APPLICABLE' ? 'N/A' : 'Unavailable'}</span><p>{rule.reason}</p></li>)}</ul>{decision.blocking_rules?.length > 0 && <p role="alert"><ShieldAlert size={16}/> Blocking rules: {decision.blocking_rules.join(', ')}</p>}</> : <><p>{detail.status === 'EVALUATED' ? 'No current evaluation is available in this response.' : 'Policy evaluation has not been requested.'}</p><p>Evaluation never approves the proposal. Any future approval requires a separate user action.</p><button type="button" className="button-primary" disabled={evaluating} onClick={proposals.evaluate}>{evaluating ? <Loader className="spinning" size={16}/> : null}Evaluate with backend</button></>}</div>
          {proposals.intentAssessment && <IntentAssessmentCard
            assessmentState={proposals.intentAssessment}
            policyDecision={decision}
          />}
          <p className="immutability-note"><AlertTriangle size={14}/> No approval-token or payment actions are available in this review experience.</p>
        </>}
      </section>
    </div>
  </section>;
}
