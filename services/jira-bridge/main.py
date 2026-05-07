"""
CausalIQ Jira Bridge Service
Creates Jira tickets for high-confidence incidents and syncs Jira status updates back to CausalIQ.
"""
import logging
import os
from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logger = logging.getLogger("jira-bridge")

JIRA_CLOUD_URL = os.getenv("JIRA_CLOUD_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "OPS")
JIRA_ISSUE_TYPE = os.getenv("JIRA_ISSUE_TYPE", "Bug")
CAUSALIQ_API_URL = os.getenv("CAUSALIQ_API_URL", "http://backend:9000")
JIRA_WEBHOOK_SECRET = os.getenv("JIRA_WEBHOOK_SECRET", "")
JIRA_AUTO_TICKET_THRESHOLD = float(os.getenv("JIRA_AUTO_TICKET_THRESHOLD", "0.8"))

app = FastAPI(title="CausalIQ Jira Bridge", version="1.0.0")

TICKET_STORE: dict[str, dict[str, Any]] = {}


class JiraTicketRequest(BaseModel):
    incident_id: str
    root_cause: str
    confidence: float
    explanation: str
    impact_chain: list[str] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class JiraWebhookRequest(BaseModel):
    incident_id: Optional[str] = None
    ticket_id: Optional[str] = None
    ticket_url: Optional[str] = None
    status: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolution_action: Optional[str] = None
    updated_at: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class JiraTicketResponse(BaseModel):
    incident_id: str
    ticket_id: str
    ticket_url: str
    status: str
    source: str
    created_at: str


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "jira-bridge",
        "jira_configured": bool(JIRA_CLOUD_URL and JIRA_EMAIL and JIRA_API_TOKEN),
        "threshold": JIRA_AUTO_TICKET_THRESHOLD,
        "ts": datetime.utcnow().isoformat(),
    }


async def _sync_backend(ticket_payload: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{CAUSALIQ_API_URL}/jira/webhook", json=ticket_payload)
    except Exception as exc:
        logger.warning("Backend sync failed for incident=%s: %s", ticket_payload.get("incident_id"), exc)


async def _create_jira_issue(ticket_request: JiraTicketRequest) -> tuple[str, str, str]:
    summary = f"[CausalIQ] Root Cause: {ticket_request.root_cause} — {ticket_request.incident_id}"
    description_lines = [
        f"Incident ID: {ticket_request.incident_id}",
        f"Root Cause: {ticket_request.root_cause}",
        f"Confidence: {ticket_request.confidence:.2%}",
        f"Impact Chain: {' -> '.join(ticket_request.impact_chain) if ticket_request.impact_chain else 'N/A'}",
        "",
        "Evidence:",
        f"{ticket_request.evidence or {}}",
        "",
        "Explanation:",
        ticket_request.explanation,
    ]

    fields = {
        "project": {"key": JIRA_PROJECT_KEY},
        "summary": summary,
        "description": "\n".join(description_lines),
        "issuetype": {"name": JIRA_ISSUE_TYPE},
        "labels": ["causaliq", "incident", ticket_request.root_cause.replace("_", "-")],
    }

    if JIRA_CLOUD_URL and JIRA_EMAIL and JIRA_API_TOKEN:
        url = f"{JIRA_CLOUD_URL.rstrip('/')}/rest/api/2/issue"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json={"fields": fields},
                auth=(JIRA_EMAIL, JIRA_API_TOKEN),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Jira API error {response.status_code}: {response.text}")
        payload = response.json()
        ticket_id = payload.get("key", f"{JIRA_PROJECT_KEY}-{ticket_request.incident_id.upper()}")
        ticket_url = f"{JIRA_CLOUD_URL.rstrip('/')}/browse/{ticket_id}"
        return ticket_id, ticket_url, "jira"

    ticket_id = f"{JIRA_PROJECT_KEY}-{ticket_request.incident_id.upper()}"
    ticket_url = f"https://jira.local/browse/{ticket_id}"
    return ticket_id, ticket_url, "local"


@app.post("/tickets/create", response_model=JiraTicketResponse)
async def create_ticket(ticket_request: JiraTicketRequest):
    if ticket_request.confidence < JIRA_AUTO_TICKET_THRESHOLD:
        raise HTTPException(status_code=400, detail="Confidence below ticket creation threshold")

    ticket_id, ticket_url, source = await _create_jira_issue(ticket_request)
    record = {
        "incident_id": ticket_request.incident_id,
        "ticket_id": ticket_id,
        "ticket_url": ticket_url,
        "status": "OPEN",
        "source": source,
        "created_at": datetime.utcnow().isoformat(),
        "root_cause": ticket_request.root_cause,
        "confidence": ticket_request.confidence,
        "impact_chain": ticket_request.impact_chain,
        "anomalies": ticket_request.anomalies,
        "explanation": ticket_request.explanation,
        "evidence": ticket_request.evidence,
    }
    TICKET_STORE[ticket_request.incident_id] = record
    await _sync_backend(record)
    logger.info("Created Jira ticket %s for incident %s", ticket_id, ticket_request.incident_id)
    return JiraTicketResponse(**record)


@app.get("/tickets/{incident_id}")
async def get_ticket(incident_id: str):
    ticket = TICKET_STORE.get(incident_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@app.post("/jira-webhook")
async def jira_webhook(payload: JiraWebhookRequest):
    incident_id = payload.incident_id
    if not incident_id and payload.payload:
        issue = payload.payload.get("issue", {})
        fields = issue.get("fields", {})
        incident_id = fields.get("customfield_10000") or fields.get("cf[10000]")
        payload.ticket_id = payload.ticket_id or issue.get("key")
        payload.ticket_url = payload.ticket_url or (
            f"{JIRA_CLOUD_URL.rstrip('/')}/browse/{payload.ticket_id}" if payload.ticket_id and JIRA_CLOUD_URL else None
        )
        if not payload.status:
            status_obj = fields.get("status", {})
            payload.status = status_obj.get("name")

    if not incident_id:
        raise HTTPException(status_code=400, detail="incident_id is required")

    current = TICKET_STORE.get(incident_id, {"incident_id": incident_id})
    updated = {
        **current,
        "incident_id": incident_id,
        "ticket_id": payload.ticket_id or current.get("ticket_id"),
        "ticket_url": payload.ticket_url or current.get("ticket_url"),
        "status": payload.status or current.get("status", "OPEN"),
        "resolution_notes": payload.resolution_notes or current.get("resolution_notes"),
        "resolution_action": payload.resolution_action or current.get("resolution_action"),
        "updated_at": payload.updated_at or datetime.utcnow().isoformat(),
    }
    TICKET_STORE[incident_id] = updated
    await _sync_backend(updated)
    return {"status": "synced", **updated}
