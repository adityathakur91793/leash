import { Shield } from 'lucide-react';
import { useBackendStatus } from './hooks/useBackendStatus';
import { StatusCard } from './components/StatusCard';
import './App.css';

function App() {
  const { health, status, loading, error } = useBackendStatus();

  return (
    <div className="app">
      <header className="app-header">
        <Shield size={32} className="logo-icon" />
        <h1>LEASH</h1>
        <p className="tagline">The Trust Layer for Agentic Commerce</p>
      </header>

      <main className="app-main">
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
      </main>

      <footer className="app-footer">
        <p>LEASH v0.1.0 — Foundation</p>
      </footer>
    </div>
  );
}

export default App;
