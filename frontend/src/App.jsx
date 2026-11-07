import { CheckCircle, Loader, LogOut, RefreshCw, Shield, XCircle } from 'lucide-react';
import { useBackendStatus } from './hooks/useBackendStatus';
import { useSession } from './hooks/useSession';
import { StatusCard } from './components/StatusCard';
import { ShoppingAgent } from './components/ShoppingAgent';
import './App.css';

function App() {
  const { health, status, loading, error } = useBackendStatus();
  const session = useSession();

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
        <section className="status-section">
          <h2>System Status</h2>
          <div className="status-grid">
            <StatusCard
              title="Backend Health"
              loading={loading}
              error={error}
              details={health}
            />
            <StatusCard
              title="API Status"
              loading={loading}
              error={error}
              details={status}
            />
          </div>
        </section>
        {session.status === 'loading' ? (
          <div className="session-loading" role="status">Checking your session…</div>
        ) : (
          <ShoppingAgent token={session.status === 'active' ? session.token : null}
            onSessionExpired={session.markExpired} />
        )}
      </main>

      <footer className="app-footer">
        <p>LEASH v0.1.0 — Foundation</p>
      </footer>
    </div>
  );
}

export default App;
