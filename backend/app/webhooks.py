"""
CausalIQ Slack Interactivity Webhook Handler
Receives button callbacks from Slack when operators click Approve/Reject
on remediation approval messages.

Slack sends interactive payloads as `application/x-www-form-urlencoded`
with a `payload` field containing JSON.
"""
from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from .remediation.queue import PendingRemediationQueue
import json
import hashlib
import hmac
import os
import time
import logging
import httpx

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
queue = PendingRemediationQueue()

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")


def verify_slack_signature(payload: bytes, timestamp: str, signature: str) -> bool:
    """Verifies that the request actually came from Slack using the signing secret."""
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set. Signature verification bypassed.")
        return True

    # Reject requests older than 5 minutes (replay protection)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            logger.error("Slack request timestamp too old")
            return False
    except (ValueError, TypeError):
        return False

    secret = SLACK_SIGNING_SECRET.encode()
    basestring = f"v0:{timestamp}:".encode() + payload
    my_sig = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(my_sig, signature)


async def _update_slack_message(response_url: str, incident_id: str, action: str, user: str):
    """Updates the original Slack message to show the decision result."""
    if action == "APPROVED":
        emoji = "✅"
        color = "#2eb886"
        text = f"{emoji} *APPROVED* by @{user}\nExecuting remediation for `{incident_id}`..."
    else:
        emoji = "❌"
        color = "#dc3545"
        text = f"{emoji} *REJECTED* by @{user}\nRemediation for `{incident_id}` was cancelled."

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(response_url, json={
                "replace_original": True,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": text
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"Decision made at <!date^{int(time.time())}^{{date_short_pretty}} at {{time}}|{time.strftime('%H:%M:%S')}>"
                            }
                        ]
                    }
                ]
            })
    except Exception as e:
        logger.error(f"Failed to update Slack message: {e}")


@router.post("/slack")
async def slack_interaction(request: Request):
    """
    Handles Slack interactive button callbacks.
    Slack sends the payload as form-encoded with a 'payload' JSON field.
    We must respond within 3 seconds or Slack shows a timeout error.
    """
    body = await request.body()

    # Extract signature headers (Slack sends them with X- prefix)
    sig = request.headers.get("x-slack-signature", "")
    ts = request.headers.get("x-slack-request-timestamp", "")

    if sig and ts:
        if not verify_slack_signature(body, ts, sig):
            logger.error("Invalid Slack signature received.")
            raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Parse the form-encoded payload
    form_data = await request.form()

    try:
        if "payload" not in form_data:
            raise KeyError("missing 'payload' field")
        payload = json.loads(form_data["payload"])
        action = payload["actions"][0]
        action_id = action["action_id"]
        incident_id = action["value"]
        user = payload.get("user", {}).get("username", "unknown")
        response_url = payload.get("response_url", "")
    except (KeyError, json.JSONDecodeError, IndexError) as e:
        logger.error(f"Malformed Slack payload: {e}")
        raise HTTPException(status_code=400, detail="Malformed payload")

    status = "APPROVED" if action_id == "approve_remediation" else "REJECTED"

    # Update the remediation queue state in Redis
    success = await queue.update_status(incident_id, status, approved_by=user)

    if not success:
        # The action may have expired (TTL) or already been processed
        if response_url:
            await _update_slack_message(
                response_url, incident_id,
                "EXPIRED", user
            )
        return JSONResponse(content={
            "response_type": "ephemeral",
            "text": "⏰ This action has already expired or been processed."
        })

    logger.info(f"Incident {incident_id} {status} by {user}")

    # Update the Slack message to reflect the decision (async, don't block response)
    if response_url:
        await _update_slack_message(response_url, incident_id, status, user)

    # Return immediate acknowledgment to Slack (must be < 3 seconds)
    if status == "APPROVED":
        logger.warning(f"USER {user} APPROVED REMEDIATION for {incident_id}. Triggering runner...")
        return JSONResponse(content={
            "response_type": "in_channel",
            "text": f"✅ Approved! Executing remediation for `{incident_id}`."
        })

    return JSONResponse(content={
        "response_type": "in_channel",
        "text": f"❌ Rejected. Remediation for `{incident_id}` was cancelled."
    })
