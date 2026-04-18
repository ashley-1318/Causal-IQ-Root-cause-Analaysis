import os
import logging
from qdrant_client import QdrantClient, models

logger = logging.getLogger("causaliq-rag")

class IncidentRAG:
    """
    Metadata-rich RAG pipeline for storing and retrieving historical 
    incidents based on structural and vector similarity.
    """
    def __init__(self):
        self.client = QdrantClient(url=os.getenv("QDRANT_URL", "http://qdrant:6333"))
        self._ensure_collection()

    def _ensure_collection(self):
        try:
            self.client.recreate_collection(
                collection_name="incidents",
                vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE),
            )
        except Exception as e:
            logger.error(f"Failed to ensure Qdrant collection: {e}")

    def store_incident(self, incident_id, vector, metadata):
        """Stores report with high-fidelity metadata for hard-filtering."""
        try:
            self.client.upsert(
                collection_name="incidents",
                points=[
                    models.PointStruct(
                        id=incident_id,
                        vector=vector,
                        payload={
                            "service": metadata["service"],
                            "fault_category": metadata.get("fault_category", "unknown"),
                            "explanation": metadata["explanation"],
                            "confidence": metadata.get("confidence", 0.0),
                            "timestamp": metadata.get("timestamp")
                        }
                    )
                ]
            )
            logger.info(f"Stored incident {incident_id} in RAG memory.")
        except Exception as e:
            logger.error(f"RAG Storage error: {e}")

    def retrieve_similar(self, query_vector, filter_service=None):
        """Retrieves past incidents, optionally filtering by service name."""
        try:
            filter_conditions = []
            if filter_service:
                filter_conditions.append(models.FieldCondition(key="service", match=models.MatchValue(value=filter_service)))

            results = self.client.search(
                collection_name="incidents",
                query_vector=query_vector,
                query_filter=models.Filter(must=filter_conditions) if filter_conditions else None,
                limit=2
            )
            return [r.payload for r in results]
        except Exception as e:
            logger.error(f"RAG Retrieval error: {e}")
            return []
