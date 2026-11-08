import { Loader, RefreshCw, ShieldCheck } from 'lucide-react';

const RISK_LABELS = {
  LOW: 'Low',
  MEDIUM: 'Medium',
  HIGH: 'High',
  UNCERTAIN: 'Uncertain',
};

const RISK_CLASSES = {
  LOW: 'intent-risk-low',
  MEDIUM: 'intent-risk-medium',
  HIGH: 'intent-risk-high',
  UNCERTAIN: 'intent-risk-uncertain',
};

const STATUS_LABELS = {
  VALID: 'Valid assessment',
  FAILED: 'Assessment failed',
  STALE: 'Stale assessment',
};

const STATUS_CLASSES = {
  VALID: 'intent-status-valid',
  FAILED: 'intent-status-failed',
  STALE: 'intent-status-stale',
};

const FINDING_LABELS = {
  AMBIGUOUS_INTENT: 'Ambiguous intent',
  BUDGET_TENSION: 'Budget tension',
  CATEGORY_MISMATCH: 'Category mismatch',
  CURRENCY_INCONSISTENCY: 'Currency inconsistency',
  MERCHANT_MISMATCH: 'Merchant mismatch',
  PRICE_INCONSISTENCY: 'Price inconsistency',
  QUANTITY_INCONSISTENCY: 'Quantity inconsistency',
  SUBSCRIPTION_CONCERN: 'Subscription concern',
  INSUFFICIENT_CONTEXT: 'Insufficient context',
};

const FIELD_LABELS = {
  product_name: 'Product name',
  merchant: 'Merchant',
  category: 'Category',
  quantity: 'Quantity',
  unit_price: 'Unit price',
  total: 'Total',
  currency: 'Currency',
  mandate_budget: 'Mandate budget',
  allowed_merchants: 'Allowed merchants',
  allowed_categories: 'Allowed categories',
  item_limits: 'Item limits',
  subscriptions: 'Subscriptions',
};

function formatTimestamp(value) {
  if (typeof value !== 'string') return 'Timestamp unavailable';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Timestamp unavailable';
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  } catch {
    return 'Timestamp unavailable';
  }
}

function StatusMessage({ children, error = false }) {
  return (
    <p className={error ? 'intent-assessment-message intent-assessment-error' : 'intent-assessment-message'}
      role={error ? 'alert' : 'status'}>
      {children}
    </p>
  );
}

export function IntentAssessmentCard({ assessmentState, policyDecision }) {
  const { assessment, error, loaded, loading, unavailable, refresh } = assessmentState;

  return (
    <section className="intent-assessment-card" aria-labelledby="intent-assessment-title" aria-busy={loading}>
      <header className="intent-assessment-header">
        <div className="intent-assessment-heading">
          <span className="intent-assessment-icon" aria-hidden="true"><ShieldCheck size={19} /></span>
          <div>
            <p className="section-eyebrow">Independent advisory review</p>
            <h3 id="intent-assessment-title">Intent Auditor</h3>
          </div>
        </div>
        <button
          type="button"
          className="button-secondary compact-button intent-assessment-refresh"
          onClick={() => { void refresh(); }}
          disabled={loading}
          aria-label="Refresh Intent Auditor assessment"
        >
          {loading ? <Loader className="spinning" size={15} aria-hidden="true" />
            : <RefreshCw size={15} aria-hidden="true" />}
          <span>{loading ? 'Refreshing' : 'Refresh'}</span>
        </button>
      </header>

      <p className="intent-assessment-advisory">
        Advisory only. This assessment does not approve a purchase or authorize payment.
      </p>

      {policyDecision?.decision === 'BLOCK' && (
        <p className="intent-assessment-policy-note" role="status">
          Deterministic policy BLOCK remains in force. This advisory assessment cannot change that decision.
        </p>
      )}

      {loading && <StatusMessage><Loader className="spinning" size={15} aria-hidden="true" /> Loading assessment data…</StatusMessage>}
      {!loading && error && (
        <div className="intent-assessment-error-state">
          <StatusMessage error>{error.message || 'Assessment data could not be retrieved.'}</StatusMessage>
          <button type="button" className="button-secondary compact-button" onClick={() => { void refresh(); }}>
            <RefreshCw size={15} aria-hidden="true" /> Try again
          </button>
        </div>
      )}
      {!loading && !error && !loaded && (
        <StatusMessage><Loader className="spinning" size={15} aria-hidden="true" /> Loading assessment data…</StatusMessage>
      )}
      {!loading && !error && loaded && unavailable && (
        <div className="intent-assessment-empty">
          <p>No persisted assessment is available for this proposal.</p>
          <p>This does not indicate whether an assessment was run or bypassed.</p>
        </div>
      )}
      {!loading && !error && assessment && (
        <>
          <div className="intent-assessment-badges" aria-label="Assessment summary">
            <span className={`intent-assessment-badge ${RISK_CLASSES[assessment.risk_level] || 'intent-risk-uncertain'}`}>
              Risk level: {RISK_LABELS[assessment.risk_level] || assessment.risk_level}
            </span>
            <span className={`intent-assessment-badge ${STATUS_CLASSES[assessment.status] || 'intent-status-stale'}`}>
              {STATUS_LABELS[assessment.status] || assessment.status}
            </span>
            <span className={`intent-assessment-badge ${assessment.is_current ? 'intent-current' : 'intent-not-current'}`}>
              {assessment.is_current ? 'Current assessment' : 'Not current'}
            </span>
          </div>

          <p className="intent-assessment-timestamp">
            Recorded <time dateTime={Number.isFinite(new Date(assessment.created_at).getTime())
              ? assessment.created_at : undefined}>{formatTimestamp(assessment.created_at)}</time>
          </p>

          <div className="intent-assessment-sections">
            <section aria-labelledby="intent-findings-title">
              <h4 id="intent-findings-title">Findings</h4>
              {assessment.finding_codes.length > 0 ? (
                <ul className="intent-assessment-list">
                  {assessment.finding_codes.map((code, index) => (
                    <li key={`${code}-${index}`}>
                      <span>{FINDING_LABELS[code] || code}</span>
                      <code>{code}</code>
                    </li>
                  ))}
                </ul>
              ) : <p className="intent-assessment-empty-list">No findings were recorded.</p>}
            </section>
            <section aria-labelledby="intent-fields-title">
              <h4 id="intent-fields-title">Affected fields</h4>
              {assessment.affected_fields.length > 0 ? (
                <ul className="intent-assessment-fields">
                  {assessment.affected_fields.map((field, index) => (
                    <li key={`${field}-${index}`}>
                      <span>{FIELD_LABELS[field] || field}</span>
                      <code>{field}</code>
                    </li>
                  ))}
                </ul>
              ) : <p className="intent-assessment-empty-list">No affected fields were recorded.</p>}
            </section>
          </div>
        </>
      )}
    </section>
  );
}
