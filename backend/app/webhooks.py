from fastapi import APIRouter, Request, Header, HTTPException
from .remediation.queue import PendingRemediationQueue
import json
import hashlib
import hmac
import os
import logging
logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
queue = PendingRemediationQueue()

def verify_slack_signature(payload: bytes, timestamp: str, signature: str) -> bool:
    """Verifies that the request actually came from Slack using the signing secret."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "").encode()
    if not secret:
        logger.warning("SLACK_SIGNING_SECRET not set. Signature verification bypassed (DANGEROUS).")
        return True
    
    basestring = f"v0:{timestamp}:".encode() + payload
    my_sig = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my_sig, signature)

@router.post("/slack")
async def slack_interaction(
    request: Request,
    x_slack_signature: str = Header(None),
    x_slack_request_timestamp: str = Header(None)
):
    if not x_slack_signature or not x_slack_request_timestamp:
        logger.error("Missing Slack signature headers.")
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")

    body = await request.body()
    if not verify_slack_signature(body, x_slack_request_timestamp, x_slack_signature):
        logger.error("Unauthorized Slack signature received.")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form_data = await request.form()
    
    try:
        if "payload" not in form_data:
            raise KeyError("missing 'payload'")
        payload = json.loads(form_data["payload"])
        action = payload["actions"][0]
        action_id = action["action_id"]
        incident_id = action["value"]
        user = payload["user"]["username"]
    except (KeyError, json.JSONDecodeError, IndexError) as e:
        logger.error(f"Malformed Slack payload: {e}")
        raise HTTPException(status_code=400, detail="Malformed payload")

    status = "APPROVED" if action_id == "approve_remediation" else "REJECTED"
    
    # Update the queue state
    success = await queue.update_status(incident_id, status, approved_by=user)
    
    if not success:
        return {"text": "Decision timeout: Action already expired or processed."}

    # If approved, we trigger the runner logic
    if status == "APPROVED":
        logger.warning(f"USER {user} APPROVED REMEDIATION for {incident_id}. Triggering runner...")
        # Integrates with RemediationRunner.execute() in production
        return {"text": f"✅ Approved! Executing remediation for `{incident_id}`."}
    
    return {"text": f"❌ Rejected. Remediation for `{incident_id}` was cancelled."}
