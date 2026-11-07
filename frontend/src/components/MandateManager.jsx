import { useState } from 'react';
import { AlertTriangle, Loader, Plus, ShieldCheck } from 'lucide-react';
import { MANDATE_LIST_LIMITATION } from '../services/mandates';
import { MandateCreateForm } from './MandateCreateForm';
import { MandateDetail } from './MandateDetail';
import { MandateList } from './MandateList';

export function MandateManager({ mandates }) {
  const [view, setView] = useState('list');
  const [reconciling, setReconciling] = useState(false);
  const unknown = Boolean(mandates.unknownOperation);
  const busy = Boolean(mandates.pendingOperation && !mandates.pendingOperation.outcome);

  async function transition(action, id) {
    const updated = await action(id);
    if (updated?.mandate_id) await mandates.refreshDetail(id);
    return updated;
  }

  async function reconcile() {
    setReconciling(true);
    try { await mandates.reconcileUnknown(); } finally { setReconciling(false); }
  }

  return (
    <section className="mandate-workspace" aria-labelledby="mandates-page-title">
      <div className="workspace-intro">
        <div>
          <div className="section-eyebrow"><ShieldCheck size={16} /> Authorization controls</div>
          <h1 id="mandates-page-title">Mandate management</h1>
          <p>Set the boundaries for agentic purchases. LEASH enforces every permission on the backend.</p>
        </div>
        {view === 'list' && <button type="button" className="button-primary desktop-create" onClick={() => setView('create')}><Plus size={16} /> New mandate</button>}
      </div>

      <p className="list-limitation">{MANDATE_LIST_LIMITATION}</p>

      {mandates.error && <div className="mandate-alert" role="alert"><AlertTriangle size={18} /><div><span>{mandates.error.message}</span>{Array.isArray(mandates.error.details) && mandates.error.details.length > 0 && <ul className="validation-error-list">{mandates.error.details.map((field, index) => <li key={`${field.loc.join('.')}-${index}`}>{field.loc.filter((part) => part !== 'body').join('.')}: {field.msg}</li>)}</ul>}</div></div>}
      {unknown && (
        <div className="unknown-banner" role="alert">
          <div><strong>Operation outcome unknown</strong><p>{mandates.unknownOperation.message || 'The backend did not confirm this operation.'} Do not repeat the operation.{mandates.unknownOperation.name === 'create' && ' A list refresh cannot prove which mandate was created.'}</p></div>
          <button type="button" className="button-secondary" onClick={reconcile} disabled={reconciling || mandates.reconciliationLoading}>
            {reconciling || mandates.reconciliationLoading ? <Loader className="spinning" size={15} /> : null}
            Refresh backend records
          </button>
        </div>
      )}

      {view === 'create' ? (
        <MandateCreateForm
          onCreate={mandates.create}
          onCancel={() => setView('list')}
          disabled={unknown || busy || !mandates.markerStorageAvailable}
          onCreated={(id) => { mandates.selectMandate(id); setView('list'); }}
        />
      ) : (
        <div className="mandate-layout">
          <MandateList
            mandates={mandates.mandates}
            selectedId={mandates.selectedMandateId}
            loading={mandates.listLoading}
            error={mandates.error?.status ? null : mandates.error}
            onSelect={mandates.selectMandate}
            onRefresh={mandates.refresh}
            onCreate={() => setView('create')}
          />
          <MandateDetail
            mandate={mandates.selectedMandate}
            verification={mandates.verification}
            loading={mandates.detailLoading}
            busy={busy}
            unknown={unknown}
            onRefresh={mandates.refreshDetail}
            onVerify={mandates.verify}
            onConfirm={(id) => transition(mandates.confirm, id)}
            onSign={(id) => transition(mandates.sign, id)}
            onActivate={(id) => transition(mandates.activate, id)}
            onRevoke={(id) => transition(mandates.revoke, id)}
          />
        </div>
      )}

      <p className="mandate-footnote">{mandates.listLoading ? 'Refreshing mandate records…' : 'Mandate records and authorization decisions are owned by LEASH.'}</p>
    </section>
  );
}
