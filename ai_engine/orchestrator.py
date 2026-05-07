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
import httpx
from confluent_kafka import Consumer, KafkaError

from causal.graph_engine import CausalGraphEngine
from causal.bayesian_engine import BayesianCausalEngine
from llm.agent import RCAAgent
from llm.rag_manager import IncidentRAG
from llm.explainer import RCAExplainer
from remediation.queue import PendingRemediationQueue
from remediation.slack_gate import SlackApprovalGate
from remediation.executor import AutonomousRemediationExecutor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("rca-orchestrator")

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
CH_HOST           = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT           = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CH_USER           = os.getenv("CLICKHOUSE_USER", "default")
CH_PASS           = os.getenv("CLICKHOUSE_PASSWORD", "causaliq123")
CH_DB             = os.getenv("CLICKHOUSE_DB", "causaliq")
JIRA_BRIDGE_URL   = os.getenv("JIRA_BRIDGE_URL", "http://jira-bridge:8010")
JIRA_AUTO_TICKET_THRESHOLD = float(os.getenv("JIRA_AUTO_TICKET_THRESHOLD", "0.8"))

INIT_SQL = [
    f"CREATE DATABASE IF NOT EXISTS {CH_DB}",
    f"CREATE TABLE IF NOT EXISTS {CH_DB}.incidents (incident_id String, root_cause String, confidence Float64, impact_chain String, anomaly_count UInt32, explanation String, evidence_json String, ticket_id String, ticket_url String, ticket_status String, ticket_source String, ticket_created_at DateTime, created_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY (created_at, root_cause)",
    f"CREATE TABLE IF NOT EXISTS {CH_DB}.anomaly_events (event_id String, service String, anomaly_score Float64, avg_latency_ms Float64, error_rate Float64, throughput_rps Float64, detected_at DateTime DEFAULT now()) ENGINE = MergeTree() ORDER BY (detected_at, service)"
]

def init_clickhouse(client) -> None:
    for sql in INIT_SQL:
        client.command(sql.strip())
    client.command(f"ALTER TABLE {CH_DB}.incidents ADD COLUMN IF NOT EXISTS ticket_id String AFTER evidence_json")
    client.command(f"ALTER TABLE {CH_DB}.incidents ADD COLUMN IF NOT EXISTS ticket_url String AFTER ticket_id")
    client.command(f"ALTER TABLE {CH_DB}.incidents ADD COLUMN IF NOT EXISTS ticket_status String AFTER ticket_url")
    client.command(f"ALTER TABLE {CH_DB}.incidents ADD COLUMN IF NOT EXISTS ticket_source String AFTER ticket_status")
    client.command(f"ALTER TABLE {CH_DB}.incidents ADD COLUMN IF NOT EXISTS ticket_created_at DateTime AFTER ticket_source")
    logger.info("ClickHouse schema ready")

def insert_incident(client, incident: dict):
    client.insert(f"{CH_DB}.incidents", [[
        incident["incident_id"], incident["root_cause"], incident["confidence"],
        json.dumps(incident["impact_chain"]), incident["anomaly_count"],
        incident["explanation"], json.dumps(incident["evidence"]),
        incident.get("ticket_id", ""), incident.get("ticket_url", ""),
        incident.get("ticket_status", ""), incident.get("ticket_source", ""),
        incident.get("ticket_created_at", datetime.utcnow())
    ]], column_names=["incident_id", "root_cause", "confidence", "impact_chain", "anomaly_count", "explanation", "evidence_json", "ticket_id", "ticket_url", "ticket_status", "ticket_source", "ticket_created_at"])

async def maybe_create_jira_ticket(incident: dict) -> dict:
    if incident.get("confidence", 0.0) < JIRA_AUTO_TICKET_THRESHOLD:
        return {}

    payload = {
        "incident_id": incident["incident_id"],
        "root_cause": incident["root_cause"],
        "confidence": incident["confidence"],
        "explanation": incident["explanation"],
        "impact_chain": incident.get("impact_chain", []),
        "anomalies": incident.get("evidence", {}).get("anomalies", []),
        "evidence": incident.get("evidence", {}),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{JIRA_BRIDGE_URL.rstrip('/')}/tickets/create", json=payload)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.warning("Jira ticket creation skipped for %s: %s", incident.get("incident_id"), exc)
        return {}

def insert_anomaly_events(client, anomalies: list[dict]):
    rows = [[str(uuid.uuid4()), a.get("service", "unknown"), float(a.get("anomaly_score", 0)), float(a.get("avg_latency_ms", 0)), float(a.get("error_rate", 0)), float(a.get("throughput_rps", 0))] for a in anomalies]
    if rows:
        client.insert(f"{CH_DB}.anomaly_events", rows, column_names=["event_id", "service", "anomaly_score", "avg_latency_ms", "error_rate", "throughput_rps"])

# ── Fault-Family-Aware Remediation Suggestions ────────────────────────────────
REMEDIATION_PLAYBOOK = {
    "db-latency": "Scale DB connection pool for {service} (increase max_connections), flush idle connections",
    "memory-leak": "Restart {service} pods with rolling deployment, enable heap dump capture",
    "cpu-spike": "Scale {service} horizontally (add replicas), check for tight loops or regex catastrophe",
    "network-timeout": "Check upstream dependencies of {service}, verify DNS resolution, increase timeout budgets",
    "cascading-failure": "Isolate {service} via circuit breaker, shed load, trigger runbook for multi-service recovery",
    "unknown": "Restart {service} and collect diagnostic logs for manual triage",
}


def _suggest_remediation(service: str, fault_family: str) -> str:
    """Generate a fault-family-specific remediation action string."""
    template = REMEDIATION_PLAYBOOK.get(fault_family, REMEDIATION_PLAYBOOK["unknown"])
    return template.format(service=service)


async def run():
    ch_client = clickhouse_connect.get_client(host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASS)
    causal_engine = CausalGraphEngine()
    bayesian_engine = BayesianCausalEngine()
    agent_brain = RCAAgent(ch_client, causal_engine)
    queue = PendingRemediationQueue()
    slack = SlackApprovalGate()
    executor = AutonomousRemediationExecutor()
    
    # Auto-remediation threshold
    AUTO_THRESHOLD = float(os.getenv("AUTO_REMEDIATION_THRESHOLD", "0.95"))
    
    # Cooldown tracker for LLM calls (service -> last_call_time)
    llm_cooldowns = {}
    COOLDOWN_WINDOW = 120  # 2 minutes
    
    init_clickhouse(ch_client)
    
    # Build Bayesian network from service topology (non-fatal if fails)
    try:
        edges = causal_engine.get_all_dependencies()
        bayesian_engine.build_network(edges)
        
        # Train from historical incident data
        try:
            history_rows = ch_client.query(f"SELECT * FROM {CH_DB}.anomaly_events ORDER BY detected_at DESC LIMIT 1000").result_rows
            if history_rows and len(history_rows) > 10:
                import pandas as pd
                services = set()
                for row in history_rows:
                    services.add(row[1])  # service column
                history_df = pd.DataFrame({svc: [0] * len(history_rows) for svc in services})
                for idx, row in enumerate(history_rows):
                    service = row[1]
                    if service in history_df.columns:
                        history_df.at[idx, service] = 1
                bayesian_engine.train_from_history(history_df)
                logger.info(f"Bayesian engine trained with {len(history_rows)} historical events from {len(services)} services")
        except Exception as e:
            logger.warning(f"Bayesian training from history failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"Bayesian engine initialization failed (non-fatal, using heuristics): {e}")

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

                # 4. Filter root cause candidates and rank them
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

                # Get ranked root cause candidates using graph-based ranking
                ranked_candidates = causal_engine.rank_root_causes(anomalies, {})

                # Determine dominant fault family from anomaly events
                fault_families = [a.get("fault_family", "unknown") for a in anomalies if a.get("fault_family", "unknown") != "unknown"]
                dominant_fault_family = max(set(fault_families), key=fault_families.count) if fault_families else "unknown"
                
                impact_chain = [root_svc] + causal_engine.trace_downstream(root_svc)
                confidence = round(causal_prob * 0.6 + root_candidate.get("anomaly_score", 0.5) * 0.4, 3)
                
                # LLM analysis with cooldown and timeout
                current_time = datetime.utcnow().timestamp()
                last_call = llm_cooldowns.get(root_svc, 0)
                
                can_call_llm = (current_time - last_call) > COOLDOWN_WINDOW
                
                rca_report = None
                if can_call_llm:
                    try:
                        rca_report = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, agent_brain.run_analysis, root_svc, anomalies
                            ),
                            timeout=15.0
                        )
                        llm_cooldowns[root_svc] = current_time
                    except (asyncio.TimeoutError, Exception) as llm_err:
                        logger.warning(f"LLM analysis timed out or failed ({llm_err}), using generated explanation")
                else:
                    logger.info(f"LLM call skipped for {root_svc} due to cooldown ({round(COOLDOWN_WINDOW - (current_time - last_call))}s remaining)")

                if not rca_report:
                    top_anomalies_list = "\n".join(f"- **{a['service']}**: Anomaly Score {a.get('anomaly_score', 0):.3f} (Latency: {a.get('avg_latency_ms', 0):.1f}ms, Error Rate: {a.get('error_rate', 0):.1%})" for a in anomalies[:5])
                    chain_str = " ➔ ".join(f"`{s}`" for s in impact_chain)
                    rca_report = f"""### 🕵️ Autonomous Root Cause Analysis
Based on real-time statistical drift and Bayesian network inference, CausalIQ has successfully isolated the root cause to the **`{root_svc}`**.

#### 🔍 Diagnostic Evidence
* **Primary Fault Service**: `{root_svc}` (Confidence: **{confidence:.1%}**)
* **Fault Signature Profile**: `{dominant_fault_family}`
* **Causal Impact Chain**: {chain_str}

#### 📊 Affected Services
The following services exhibited correlated anomalous behavior due to cascading downstream failures:
{top_anomalies_list}

#### 🛠️ AI-Recommended Remediation
**Action Required:** {_suggest_remediation(root_svc, dominant_fault_family)}

*This detailed report was generated instantly by the CausalIQ deterministic heuristic engine to ensure zero-delay incident response (LLM Agent bypassed).*
"""
                
                incident = {
                    "incident_id": str(uuid.uuid4())[:8],
                    "root_cause": root_svc,
                    "confidence": confidence,
                    "fault_family": dominant_fault_family,
                    "impact_chain": impact_chain,
                    "anomaly_count": len(anomalies),
                    "explanation": rca_report,
                    "evidence": {
                        "anomalies": anomalies, 
                        "bayesian_probs": bayesian_results[:5] if bayesian_results else [],
                        "ranked_candidates": ranked_candidates[:5],
                        "fault_family": dominant_fault_family,
                        "causal_inference": {
                            "method": "bayesian" if bayesian_results else "graph_heuristic",
                            "causal_probability": causal_prob,
                            "evidence_services": [a["service"] for a in anomalies[:3]]
                        }
                    },
                    "detected_at": detected_at,
                }

                ticket_info = await maybe_create_jira_ticket(incident)
                if ticket_info:
                    incident["ticket_id"] = ticket_info.get("ticket_id", "")
                    incident["ticket_url"] = ticket_info.get("ticket_url", "")
                    incident["ticket_status"] = ticket_info.get("status", "OPEN")
                    incident["ticket_source"] = ticket_info.get("source", "local")
                    incident["ticket_created_at"] = datetime.utcnow()
                else:
                    incident["ticket_id"] = ""
                    incident["ticket_url"] = ""
                    incident["ticket_status"] = ""
                    incident["ticket_source"] = ""
                    incident["ticket_created_at"] = datetime.utcnow()

                incident_id = incident["incident_id"]

                # Store and Notify
                insert_incident(ch_client, incident)
                insert_anomaly_events(ch_client, anomalies)
                
                stored = await queue.store_pending_action(incident_id, {
                    "action": _suggest_remediation(root_svc, dominant_fault_family),
                    "service": root_svc, "confidence": confidence,
                    "fault_family": dominant_fault_family,
                })
                
                if stored:
                    suggested_action = _suggest_remediation(root_svc, dominant_fault_family)
                    evidence_lines = [f"{a['service']}: Score {a['anomaly_score']} ({a.get('fault_family', 'unknown')})" for a in anomalies[:3]]
                    
                    # --- CLOSED-LOOP REMEDIATION LOGIC ---
                    if confidence >= AUTO_THRESHOLD:
                        logger.info(f"CONFIDENCE HIGH ({confidence:.2f}). Triggering Autonomous Remediation...")
                        success = await executor.execute(incident_id, root_svc, suggested_action, confidence)
                        
                        if success:
                            # Notify Slack of the action taken
                            await slack.send_approval_request(
                                incident_id, confidence, 
                                f"✅ [AUTO-FIX APPLIED] {suggested_action}", 
                                evidence_lines,
                                auto_applied=True
                            )
                            await queue.update_status(incident_id, "AUTO_EXECUTED", approved_by="AI_ENGINE")
                        else:
                            # Fallback to manual if auto-fix fails
                            await slack.send_approval_request(incident_id, confidence, suggested_action, evidence_lines)
                    else:
                        # Standard Human-in-the-loop for lower confidence
                        await slack.send_approval_request(incident_id, confidence, suggested_action, evidence_lines)
                    
                    logger.info(f"Incident {incident_id} processed (family={dominant_fault_family}).")
                else:
                    logger.error(f"Failed to store pending action for {incident_id}. Slack message skipped.")

            except Exception as e:
                logger.error("Error processing RCA message: %s", e)

    finally:
        consumer.close()
        causal_engine.close()

if __name__ == "__main__":
    asyncio.run(run())
