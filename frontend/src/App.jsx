import { useState } from 'react';
import { CheckCircle, Loader, LogOut, RefreshCw, Shield, ShoppingBag, SlidersHorizontal, XCircle } from 'lucide-react';
import { useBackendStatus } from './hooks/useBackendStatus';
import { useSession } from './hooks/useSession';
import { StatusCard } from './components/StatusCard';
import { ShoppingAgent } from './components/ShoppingAgent';
import { MandateManager } from './components/MandateManager';
import { useMandates } from './hooks/useMandates';
import './App.css';

function App() {
  const { health, status, loading, error } = useBackendStatus();
  const session = useSession();
  const [activeView, setActiveView] = useState('mandates');
  const mandateState = useMandates(session.status === 'active' ? session.token : null, session.markExpired);

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
          <button type="button" aria-current={activeView === 'mandates' ? 'page' : undefined} className={activeView === 'mandates' ? 'nav-active' : ''} onClick={() => setActiveView('mandates')}><SlidersHorizontal size={17} /> Mandates</button>
          <button type="button" aria-current={activeView === 'shopping' ? 'page' : undefined} className={activeView === 'shopping' ? 'nav-active' : ''} onClick={() => setActiveView('shopping')}><ShoppingBag size={17} /> Shopping agent</button>
          <button type="button" aria-current={activeView === 'status' ? 'page' : undefined} className={activeView === 'status' ? 'nav-active' : ''} onClick={() => setActiveView('status')}><Shield size={17} /> System status</button>
        </nav>
        {session.status === 'loading' ? (
          <div className="session-loading" role="status">Checking your session…</div>
        ) : activeView === 'mandates' ? (
          session.status === 'active' ? <MandateManager mandates={mandateState} /> : (
            <section className="mandate-card session-required">
              <Shield size={24} />
              <h2>Start a session to manage mandates</h2>
              <p>Mandates are scoped to your authenticated LEASH session.</p>
            </section>
          )
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
