"""
CausalIQ LLM RCA Explanation Engine
- Uses Ollama (Llama3/Mistral) for local LLM inference
- Implements RAG with Qdrant vector DB for past incident retrieval
- Generates structured, explainable RCA reports
"""
import os
import json
import uuid
import hashlib
import logging
import httpx
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("llm-engine")

OLLAMA_URL  = os.getenv("OLLAMA_URL",  "http://ollama:11434")
QDRANT_URL  = os.getenv("QDRANT_URL",  "http://qdrant:6333")
LLM_MODEL   = os.getenv("LLM_MODEL",  "llama3")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
COLLECTION  = "causaliq-incidents"


# ── Qdrant client (minimal, using REST) ───────────────────────────────────────
class QdrantClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._ensure_collection()

    def _ensure_collection(self):
        try:
            r = httpx.get(f"{self.url}/collections/{COLLECTION}", timeout=5)
            if r.status_code == 404:
                httpx.put(
                    f"{self.url}/collections/{COLLECTION}",
                    json={"vectors": {"size": 768, "distance": "Cosine"}},
                    timeout=10,
                )
                logger.info("Qdrant collection '%s' created", COLLECTION)
        except Exception as exc:
            logger.warning("Qdrant init warning: %s", exc)

    def upsert(self, point_id: str, vector: list[float], payload: dict):
        try:
            httpx.put(
                f"{self.url}/collections/{COLLECTION}/points",
                json={"points": [{"id": point_id, "vector": vector, "payload": payload}]},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Qdrant upsert error: %s", exc)

    def search(self, vector: list[float], limit: int = 3) -> list[dict]:
        try:
            r = httpx.post(
                f"{self.url}/collections/{COLLECTION}/points/search",
                json={"vector": vector, "limit": limit, "with_payload": True},
                timeout=10,
            )
            if r.status_code == 200:
                return [hit["payload"] for hit in r.json().get("result", [])]
        except Exception as exc:
            logger.warning("Qdrant search error: %s", exc)
        return []


# ── Ollama helpers ─────────────────────────────────────────────────────────────
def embed_text(text: str) -> Optional[list[float]]:
    """Get embedding from Ollama."""
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception as exc:
        logger.warning("Embedding error: %s", exc)
    return None


def generate_llm(prompt: str, system: str = "") -> str:
    """Generate text from Ollama LLM."""
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": LLM_MODEL,
                "prompt": prompt,
                "system": system,
                "stream": False,
                "options": {"temperature": 0.3, "num_predict": 1024},
            },
            timeout=300,
        )
        if r.status_code == 200:
            return r.json().get("response", "")
    except Exception as exc:
        logger.warning("LLM generate error: %s", exc)
    return ""


# ── Main RCA Explainer ─────────────────────────────────────────────────────────
class RCAExplainer:
    def __init__(self):
        self.qdrant = QdrantClient(QDRANT_URL)

    def _build_rag_context(self, query: str) -> str:
        """Retrieve similar past incidents from Qdrant."""
        vec = embed_text(query)
        if not vec:
            return ""
        past = self.qdrant.search(vec, limit=3)
        if not past:
            return ""
        context_parts = []
        for p in past:
            context_parts.append(
                f"Past Incident [{p.get('incident_id', '?')}]:\n"
                f"  Root Cause: {p.get('root_cause', '?')}\n"
                f"  Impact Chain: {' → '.join(p.get('impact_chain', []))}\n"
                f"  Resolution: {p.get('resolution', 'N/A')}\n"
                f"  Confidence: {p.get('confidence', '?')}"
            )
        return "\n\n".join(context_parts)

    def generate_rca(self, rca_data: dict) -> dict:
        """
        Generate a full RCA explanation using LLM + RAG.
        rca_data: output from the causal engine
        """
        root = rca_data.get("root_cause_service", "unknown")
        chain = rca_data.get("impact_chain", [])
        anomalies_summary = rca_data.get("anomalies_summary", [])
        confidence = rca_data.get("confidence", 0.0)

        # RAG: retrieve similar past incidents
        query = f"root cause {root} latency error cascade {' '.join(chain)}"
        rag_context = self._build_rag_context(query)

        # Build LLM prompt
        system_prompt = (
            "You are a senior SRE and root cause analysis expert. "
            "Analyze distributed system anomalies and provide precise, "
            "actionable RCA explanations. Be technical but clear. "
            "Format your response as a structured incident report."
        )

        anomaly_text = ""
        for a in anomalies_summary[:5]:
            anomaly_text += (
                f"\n  - Service: {a.get('service')}, "
                f"Latency: {a.get('avg_latency_ms', 0):.1f}ms, "
                f"Error Rate: {a.get('error_rate', 0)*100:.1f}%, "
                f"Anomaly Score: {a.get('anomaly_score', 0):.4f}"
            )

        prompt = f"""
INCIDENT ANALYSIS REQUEST
==========================
Timestamp: {datetime.utcnow().isoformat()}
Detected Root Cause Service: {root}
Confidence: {confidence*100:.1f}%
Impact Chain: {' → '.join(chain) if chain else 'N/A'}

ANOMALY SIGNALS DETECTED:
{anomaly_text or '  No anomalies provided'}

SIMILAR PAST INCIDENTS (RAG Context):
{rag_context or '  No historical incidents found'}

TASK:
1. Confirm or refine the root cause hypothesis
2. Explain the failure cascade through the impact chain
3. Identify contributing factors (latency, errors, dependencies)
4. Provide 3–5 specific remediation steps
5. Estimate time-to-resolve if remediation is applied
6. Rate confidence in this RCA (0–100%)

Provide a concise but complete incident RCA report.
"""

        logger.info("Generating LLM explanation for root_cause=%s", root)
        explanation = generate_llm(prompt, system=system_prompt)

        # Store this incident in Qdrant for future RAG
        incident_id = str(uuid.uuid4())[:8]
        vec = embed_text(f"{root} {' '.join(chain)} {explanation[:500]}")
        if vec:
            self.qdrant.upsert(
                point_id=str(uuid.uuid4()).replace("-", "")[:32],
                vector=vec,
                payload={
                    "incident_id": incident_id,
                    "root_cause": root,
                    "impact_chain": chain,
                    "confidence": confidence,
                    "resolution": explanation[:500],
                    "ts": datetime.utcnow().isoformat(),
                },
            )

        return {
            "incident_id": incident_id,
            "explanation": explanation,
            "rag_context_used": bool(rag_context),
            "past_incidents_retrieved": len(rag_context.split("Past Incident")) - 1 if rag_context else 0,
            "model": LLM_MODEL,
            "generated_at": datetime.utcnow().isoformat(),
        }
