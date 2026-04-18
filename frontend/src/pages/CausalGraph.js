import React, { useEffect, useRef, useState } from 'react';
import { useStore } from '../store/useStore';
import CytoscapeComponent from 'react-cytoscapejs';

const CYTOSCAPE_STYLE = [
  {
    selector: 'node',
    style: {
      'background-color': '#1c2130',
      'border-color': '#58a6ff',
      'border-width': 2,
      color: '#e6edf3',
      label: 'data(label)',
      'font-size': '11px',
      'font-family': 'JetBrains Mono, monospace',
      'text-valign': 'center',
      'text-halign': 'center',
      'text-wrap': 'wrap',
      width: 90,
      height: 90,
      'text-max-width': '80px',
      'overlay-padding': '4px',
      'transition-property': 'border-color, background-color',
      'transition-duration': '0.3s',
    },
  },
  {
    selector: 'node:hover',
    style: {
      'border-color': '#a371f7',
      'border-width': 3,
      'background-color': '#213150',
    },
  },
  {
    selector: 'node.root-cause',
    style: {
      'background-color': '#3d0d0d',
      'border-color': '#f85149',
      'border-width': 4,
      color: '#f85149',
      width: 110,
      height: 110,
    },
  },
  {
    selector: 'node.anomalous',
    style: {
      'background-color': '#2d1f0d',
      'border-color': '#d29922',
      'border-width': 3,
      color: '#d29922',
    },
  },
  {
    selector: 'edge',
    style: {
      width: 2,
      'line-color': '#30363d',
      'target-arrow-color': '#30363d',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      label: 'data(count)',
      'font-size': '9px',
      color: '#484f58',
      'text-background-color': '#0d1117',
      'text-background-opacity': 0.8,
      'text-background-padding': '2px',
    },
  },
  {
    selector: 'edge.highlighted',
    style: {
      width: 3,
      'line-color': '#f85149',
      'target-arrow-color': '#f85149',
    },
  },
  {
    selector: ':selected',
    style: {
      'border-color': '#a371f7',
      'border-width': 4,
    },
  },
];

const LAYOUT = {
  name: 'cose',
  animate: true,
  animationDuration: 600,
  nodeRepulsion: 8000,
  idealEdgeLength: 120,
  gravity: 1,
  numIter: 1000,
  randomize: false,
};

function NodePanel({ node, onClose }) {
  if (!node) return null;
  const d = node.data();
  return (
    <div style={{
      position: 'absolute', right: 16, top: 16,
      background: 'var(--bg-elevated)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)', padding: '16px', width: '200px',
      boxShadow: 'var(--shadow-lg)', zIndex: 10,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '10px' }}>
        <div style={{ fontWeight: 600, fontSize: '13px' }}>{d.label}</div>
        <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer' }}>✕</button>
      </div>
      <div style={{ fontSize: '11px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
        <div style={{ color: 'var(--text-muted)' }}>Service ID</div>
        <div style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-blue)', wordBreak: 'break-all' }}>{d.id}</div>
        {d.last_seen && (
          <>
            <div style={{ color: 'var(--text-muted)', marginTop: '4px' }}>Last Seen</div>
            <div style={{ color: 'var(--text-primary)' }}>{d.last_seen}</div>
          </>
        )}
      </div>
    </div>
  );
}

export default function CausalGraph() {
  const graph    = useStore(s => s.graph);
  const incidents = useStore(s => s.incidents);
  const fetchGraph = useStore(s => s.fetchGraph);
  const loading  = useStore(s => s.loading.graph);
  const [cyRef, setCyRef] = useState(null);
  const [selectedNode, setSelectedNode] = useState(null);
  const [layout, setLayout] = useState('cose');

  // Identify root causes from latest incidents
  const rootCauses = new Set(incidents.map(i => i.root_cause));
  const anomalousServices = new Set(
    (incidents.flatMap(i => i.impact_chain || [])).filter(s => !rootCauses.has(s))
  );

  // Build Cytoscape elements
  const elements = [
    ...graph.nodes.map(n => ({
      data: { id: n.id, label: n.label || n.id, last_seen: n.last_seen },
      classes: rootCauses.has(n.id) ? 'root-cause' : anomalousServices.has(n.id) ? 'anomalous' : '',
    })),
    ...graph.edges.map(e => ({
      data: {
        id:     `${e.source}-${e.target}`,
        source: e.source,
        target: e.target,
        count:  e.count ? `${e.count}` : '',
      },
      classes: rootCauses.has(e.source) ? 'highlighted' : '',
    })),
  ];

  const handleCy = (cy) => {
    setCyRef(cy);
    cy.on('tap', 'node', (e) => setSelectedNode(e.target));
    cy.on('tap', (e) => { if (e.target === cy) setSelectedNode(null); });
  };

  useEffect(() => {
    fetchGraph();
    const id = setInterval(fetchGraph, 8000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (cyRef && elements.length > 0) {
      cyRef.layout({ ...LAYOUT, name: layout }).run();
    }
  }, [cyRef, graph, layout]);

  return (
    <div className="page-content">
      {/* Legend + Controls */}
      <div className="card" style={{ padding: '12px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '24px', flex: 1 }}>
            <div style={{ display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
              {[
                { color: '#f85149', label: 'Root Cause', border: 4 },
                { color: '#d29922', label: 'Impacted Service', border: 3 },
                { color: '#58a6ff', label: 'Healthy Service', border: 2 },
              ].map(leg => (
                <div key={leg.label} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <div style={{
                    width: 14, height: 14, borderRadius: '50%',
                    border: `${leg.border}px solid ${leg.color}`,
                    background: 'var(--bg-elevated)',
                  }} />
                  <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>{leg.label}</span>
                </div>
              ))}
            </div>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Layout:</span>
            {['cose', 'circle', 'grid', 'breadthfirst'].map(l => (
              <button
                key={l} className={`btn ${layout === l ? 'btn-primary' : 'btn-ghost'}`}
                style={{ padding: '4px 10px', fontSize: '11px' }}
                onClick={() => setLayout(l)}
              >{l}</button>
            ))}
            <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: '11px' }} onClick={fetchGraph}>
              ↻ Refresh
            </button>
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            <span className="badge badge-blue">{graph.nodes.length} services</span>
            <span className="badge badge-purple">{graph.edges.length} dependencies</span>
            {rootCauses.size > 0 && <span className="badge badge-red">{rootCauses.size} root causes</span>}
          </div>
        </div>
      </div>

      {/* Graph */}
      <div className="card" style={{ padding: 0, position: 'relative', flex: 1 }}>
        {loading && elements.length === 0 ? (
          <div className="loading-center"><div className="spinner" /></div>
        ) : elements.length === 0 ? (
          <div className="loading-center" style={{ flexDirection: 'column', gap: '12px' }}>
            <div style={{ fontSize: '32px' }}>🕸️</div>
            <div style={{ color: 'var(--text-muted)' }}>No graph data yet. Generate some traffic first.</div>
          </div>
        ) : (
          <div className="graph-container" style={{ height: '520px', borderRadius: 'var(--radius-lg)' }}>
            <CytoscapeComponent
              elements={elements}
              style={{ width: '100%', height: '100%' }}
              stylesheet={CYTOSCAPE_STYLE}
              layout={LAYOUT}
              cy={handleCy}
              wheelSensitivity={0.2}
            />
            {selectedNode && (
              <NodePanel node={selectedNode} onClose={() => setSelectedNode(null)} />
            )}
          </div>
        )}
      </div>

      {/* Stats Below Graph */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '16px' }}>
        {/* Root cause list */}
        <div className="card">
          <div className="card-title" style={{ marginBottom: '12px' }}>🔴 Root Cause Services</div>
          {rootCauses.size === 0 ? (
            <div style={{ color: 'var(--text-muted)', fontSize: '13px' }}>None detected</div>
          ) : [...rootCauses].map(svc => (
            <div key={svc} style={{
              display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 0',
              borderBottom: '1px solid var(--border)',
            }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--accent-red)' }} />
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--accent-red)' }}>{svc}</span>
            </div>
          ))}
        </div>

        {/* Dependencies table */}
        <div className="card" style={{ gridColumn: 'span 2' }}>
          <div className="card-title" style={{ marginBottom: '12px' }}>🔗 Service Dependencies</div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Calls</th>
                  <th>Target</th>
                  <th>Call Count</th>
                </tr>
              </thead>
              <tbody>
                {graph.edges.map((e, i) => (
                  <tr key={i}>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: rootCauses.has(e.source) ? 'var(--accent-red)' : 'var(--text-primary)' }}>{e.source}</td>
                    <td style={{ color: 'var(--text-muted)', fontSize: 16 }}>→</td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{e.target}</td>
                    <td><span className="badge badge-blue">{e.count ?? '—'}</span></td>
                  </tr>
                ))}
                {graph.edges.length === 0 && (
                  <tr><td colSpan={4} style={{ textAlign: 'center', color: 'var(--text-muted)' }}>No edges yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
