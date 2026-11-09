import { Activity, ArrowRight, CircleDollarSign, FileText, ShieldCheck } from 'lucide-react';

const money = (value, currency) => `${currency} ${value}`;
const hasExactCounts = (value, statuses) => value && typeof value === 'object'
  && !Array.isArray(value) && Object.keys(value).length === statuses.length
  && statuses.every((status) => Number.isInteger(value[status]) && value[status] >= 0);
const PAYMENT_STATUSES = ['CREATED', 'PAYER_ACTION_REQUIRED', 'APPROVED', 'CAPTURING', 'COMPLETED', 'FAILED', 'CANCELLED', 'RECONCILIATION_REQUIRED'];
const ATTEMPT_STATUSES = ['UNKNOWN', 'PROVIDER_CREATED', 'RECONCILIATION_REQUIRED'];
const MANDATE_STATUSES = ['DRAFT', 'CONFIRMED', 'SIGNED', 'ACTIVE', 'EXPIRED', 'REVOKED'];
const PROPOSAL_STATUSES = ['SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED', 'REJECTED', 'WITHDRAWN'];

function isCompleteActivityOverview(data) {
  return data && typeof data === 'object'
    && Array.isArray(data.items) && data.items.length <= 100
    && Number.isInteger(data.total) && data.total >= data.items.length
    && typeof data.has_more === 'boolean' && data.has_more === (data.total > data.items.length)
    && hasExactCounts(data.mandate_status_counts, MANDATE_STATUSES)
    && hasExactCounts(data.proposal_status_counts, PROPOSAL_STATUSES)
    && hasExactCounts(data.payment_status_counts, PAYMENT_STATUSES)
    && Number.isInteger(data.unresolved_checkout_attempt_count)
    && data.unresolved_checkout_attempt_count >= 0
    && hasExactCounts(data.unresolved_checkout_attempt_status_counts, ATTEMPT_STATUSES)
    && data.unresolved_checkout_attempt_count === ATTEMPT_STATUSES.reduce(
      (sum, status) => sum + data.unresolved_checkout_attempt_status_counts[status], 0,
    )
    && Number.isInteger(data.confirmed_settlement_count)
    && data.confirmed_settlement_count >= 0
    && Array.isArray(data.confirmed_settlement_totals)
    && data.confirmed_settlement_totals.every((total) => total
      && /^[A-Z]{3}$/.test(total.currency)
      && typeof total.amount === 'string'
      && /^(?:0|[1-9]\d*)\.\d{2}$/.test(total.amount))
    && typeof data.hash_chain_valid === 'boolean'
    && typeof data.integrity_note === 'string';
}

export function DashboardOverview({ mandates, proposals, mandatesLoading, proposalsLoading, dataErrors, activity, onRefresh, onNavigate }) {
  const activeMandates = mandates.filter((item) => item.status === 'ACTIVE').length;
  const proposalStatuses = ['SUBMITTED', 'EVALUATED', 'APPROVED', 'SETTLED', 'REJECTED', 'WITHDRAWN'];
  const proposalCounts = Object.fromEntries(proposalStatuses.map((status) => [
    status, proposals.filter((item) => item.status === status).length,
  ]));
  const proposalSummary = activity.data?.proposal_status_counts || proposalCounts;
  const mandateSummary = activity.data?.mandate_status_counts;
  const totalMandates = mandateSummary
    ? Object.values(mandateSummary).reduce((sum, count) => sum + count, 0) : mandates.length;
  const activeMandateCount = mandateSummary?.ACTIVE ?? activeMandates;
  const hasActivityData = isCompleteActivityOverview(activity.data);
  const paymentCounts = hasActivityData ? activity.data.payment_status_counts : null;
  const settledCount = hasActivityData ? activity.data.confirmed_settlement_count : null;
  const confirmedTotals = hasActivityData ? activity.data.confirmed_settlement_totals : null;
  const unresolvedAttempts = hasActivityData ? activity.data.unresolved_checkout_attempt_count : null;
  const unresolvedAttemptStates = hasActivityData
    ? activity.data.unresolved_checkout_attempt_status_counts : null;
  const recentEvents = activity.data?.items?.slice(0, 4) || [];
  const hasAccountRecords = totalMandates > 0
    || Object.values(proposalSummary).some((count) => count > 0);

  return (
    <section className="overview-page" aria-labelledby="overview-title">
      <div className="page-heading">
        <div><p className="eyebrow">ACCOUNT OVERVIEW</p><h2 id="overview-title">Dashboard</h2>
          <p>Your mandates, purchase requests and confirmed payment activity.</p></div>
        <button type="button" className="text-action" onClick={() => onNavigate('activity')}>View activity <ArrowRight size={16} /></button>
      </div>
      {(activity.error || activity.loading || mandatesLoading || proposalsLoading || dataErrors.length > 0 || !hasAccountRecords || !hasActivityData) && (
        <div className="overview-notice" role={activity.error ? 'alert' : 'status'}>
          {activity.loading || mandatesLoading || proposalsLoading ? 'Loading your account overview…'
            : activity.error || dataErrors.length || !hasActivityData ? `Some account data is unavailable. ${activity.error?.message || dataErrors[0]?.message || 'Refresh to try again.'}`
              : 'Your account is ready. Create a mandate or submit a proposal to see activity here.'}
          <button type="button" className="secondary-button" onClick={onRefresh}>Refresh</button>
        </div>
      )}
      <div className="metric-grid">
        <article className="metric-card"><span className="metric-icon"><ShieldCheck size={19} /></span>
          <p>Active mandates</p><strong>{activeMandateCount}</strong>
          <small>{totalMandates} total mandates</small></article>
        <article className="metric-card"><span className="metric-icon"><FileText size={19} /></span>
          <p>Purchase proposals</p><strong>{Object.values(proposalSummary).reduce((sum, count) => sum + count, 0)}</strong>
          <small>{proposalSummary.SUBMITTED} submitted · {proposalSummary.EVALUATED} evaluated</small></article>
        <article className="metric-card"><span className="metric-icon"><CircleDollarSign size={19} /></span>
          <p>Confirmed settlements</p><strong>{hasActivityData ? settledCount : '—'}</strong>
          <small>{!hasActivityData ? 'Settlement data unavailable'
            : confirmedTotals.length ? confirmedTotals.map((total) => money(total.amount, total.currency)).join(' · ') : 'No confirmed payments'}</small></article>
        <article className="metric-card"><span className="metric-icon"><Activity size={19} /></span>
          <p>Payments needing attention</p><strong>{paymentCounts ? paymentCounts.RECONCILIATION_REQUIRED + paymentCounts.FAILED : '—'}</strong>
          <small>{paymentCounts ? `${paymentCounts.CAPTURING} capturing · ${paymentCounts.APPROVED} approved` : 'Payment status unavailable'}</small></article>
        <article className="metric-card"><span className="metric-icon"><Activity size={19} /></span>
          <p>Checkout attempts needing recovery</p><strong>{hasActivityData ? unresolvedAttempts : '—'}</strong>
          <small>{unresolvedAttemptStates ? `Unknown ${unresolvedAttemptStates.UNKNOWN} · provider created ${unresolvedAttemptStates.PROVIDER_CREATED} · reconciliation required ${unresolvedAttemptStates.RECONCILIATION_REQUIRED}` : 'Checkout attempt status unavailable'}</small></article>
      </div>

      <div className="overview-columns">
        <section className="overview-card" aria-labelledby="proposal-state-title">
          <div className="section-heading"><div><p className="eyebrow">LIFECYCLE</p><h3 id="proposal-state-title">Proposal status</h3></div>
            <button type="button" className="text-action" onClick={() => onNavigate('proposals')}>Open inbox <ArrowRight size={15} /></button></div>
          {Object.values(proposalSummary).some((count) => count > 0) ? <ul className="status-breakdown">{proposalStatuses.filter((status) => proposalSummary[status] > 0).map((status) => (
            <li key={status}><span className={`status-dot status-${status.toLowerCase()}`} />{status.replaceAll('_', ' ').toLowerCase()}<strong>{proposalSummary[status]}</strong></li>
          ))}</ul> : <p className="muted-copy">No proposals have been submitted yet.</p>}
          <p className="overview-footnote">Proposal status is a workflow indicator. It does not replace current authorization checks.</p>
        </section>
        <section className="overview-card" aria-labelledby="recent-title">
          <div className="section-heading"><div><p className="eyebrow">LATEST</p><h3 id="recent-title">Recent activity</h3></div>
            <button type="button" className="text-action" onClick={() => onNavigate('activity')}>See all <ArrowRight size={15} /></button></div>
          {recentEvents.length ? <ul className="recent-list">{recentEvents.map((event, index) => (
            <li key={`${event.timestamp}:${event.event_type}:${event.entity_type}:${event.display_name}:${index}`}><span><strong>{event.summary}</strong><small>{event.display_name}</small></span>
              <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleDateString()}</time></li>
          ))}</ul> : <p className="muted-copy">{activity.loading ? 'Loading activity…'
            : activity.error || dataErrors.length ? 'Recent activity is unavailable.'
              : 'No recorded activity yet.'}</p>}
        </section>
      </div>
      {hasActivityData && <p className="integrity-inline">Audit hash chain: <strong>{activity.data.hash_chain_valid ? 'verified' : 'verification issue'}</strong>. This verifies log integrity only, not business correctness.</p>}
    </section>
  );
}
