import React, { useEffect, useState } from 'react';
import { useStore } from '../store/useStore';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, RadarChart, PolarGrid, PolarAngleAxis,
  PolarRadiusAxis, Radar, LineChart, Line, Legend
} from 'recharts';
import { format } from 'date-fns';

function ServiceCard({ metric }) {
  const isHighLatency = metric.avg_latency_ms > 200;
  const isHighErr     = metric.error_rate > 0.05;

  return (
    <div className="card" style={{
      borderColor: isHighLatency || isHighErr ? 'var(--accent-red)' : 'var(--border)',
      position: 'relative', overflow: 'hidden',
    }}>
      {(isHighLatency || isHighErr) && (
        <div style={{
          position: 'absolute', top: 0, left: 0, right: 0, height: 3,
          background: 'var(--accent-red)',
        }} />
      )}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '16px' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: '14px', fontWeight: 700, color: 'var(--text-primary)' }}>
            {metric.service}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
            Last updated: {metric.last_ts ? format(new Date(metric.last_ts), 'HH:mm:ss') : '—'}
          </div>
        </div>
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
          {isHighLatency && <span className="badge badge-red">High Latency</span>}
          {isHighErr     && <span className="badge badge-red">High Errors</span>}
          {!isHighLatency && !isHighErr && <span className="badge badge-green">Healthy</span>}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
        {[
          { label: 'Avg Latency', value: `${metric.avg_latency_ms?.toFixed(1)}ms`, color: isHighLatency ? 'var(--accent-red)' : 'var(--accent-blue)' },
          { label: 'P99 Latency', value: `${metric.p99_latency_ms?.toFixed(1)}ms`, color: metric.p99_latency_ms > 500 ? 'var(--accent-red)' : 'var(--accent-cyan)' },
          { label: 'Error Rate',  value: `${(metric.error_rate * 100).toFixed(2)}%`, color: isHighErr ? 'var(--accent-red)' : 'var(--accent-green)' },
          { label: 'Throughput',  value: `${metric.throughput_rps?.toFixed(1)} rps`, color: 'var(--accent-purple)' },
        ].map(m => (
          <div key={m.label} style={{
            background: 'var(--bg-elevated)', borderRadius: 'var(--radius-md)',
            padding: '12px', textAlign: 'center',
          }}>
            <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginBottom: '4px' }}>{m.label}</div>
            <div style={{ fontSize: '18px', fontWeight: 700, color: m.color }}>{m.value}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function MetricsPage() {
  const metrics   = useStore(s => s.metrics);
  const anomalies = useStore(s => s.anomalies);
  const fetchMetrics = useStore(s => s.fetchMetrics);

  useEffect(() => {
    fetchMetrics();
    const id = setInterval(fetchMetrics, 8000);
    return () => clearInterval(id);
  }, []);

  // Build time-series from anomalies
  const anomalyTimeline = anomalies
    .slice(0, 50)
    .reverse()
    .map((a, i) => ({
      t: a.detected_at ? format(new Date(a.detected_at), 'HH:mm') : `${i}`,
      [a.service]: +(a.anomaly_score * -1 * 100).toFixed(1),
    }));

  const radarData = metrics.map(m => ({
    subject: m.service,
    latency: Math.min(m.avg_latency_ms / 10, 100),
    errors:  Math.min(m.error_rate * 1000, 100),
    throughput: Math.min(m.throughput_rps * 5, 100),
  }));

  const barData = metrics.map(m => ({
    service: m.service,
    avg: Math.round(m.avg_latency_ms),
    p99: Math.round(m.p99_latency_ms),
  }));

  return (
    <div className="page-content">
      {/* Service Health Cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '16px' }}>
        {metrics.length === 0
          ? [1, 2, 3].map(i => (
              <div key={i} className="card" style={{ height: '180px' }}>
                <div className="loading-center"><div className="spinner" /></div>
              </div>
            ))
          : metrics.map(m => <ServiceCard key={m.service} metric={m} />)
        }
      </div>

      {/* Charts Row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
        {/* Latency Bar Chart */}
        <div className="card">
          <div className="card-header">
            <div><div className="card-title">Latency Comparison</div><div className="card-subtitle">Avg vs P99 per service</div></div>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={barData} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="service" tick={{ fill: 'var(--text-muted)', fontSize: 11 }} axisLine={false} />
              <YAxis tick={{ fill: 'var(--text-muted)', fontSize: 11 }} unit="ms" axisLine={false} />
              <Tooltip contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 12, color: 'var(--text-secondary)' }} />
              <Bar dataKey="avg" fill="var(--accent-blue)"   name="Avg Latency" radius={[4, 4, 0, 0]} />
              <Bar dataKey="p99" fill="var(--accent-red)"    name="P99 Latency" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Radar Chart */}
        <div className="card">
          <div className="card-header">
            <div><div className="card-title">Service Health Radar</div><div className="card-subtitle">Normalized health dimensions</div></div>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <RadarChart data={radarData}>
              <PolarGrid stroke="var(--border)" />
              <PolarAngleAxis dataKey="subject" tick={{ fill: 'var(--text-muted)', fontSize: 11 }} />
              <PolarRadiusAxis angle={90} domain={[0, 100]} tick={{ fill: 'var(--text-muted)', fontSize: 9 }} axisLine={false} />
              <Radar name="Latency"   dataKey="latency"   stroke="var(--accent-red)"    fill="var(--accent-red)"    fillOpacity={0.15} />
              <Radar name="Errors"    dataKey="errors"    stroke="var(--accent-yellow)" fill="var(--accent-yellow)" fillOpacity={0.15} />
              <Radar name="Throughput" dataKey="throughput" stroke="var(--accent-green)"  fill="var(--accent-green)"  fillOpacity={0.15} />
              <Legend wrapperStyle={{ fontSize: 11, color: 'var(--text-secondary)' }} />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Anomaly Events Table */}
      <div className="card">
        <div className="card-header">
          <div><div className="card-title">Recent Anomaly Events</div><div className="card-subtitle">Detected by Isolation Forest ML</div></div>
          <span className="badge badge-red">{anomalies.length} events</span>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Service</th>
                <th>Anomaly Score</th>
                <th>Avg Latency</th>
                <th>Error Rate</th>
                <th>Throughput</th>
                <th>Detected</th>
              </tr>
            </thead>
            <tbody>
              {anomalies.slice(0, 20).map((a, i) => (
                <tr key={i} className="fade-in">
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--accent-yellow)' }}>{a.service}</td>
                  <td>
                    <div className="confidence-bar">
                      <div className="conf-track">
                        <div className="conf-fill" style={{ width: `${Math.min(Math.abs(a.anomaly_score) * 200, 100)}%`, background: 'var(--accent-red)' }} />
                      </div>
                      <span style={{ fontSize: 11, color: 'var(--accent-red)', fontFamily: 'var(--font-mono)' }}>
                        {a.anomaly_score?.toFixed(4)}
                      </span>
                    </div>
                  </td>
                  <td style={{ color: a.avg_latency_ms > 200 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                    {a.avg_latency_ms?.toFixed(1)}ms
                  </td>
                  <td style={{ color: a.error_rate > 0.05 ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                    {(a.error_rate * 100).toFixed(2)}%
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>{a.throughput_rps?.toFixed(1)} rps</td>
                  <td style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                    {a.detected_at ? format(new Date(a.detected_at), 'HH:mm:ss') : '—'}
                  </td>
                </tr>
              ))}
              {anomalies.length === 0 && (
                <tr><td colSpan={6} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '40px' }}>
                  No anomalies detected yet. Generate traffic to see data.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
