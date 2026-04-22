import os
import httpx
from typing import Dict, Any, List
import logging
logger = logging.getLogger("slack-gate")

class SlackApprovalGate:
    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN")
        self.channel_id = os.getenv("SLACK_CHANNEL_ID")
        self.webhook_url = "https://slack.com/api/chat.postMessage"

    async def send_approval_request(self, incident_id: str, confidence: float, action: str, evidence: List[str]) -> bool:
        """Sends a rich Block Kit message to Slack for human verification."""
        if not self.token or not self.channel_id:
            logger.warning("SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set. Remediation approval message skipped.")
            return False

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 CausalIQ: Remediation Approval Required", "emoji": True}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Incident ID:*\n{incident_id}"},
                    {"type": "mrkdwn", "text": f"*AI Confidence:*\n{confidence * 100:.1f}%"}
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Proposed Corrective Action:*\n`{action}`"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Key Evidence:*\n> " + "\n> ".join(evidence[:3])}
            },
            {
                "type": "divider"
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve & Execute", "emoji": True},
                        "style": "primary",
                        "value": incident_id,
                        "action_id": "approve_remediation"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject Action", "emoji": True},
                        "style": "danger",
                        "value": incident_id,
                        "action_id": "reject_remediation"
                    }
                ]
            }
        ]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.webhook_url,
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    json={"channel": self.channel_id, "blocks": blocks}
                )
                if resp.status_code != 200 or not resp.json().get("ok"):
                    logger.error(f"Slack API error: {resp.text}")
                    return False
                
                logger.info(f"Remediation approval request sent to Slack for incident {incident_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to communicate with Slack: {e}")
            return False
