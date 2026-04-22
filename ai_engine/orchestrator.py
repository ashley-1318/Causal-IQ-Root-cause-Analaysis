"""
CausalIQ RCA Orchestrator Service
Consumes rca-results from Kafka, runs full RCA pipeline, stores in ClickHouse + Neo4j.
"""
import os
import json
import asyncio
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
from remediation.queue import PendingRemediationQueue
from remediation.slack_gate import SlackApprovalGate

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("rca-orchestrator")

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
CH_HOST           = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT           = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER           = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS           = os.getenv("CLICKHOUSE_PASSWORD", "causaliq123")
CH_DB             = os.getenv("CLICKHOUSE_DB", "causaliq")

INIT_SQL = [
    f"CREATE DATABASE IF NOT EXISTS {CH_DB}",
    f"CREATE TABLE IF NOT EXISTS {CH_DB}.incidents (incident_id String, root_cause String, confidence Float64, impact_chain String, anomaly_count UInt32, explanation String, evidence_json String, created_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY (created_at, root_cause)",
    f"CREATE TABLE IF NOT EXISTS {CH_DB}.anomaly_events (event_id String, service String, anomaly_score Float64, avg_latency_ms Float64, error_rate Float64, throughput_rps Float64, detected_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY (detected_at, service)"
]

def init_clickhouse(client) -> None:
    for sql in INIT_SQL:
        client.command(sql.strip())
    logger.info("ClickHouse schema ready")

def insert_incident(client, incident: dict):
    client.insert(f"{CH_DB}.incidents", [[
        incident["incident_id"], incident["root_cause"], incident["confidence"],
        json.dumps(incident["impact_chain"]), incident["anomaly_count"],
        incident["explanation"], json.dumps(incident["evidence"])
    ]], column_names=["incident_id", "root_cause", "confidence", "impact_chain", "anomaly_count", "explanation", "evidence_json"])

def insert_anomaly_events(client, anomalies: list[dict]):
    rows = [[str(uuid.uuid4()), a.get("service", "unknown"), float(a.get("anomaly_score", 0)), float(a.get("avg_latency_ms", 0)), float(a.get("error_rate", 0)), float(a.get("throughput_rps", 0))] for a in anomalies]
    if rows:
        client.insert(f"{CH_DB}.anomaly_events", rows, column_names=["event_id", "service", "anomaly_score", "avg_latency_ms", "error_rate", "throughput_rps"])

async def run():
    ch_client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASS)
    causal_engine = CausalGraphEngine()
    bayesian_engine = BayesianCausalEngine()
    agent_brain = RCAAgent(ch_client, causal_engine)
    queue = PendingRemediationQueue()
    slack = SlackApprovalGate()
    
    init_clickhouse(ch_client)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "rca-orchestrator",
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
        "max.poll.interval.ms": 600000
    })
    consumer.subscribe(["rca-results"])

    logger.info("RCA Orchestrator (V4-FORCE-RELOAD) started.")

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None: continue
            if msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    logger.error("Kafka error: %s", msg.error())
                continue

            try:
                data = json.loads(msg.value().decode("utf-8"))
                detected_at = data.get("timestamp", datetime.utcnow().isoformat())
                anomalies = data.get("anomalies", [])
                
                if not anomalies: continue

                # 4. Filter root cause candidates
                root_candidate = min(anomalies, key=lambda x: x["anomaly_score"])
                
                # Convert anomaly list to the format BayesianEngine expects: {service: 1}
                evidence = {a["service"]: 1 for a in anomalies}
                bayesian_results = bayesian_engine.identify_root_cause(evidence)

                if bayesian_results:
                    root_svc = bayesian_results[0]["service"]
                    causal_prob = bayesian_results[0]["probability"]
                else:
                    root_svc = root_candidate["service"]
                    causal_prob = root_candidate.get("anomaly_score", 0.5)

                impact_chain = [root_svc] + causal_engine.trace_downstream(root_svc)
                confidence = round(causal_prob * 0.6 + root_candidate.get("anomaly_score", 0.5) * 0.4, 3)
                rca_report = agent_brain.run_analysis(root_svc, anomalies)
                incident_id = str(uuid.uuid4())[:8]

                incident = {
                    "incident_id": incident_id, "root_cause": root_svc, "confidence": confidence,
                    "impact_chain": impact_chain, "anomaly_count": len(anomalies), "explanation": rca_report,
                    "evidence": {"anomalies": anomalies, "bayesian_probs": bayesian_results[:5]},
                    "detected_at": detected_at
                }

                # Store and Notify
                insert_incident(ch_client, incident)
                insert_anomaly_events(ch_client, anomalies)
                
                stored = await queue.store_pending_action(incident_id, {
                    "action": f"Restart {root_svc} and optimize memory",
                    "service": root_svc, "confidence": confidence
                })
                
                if stored:
                    evidence_lines = [f"{a['service']}: Score {a['anomaly_score']}" for a in anomalies[:3]]
                    await slack.send_approval_request(incident_id, confidence, f"Restart {root_svc}", evidence_lines)
                    logger.info(f"Incident {incident_id} processed and sent to Slack.")
                else:
                    logger.error(f"Failed to store pending action for {incident_id}. Slack message skipped.")

            except Exception as e:
                logger.error("Error processing RCA message: %s", e)

    finally:
        consumer.close()
        causal_engine.close()

if __name__ == "__main__":
    asyncio.run(run())
