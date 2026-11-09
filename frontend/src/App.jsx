import { useState } from 'react';
import { Activity, CheckCircle, LayoutDashboard, Loader, LogOut, RefreshCw, Shield, ShoppingBag, SlidersHorizontal, XCircle } from 'lucide-react';
import { useBackendStatus } from './hooks/useBackendStatus';
import { useSession } from './hooks/useSession';
import { StatusCard } from './components/StatusCard';
import { ShoppingAgent } from './components/ShoppingAgent';
import { MandateManager } from './components/MandateManager';
import { useMandates } from './hooks/useMandates';
import { useProposals } from './hooks/useProposals';
import { useIntentAssessment } from './hooks/useIntentAssessment';
import { ProposalInbox } from './components/ProposalInbox';
import { CheckoutReturn } from './components/CheckoutReturn';
import { DashboardOverview } from './components/DashboardOverview';
import { ActivityAudit } from './components/ActivityAudit';
import { useActivityOverview } from './hooks/useActivityOverview';
import './App.css';

function App() {
  const { health, status, loading, error } = useBackendStatus();
  const session = useSession();
  const [activeView, setActiveView] = useState('dashboard');
  const mandateState = useMandates(session.status === 'active' ? session.token : null, session.markExpired);
  const proposalState = useProposals(session.status === 'active' ? session.token : null, session.markExpired);
  const intentAssessmentState = useIntentAssessment(
    session.status === 'active' ? session.token : null,
    session.markExpired,
    proposalState.selectedId,
    proposalState.evaluationRevision,
    session.status,
  );
  const proposalInboxState = { ...proposalState, intentAssessment: intentAssessmentState };
  const activityState = useActivityOverview(
    session.status === 'active' ? session.token : null,
    session.markExpired,
    session.status === 'active' ? session.userId : null,
  );

  const sessionLabel = {
    loading: 'Connecting',
    active: 'Session active',
    expired: 'Session ended',
    revoked: 'Session ended',
    error: 'Session unavailable',
  }[session.status];

  return (
    <div className="app">
      <header className="app-header">
        <Shield size={32} className="logo-icon" />
        <h1>LEASH</h1>
        <p className="tagline">The Trust Layer for Agentic Commerce</p>
      </header>

      <main className="app-main">
        <CheckoutReturn
          sessionToken={session.status === 'active' ? session.token : null}
          onSessionExpired={session.markExpired}
        />
        <section className={`session-panel session-${session.status}`} aria-labelledby="session-title">
          <div className="session-summary">
            {session.status === 'loading' ? <Loader className="session-icon spinning" size={20} />
              : session.status === 'active' ? <CheckCircle className="session-icon success" size={20} />
                : <XCircle className="session-icon error" size={20} />}
            <div>
              <h2 id="session-title">{sessionLabel}</h2>
              {session.status === 'active' && session.expiresAt && (
                <p>Available until {new Date(session.expiresAt).toLocaleString()}</p>
              )}
              {(session.status === 'expired' || session.status === 'revoked') && (
                <p>Your session ended. Start a new session to continue; previous account data may not be available.</p>
              )}
              {session.status === 'error' && <p role="alert">{session.error}</p>}
              {session.actionError && <p role="alert">{session.actionError}</p>}
            </div>
          </div>
          <div className="session-actions">
            {session.status === 'active' && (
              <button type="button" className="secondary-button" onClick={session.logout}>
                <LogOut size={16} /> End session
              </button>
            )}
            {(session.status === 'expired' || session.status === 'revoked') && (
              <button type="button" onClick={session.restartSession}>
                <RefreshCw size={16} /> Start new session
              </button>
            )}
            {session.status === 'error' && (
              <button type="button" onClick={session.retrySession}>
                <RefreshCw size={16} /> Retry connection
              </button>
            )}
          </div>
        </section>
        <nav className="primary-nav" aria-label="Main navigation">
          <button type="button" aria-current={activeView === 'dashboard' ? 'page' : undefined} className={activeView === 'dashboard' ? 'nav-active' : ''} onClick={() => setActiveView('dashboard')}><LayoutDashboard size={17} /> Dashboard</button>
          <button type="button" aria-current={activeView === 'mandates' ? 'page' : undefined} className={activeView === 'mandates' ? 'nav-active' : ''} onClick={() => setActiveView('mandates')}><SlidersHorizontal size={17} /> Mandates</button>
          <button type="button" aria-current={activeView === 'proposals' ? 'page' : undefined} className={activeView === 'proposals' ? 'nav-active' : ''} onClick={() => setActiveView('proposals')}>Proposal inbox</button>
          <button type="button" aria-current={activeView === 'shopping' ? 'page' : undefined} className={activeView === 'shopping' ? 'nav-active' : ''} onClick={() => setActiveView('shopping')}><ShoppingBag size={17} /> Shopping agent</button>
          <button type="button" aria-current={activeView === 'activity' ? 'page' : undefined} className={activeView === 'activity' ? 'nav-active' : ''} onClick={() => setActiveView('activity')}><Activity size={17} /> Activity &amp; audit</button>
          <button type="button" aria-current={activeView === 'status' ? 'page' : undefined} className={activeView === 'status' ? 'nav-active' : ''} onClick={() => setActiveView('status')}><Shield size={17} /> System status</button>
        </nav>
        {session.status === 'loading' ? (
          <div className="session-loading" role="status">Checking your session…</div>
        ) : activeView === 'dashboard' ? (
          session.status === 'active' ? <DashboardOverview
            mandates={mandateState.mandates}
            proposals={proposalState.items}
            mandatesLoading={mandateState.loading}
            proposalsLoading={proposalState.loading}
            dataErrors={[mandateState.error, proposalState.error].filter(Boolean)}
            activity={activityState}
            onRefresh={() => { void mandateState.refresh(); void proposalState.refresh(); activityState.refresh(); }}
            onNavigate={setActiveView}
          /> : <section className="mandate-card session-required"><Shield size={24} /><h2>Start a session to view your dashboard</h2><p>Your overview is built from your authenticated LEASH data.</p></section>
        ) : activeView === 'mandates' ? (
          session.status === 'active' ? <MandateManager mandates={mandateState} /> : (
            <section className="mandate-card session-required">
              <Shield size={24} />
              <h2>Start a session to manage mandates</h2>
              <p>Mandates are scoped to your authenticated LEASH session.</p>
            </section>
          )
        ) : activeView === 'proposals' ? (
          session.status === 'active' ? <ProposalInbox
            proposals={proposalInboxState}
            sessionToken={session.token}
            sessionIdentity={session.userId}
            onSessionExpired={session.markExpired}
          /> : (
            <section className="mandate-card session-required"><Shield size={24} /><h2>Start a session to review proposals</h2><p>Proposals are scoped to your authenticated LEASH session.</p></section>
          )
        ) : activeView === 'activity' ? (
          session.status === 'active' ? <ActivityAudit activity={activityState} onRetry={activityState.refresh} /> : <section className="mandate-card session-required"><Shield size={24} /><h2>Start a session to view activity</h2><p>Audit activity is scoped to your authenticated LEASH session.</p></section>
        ) : activeView === 'shopping' ? (
          <ShoppingAgent token={session.status === 'active' ? session.token : null}
            onSessionExpired={session.markExpired} />
        ) : (
          <section className="status-section">
            <h2>System status</h2>
            <div className="status-grid">
              <StatusCard title="Backend Health" loading={loading} error={error} details={health} />
              <StatusCard title="API Status" loading={loading} error={error} details={status} />
            </div>
          </section>
        )}
      </main>

      <footer className="app-footer">
        <p>LEASH v0.1.0 — Foundation</p>
      </footer>
    </div>
  );
}

export default App;
