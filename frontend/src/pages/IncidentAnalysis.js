import React, { useState } from 'react';
import { useStore } from '../store/useStore';
import { formatDistanceToNow, format } from 'date-fns';

function confColor(c) {
  if (c >= 0.8) return 'var(--accent-red)';
  if (c >= 0.5) return 'var(--accent-yellow)';
  return 'var(--accent-green)';
}

function ImpactChain({ chain }) {
  if (!chain || !chain.length) return <span style={{ color: 'var(--text-muted)' }}>N/A</span>;
  return (
    <div className="impact-chain">
      {chain.map((node, i) => (
        <React.Fragment key={i}>
          <span className={`impact-node ${i === 0 ? 'root' : i === chain.length - 1 ? 'leaf' : 'mid'}`}>
            {node}
          </span>
          {i < chain.length - 1 && <span className="impact-arrow">→</span>}
        </React.Fragment>
      ))}
    </div>
  );
}

function IncidentDetail({ incident, onClose }) {
  const evidence = incident.evidence || {};
  const ranked   = evidence.ranked_candidates || [];
  const causal   = evidence.causal_inference  || {};

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      z: 9999, padding: '24px',
    }} onClick={onClose}>
      <div style={{
        background: 'var(--bg-card)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-xl)', width: '100%', maxWidth: '800px',
        maxHeight: '90vh', overflowY: 'auto', padding: '24px',
        animation: 'fadeIn 0.2s ease',
      }} onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '20px' }}>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginBottom: '4px' }}>
              INCIDENT #{incident.incident_id}
            </div>
            <div style={{ fontSize: '20px', fontWeight: 700, color: 'var(--text-primary)' }}>
              Root Cause Analysis Report
            </div>
          </div>
          <button className="btn btn-ghost" onClick={onClose} style={{ padding: '6px 12px' }}>✕ Close</button>
        </div>

        {/* RCA Panel */}
        <div className="rca-panel" style={{ marginBottom: '16px' }}>
          <div className="rca-root-tag">
            🔴 Root Cause: <span style={{ fontFamily: 'var(--font-mono)' }}>{incident.root_cause}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px' }}>
            <div>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Confidence</div>
              <div className="confidence-bar">
                <div className="conf-track" style={{ height: 10 }}>
                  <div className="conf-fill" style={{ width: `${incident.confidence * 100}%`, background: confColor(incident.confidence) }} />
                </div>
                <span style={{ fontWeight: 700, fontSize: 16, color: confColor(incident.confidence) }}>
                  {(incident.confidence * 100).toFixed(1)}%
                </span>
              </div>
            </div>
            <div>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Anomalies Detected</div>
              <div style={{ fontSize: '22px', fontWeight: 700, color: 'var(--accent-yellow)' }}>{incident.anomaly_count}</div>
            </div>
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Impact Chain</div>
          <ImpactChain chain={incident.impact_chain} />
        </div>

        {/* Ranked Candidates */}
        {ranked.length > 0 && (
          <div className="card" style={{ marginBottom: '16px' }}>
            <div className="card-title" style={{ marginBottom: '12px' }}>🏆 Root Cause Candidates (Ranked)</div>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr>
                  <th style={{ padding: '8px', textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11 }}>Service</th>
                  <th style={{ padding: '8px', textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11 }}>Probability</th>
                  <th style={{ padding: '8px', textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11 }}>Avg Latency</th>
                  <th style={{ padding: '8px', textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11 }}>Error Rate</th>
                  <th style={{ padding: '8px', textAlign: 'left', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', fontSize: 11 }}>Depth</th>
                </tr>
              </thead>
              <tbody>
                {ranked.map((r, i) => (
                  <tr key={i} style={{ background: i === 0 ? '#1a0d0d' : 'transparent' }}>
                    <td style={{ padding: '8px', fontFamily: 'var(--font-mono)', color: i === 0 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                      {i === 0 && '★ '}{r.service}
                    </td>
                    <td style={{ padding: '8px' }}>
                      <div className="confidence-bar">
                        <div className="conf-track"><div className="conf-fill" style={{ width: `${r.probability * 100}%`, background: confColor(r.probability) }} /></div>
                        <span style={{ color: confColor(r.probability), fontSize: 11 }}>{(r.probability * 100).toFixed(0)}%</span>
                      </div>
                    </td>
                    <td style={{ padding: '8px', color: r.avg_latency_ms > 200 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                      {r.avg_latency_ms?.toFixed(1) ?? '—'}ms
                    </td>
                    <td style={{ padding: '8px', color: r.error_rate > 0.05 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                      {r.error_rate ? (r.error_rate * 100).toFixed(1) + '%' : '—'}
                    </td>
                    <td style={{ padding: '8px', color: 'var(--text-secondary)' }}>{r.upstream_depth ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Causal Inference */}
        {causal.method && (
          <div className="card" style={{ marginBottom: '16px' }}>
            <div className="card-title" style={{ marginBottom: '8px' }}>🧬 Causal Inference Result</div>
            <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
              <span className="badge badge-purple">Method: {causal.method}</span>
              {causal.causal_probability && (
                <span className="badge badge-red">P(root_cause) = {(causal.causal_probability * 100).toFixed(1)}%</span>
              )}
              {(causal.evidence_services || []).map(s => (
                <span key={s} className="badge badge-yellow">Evidence: {s}</span>
              ))}
            </div>
          </div>
        )}

        {/* LLM Explanation */}
        {incident.explanation && (
          <div className="card">
            <div className="card-title" style={{ marginBottom: '12px' }}>🤖 AI-Generated Explanation</div>
            <div className="explanation-box">{incident.explanation}</div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function IncidentAnalysis() {
  const incidents = useStore(s => s.incidents);
  const loading   = useStore(s => s.loading.incidents);
  const fetchIncidentDetail = useStore(s => s.fetchIncidentDetail);
  const activeIncident = useStore(s => s.activeIncident);
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');

  const handleView = async (inc) => {
    await fetchIncidentDetail(inc.incident_id);
  };

  const filtered = incidents.filter(inc => {
    const matchSev = filter === 'all'
      || (filter === 'critical' && inc.confidence >= 0.8)
      || (filter === 'high'     && inc.confidence >= 0.5 && inc.confidence < 0.8)
      || (filter === 'medium'   && inc.confidence < 0.5);
    const matchSearch = !search || inc.root_cause.toLowerCase().includes(search.toLowerCase());
    return matchSev && matchSearch;
  });

  return (
    <div className="page-content">
      {/* Controls */}
      <div className="card" style={{ padding: '14px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <input
            type="text" placeholder="Search by root cause…" value={search}
            onChange={e => setSearch(e.target.value)}
            style={{
              flex: 1, minWidth: '200px', padding: '8px 12px',
              background: 'var(--bg-elevated)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-md)', color: 'var(--text-primary)', fontSize: '13px',
            }}
          />
          {['all', 'critical', 'high', 'medium'].map(f => (
            <button
              key={f}
              className={`btn ${filter === f ? 'btn-primary' : 'btn-ghost'}`}
              onClick={() => setFilter(f)}
              style={{ padding: '6px 14px', textTransform: 'capitalize' }}
            >
              {f}
            </button>
          ))}
          <span className="badge badge-blue">{filtered.length} incidents</span>
        </div>
      </div>

      {/* Timeline + Table */}
      <div style={{ display: 'grid', gridTemplateColumns: '260px 1fr', gap: '16px' }}>
        {/* Timeline */}
        <div className="card">
          <div className="card-title" style={{ marginBottom: '16px' }}>Timeline</div>
          <div className="timeline">
            {filtered.slice(0, 12).map(inc => {
              const color = inc.confidence >= 0.8 ? 'red' : inc.confidence >= 0.5 ? 'yellow' : 'blue';
              return (
                <div key={inc.incident_id} className="timeline-item" style={{ cursor: 'pointer' }} onClick={() => handleView(inc)}>
                  <div className={`timeline-dot ${color}`} />
                  <div>
                    <div className="timeline-time">
                      {inc.created_at ? format(new Date(inc.created_at), 'HH:mm:ss') : '—'}
                    </div>
                    <div className="timeline-title" style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>
                      {inc.root_cause}
                    </div>
                    <div className="timeline-desc">{(inc.confidence * 100).toFixed(0)}% confident</div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Table */}
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          {loading ? (
            <div className="loading-center"><div className="spinner" /></div>
          ) : (
            <div className="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Root Cause</th>
                    <th>Impact Chain</th>
                    <th>Confidence</th>
                    <th>Anomalies</th>
                    <th>Detected</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(inc => (
                    <tr key={inc.incident_id}>
                      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                        #{inc.incident_id}
                      </td>
                      <td style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-red)', fontWeight: 600 }}>
                        {inc.root_cause}
                      </td>
                      <td>
                        <ImpactChain chain={(inc.impact_chain || []).slice(0, 4)} />
                      </td>
                      <td>
                        <div className="confidence-bar">
                          <div className="conf-track"><div className="conf-fill" style={{ width: `${inc.confidence * 100}%`, background: confColor(inc.confidence) }} /></div>
                          <span style={{ fontSize: 11, color: confColor(inc.confidence) }}>{(inc.confidence * 100).toFixed(0)}%</span>
                        </div>
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <span className="badge badge-yellow">{inc.anomaly_count}</span>
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        {inc.created_at ? formatDistanceToNow(new Date(inc.created_at), { addSuffix: true }) : '—'}
                      </td>
                      <td>
                        {inc.status === 'PENDING' ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                            <span className="badge badge-yellow" style={{ fontSize: '9px', padding: '2px 6px' }}>PENDING APPROVAL</span>
                            <button className="btn btn-primary" style={{ padding: '2px 8px', fontSize: '10px' }} onClick={() => handleView(inc)}>
                              Approve?
                            </button>
                          </div>
                        ) : inc.status === 'APPROVED' ? (
                          <span className="badge badge-green" style={{ fontSize: '10px' }}>APPROVED ✅</span>
                        ) : (
                          <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 11 }} onClick={() => handleView(inc)}>
                            View RCA →
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {filtered.length === 0 && (
                    <tr><td colSpan={7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '40px' }}>
                      No incidents match the current filter
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {activeIncident && (
        <IncidentDetail incident={activeIncident} onClose={() => useStore.setState({ activeIncident: null })} />
      )}
    </div>
  );
}
