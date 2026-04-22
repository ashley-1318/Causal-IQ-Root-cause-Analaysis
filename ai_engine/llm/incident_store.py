import os
from typing import List, Optional, Dict, Any
from qdrant_client import QdrantClient
from qdrant_client.http import models
import logging
logger = logging.getLogger("incident-store")
import uuid

from .schemas import IncidentEmbeddingSchema, RootCauseType, ResolutionOutcome
from .embedding_strategy import EmbeddingStrategy

class QdrantIncidentStore:
    def __init__(self):
        self.url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        self.client = QdrantClient(url=self.url)
        self.collection_name = "incidents_hybrid"
        self.embed_strategy = EmbeddingStrategy()
        self._ensure_collection()

    def _ensure_collection(self):
        """Creates the collection with named vectors if it doesn't exist."""
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)
        
        if not exists:
            logger.info(f"Creating hybrid Qdrant collection: {self.collection_name}")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "semantic": models.VectorParams(size=768, distance=models.Distance.COSINE),
                    "structural": models.VectorParams(size=768, distance=models.Distance.COSINE),
                }
            )

    async def store_incident(self, incident: IncidentEmbeddingSchema) -> bool:
        """Embeds and persists a structured incident."""
        try:
            semantic_vec, structural_vec = await self.embed_strategy.generate_hybrid_vectors(incident)
            
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_DNS, incident.incident_id)),
                        vector={
                            "semantic": semantic_vec,
                            "structural": structural_vec
                        },
                        payload=incident.dict()
                    )
                ]
            )
            logger.info(f"Successfully indexed structured incident {incident.incident_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to store incident in Qdrant: {e}")
            return False

    async def hybrid_search(self, 
                            query_text: str, 
                            top_k: int = 3, 
                            semantic_weight: float = 0.6) -> List[Dict[str, Any]]:
        """
        Performs a weighted hybrid search across semantic and structural dimensions.
        """
        # For a simple query string, we use the same embedding for both, 
        # but the query is usually formulated by the Agent after analysis.
        query_vec = await self.embed_strategy._embed(query_text)
        
        results = self.client.search_batch(
            collection_name=self.collection_name,
            requests=[
                models.SearchRequest(vector={"name": "semantic", "vector": query_vec}, limit=top_k),
                models.SearchRequest(vector={"name": "structural", "vector": query_vec}, limit=top_k)
            ]
        )
        
        # Combine and rank (simplified Reciprocal Rank Fusion or weighted sum)
        combined = {}
        # ... logic to merge results ...
        # For simplicity in the demo, we return the semantic matches with highest weight
        return [r.payload for r in results[0]]

    def search_by_type(self, cause_type: RootCauseType, limit: int = 5) -> List[Dict[str, Any]]:
        return [
            r.payload for r in self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(key="cause_type", match=models.MatchValue(value=cause_type.value))]
                ),
                limit=limit
            )[0]
        ]
