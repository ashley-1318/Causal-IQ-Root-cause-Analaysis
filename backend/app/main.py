"""
CausalIQ FastAPI Backend
Full production REST + WebSocket API
"""
import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, AsyncGenerator

import clickhouse_connect
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("backend")

# ── Config ────────────────────────────────────────────────────────────────────
CH_HOST         = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT         = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER         = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS         = os.getenv("CLICKHOUSE_PASSWORD", "causaliq123")
CH_DB           = os.getenv("CLICKHOUSE_DB", "causaliq")

REDIS_URL       = os.getenv("REDIS_URL", "redis://redis:6379")
NEO4J_URI       = os.getenv("NEO4J_URI",       "bolt://neo4j:7687")
NEO4J_USER      = os.getenv("NEO4J_USER",      "neo4j")
NEO4J_PASS      = os.getenv("NEO4J_PASS",      "causaliq123")
PAYMENT_URL     = os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8002")
AUTH_URL        = os.getenv("AUTH_SERVICE_URL",    "http://auth-service:8000")
ORDER_URL       = os.getenv("ORDER_SERVICE_URL",   "http://order-service:8001")
PROMETHEUS_URL  = os.getenv("PROMETHEUS_URL",  "http://prometheus:9090")

from .webhooks import router as webhook_router

app = FastAPI(title="CausalIQ Backend", version="2.0.0", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(webhook_router)

# ── Clients ───────────────────────────────────────────────────────────────────
_ch_client = None
_redis_client = None

def get_ch():
    global _ch_client
    if _ch_client is None:
        _ch_client = clickhouse_connect.get_client(
            host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASS
        )
    return _ch_client

async def get_redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client

# ── WebSocket Connection Manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

# ── Kafka consumer for real-time incidents → WebSocket ────────────────────────
def kafka_worker():
    """Sync Kafka consumer loop intended to run in a separate thread."""
    from confluent_kafka import Consumer, KafkaError
    KAFKA = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
    c = Consumer({
        "bootstrap.servers": KAFKA,
        "group.id": "backend-ws",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })
    c.subscribe(["rca-results", "anomalies"])
    logger.info("WS Kafka bridge worker started")
    
    # Needs access to the event loop to call manager.broadcast (which is async)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        while True:
            msg = c.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.warning("Kafka WS bridge error: %s", msg.error())
                continue
            
            try:
                payload = json.loads(msg.value().decode("utf-8"))
                payload["_topic"] = msg.topic()
                # Use the main app's manager and loop to broadcast
                asyncio.run_coroutine_threadsafe(manager.broadcast(payload), MAIN_LOOP)
            except Exception:
                pass
    finally:
        c.close()

MAIN_LOOP = None

@app.on_event("startup")
async def startup():
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    
    import threading
    # We use a simple thread here because confluent-kafka's sync consumer 
    # blocks the event loop and isn't natively async-friendly.
    threading.Thread(target=kafka_worker, daemon=True).start()
    logger.info("CausalIQ backend started")

# ── Models ────────────────────────────────────────────────────────────────────
class LoadTrigger(BaseModel):
    duration_seconds: int = 60
    concurrency: int = 10
    inject_fault: bool = True
    fault_db_latency_ms: int = 500

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "causaliq-backend", "ts": datetime.utcnow().isoformat()}

from .remediation.queue import PendingRemediationQueue
pending_queue = PendingRemediationQueue(REDIS_URL + "/1")

@app.get("/incidents")
async def list_incidents(limit: int = 50, offset: int = 0):
    """Return recent incidents with real-time approval status."""
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT incident_id, root_cause, confidence, impact_chain,
               anomaly_count, explanation, created_at
        FROM {CH_DB}.incidents
        ORDER BY created_at DESC
        LIMIT {limit} OFFSET {offset}
        """
    )
    result = []
    for row in rows.result_rows:
        inc_id = row[0]
        # Check if this incident is currently pending approval
        pending = await pending_queue.get_action(inc_id)
        status = pending.get("status", "RESOLVED") if pending else "DONE"
        
        result.append({
            "incident_id": inc_id,
            "root_cause": row[1],
            "confidence": row[2],
            "impact_chain": json.loads(row[3]) if row[3] else [],
            "anomaly_count": row[4],
            "explanation": row[5][:500] if row[5] else "",
            "created_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]),
            "status": status,
            "remediation": pending.get("action", None) if pending else None
        })
    return result


@app.get("/rca/{incident_id}")
async def get_rca(incident_id: str):
    """Full RCA detail for a specific incident."""
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT incident_id, root_cause, confidence, impact_chain,
               anomaly_count, explanation, evidence_json, created_at
        FROM {CH_DB}.incidents
        WHERE incident_id = '{incident_id}'
        LIMIT 1
        """
    )
    if not rows.result_rows:
        raise HTTPException(status_code=404, detail="Incident not found")
    row = rows.result_rows[0]
    return {
        "incident_id": row[0],
        "root_cause": row[1],
        "confidence": row[2],
        "impact_chain": json.loads(row[3]) if row[3] else [],
        "anomaly_count": row[4],
        "explanation": row[5],
        "evidence": json.loads(row[6]) if row[6] else {},
        "created_at": row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
    }


# ── Graph ─────────────────────────────────────────────────────────────────────
@app.get("/graph")
async def get_graph():
    """Return full service graph from Neo4j."""
    redis = await get_redis()
    cached = await redis.get("service_graph")
    if cached:
        return json.loads(cached)

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with driver.session() as s:
        nodes = [
            {"id": r["name"], "label": r["name"], "last_seen": r["last_seen"]}
            for r in s.run("MATCH (svc:Service) RETURN svc.name AS name, svc.last_seen AS last_seen")
        ]
        edges = [
            {"source": r["src"], "target": r["tgt"], "count": r["cnt"]}
            for r in s.run(
                "MATCH (a:Service)-[r:CALLS]->(b:Service) "
                "RETURN a.name AS src, b.name AS tgt, r.count AS cnt"
            )
        ]
    driver.close()
    graph = {"nodes": nodes, "edges": edges}
    await redis.setex("service_graph", 5, json.dumps(graph))
    return graph


# ── Metrics ───────────────────────────────────────────────────────────────────
@app.get("/metrics")
async def get_metrics():
    """Return recent per-service metrics from ClickHouse."""
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT service,
               avg(avg_latency_ms) AS avg_lat,
               avg(p99_latency_ms) AS p99_lat,
               avg(error_rate) AS err_rate,
               avg(throughput_rps) AS throughput,
               max(ts) AS last_ts
        FROM {CH_DB}.service_metrics
        WHERE ts > now() - INTERVAL 1 HOUR
        GROUP BY service
        ORDER BY service
        """
    )
    return [
        {
            "service": r[0],
            "avg_latency_ms": round(r[1], 2),
            "p99_latency_ms": round(r[2], 2),
            "error_rate": round(r[3], 4),
            "throughput_rps": round(r[4], 2),
            "last_ts": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
        }
        for r in rows.result_rows
    ]


@app.get("/anomalies")
async def get_anomalies(limit: int = 100):
    """Recent anomaly events."""
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT service, anomaly_score, avg_latency_ms, error_rate, throughput_rps, detected_at
        FROM {CH_DB}.anomaly_events
        ORDER BY detected_at DESC
        LIMIT {limit}
        """
    )
    return [
        {
            "service": r[0], "anomaly_score": r[1],
            "avg_latency_ms": r[2], "error_rate": r[3],
            "throughput_rps": r[4],
            "detected_at": r[5].isoformat() if hasattr(r[5], "isoformat") else str(r[5]),
        }
        for r in rows.result_rows
    ]


# ── Load Trigger ──────────────────────────────────────────────────────────────
@app.post("/trigger-load")
async def trigger_load(config: LoadTrigger):
    """
    Trigger a real load test + fault injection.
    Calls the load generator service and optionally injects DB latency fault.
    """
    tasks = []

    if config.inject_fault:
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{PAYMENT_URL}/admin/fault",
                    json={"active": True, "db_latency_ms": config.fault_db_latency_ms},
                    timeout=5,
                )
                logger.info("Fault injection enabled: db_latency_ms=%d", config.fault_db_latency_ms)
            except Exception as exc:
                logger.warning("Fault injection failed: %s", exc)

    async def generate_load():
        import asyncio
        token = None
        async with httpx.AsyncClient() as client:
            # Get auth token
            try:
                r = await client.post(f"{AUTH_URL}/auth/login",
                                      json={"username": "alice", "password": "pass123"}, timeout=5)
                if r.status_code == 200:
                    token = r.json().get("access_token")
            except Exception:
                pass

        products = ["laptop", "phone", "tablet", "monitor", "keyboard"]
        import random

        async def one_request(session: httpx.AsyncClient):
            try:
                await session.post(
                    f"{ORDER_URL}/orders",
                    json={
                        "product_id": random.choice(products),
                        "quantity": random.randint(1, 5),
                        "amount": round(random.uniform(10.0, 999.0), 2),
                    },
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    timeout=10,
                )
            except Exception:
                pass

        start = asyncio.get_event_loop().time()
        async with httpx.AsyncClient() as session:
            while asyncio.get_event_loop().time() - start < config.duration_seconds:
                coros = [one_request(session) for _ in range(config.concurrency)]
                await asyncio.gather(*coros)
                await asyncio.sleep(0.5)

        # Disable fault after load test
        if config.inject_fault:
            async with httpx.AsyncClient() as c:
                try:
                    await c.post(f"{PAYMENT_URL}/admin/fault",
                                 json={"active": False, "db_latency_ms": 50}, timeout=5)
                    logger.info("Fault injection disabled")
                except Exception:
                    pass

    asyncio.create_task(generate_load())
    return {
        "status": "load_test_started",
        "duration_seconds": config.duration_seconds,
        "concurrency": config.concurrency,
        "fault_injected": config.inject_fault,
        "fault_db_latency_ms": config.fault_db_latency_ms if config.inject_fault else 0,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws/live")
async def live_ws(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep-alive ping
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")
