import { CheckCircle, XCircle, Loader } from 'lucide-react';

export function StatusCard({ title, status, loading, error, details }) {
  return (
    <div className="status-card">
      <div className="status-card-header">
        {loading ? (
          <Loader className="status-icon spinning" size={20} />
        ) : error ? (
          <XCircle className="status-icon error" size={20} />
        ) : (
          <CheckCircle className="status-icon success" size={20} />
        )}
        <h3>{title}</h3>
      </div>
      <div className="status-card-body">
        {loading && <p>Checking connection...</p>}
        {error && <p className="error-text">{error}</p>}
        {!loading && !error && details && (
          <ul className="status-details">
            {Object.entries(details).map(([key, value]) => (
              <li key={key}>
                <span className="detail-key">{key}:</span>{' '}
                <span className="detail-value">{String(value)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
