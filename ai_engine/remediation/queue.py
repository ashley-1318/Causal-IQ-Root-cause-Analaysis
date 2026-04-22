import json
import redis.asyncio as redis
from typing import Optional, Dict, Any
import logging
logger = logging.getLogger("remediation-queue")
from datetime import datetime
import os

class PendingRemediationQueue:
    def __init__(self, redis_url: str = None):
        if not redis_url:
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/1")
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.timeout = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "300"))

    async def store_pending_action(self, incident_id: str, action_data: Dict[str, Any]) -> bool:
        """Stores action with a TTL defined in env."""
        try:
            key = f"pending_remediation:{incident_id}"
            data = {
                **action_data,
                "status": "PENDING",
                "created_at": datetime.utcnow().isoformat()
            }
            await self.redis.setex(key, self.timeout, json.dumps(data))
            logger.info(f"Stored pending remediation for incident {incident_id} (TTL: {self.timeout}s)")
            return True
        except Exception as e:
            logger.error(f"Failed to store remediation for {incident_id}: {e}")
            return False

    async def get_action(self, incident_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self.redis.get(f"pending_remediation:{incident_id}")
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Error fetching pending action {incident_id}: {e}")
            return None

    async def update_status(self, incident_id: str, status: str, approved_by: str = "system") -> bool:
        """Updates status to APPROVED, REJECTED, or EXPIRED."""
        key = f"pending_remediation:{incident_id}"
        data = await self.get_action(incident_id)
        if not data:
            logger.warning(f"No pending action found for {incident_id} (expired or not found)")
            return False
        
        data["status"] = status
        data["approved_by"] = approved_by
        data["updated_at"] = datetime.utcnow().isoformat()
        
        # Preserve decision history for 24h
        try:
            await self.redis.setex(f"history_remediation:{incident_id}", 86400, json.dumps(data))
            await self.redis.delete(key)
            logger.info(f"Remediation for incident {incident_id} set to {status} by {approved_by}")
            return True
        except Exception as e:
            logger.error(f"Failed to update remediation status for {incident_id}: {e}")
            return False
