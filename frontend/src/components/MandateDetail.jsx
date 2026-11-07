import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, CircleHelp, Loader, RefreshCw, ShieldCheck, XCircle } from 'lucide-react';

function dateLabel(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Unavailable';
}

export function MandateDetail({ mandate, verification, loading, busy, unknown, onRefresh, onVerify, onConfirm, onSign, onActivate, onRevoke }) {
  const [verifying, setVerifying] = useState(false);
  const [actionError, setActionError] = useState('');
  const [revokeDialogOpen, setRevokeDialogOpen] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState('');
  const revokeInFlightRef = useRef(false);
  const cancelRevokeRef = useRef(null);
  const confirmRevokeRef = useRef(null);
  const revokeDialogRef = useRef(null);
  const detailHeadingRef = useRef(null);
  useEffect(() => {
    if (!revokeDialogOpen) return undefined;
    const previousFocus = document.activeElement;
    cancelRevokeRef.current?.focus();
    const handleKeyDown = (event) => {
      if (event.key === 'Escape' && !revokeInFlightRef.current) {
        event.preventDefault();
        setRevokeDialogOpen(false);
        return;
      }
      if (event.key !== 'Tab') return;
      const first = cancelRevokeRef.current;
      const last = confirmRevokeRef.current;
      if (!revokeDialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        first?.focus();
        return;
      }
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
      else detailHeadingRef.current?.focus();
    };
  }, [revokeDialogOpen]);
  useEffect(() => {
    if (revokeDialogOpen && mandate?.status === 'REVOKED') {
      setRevokeDialogOpen(false);
      setRevokeError('');
    }
  }, [revokeDialogOpen, mandate?.mandate_id, mandate?.status]);

  if (loading && !mandate) return <section className="mandate-card detail-loading" role="status"><Loader className="spinning" size={20} /> Loading mandate details…</section>;
  if (!mandate) return <section className="mandate-card detail-placeholder"><CircleHelp size={24} /><h2>Select a mandate</h2><p>Choose one from your list to review its permissions and status.</p></section>;

  const status = mandate.status;
  const active = status === 'ACTIVE';
  const expired = status === 'EXPIRED';
  const revoked = status === 'REVOKED';
  const actionsDisabled = busy || unknown || revoked;

  async function verify() {
    setVerifying(true);
    setActionError('');
    try { await onVerify(mandate.mandate_id); } catch (error) {
      setActionError(error?.message || 'Verification could not be completed.');
    } finally { setVerifying(false); }
  }

  async function run(action) {
    setActionError('');
    try { await action(mandate.mandate_id); } catch (error) {
      setActionError(error?.message || 'The mandate action could not be completed.');
    }
  }

  function openRevokeDialog() {
    setRevokeError('');
    setRevokeDialogOpen(true);
  }

  async function confirmRevocation() {
    if (revokeInFlightRef.current || busy || unknown || revoked) return;
    revokeInFlightRef.current = true;
    setRevoking(true);
    setRevokeError('');
    try {
      const result = await onRevoke(mandate.mandate_id);
      if (result?.mandate_id === mandate.mandate_id && result.status === 'REVOKED') {
        setRevokeDialogOpen(false);
      } else {
        setRevokeError('LEASH did not confirm revocation. The mandate was not marked revoked. Refresh its status or try again when safe.');
      }
    } catch {
      setRevokeError('Revocation could not be confirmed. Refresh the mandate status before trying again.');
    } finally {
      revokeInFlightRef.current = false;
      setRevoking(false);
    }
  }

  return (
    <section className="mandate-card mandate-detail" aria-labelledby="detail-title">
      <div className="detail-heading">
        <div>
          <div className="section-eyebrow"><ShieldCheck size={16} /> Permission details</div>
          <h2 id="detail-title" ref={detailHeadingRef} tabIndex="-1">Mandate details</h2>
        </div>
        <button type="button" className="icon-button" onClick={() => onRefresh(mandate.mandate_id)} disabled={loading} aria-label="Refresh mandate details">
          <RefreshCw size={16} className={loading ? 'spinning' : ''} />
        </button>
      </div>

      <div className="detail-id-row"><span>Mandate ID</span><code>{mandate.mandate_id}</code></div>
      <div className="detail-status-row">
        <span className={`status-pill status-${String(status || 'unknown').toLowerCase()}`}>{status || 'Unknown'}</span>
        {active && <span className="active-note">Server reports this mandate active.</span>}
        {expired && <span className="muted-copy">This mandate has expired and cannot be used.</span>}
        {revoked && <span className="muted-copy">This mandate was revoked and cannot be restored.</span>}
      </div>

      <dl className="mandate-facts">
        <div><dt>Total budget</dt><dd>{mandate.currency} {mandate.max_total_budget}</dd></div>
        <div><dt>Maximum item price</dt><dd>{mandate.max_price_per_item == null ? 'No separate limit' : `${mandate.currency} ${mandate.max_price_per_item}`}</dd></div>
        <div><dt>Maximum item count</dt><dd>{mandate.max_item_count}</dd></div>
        <div><dt>Maximum quantity per item</dt><dd>{mandate.max_quantity_per_item}</dd></div>
        <div><dt>Subscriptions</dt><dd>{mandate.allow_subscriptions ? 'Allowed' : 'Not allowed'}</dd></div>
        <div><dt>Purchase approval</dt><dd>{mandate.require_user_approval ? 'Required' : 'Not required by this mandate'}</dd></div>
        <div><dt>Created</dt><dd>{dateLabel(mandate.created_at)}</dd></div>
        <div><dt>Expires</dt><dd>{dateLabel(mandate.expires_at)}</dd></div>
      </dl>

      <div className="detail-lists">
        <div><h3>Allowed merchants</h3><ul>{mandate.allowed_merchants?.map((merchant) => <li key={merchant}>{merchant}</li>)}</ul></div>
        <div><h3>Allowed categories</h3><ul>{mandate.allowed_categories?.map((category) => <li key={category}>{category}</li>)}</ul></div>
      </div>

      <div className={`verification-panel ${verification ? (verification.valid ? 'verification-valid' : 'verification-invalid') : ''}`}>
        <div className="verification-icon">
          {!verification ? <ShieldCheck size={19} /> : verification.valid ? <CheckCircle2 size={19} /> : <XCircle size={19} />}
        </div>
        <div className="verification-copy">
          <strong>{!verification ? 'Integrity verification' : verification.valid ? 'Verification passed' : 'Verification failed'}</strong>
          <p>{verification ? (verification.reason || 'The backend verified this mandate.') : 'Ask the backend to verify its signature, expiry and lifecycle status.'}</p>
        </div>
        <button type="button" className="button-secondary compact-button" onClick={verify} disabled={verifying || busy}>
          {verifying ? <Loader className="spinning" size={15} /> : null}{verifying ? 'Checking…' : 'Verify'}
        </button>
      </div>

      {unknown && <p className="inline-warning" role="alert">A previous mandate operation has an unknown outcome. Reconcile before trying another action.</p>}
      {actionError && <p className="inline-error" role="alert">{actionError}</p>}
      <div className="lifecycle-actions" aria-label="Mandate lifecycle actions">
        {status === 'DRAFT' && <button className="button-primary" type="button" disabled={actionsDisabled} onClick={() => run(onConfirm)}>Confirm permissions</button>}
        {status === 'CONFIRMED' && <button className="button-primary" type="button" disabled={actionsDisabled} onClick={() => run(onSign)}>Sign mandate</button>}
        {status === 'SIGNED' && <button className="button-primary" type="button" disabled={actionsDisabled} onClick={() => run(onActivate)}>Activate mandate</button>}
        {!revoked && <button className="button-danger" type="button" disabled={busy || unknown || revoked} onClick={openRevokeDialog}>Revoke mandate</button>}
        {busy && <span className="pending-label" role="status"><Loader className="spinning" size={15} /> Updating status…</span>}
      </div>
      <p className="immutability-note"><AlertTriangle size={14} /> Permissions cannot be edited after creation. Lifecycle transitions are checked by LEASH.</p>

      {revokeDialogOpen && (
        <div className="revoke-dialog-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !revokeInFlightRef.current) setRevokeDialogOpen(false);
        }}>
          <section
            ref={revokeDialogRef}
            className="revoke-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="revoke-dialog-title"
            aria-describedby="revoke-dialog-description"
            aria-busy={revoking}
          >
            <div className="revoke-dialog-icon"><AlertTriangle size={22} /></div>
            <h2 id="revoke-dialog-title">Permanently revoke this mandate?</h2>
            <p id="revoke-dialog-description">This action is permanent. <strong>{mandate.mandate_id}</strong> will no longer authorize future purchases, and the revocation cannot be undone.</p>
            {revokeError && <p className="inline-error" role="alert">{revokeError}</p>}
            <div className="revoke-dialog-actions">
              <button
                ref={cancelRevokeRef}
                type="button"
                className="button-secondary"
                disabled={revoking}
                onClick={() => setRevokeDialogOpen(false)}
              >Cancel</button>
              <button
                ref={confirmRevokeRef}
                type="button"
                className="button-danger"
                disabled={revoking || busy || unknown || revoked}
                onClick={confirmRevocation}
              >{revoking ? <><Loader className="spinning" size={16} /> Revoking…</> : 'Confirm Revocation'}</button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
