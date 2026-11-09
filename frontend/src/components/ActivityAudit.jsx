import { AlertTriangle, CheckCircle2, Clock3, ShieldCheck } from 'lucide-react';

const ACTOR_LABELS = { USER: 'You', AGENT: 'Shopping agent', SYSTEM: 'LEASH system', ADMIN: 'Administrator', POLICY_ENGINE: 'Policy engine' };

export function ActivityAudit({ activity, onRetry }) {
  const data = activity.data;
  return (
    <section className="activity-page" aria-labelledby="activity-title">
      <div className="page-heading"><div><p className="eyebrow">ACCOUNT RECORD</p><h2 id="activity-title">Activity &amp; audit</h2>
        <p>Read-only history of recorded mandate, proposal and payment events.</p></div></div>
      {activity.loading && <div className="overview-notice" role="status">Loading your activity…</div>}
      {activity.error && <div className="overview-notice error-notice" role="alert"><span>{activity.error.message}</span>
        <button type="button" className="secondary-button" onClick={onRetry}>Retry</button></div>}
      {data && <>
        <div className={`integrity-banner ${data.hash_chain_valid ? 'integrity-valid' : 'integrity-warning'}`} role="status">
          {data.hash_chain_valid ? <CheckCircle2 size={20} /> : <AlertTriangle size={20} />}
          <div><strong>{data.hash_chain_valid ? 'Audit hash chain verified' : 'Audit hash-chain verification needs attention'}</strong>
            <p>{data.integrity_note}</p></div>
        </div>
        <section className="activity-card" aria-label="Recorded events">
          {data.items.length ? <ol className="activity-list">{data.items.map((event, index) => (
            <li key={`${event.timestamp}:${event.event_type}:${event.entity_type}:${event.display_name}:${index}`}>
              <span className="activity-marker"><Clock3 size={16} /></span>
              <div className="activity-event"><div className="activity-event-heading"><div><strong>{event.summary}</strong><span className="activity-entity">{event.display_name} · {event.entity_type.toLowerCase()}</span></div>
                <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString()}</time></div>
                <p>{ACTOR_LABELS[event.actor] || 'LEASH system'} · {event.event_type.replaceAll('_', ' ').toLowerCase()}</p>
                {event.amount && event.currency && <small>Recorded amount {event.amount} {event.currency}</small>}
              </div>
            </li>
          ))}</ol> : <div className="activity-empty"><ShieldCheck size={25} /><h3>No activity recorded</h3><p>Events will appear here after a mandate, proposal or payment lifecycle action.</p></div>}
          {data.has_more && <p className="activity-pagination-note">Showing the latest 100 of {data.total} events.</p>}
        </section>
      </>}
    </section>
  );
}
