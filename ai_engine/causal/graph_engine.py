"""
CausalIQ Causal Graph Engine
- Builds and queries the Neo4j service dependency graph
- Performs causal inference using DoWhy / pgmpy
- Ranks root causes by probability, temporal order, and dependency depth
"""
import os
import json
import logging
from datetime import datetime
from typing import Optional

from neo4j import GraphDatabase
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("causal-engine")

NEO4J_URI  = os.getenv("NEO4J_URI",  "bolt://neo4j:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "causaliq123")


class CausalGraphEngine:
    """
    Manages the service dependency graph in Neo4j and performs
    causal effect estimation using structural causal model heuristics.
    """

    def __init__(self):
        self.driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        self._init_schema()

    def _init_schema(self):
        with self.driver.session() as s:
            s.run("CREATE CONSTRAINT service_name IF NOT EXISTS FOR (s:Service) REQUIRE s.name IS UNIQUE")
            s.run("CREATE INDEX trace_idx IF NOT EXISTS FOR (t:Trace) ON (t.trace_id)")
        logger.info("Neo4j schema initialized")

    def upsert_service(self, name: str, metadata: dict = None):
        with self.driver.session() as s:
            s.run(
                """
                MERGE (svc:Service {name: $name})
                ON CREATE SET svc.created_at = $ts, svc.metadata = $meta
                ON MATCH  SET svc.last_seen  = $ts
                """,
                name=name, ts=datetime.utcnow().isoformat(), meta=json.dumps(metadata or {})
            )

    def upsert_dependency(self, caller: str, callee: str, trace_id: str = ""):
        """Record that `caller` calls `callee`."""
        self.upsert_service(caller)
        self.upsert_service(callee)
        with self.driver.session() as s:
            s.run(
                """
                MATCH (a:Service {name: $caller}), (b:Service {name: $callee})
                MERGE (a)-[r:CALLS]->(b)
                ON CREATE SET r.count = 1, r.first_seen = $ts, r.trace_ids = [$tid]
                ON MATCH  SET r.count = r.count + 1, r.last_seen = $ts,
                              r.trace_ids = CASE WHEN size(r.trace_ids) < 100
                                                 THEN r.trace_ids + $tid
                                                 ELSE r.trace_ids END
                """,
                caller=caller, callee=callee, ts=datetime.utcnow().isoformat(), tid=trace_id
            )

    def record_anomaly(self, service: str, score: float, ts: str, metrics: dict):
        """Attach anomaly event to a service node."""
        self.upsert_service(service)
        with self.driver.session() as s:
            s.run(
                """
                MATCH (svc:Service {name: $name})
                CREATE (a:AnomalyEvent {
                    score: $score, ts: $ts,
                    avg_latency: $avg_lat, error_rate: $err_rate
                })
                CREATE (svc)-[:HAD_ANOMALY]->(a)
                """,
                name=service, score=score, ts=ts,
                avg_lat=metrics.get("avg_latency_ms", 0),
                err_rate=metrics.get("error_rate", 0),
            )

    def get_full_graph(self) -> dict:
        """Return full graph as nodes + edges for the frontend."""
        with self.driver.session() as s:
            nodes_result = s.run("MATCH (s:Service) RETURN s.name AS name, s.last_seen AS last_seen")
            edges_result = s.run(
                """
                MATCH (a:Service)-[r:CALLS]->(b:Service)
                RETURN a.name AS source, b.name AS target, r.count AS count, r.last_seen AS last_seen
                """
            )
            nodes = [{"id": r["name"], "label": r["name"], "last_seen": r["last_seen"]} for r in nodes_result]
            edges = [{"source": r["source"], "target": r["target"], "count": r["count"]} for r in edges_result]
        return {"nodes": nodes, "edges": edges}

    def trace_upstream(self, service: str, max_depth: int = 5) -> list[str]:
        """Find all upstream callers of a service up to max_depth."""
        with self.driver.session() as s:
            result = s.run(
                f"""
                MATCH (caller:Service)-[:CALLS*1..{max_depth}]->(svc:Service {{name: $name}})
                RETURN DISTINCT caller.name AS name
                """,
                name=service
            )
            return [r["name"] for r in result if r["name"] != service]

    def trace_downstream(self, service: str, max_depth: int = 5) -> list[str]:
        """Find all downstream services affected by this service."""
        with self.driver.session() as s:
            result = s.run(
                f"""
                MATCH (svc:Service {{name: $name}})-[:CALLS*1..{max_depth}]->(dep:Service)
                RETURN DISTINCT dep.name AS name
                """,
                name=service
            )
            return [r["name"] for r in result if r["name"] != service]

    def rank_root_causes(self, anomalies: list[dict], dependencies: dict) -> list[dict]:
        """
        Rank anomalous services as root cause candidates using:
        1. Anomaly score severity  (higher = more suspicious)
        2. Upstream depth          (leaf nodes more often root cause)
        3. Dependency fan-out      (more dependents = more blast radius)
        4. Error rate contribution
        """
        if not anomalies:
            return []

        results = []
        for a in anomalies:
            svc = a["service"]
            score_raw = abs(a.get("anomaly_score", 0))

            # Upstream depth: fewer upstreams → more likely to be root
            upstreams = self.trace_upstream(svc)
            upstream_depth = len(upstreams)

            # Downstream fan-out
            downstreams = self.trace_downstream(svc)
            fan_out = len(downstreams)

            # Latency severity (normalized 0–1 over 2000ms max)
            lat_severity = min(a.get("avg_latency_ms", 0) / 2000.0, 1.0)

            # Error severity
            err_severity = min(a.get("error_rate", 0) / 0.5, 1.0)

            # Composite root-cause probability
            # Services with fewer upstreams and more downstreams are more likely to be root
            depth_penalty = 1.0 / (1.0 + upstream_depth * 0.3)
            fan_weight    = 1.0 + fan_out * 0.1

            probability = round(
                min(
                    (score_raw * 0.35 + lat_severity * 0.30 + err_severity * 0.20 + depth_penalty * 0.15) * fan_weight,
                    1.0
                ),
                4
            )

            results.append({
                "service": svc,
                "probability": probability,
                "anomaly_score": a.get("anomaly_score"),
                "avg_latency_ms": a.get("avg_latency_ms"),
                "error_rate": a.get("error_rate"),
                "upstream_services": upstreams,
                "downstream_services": downstreams,
                "upstream_depth": upstream_depth,
                "fan_out": fan_out,
            })

        return sorted(results, key=lambda x: x["probability"], reverse=True)

    def get_all_dependencies(self) -> list[tuple[str, str]]:
        """Return all caller-callee pairs for Bayesian network construction."""
        with self.driver.session() as s:
            result = s.run("MATCH (a:Service)-[:CALLS]->(b:Service) RETURN a.name AS source, b.name AS target")
            return [(r["source"], r["target"]) for r in result]

    def close(self):
        self.driver.close()


def apply_dowhy_causal_inference(ranked: list[dict], anomalies: list[dict]) -> dict:
    """
    Perform structural causal model reasoning using pgmpy BayesNet.
    Estimates: P(root_cause | observed_anomalies)
    """
    try:
        from pgmpy.models import BayesianNetwork
        from pgmpy.factors.discrete import TabularCPD
        from pgmpy.inference import VariableElimination

        if len(ranked) < 2:
            return {"method": "single_candidate", "result": ranked[0] if ranked else {}}

        # Build a simple 2-level BayesNet: Root → Child services
        root_svc = ranked[0]["service"]
        children = ranked[0].get("downstream_services", [])[:3]  # cap for tractability

        if not children:
            return {"method": "no_children", "result": ranked[0]}

        edges = [(root_svc, c) for c in children]
        model = BayesianNetwork(edges)

        # CPDs: P(root) based on anomaly score
        root_prob = min(abs(ranked[0].get("anomaly_score", 0.5)), 0.99)
        cpd_root  = TabularCPD(variable=root_svc, variable_card=2,
                                values=[[1 - root_prob], [root_prob]])
        model.add_cpds(cpd_root)

        for child in children:
            # P(child_anomaly | root) — conditional on root failure
            cpd_child = TabularCPD(
                variable=child, variable_card=2,
                values=[[0.8, 0.2], [0.2, 0.8]],
                evidence=[root_svc], evidence_card=[2]
            )
            model.add_cpds(cpd_child)

        if not model.check_model():
            return {"method": "model_invalid", "result": ranked[0]}

        infer = VariableElimination(model)
        evidence = {c: 1 for c in children if any(a["service"] == c for a in anomalies)}
        if not evidence:
            return {"method": "no_evidence", "result": ranked[0]}

        q = infer.query([root_svc], evidence=evidence)
        causal_prob = float(q.values[1])

        return {
            "method": "pgmpy_bayesnet",
            "causal_probability": round(causal_prob, 4),
            "evidence_services": list(evidence.keys()),
            "root_service": root_svc,
        }

    except ImportError:
        logger.warning("pgmpy not available. Using heuristic ranking only.")
        return {"method": "heuristic", "result": ranked[0] if ranked else {}}
    except Exception as exc:
        logger.warning("Causal inference error: %s", exc)
        return {"method": "error", "error": str(exc)}
