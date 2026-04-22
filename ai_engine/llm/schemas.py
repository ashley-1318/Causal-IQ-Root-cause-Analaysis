from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime

class RootCauseType(str, Enum):
    DB_CONNECTION = "DB_CONNECTION"
    MEMORY_LEAK = "MEMORY_LEAK"
    CPU_SPIKE = "CPU_SPIKE"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    DISK_IO = "DISK_IO"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CONFIG_ERROR = "CONFIG_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    UNKNOWN = "UNKNOWN"

class ResolutionOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    PENDING = "PENDING"

class CausalEdge(BaseModel):
    service: str
    metric: str
    value: float
    timestamp: datetime

class IncidentEmbeddingSchema(BaseModel):
    incident_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    root_cause_service: str
    cause_type: RootCauseType = RootCauseType.UNKNOWN
    confidence_score: float = Field(ge=0.0, le=1.0)
    affected_services: List[str] = []
    causal_chain: List[CausalEdge] = []
    anomaly_scores: Dict[str, float] = {} # Service -> Score
    evidence_logs: List[str] = Field(default_factory=list, description="Top 5 log lines confirming cause")
    resolution_action: Optional[str] = None
    resolution_outcome: ResolutionOutcome = ResolutionOutcome.PENDING
    time_to_resolve_seconds: Optional[int] = None
    environment_tags: Dict[str, str] = Field(default_factory=lambda: {"region": "prod-1", "cluster": "main"})

    def to_semantic_string(self) -> str:
        """Converts to natural language for fuzzy RAG search."""
        log_snippet = " ".join(self.evidence_logs[:2])
        return (f"Incident in {self.root_cause_service} classified as {self.cause_type}. "
                f"Confidence {self.confidence_score*100:.0f}%. "
                f"Affected: {', '.join(self.affected_services)}. "
                f"Logs: {log_snippet}")

    def to_structural_string(self) -> str:
        """Converts to a rigid format for pattern matching."""
        chain = " -> ".join([f"{e.service}:{e.metric}" for e in self.causal_chain])
        return f"PATTERN: {chain} | TYPE: {self.cause_type}"
