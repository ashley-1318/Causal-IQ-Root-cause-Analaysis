import React, { useState, useEffect } from 'react';
import { useStore } from '../store/useStore';
import { formatDistanceToNow } from 'date-fns';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, LineChart, Line, Legend
} from 'recharts';

// ── Helpers ──────────────────────────────────────────────────────────────────
function confColor(c) {
  if (c >= 0.8) return 'var(--accent-red)';
  if (c >= 0.5) return 'var(--accent-yellow)';
  return 'var(--accent-green)';
}

function severityBadge(confidence) {
  if (confidence >= 0.8) return <span className="badge badge-red">Critical</span>;
  if (confidence >= 0.5) return <span className="badge badge-yellow">High</span>;
  return <span className="badge badge-blue">Medium</span>;
}

// ── Live Feed ─────────────────────────────────────────────────────────────────
function LiveFeed() {
  const liveEvents = useStore(s => s.liveEvents);
  const recent = liveEvents.slice(0, 15);

  return (
    <div className="card" style={{ flex: 1 }}>
      <div className="card-header">
        <div>
          <div className="card-title">Live Event Stream</div>
          <div className="card-subtitle">Real-time from Kafka topics</div>
        </div>
        <span className="badge badge-green">{liveEvents.length} events</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '320px', overflowY: 'auto' }}>
        {recent.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', fontSize: '13px', padding: '20px 0', textAlign: 'center' }}>
            Waiting for events…
          </div>
        ) : recent.map((e, i) => (
          <div key={i} className="fade-in" style={{
            background: 'var(--bg-elevated)', borderRadius: 'var(--radius-md)',
            padding: '10px 12px', display: 'flex', gap: '10px', alignItems: 'flex-start',
            borderLeft: `3px solid ${e._topic === 'rca-results' ? 'var(--accent-red)' : 'var(--accent-yellow)'}`,
          }}>
            <span style={{ fontSize: '16px' }}>{e._topic === 'rca-results' ? '🚨' : '⚠️'}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {e._ts ? new Date(e._ts).toLocaleTimeString() : ''} · {e._topic}
              </div>
              <div style={{ fontSize: '12px', color: 'var(--text-primary)', marginTop: '2px', wordBreak: 'break-word' }}>
                {e._topic === 'rca-results'
                  ? `RCA: ${(e.anomalies || [])[0]?.service || 'unknown'} — ${(e.anomalies || []).length} anomalies`
                  : `Features: ${(e.features || []).map(f => f.service).join(', ')}`}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Active Alerts ─────────────────────────────────────────────────────────────
function ActiveAlerts() {
  const incidents = useStore(s => s.incidents);
  const recent    = incidents.slice(0, 3);

  if (recent.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      {recent.map(inc => (
        <div key={inc.incident_id} className="alert-banner">
          <span className="alert-icon">🚨</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: '13px', color: 'var(--accent-red)' }}>
              Root Cause: <span style={{ fontFamily: 'var(--font-mono)' }}>{inc.root_cause}</span>
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
              Confidence {(inc.confidence * 100).toFixed(1)}% · {inc.anomaly_count} anomalies ·{' '}
              {inc.created_at ? formatDistanceToNow(new Date(inc.created_at), { addSuffix: true }) : ''}
            </div>
          </div>
          {severityBadge(inc.confidence)}
        </div>
      ))}
    </div>
  );
}

// ── Load Trigger Panel ─────────────────────────────────────────────────────────
function LoadTriggerPanel() {
  const triggerLoad = useStore(s => s.triggerLoad);
  const [cfg, setCfg] = useState({
    duration_seconds: 60,
    concurrency: 10,
    inject_fault: true,
    fault_db_latency_ms: 500,
  });
  const [running, setRunning] = useState(false);

  async function handleTrigger() {
    setRunning(true);
    await triggerLoad(cfg);
    setTimeout(() => setRunning(false), cfg.duration_seconds * 1000);
  }

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="card-title">🎯 Incident Simulation</div>
          <div className="card-subtitle">Generate real traffic + inject faults</div>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '16px' }}>
        {[
          { label: 'Duration (sec)', key: 'duration_seconds', min: 10, max: 300 },
          { label: 'Concurrency', key: 'concurrency', min: 1, max: 50 },
          { label: 'DB Fault Latency (ms)', key: 'fault_db_latency_ms', min: 100, max: 5000 },
        ].map(f => (
          <div key={f.key}>
            <label style={{ fontSize: '11px', color: 'var(--text-secondary)', display: 'block', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>
              {f.label}
            </label>
            <input
              type="number" min={f.min} max={f.max}
              value={cfg[f.key]}
              onChange={e => setCfg(p => ({ ...p, [f.key]: +e.target.value }))}
              style={{
                width: '100%', padding: '8px 10px',
                background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius-md)', color: 'var(--text-primary)', fontSize: '13px',
              }}
            />
          </div>
        ))}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <input
            type="checkbox"
            id="fault-toggle"
            checked={cfg.inject_fault}
            onChange={e => setCfg(p => ({ ...p, inject_fault: e.target.checked }))}
            style={{ accentColor: 'var(--accent-red)', width: '16px', height: '16px' }}
          />
          <label htmlFor="fault-toggle" style={{ fontSize: '13px', color: 'var(--text-primary)', cursor: 'pointer', fontWeight: 500 }}>
            Inject DB Fault
          </label>
        </div>
      </div>
      <button
        className={`btn ${running ? 'btn-ghost' : 'btn-danger'}`}
        onClick={handleTrigger}
        disabled={running}
        style={{ width: '100%', justifyContent: 'center', padding: '10px' }}
      >
        {running ? (
          <><span className="spinner" style={{ width: 16, height: 16 }} /> Running…</>
        ) : (
          '🚀 Launch Incident Simulation'
        )}
      </button>
    </div>
  );
}

// ── Latency Spark ─────────────────────────────────────────────────────────────
function MetricsOverview() {
  const metrics = useStore(s => s.metrics);
  const COLORS  = ['var(--accent-blue)', 'var(--accent-purple)', 'var(--accent-cyan)', 'var(--accent-green)'];

  if (metrics.length === 0) {
    return (
      <div className="card" style={{ flex: 1 }}>
        <div className="card-header"><div className="card-title">Service Latency</div></div>
        <div className="loading-center"><div className="spinner" /></div>
      </div>
    );
  }

  const chartData = metrics.map(m => ({
    name: m.service,
    avg: Math.round(m.avg_latency_ms),
    p99: Math.round(m.p99_latency_ms),
    errPct: +(m.error_rate * 100).toFixed(2),
  }));

  return (
    <div className="card" style={{ flex: 1 }}>
      <div className="card-header">
        <div><div className="card-title">Service Latency (avg vs p99)</div><div className="card-subtitle">Last 5 minutes</div></div>
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <AreaChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="avgGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--accent-blue)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--accent-blue)" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="p99Grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--accent-red)" stopOpacity={0.3} />
              <stop offset="95%" stopColor="var(--accent-red)" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis dataKey="name" tick={{ fill: 'var(--text-muted)', fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: 'var(--text-muted)', fontSize: 11 }} axisLine={false} tickLine={false} unit="ms" />
          <Tooltip
            contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: 'var(--text-primary)' }}
          />
          <Area type="monotone" dataKey="avg" stroke="var(--accent-blue)" fill="url(#avgGrad)" strokeWidth={2} name="Avg Latency" />
          <Area type="monotone" dataKey="p99" stroke="var(--accent-red)"  fill="url(#p99Grad)"  strokeWidth={2} name="P99 Latency" />
          <Legend wrapperStyle={{ fontSize: 12, color: 'var(--text-secondary)' }} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Main Dashboard ─────────────────────────────────────────────────────────────
export default function Dashboard() {
  const incidents  = useStore(s => s.incidents);
  const anomalies  = useStore(s => s.anomalies);
  const metrics    = useStore(s => s.metrics);
  const wsConnected = useStore(s => s.wsConnected);

  const totalErr   = metrics.reduce((a, m) => a + m.error_rate, 0) / (metrics.length || 1);
  const avgLat     = metrics.reduce((a, m) => a + m.avg_latency_ms, 0) / (metrics.length || 1);
  const critCount  = incidents.filter(i => i.confidence >= 0.8).length;

  return (
    <div className="page-content">
      {/* Stat Cards */}
      <div className="stats-grid">
        <div className="stat-card red">
          <span className="stat-icon">🔥</span>
          <div className="stat-label">Active Incidents</div>
          <div className="stat-value">{incidents.length}</div>
          <div className={`stat-change ${critCount > 0 ? 'down' : 'up'}`}>
            {critCount > 0 ? `↑ ${critCount} critical` : '✓ No critical'}
          </div>
        </div>
        <div className="stat-card yellow">
          <span className="stat-icon">⚠️</span>
          <div className="stat-label">Anomalies (last hour)</div>
          <div className="stat-value">{anomalies.length}</div>
          <div className="stat-change up">From {metrics.length} services</div>
        </div>
        <div className="stat-card blue">
          <span className="stat-icon">⏱️</span>
          <div className="stat-label">Avg Latency</div>
          <div className="stat-value">{avgLat.toFixed(0)}<span style={{ fontSize: 16 }}>ms</span></div>
          <div className={`stat-change ${avgLat > 200 ? 'down' : 'up'}`}>
            {avgLat > 200 ? '↑ Elevated' : '↓ Normal range'}
          </div>
        </div>
        <div className="stat-card purple">
          <span className="stat-icon">❌</span>
          <div className="stat-label">Error Rate</div>
          <div className="stat-value">{(totalErr * 100).toFixed(1)}<span style={{ fontSize: 16 }}>%</span></div>
          <div className={`stat-change ${totalErr > 0.05 ? 'down' : 'up'}`}>
            {totalErr > 0.05 ? '↑ Above threshold' : '↓ Below 5%'}
          </div>
        </div>
        <div className="stat-card green">
          <span className="stat-icon">🌐</span>
          <div className="stat-label">Services Monitored</div>
          <div className="stat-value">{metrics.length || 3}</div>
          <div className="stat-change up">auth · order · payment</div>
        </div>
        <div className="stat-card cyan">
          <span className="stat-icon">🔗</span>
          <div className="stat-label">WS Connection</div>
          <div className="stat-value" style={{ fontSize: 18, paddingTop: 8 }}>
            {wsConnected ? '🟢 Live' : '🔴 Off'}
          </div>
          <div className="stat-change up">Real-time pipeline</div>
        </div>
      </div>

      {/* Active Alerts */}
      {incidents.length > 0 && <ActiveAlerts />}

      {/* Charts + Live Feed */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: '16px' }}>
        <MetricsOverview />
        <LiveFeed />
      </div>

      {/* Bottom Row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
        <LoadTriggerPanel />

        {/* Recent Incidents Mini-Table */}
        <div className="card">
          <div className="card-header">
            <div className="card-title">Recent Incidents</div>
            <span className="badge badge-red">{incidents.length}</span>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Root Cause</th>
                  <th>Confidence</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {incidents.slice(0, 6).map(inc => (
                  <tr key={inc.incident_id}>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{inc.root_cause}</td>
                    <td>
                      <div className="confidence-bar">
                        <div className="conf-track">
                          <div className="conf-fill" style={{ width: `${inc.confidence * 100}%`, background: confColor(inc.confidence) }} />
                        </div>
                        <span style={{ fontSize: 11, color: confColor(inc.confidence), minWidth: 36 }}>
                          {(inc.confidence * 100).toFixed(0)}%
                        </span>
                      </div>
                    </td>
                    <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                      {inc.created_at ? formatDistanceToNow(new Date(inc.created_at), { addSuffix: true }) : '—'}
                    </td>
                  </tr>
                ))}
                {incidents.length === 0 && (
                  <tr><td colSpan={3} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No incidents yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
