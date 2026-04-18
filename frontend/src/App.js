import React, { useEffect } from 'react';
import { BrowserRouter, Routes, Route, NavLink, useLocation } from 'react-router-dom';
import { useStore } from './store/useStore';
import Dashboard from './pages/Dashboard';
import IncidentAnalysis from './pages/IncidentAnalysis';
import CausalGraph from './pages/CausalGraph';
import MetricsPage from './pages/MetricsPage';

const NAV_ITEMS = [
  { to: '/',         icon: '⚡', label: 'Dashboard' },
  { to: '/incidents',icon: '🔥', label: 'Incidents' },
  { to: '/graph',    icon: '🕸️', label: 'Causal Graph' },
  { to: '/metrics',  icon: '📈', label: 'Metrics' },
];

function ToastContainer() {
  const toasts = useStore(s => s.toasts);
  return (
    <div className="toast-container">
      {toasts.map(t => (
        <div key={t.id} className={`toast ${t.type}`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

function Sidebar() {
  const wsConnected = useStore(s => s.wsConnected);
  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <div className="logo-icon">⚡</div>
        <div>
          <div className="logo-text">CausalIQ</div>
          <div className="logo-sub">Autonomous RCA Engine</div>
        </div>
      </div>
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(n => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === '/'}
            className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
          >
            <span className="nav-icon">{n.icon}</span>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div style={{ padding: '16px', borderTop: '1px solid var(--border)' }}>
        <div className="live-badge">
          <div className={`live-dot`} style={{ background: wsConnected ? 'var(--accent-green)' : 'var(--accent-red)' }} />
          {wsConnected ? 'Live Connected' : 'Reconnecting...'}
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
          v2.0.0-production
        </div>
      </div>
    </aside>
  );
}

function PageTopBar() {
  const location = useLocation();
  const titles = {
    '/':          'Overview Dashboard',
    '/incidents': 'Incident Analysis',
    '/graph':     'Causal Graph Viewer',
    '/metrics':   'Service Metrics',
  };
  const title = titles[location.pathname] || 'CausalIQ';
  const wsConnected = useStore(s => s.wsConnected);

  return (
    <header className="topbar">
      <span className="topbar-title">{title}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        <div className="live-badge">
          <div className="live-dot" style={{ background: wsConnected ? '' : 'var(--accent-red)' }} />
          Real-time
        </div>
        <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
          {new Date().toLocaleString()}
        </div>
      </div>
    </header>
  );
}

export default function App() {
  const connectWS    = useStore(s => s.connectWS);
  const fetchIncidents = useStore(s => s.fetchIncidents);
  const fetchGraph   = useStore(s => s.fetchGraph);
  const fetchMetrics = useStore(s => s.fetchMetrics);

  useEffect(() => {
    connectWS();
    fetchIncidents();
    fetchGraph();
    fetchMetrics();

    // Poll metrics every 10s
    const interval = setInterval(() => {
      fetchMetrics();
      fetchIncidents();
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  return (
    <BrowserRouter>
      <div className="app-shell">
        <Sidebar />
        <div className="main-content">
          <PageTopBar />
          <Routes>
            <Route path="/"          element={<Dashboard />} />
            <Route path="/incidents" element={<IncidentAnalysis />} />
            <Route path="/graph"     element={<CausalGraph />} />
            <Route path="/metrics"   element={<MetricsPage />} />
          </Routes>
        </div>
      </div>
      <ToastContainer />
    </BrowserRouter>
  );
}
