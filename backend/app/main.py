"""
CausalIQ FastAPI Backend
Full production REST + WebSocket API
"""
import os
import json
import asyncio
import logging
import re
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


class CircuitBreakerOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, reset_timeout_seconds: int = 30):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.failure_count = 0
        self.opened_at: Optional[datetime] = None

    def _is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if (datetime.utcnow() - self.opened_at).total_seconds() >= self.reset_timeout_seconds:
            self.failure_count = 0
            self.opened_at = None
            return False
        return True

    def allow(self) -> None:
        if self._is_open():
            raise CircuitBreakerOpen(f"Circuit breaker open for {self.name}")

    def record_success(self) -> None:
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.opened_at = datetime.utcnow()
            logger.warning("Circuit breaker opened for %s after %d failures", self.name, self.failure_count)


OUTBOUND_BREAKERS = {
    "auth": CircuitBreaker("auth", failure_threshold=3, reset_timeout_seconds=20),
    "order": CircuitBreaker("order", failure_threshold=3, reset_timeout_seconds=20),
    "payment": CircuitBreaker("payment", failure_threshold=3, reset_timeout_seconds=20),
    "jira": CircuitBreaker("jira", failure_threshold=2, reset_timeout_seconds=30),
}


async def guarded_post(client: httpx.AsyncClient, service: str, url: str, **kwargs):
    breaker = OUTBOUND_BREAKERS[service]
    breaker.allow()
    try:
        response = await client.post(url, **kwargs)
        response.raise_for_status()
        breaker.record_success()
        return response
    except Exception:
        breaker.record_failure()
        raise

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
    init_observability_tables()
    
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
    fault_error_rate: float = 0.20
    fault_family: str = "payment"


class RCAFeedback(BaseModel):
    is_accurate: bool
    actual_root_cause: Optional[str] = None
    operator_feedback: Optional[str] = None
    verified_by: Optional[str] = None


SAFE_INCIDENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_incident_id(incident_id: str) -> str:
    if not SAFE_INCIDENT_ID.fullmatch(incident_id or ""):
        raise HTTPException(status_code=400, detail="Invalid incident_id format")
    return incident_id


def init_observability_tables():
    ch = get_ch()
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {CH_DB}.rca_feedback (
            incident_id String,
            predicted_root_cause String,
            actual_root_cause String,
            is_accurate UInt8,
            operator_feedback String,
            verified_by String,
            confidence Float64,
            created_at DateTime,
            verified_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY (verified_at, incident_id)
        """
    )
    # MTTR tracking table — records time-to-resolve for each incident
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {CH_DB}.mttr_events (
            incident_id String,
            root_cause String,
            fault_family String,
            confidence Float64,
            detected_at DateTime,
            resolved_at DateTime DEFAULT now(),
            resolution_seconds Float64,
            resolution_method String,
            auto_remediated UInt8 DEFAULT 0,
            resolved_by String DEFAULT 'system'
        ) ENGINE = MergeTree()
        ORDER BY (resolved_at, fault_family)
        """
    )

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "causaliq-backend", "ts": datetime.utcnow().isoformat()}

from .remediation.queue import PendingRemediationQueue
pending_queue = PendingRemediationQueue(REDIS_URL + "/1")
JIRA_TICKET_PREFIX = "jira_ticket:"


class JiraTicketSync(BaseModel):
    incident_id: str
    ticket_id: Optional[str] = None
    ticket_url: Optional[str] = None
    status: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolution_action: Optional[str] = None
    updated_at: Optional[str] = None
    source: Optional[str] = None


async def store_jira_ticket(ticket: dict):
    redis = await get_redis()
    await redis.setex(f"{JIRA_TICKET_PREFIX}{ticket['incident_id']}", 7 * 24 * 3600, json.dumps(ticket))


async def get_jira_ticket(incident_id: str) -> Optional[dict]:
    redis = await get_redis()
    cached = await redis.get(f"{JIRA_TICKET_PREFIX}{incident_id}")
    return json.loads(cached) if cached else None


@app.post("/jira/webhook")
async def jira_webhook(payload: JiraTicketSync):
    record = {
        "incident_id": payload.incident_id,
        "ticket_id": payload.ticket_id,
        "ticket_url": payload.ticket_url,
        "status": payload.status or "OPEN",
        "resolution_notes": payload.resolution_notes,
        "resolution_action": payload.resolution_action,
        "updated_at": payload.updated_at or datetime.utcnow().isoformat(),
        "source": payload.source or "jira-bridge",
    }
    await store_jira_ticket(record)
    return {"status": "synced", **record}


@app.post("/incidents/{incident_id}/feedback")
async def submit_incident_feedback(incident_id: str, feedback: RCAFeedback):
    safe_incident_id = sanitize_incident_id(incident_id)
    ch = get_ch()
    incident_rows = ch.query(
        f"""
        SELECT incident_id, root_cause, confidence, created_at
        FROM {CH_DB}.incidents
        WHERE incident_id = {{p_incident_id:String}}
        LIMIT 1
        """,
        parameters={"p_incident_id": safe_incident_id},
    )
    if not incident_rows.result_rows:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident_id_db, predicted_root_cause, confidence, created_at = incident_rows.result_rows[0]
    actual_root_cause = feedback.actual_root_cause or (predicted_root_cause if feedback.is_accurate else "unknown")
    ch.insert(
        f"{CH_DB}.rca_feedback",
        [[
            incident_id_db,
            predicted_root_cause,
            actual_root_cause,
            1 if feedback.is_accurate else 0,
            feedback.operator_feedback or "",
            feedback.verified_by or "anonymous",
            float(confidence or 0.0),
            created_at,
        ]],
        column_names=[
            "incident_id",
            "predicted_root_cause",
            "actual_root_cause",
            "is_accurate",
            "operator_feedback",
            "verified_by",
            "confidence",
            "created_at",
        ],
    )
    return {
        "status": "recorded",
        "incident_id": incident_id_db,
        "predicted_root_cause": predicted_root_cause,
        "actual_root_cause": actual_root_cause,
        "is_accurate": feedback.is_accurate,
    }


@app.get("/accuracy-metrics")
async def get_accuracy_metrics():
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT
            predicted_root_cause,
            count() AS total,
            sum(is_accurate) AS correct,
            if(total = 0, 0.0, round(correct / total, 4)) AS accuracy
        FROM {CH_DB}.rca_feedback
        GROUP BY predicted_root_cause
        ORDER BY total DESC, predicted_root_cause
        """
    )
    overall = ch.query(
        f"""
        SELECT
            count() AS total,
            sum(is_accurate) AS correct,
            if(total = 0, 0.0, round(correct / total, 4)) AS accuracy
        FROM {CH_DB}.rca_feedback
        """
    )
    overall_row = overall.result_rows[0] if overall.result_rows else (0, 0, 0.0)
    return {
        "overall": {
            "total": int(overall_row[0]),
            "correct": int(overall_row[1]),
            "accuracy": float(overall_row[2] or 0.0),
        },
        "by_root_cause": [
            {
                "predicted_root_cause": r[0],
                "total": int(r[1]),
                "correct": int(r[2]),
                "accuracy": float(r[3] or 0.0),
            }
            for r in rows.result_rows
        ],
    }


@app.get("/resilience")
async def resilience_status():
    return {
        name: {
            "failure_count": breaker.failure_count,
            "opened_at": breaker.opened_at.isoformat() if breaker.opened_at else None,
            "reset_timeout_seconds": breaker.reset_timeout_seconds,
        }
        for name, breaker in OUTBOUND_BREAKERS.items()
    }


# ── MTTR Tracking ─────────────────────────────────────────────────────────────
class MTTRRecord(BaseModel):
    detected_at: str
    resolution_method: str = "manual"
    auto_remediated: bool = False
    resolved_by: str = "operator"


@app.post("/incidents/{incident_id}/resolve")
async def record_incident_resolution(incident_id: str, record: MTTRRecord):
    """Record resolution time for MTTR tracking and feedback loop validation."""
    safe_incident_id = sanitize_incident_id(incident_id)
    ch = get_ch()
    # Look up the incident
    incident_rows = ch.query(
        f"""
        SELECT incident_id, root_cause, created_at
        FROM {CH_DB}.incidents
        WHERE incident_id = {{p_incident_id:String}}
        LIMIT 1
        """,
        parameters={"p_incident_id": safe_incident_id},
    )
    if not incident_rows.result_rows:
        raise HTTPException(status_code=404, detail="Incident not found")

    inc_id, root_cause, created_at = incident_rows.result_rows[0]
    resolved_at = datetime.utcnow()
    try:
        detected = datetime.fromisoformat(record.detected_at.replace("Z", "+00:00").replace("+00:00", ""))
    except (ValueError, AttributeError):
        detected = created_at

    resolution_seconds = (resolved_at - detected).total_seconds()

    # Get fault_family from evidence if available
    evidence_rows = ch.query(
        f"""
        SELECT evidence_json FROM {CH_DB}.incidents
        WHERE incident_id = {{p_incident_id:String}} LIMIT 1
        """,
        parameters={"p_incident_id": safe_incident_id},
    )
    fault_family = "unknown"
    if evidence_rows.result_rows:
        try:
            evidence = json.loads(evidence_rows.result_rows[0][0])
            fault_family = evidence.get("fault_family", "unknown")
        except (json.JSONDecodeError, IndexError):
            pass

    ch.insert(
        f"{CH_DB}.mttr_events",
        [[
            inc_id,
            root_cause,
            fault_family,
            0.0,  # confidence — enriched separately
            detected,
            resolved_at,
            resolution_seconds,
            record.resolution_method,
            1 if record.auto_remediated else 0,
            record.resolved_by,
        ]],
        column_names=[
            "incident_id", "root_cause", "fault_family", "confidence",
            "detected_at", "resolved_at", "resolution_seconds",
            "resolution_method", "auto_remediated", "resolved_by",
        ],
    )
    return {
        "status": "recorded",
        "incident_id": inc_id,
        "resolution_seconds": round(resolution_seconds, 2),
        "fault_family": fault_family,
    }


@app.get("/mttr-analysis")
async def mttr_analysis():
    """
    MTTR Feedback Loop Analysis.
    Compares recent MTTR against historical baseline to validate
    measurable reduction. Uses rolling 7-day vs 30-day comparison.
    """
    ch = get_ch()

    # Recent 7 days
    recent = ch.query(
        f"""
        SELECT
            fault_family,
            count() AS incidents,
            avg(resolution_seconds) AS avg_mttr_sec,
            median(resolution_seconds) AS median_mttr_sec,
            min(resolution_seconds) AS min_mttr_sec,
            max(resolution_seconds) AS max_mttr_sec,
            sum(auto_remediated) AS auto_count
        FROM {CH_DB}.mttr_events
        WHERE resolved_at > now() - INTERVAL 7 DAY
        GROUP BY fault_family
        ORDER BY fault_family
        """
    )

    # Baseline: 8-30 days ago
    baseline = ch.query(
        f"""
        SELECT
            fault_family,
            count() AS incidents,
            avg(resolution_seconds) AS avg_mttr_sec,
            median(resolution_seconds) AS median_mttr_sec
        FROM {CH_DB}.mttr_events
        WHERE resolved_at > now() - INTERVAL 30 DAY
          AND resolved_at <= now() - INTERVAL 7 DAY
        GROUP BY fault_family
        ORDER BY fault_family
        """
    )

    # Overall recent vs baseline
    overall_recent = ch.query(
        f"""
        SELECT
            count() AS incidents,
            avg(resolution_seconds) AS avg_mttr,
            median(resolution_seconds) AS median_mttr
        FROM {CH_DB}.mttr_events
        WHERE resolved_at > now() - INTERVAL 7 DAY
        """
    )
    overall_baseline = ch.query(
        f"""
        SELECT
            count() AS incidents,
            avg(resolution_seconds) AS avg_mttr,
            median(resolution_seconds) AS median_mttr
        FROM {CH_DB}.mttr_events
        WHERE resolved_at > now() - INTERVAL 30 DAY
          AND resolved_at <= now() - INTERVAL 7 DAY
        """
    )

    def safe_row(rows, idx=0):
        if rows.result_rows and len(rows.result_rows) > idx:
            return rows.result_rows[idx]
        return (0, 0, 0)

    r = safe_row(overall_recent)
    b = safe_row(overall_baseline)
    recent_avg = float(r[1] or 0)
    baseline_avg = float(b[1] or 0)
    reduction_pct = (
        round((baseline_avg - recent_avg) / baseline_avg * 100, 2)
        if baseline_avg > 0 else 0.0
    )

    # Build baseline lookup
    baseline_map = {row[0]: row for row in baseline.result_rows}

    per_family = []
    for row in recent.result_rows:
        family = row[0]
        base_row = baseline_map.get(family)
        base_avg = float(base_row[2]) if base_row else 0
        curr_avg = float(row[2] or 0)
        fam_reduction = (
            round((base_avg - curr_avg) / base_avg * 100, 2)
            if base_avg > 0 else 0.0
        )
        per_family.append({
            "fault_family": family,
            "recent_incidents": int(row[1]),
            "avg_mttr_seconds": round(curr_avg, 2),
            "median_mttr_seconds": round(float(row[3] or 0), 2),
            "min_mttr_seconds": round(float(row[4] or 0), 2),
            "max_mttr_seconds": round(float(row[5] or 0), 2),
            "auto_remediated_count": int(row[6] or 0),
            "baseline_avg_mttr_seconds": round(base_avg, 2),
            "mttr_reduction_pct": fam_reduction,
            "improving": fam_reduction > 0,
        })

    return {
        "analysis_window": {
            "recent": "last 7 days",
            "baseline": "8-30 days ago",
        },
        "overall": {
            "recent_incidents": int(r[0] or 0),
            "recent_avg_mttr_seconds": round(recent_avg, 2),
            "baseline_incidents": int(b[0] or 0),
            "baseline_avg_mttr_seconds": round(baseline_avg, 2),
            "mttr_reduction_pct": reduction_pct,
            "measurable_improvement": reduction_pct > 5.0,
        },
        "per_family": per_family,
        "feedback_loop_verdict": (
            "MTTR reduction validated ✅" if reduction_pct > 5.0
            else "Insufficient data or no improvement yet ⚠️"
        ),
    }


# ── Accuracy Gate Status ──────────────────────────────────────────────────────
@app.get("/accuracy-gate")
async def accuracy_gate_status():
    """
    Returns the Phase 5 accuracy gate status by reading feedback data.
    Checks if missed_detection_rate is below the configured threshold.
    """
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT
            count() AS total,
            sum(is_accurate) AS correct,
            count() - sum(is_accurate) AS incorrect
        FROM {CH_DB}.rca_feedback
        WHERE verified_at > now() - INTERVAL 7 DAY
        """
    )
    row = rows.result_rows[0] if rows.result_rows else (0, 0, 0)
    total = int(row[0] or 0)
    correct = int(row[1] or 0)
    incorrect = int(row[2] or 0)
    accuracy = correct / total if total > 0 else 0.0
    missed_rate = incorrect / total if total > 0 else 0.0

    threshold = float(os.getenv("MAX_MISSED_DETECTION_RATE", "0.10"))
    gate_passes = missed_rate <= threshold

    return {
        "total_feedback_entries": total,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": round(accuracy, 4),
        "missed_detection_rate": round(missed_rate, 4),
        "threshold": threshold,
        "gate_passes": gate_passes,
        "verdict": "PASS ✅" if gate_passes else "FAIL ❌",
    }

@app.get("/incidents")
async def list_incidents(limit: int = 50, offset: int = 0):
    """Return recent incidents with real-time approval status."""
    # Clamp to safe ranges to prevent abuse
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, min(offset, 100000))
    ch = get_ch()
    rows = ch.query(
        f"""
         SELECT incident_id, root_cause, confidence, impact_chain,
             anomaly_count, explanation, created_at
        FROM {CH_DB}.incidents
        ORDER BY created_at DESC
        LIMIT {{p_limit:UInt32}} OFFSET {{p_offset:UInt32}}
        """,
        parameters={"p_limit": safe_limit, "p_offset": safe_offset},
    )
    result = []
    for row in rows.result_rows:
        inc_id = row[0]
        # Check if this incident is currently pending approval
        pending = await pending_queue.get_action(inc_id)
        status = pending.get("status", "RESOLVED") if pending else "DONE"
        jira_ticket = await get_jira_ticket(inc_id)
        
        result.append({
            "incident_id": inc_id,
            "root_cause": row[1],
            "confidence": row[2],
            "impact_chain": json.loads(row[3]) if row[3] else [],
            "anomaly_count": row[4],
            "explanation": row[5][:500] if row[5] else "",
            "ticket_id": (jira_ticket or {}).get("ticket_id", ""),
            "ticket_url": (jira_ticket or {}).get("ticket_url", ""),
            "ticket_status": (jira_ticket or {}).get("status", ""),
            "ticket_source": (jira_ticket or {}).get("source", ""),
            "ticket_created_at": (jira_ticket or {}).get("updated_at", ""),
            "created_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]),
            "status": status,
            "remediation": pending.get("action", None) if pending else None
        })
    return result


@app.get("/rca/{incident_id}")
async def get_rca(incident_id: str):
    """Full RCA detail for a specific incident."""
    safe_incident_id = sanitize_incident_id(incident_id)
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT incident_id, root_cause, confidence, impact_chain,
               anomaly_count, explanation, evidence_json, created_at
        FROM {CH_DB}.incidents
        WHERE incident_id = {{p_incident_id:String}}
        LIMIT 1
        """,
        parameters={"p_incident_id": safe_incident_id},
    )
    if not rows.result_rows:
        raise HTTPException(status_code=404, detail="Incident not found")
    row = rows.result_rows[0]
    jira_ticket = await get_jira_ticket(safe_incident_id)
    return {
        "incident_id": row[0],
        "root_cause": row[1],
        "confidence": row[2],
        "impact_chain": json.loads(row[3]) if row[3] else [],
        "anomaly_count": row[4],
        "explanation": row[5],
        "evidence": json.loads(row[6]) if row[6] else {},
        "ticket_id": (jira_ticket or {}).get("ticket_id", ""),
        "ticket_url": (jira_ticket or {}).get("ticket_url", ""),
        "ticket_status": (jira_ticket or {}).get("status", ""),
        "ticket_source": (jira_ticket or {}).get("source", ""),
        "ticket_created_at": (jira_ticket or {}).get("updated_at", ""),
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
    safe_limit = max(1, min(limit, 1000))
    ch = get_ch()
    rows = ch.query(
        f"""
        SELECT service, anomaly_score, avg_latency_ms, error_rate, throughput_rps, detected_at
        FROM {CH_DB}.anomaly_events
        ORDER BY detected_at DESC
        LIMIT {{p_limit:UInt32}}
        """,
        parameters={"p_limit": safe_limit},
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
    fault_targets = {
        "payment": ("payment", f"{PAYMENT_URL}/admin/fault"),
        "auth": ("auth", f"{AUTH_URL}/admin/fault"),
        "order": ("order", f"{ORDER_URL}/admin/fault"),
    }

    family = config.fault_family.lower().strip()
    if family not in fault_targets:
        raise HTTPException(status_code=400, detail=f"Unsupported fault_family '{config.fault_family}'. Use payment|auth|order")

    if config.inject_fault:
        service_name, url = fault_targets[family]
        async with httpx.AsyncClient() as client:
            try:
                await guarded_post(
                    client,
                    service_name,
                    url,
                    json={
                        "active": True,
                        "db_latency_ms": config.fault_db_latency_ms,
                        "error_rate": config.fault_error_rate,
                    },
                    timeout=5,
                )
                logger.info(
                    "Fault injection enabled: family=%s latency_ms=%d error_rate=%.2f",
                    family,
                    config.fault_db_latency_ms,
                    config.fault_error_rate,
                )
            except Exception as exc:
                logger.warning("Fault injection failed: family=%s error=%s", family, exc)

    async def generate_load():
        import asyncio
        token = None
        async with httpx.AsyncClient() as client:
            # Get auth token
            try:
                r = await guarded_post(
                    client,
                    "auth",
                    f"{AUTH_URL}/auth/login",
                    json={"username": "alice", "password": "pass123"},
                    timeout=5,
                )
                if r.status_code == 200:
                    token = r.json().get("access_token")
            except Exception:
                pass

        products = ["laptop", "phone", "tablet", "monitor", "keyboard"]
        import random

        async def one_request(session: httpx.AsyncClient):
            try:
                await guarded_post(
                    session,
                    "order",
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
            service_name, url = fault_targets[family]
            async with httpx.AsyncClient() as c:
                try:
                    await guarded_post(
                        c,
                        service_name,
                        url,
                        json={"active": False, "db_latency_ms": 50, "error_rate": 0.0},
                        timeout=5,
                    )
                    logger.info("Fault injection disabled: family=%s", family)
                except Exception:
                    pass

    asyncio.create_task(generate_load())
    return {
        "status": "load_test_started",
        "duration_seconds": config.duration_seconds,
        "concurrency": config.concurrency,
        "fault_injected": config.inject_fault,
        "fault_family": family,
        "fault_db_latency_ms": config.fault_db_latency_ms if config.inject_fault else 0,
        "fault_error_rate": config.fault_error_rate if config.inject_fault else 0.0,
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
