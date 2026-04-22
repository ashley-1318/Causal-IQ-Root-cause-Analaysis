import os
import httpx
import json
from typing import Tuple, List
import logging
logger = logging.getLogger("embedding-strategy")
from .schemas import IncidentEmbeddingSchema

class EmbeddingStrategy:
    def __init__(self, ollama_url: str = None):
        if not ollama_url:
            ollama_url = os.getenv("OLLAMA_URL", "http://ollama:11434")
        self.url = f"{ollama_url}/api/embeddings"
        self.model = os.getenv("EMBED_MODEL", "nomic-embed-text")

    async def _embed(self, text: str) -> List[float]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.url,
                    json={"model": self.model, "prompt": text},
                    timeout=10
                )
                return resp.json()["embedding"]
        except Exception as e:
            logger.error(f"Embedding failed for '{text[:50]}...': {e}")
            # Return zero vector on failure to prevent crash
            return [0.0] * 768

    async def generate_hybrid_vectors(self, incident: IncidentEmbeddingSchema) -> Tuple[List[float], List[float]]:
        """
        Generates two distinct vectors:
        1. Semantic: Natural language summary for conceptual matches.
        2. Structural: Rigid causal chain pattern for architectural matches.
        """
        semantic_text = incident.to_semantic_string()
        structural_text = incident.to_structural_string()

        logger.info(f"Generating hybrid vectors for incident {incident.incident_id}")
        
        semantic_vec = await self._embed(semantic_text)
        structural_vec = await self._embed(structural_text)
        
        return semantic_vec, structural_vec
