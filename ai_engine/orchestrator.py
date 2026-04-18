"""
CausalIQ RCA Orchestrator Service
Consumes rca-results from Kafka, runs full RCA pipeline, stores in ClickHouse + Neo4j.
"""
import os
import json
import time
import uuid
import logging
from datetime import datetime

import clickhouse_connect
from confluent_kafka import Consumer, KafkaError

from causal.graph_engine import CausalGraphEngine
from causal.bayesian_engine import BayesianCausalEngine
from llm.agent import RCAAgent
from llm.rag_manager import IncidentRAG
from llm.explainer import RCAExplainer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("rca-orchestrator")

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
CH_HOST           = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT           = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER           = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS           = os.getenv("CLICKHOUSE_PASSWORD", "causaliq123")
CH_DB             = os.getenv("CLICKHOUSE_DB", "causaliq")


# ── ClickHouse Schema ─────────────────────────────────────────────────────────
INIT_SQL = [
    f"CREATE DATABASE IF NOT EXISTS {CH_DB}",
    f"""
    CREATE TABLE IF NOT EXISTS {CH_DB}.incidents (
        incident_id    String,
        root_cause     String,
        confidence     Float64,
        impact_chain   String,   -- JSON array
        anomaly_count  UInt32,
        explanation    String,
        evidence_json  String,   -- JSON blob
        created_at     DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (created_at, root_cause)
    PARTITION BY toYYYYMM(created_at)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {CH_DB}.anomaly_events (
        event_id       String,
        service        String,
        anomaly_score  Float64,
        avg_latency_ms Float64,
        error_rate     Float64,
        throughput_rps Float64,
        detected_at    DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (detected_at, service)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {CH_DB}.service_metrics (
        service        String,
        avg_latency_ms Float64,
        p99_latency_ms Float64,
        error_rate     Float64,
        throughput_rps Float64,
        ts             DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (ts, service)
    TTL ts + INTERVAL 7 DAY
    """,
]


def init_clickhouse(client) -> None:
    for sql in INIT_SQL:
        client.command(sql.strip())
    logger.info("ClickHouse schema ready")


def insert_incident(client, incident: dict):
    client.insert(
        f"{CH_DB}.incidents",
        [[
            incident["incident_id"],
            incident["root_cause"],
            incident["confidence"],
            json.dumps(incident["impact_chain"]),
            incident["anomaly_count"],
            incident["explanation"],
            json.dumps(incident["evidence"]),
        ]],
        column_names=["incident_id", "root_cause", "confidence", "impact_chain",
                      "anomaly_count", "explanation", "evidence_json"],
    )


def insert_anomaly_events(client, anomalies: list[dict]):
    rows = []
    for a in anomalies:
        rows.append([
            str(uuid.uuid4()),
            a.get("service", "unknown"),
            float(a.get("anomaly_score", 0)),
            float(a.get("avg_latency_ms", 0)),
            float(a.get("error_rate", 0)),
            float(a.get("throughput_rps", 0)),
        ])
    if rows:
        client.insert(
            f"{CH_DB}.anomaly_events",
            rows,
            column_names=["event_id", "service", "anomaly_score",
                          "avg_latency_ms", "error_rate", "throughput_rps"],
        )


# ── Main Orchestrator ─────────────────────────────────────────────────────────
def run():
    # Initialize clients
    ch_client    = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT,
                                                  username=CH_USER, password=CH_PASS)
    causal_engine = CausalGraphEngine()
    bayesian_engine = BayesianCausalEngine()
    rag_memory      = IncidentRAG()
    llm_explainer   = RCAExplainer() # Still used for embeddings
    agent_brain     = RCAAgent(ch_client, causal_engine)
    
    init_clickhouse(ch_client)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "rca-orchestrator",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })
    consumer.subscribe(["rca-results"])

    logger.info("RCA Orchestrator started.")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error("Kafka error: %s", msg.error())
                continue

            try:
                payload = json.loads(msg.value().decode("utf-8"))
            except json.JSONDecodeError:
                continue

            anomalies     = payload.get("anomalies", [])
            dependencies  = payload.get("dependencies", {})
            detected_at   = payload.get("detected_at", datetime.utcnow().isoformat())

            if not anomalies:
                continue

            logger.info("Processing RCA for %d anomalies", len(anomalies))

            # 1. Update dependency graph
            for svc, deps in dependencies.items():
                causal_engine.upsert_service(svc)
                for dep in deps:
                    causal_engine.upsert_dependency(svc, dep)
            
            # Update Bayesian Network structure
            all_edges = causal_engine.get_all_dependencies() # Need this method
            bayesian_engine.build_network(all_edges)
            
            # For production: Pull actual bits from ClickHouse to train. 
            # For now, we use a simple identity prior if no training data is present.
            if not bayesian_engine.is_trained:
                bayesian_engine.is_trained = True # Mock training for first run

            # 2. Record anomalies in graph
            for a in anomalies:
                causal_engine.record_anomaly(
                    a["service"], a["anomaly_score"], a["timestamp"],
                    {"avg_latency_ms": a.get("avg_latency_ms"), "error_rate": a.get("error_rate")}
                )

            # 3. Rank root causes
            ranked = causal_engine.rank_root_causes(anomalies, dependencies)
            if not ranked:
                continue

            root_candidate = ranked[0]

            # 4. Bayesian Causal inference
            evidence = {a["service"]: 1 for a in anomalies}
            bayesian_results = bayesian_engine.identify_root_cause(evidence)
            
            # Use top Bayesian result to refine root cause
            if bayesian_results:
                top_bayesian = bayesian_results[0]
                root_svc = top_bayesian["service"]
                causal_prob = top_bayesian["probability"]
            else:
                root_svc = root_candidate["service"]
                causal_prob = root_candidate["probability"]

            # 5. Build impact chain
            downstream = causal_engine.trace_downstream(root_svc)
            impact_chain = [root_svc] + downstream

            # 6. Compute confidence
            confidence = round(
                causal_prob * 0.6 + root_candidate["probability"] * 0.4,
                4
            )

            # 7. Generate Agentic LLM explanation
            rca_report = agent_brain.run_analysis(root_svc, anomalies)

            # 8. Build final incident record
            incident_id = llm_result.get("incident_id", str(uuid.uuid4())[:8])
            incident = {
                "incident_id": incident_id,
                "root_cause": root_svc,
                "confidence": confidence,
                "impact_chain": impact_chain,
                "anomaly_count": len(anomalies),
                "explanation": rca_report,
                "evidence": {
                    "anomalies": anomalies,
                    "bayesian_probs": bayesian_results[:5],
                },
                "detected_at": detected_at,
                "created_at": datetime.utcnow().isoformat(),
            }

            # 9. Store in ClickHouse
            try:
                insert_incident(ch_client, incident)
                insert_anomaly_events(ch_client, anomalies)
            except Exception as exc:
                logger.error("ClickHouse insert error: %s", exc)

            logger.info(
                "RCA complete: incident_id=%s root=%s confidence=%.1f%% chain=%s",
                incident_id, root_svc, confidence * 100, " → ".join(impact_chain)
            )

    except KeyboardInterrupt:
        logger.info("Shutting down RCA orchestrator")
    finally:
        consumer.close()
        causal_engine.close()


if __name__ == "__main__":
    time.sleep(30)  # Wait for dependencies
    run()
