import { create } from 'zustand';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:9000';
const WS_URL   = process.env.REACT_APP_WS_URL  || 'ws://localhost:9000/ws/live';

export const useStore = create((set, get) => ({
  // State
  incidents:    [],
  anomalies:    [],
  metrics:      [],
  graph:        { nodes: [], edges: [] },
  liveEvents:   [],
  toasts:       [],
  loading:      { incidents: false, graph: false, metrics: false },
  wsConnected:  false,
  wsInstance:   null,
  activeIncident: null,

  // WebSocket
  connectWS: () => {
    const existing = get().wsInstance;
    if (existing && existing.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    ws.onopen  = () => set({ wsConnected: true });
    ws.onclose = () => {
      set({ wsConnected: false, wsInstance: null });
      setTimeout(() => get().connectWS(), 3000); // auto-reconnect
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        const topic = data._topic;

        if (topic === 'rca-results') {
          get().addToast({ type: 'error', message: `🚨 New Incident: Root cause — ${data.anomalies?.[0]?.service || 'unknown'}` });
          // Refresh incidents
          get().fetchIncidents();
        } else if (topic === 'anomalies') {
          const anomalyCount = (data.anomalies || []).length;
          if (anomalyCount > 0) {
            get().addToast({ type: 'info', message: `⚠️ ${anomalyCount} anomaly signals detected` });
          }
        }

        set(state => ({
          liveEvents: [{ ...data, _ts: new Date().toISOString() }, ...state.liveEvents].slice(0, 200)
        }));
      } catch {}
    };
    ws.onmessage.bind(ws);
    set({ wsInstance: ws });
  },

  disconnectWS: () => {
    const ws = get().wsInstance;
    if (ws) ws.close();
    set({ wsInstance: null, wsConnected: false });
  },

  // Toasts
  addToast: (toast) => {
    const id = Date.now();
    set(state => ({ toasts: [...state.toasts, { ...toast, id }] }));
    setTimeout(() => set(state => ({ toasts: state.toasts.filter(t => t.id !== id) })), 5000);
  },

  // API Calls
  fetchIncidents: async () => {
    set(state => ({ loading: { ...state.loading, incidents: true } }));
    try {
      const r = await fetch(`${API_BASE}/incidents?limit=50`);
      if (r.ok) set({ incidents: await r.json() });
    } catch (e) {
      get().addToast({ type: 'error', message: 'Failed to fetch incidents' });
    } finally {
      set(state => ({ loading: { ...state.loading, incidents: false } }));
    }
  },

  fetchGraph: async () => {
    set(state => ({ loading: { ...state.loading, graph: true } }));
    try {
      const r = await fetch(`${API_BASE}/graph`);
      if (r.ok) set({ graph: await r.json() });
    } catch {}
    finally { set(state => ({ loading: { ...state.loading, graph: false } })); }
  },

  fetchMetrics: async () => {
    try {
      const [metR, anomR] = await Promise.all([
        fetch(`${API_BASE}/metrics`),
        fetch(`${API_BASE}/anomalies?limit=100`),
      ]);
      if (metR.ok)  set({ metrics:   await metR.json() });
      if (anomR.ok) set({ anomalies: await anomR.json() });
    } catch {}
  },

  fetchIncidentDetail: async (id) => {
    try {
      const r = await fetch(`${API_BASE}/rca/${id}`);
      if (r.ok) set({ activeIncident: await r.json() });
    } catch {}
  },

  triggerLoad: async (config) => {
    const r = await fetch(`${API_BASE}/trigger-load`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    const data = await r.json();
    get().addToast({ type: r.ok ? 'success' : 'error', message: r.ok ? '🚀 Load test started!' : 'Load test failed' });
    return data;
  },
}));

export { API_BASE };
