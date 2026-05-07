# CausalIQ ↔ Jira Integration Plan

## Executive Summary

Integrating Jira with CausalIQ creates a **closed-loop incident-to-ticket** workflow where:

- High-confidence RCA incidents automatically create Jira tickets with root cause details
- Engineering teams track resolution status in Jira
- CausalIQ monitors Jira ticket status and updates incident resolution metadata
- Historical incident resolutions inform future Bayesian model training

**Target**: Reduce mean time from detection to ticket creation from **~15 minutes** to **<30 seconds**

---

## Use Cases

### 1. **Automatic Ticket Creation on High-Confidence RCA**

When CausalIQ detects a root cause with **≥80% confidence**, auto-create a Jira ticket:

- **Issue Type**: Bug (for infrastructure issues) or Task (for application issues)
- **Title**: `[CausalIQ] Root Cause: {service} — {incident_id}`
- **Description**: Full RCA report (root cause, evidence, impact chain, Bayesian probability, LLM explanation)
- **Priority**: Map confidence score to Jira priority:
  - 90–100% → P0 (Critical)
  - 80–89% → P1 (High)
  - 70–79% → P2 (Medium)
- **Assignee**: Route to on-call SRE or team lead
- **Labels**: `causaliq`, `incident`, `auto-created`, `{root_service}`, `{cause_type}`
- **Linked Resources**: Link to Grafana dashboard, Jaeger trace, incident in CausalIQ UI

### 2. **Bidirectional Status Sync**

- When engineer **changes ticket status** in Jira → update incident status in CausalIQ (ACKNOWLEDGED, IN_PROGRESS, RESOLVED)
- When engineer **resolves ticket** → mark incident RESOLVED in ClickHouse; log remediation action taken
- When engineer **comments** on ticket with resolution steps → ingest into Qdrant for future RAG training

### 3. **Intelligent Ticket Routing & Escalation**

- Parse Jira team structure → route tickets to service owners
- If ticket remains unacknowledged for **>5 minutes** → escalate to manager or on-call
- Link CausalIQ incident to related Jira tickets (if similar root cause detected before)

### 4. **Remediation Loop Closure**

- Store remediation action from resolved Jira tickets back into ClickHouse incident history
- Tag with resolution outcome: RESOLVED, PARTIAL, FAILED, MANUAL_WORKAROUND
- Feedback: "This incident required manual intervention. Update Bayesian priors for this cause type?"

### 5. **SLA & MTTR Metrics**

- Track **Time to Ticket** (detection → Jira creation): target <1 min
- Track **Time to Acknowledgment** (ticket created → acknowledged): target <5 min
- Track **Time to Resolution** (ticket created → resolved): target <30 min
- Export metrics to Grafana dashboard for SLA monitoring

---

## Architecture

### Integration Points

```
┌─────────────────────────────────────────────────────────────────────┐
│                         CausalIQ Platform                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌──────────────────┐           ┌─────────────────────────────┐     │
│  │ RCA Orchestrator │  ──────→  │ Jira Bridge Service         │     │
│  │                  │           │  (NEW microservice)         │     │
│  │ • Detect: 80%+   │           │  • REST API → Jira Cloud   │     │
│  │ • Route incident │           │  • Webhook listener        │     │
│  └──────────────────┘           │  • Incident ↔ Ticket sync  │     │
│         ↑                       │  • Status polling/sync     │     │
│         │                       └─────────────────────────────┘     │
│  ┌──────────────────┐                       ↓                       │
│  │ ClickHouse       │           ┌─────────────────────────────┐     │
│  │ Incident Store   │ ←──────── │ Jira Cloud                  │     │
│  │                  │           │ • Tickets                   │     │
│  │ • incidents      │           │ • Status updates            │     │
│  │ • remediations   │           │ • Comments/resolution notes │     │
│  │ • resolution_url │           │ • Webhooks on issue events  │     │
│  └──────────────────┘           └─────────────────────────────┘     │
│         ↑                                                            │
│  ┌──────────────────┐                                                │
│  │ Qdrant RAG       │                                                │
│  │ (Learn from past)│                                                │
│  │ • resolution_url │                                                │
│  │ • resolution_notes                                               │
│  │ • cause_pattern                                                  │
│  └──────────────────┘                                                │
└─────────────────────────────────────────────────────────────────────┘
```

### Jira API Flow

1. **Create Ticket**

   ```
   POST /rest/api/3/issues
   {
     "fields": {
       "project": {"key": "OPS"},
       "summary": "[CausalIQ] Root Cause: payment-service — latency spike",
       "description": "RCA Report (markdown): ...",
       "issuetype": {"name": "Bug"},
       "priority": {"name": "Critical"},
       "labels": ["causaliq", "incident", "payment-service"],
       "assignee": {"id": "{on_call_id}"},
       "customfield_10000": "{incident_id}",  // Custom field: CausalIQ Incident ID
       "customfield_10001": "0.94"  // Custom field: Confidence Score
     }
   }
   ```

2. **Webhook Listener** (Jira → CausalIQ)

   ```
   POST /jira-webhook
   {
     "webhookEvent": "jira:issue_updated",
     "issue": {
       "key": "OPS-1234",
       "fields": {
         "status": {"name": "In Progress"},
         "resolution": null,
         "customfield_10000": "3a7f8d9c"  // CausalIQ Incident ID
       }
     }
   }
   ```

   → Update ClickHouse: `UPDATE incidents SET ticket_id='OPS-1234', status='IN_PROGRESS' WHERE incident_id='3a7f8d9c'`

3. **Ticket Resolved** → Sync Resolution
   ```
   Jira Webhook: "resolution": "Fixed"
   CausalIQ: Mark incident RESOLVED + capture ticket comment as resolution_notes
   ClickHouse: INSERT INTO incident_resolutions (incident_id, ticket_id, resolution_action, time_to_resolve_seconds, ...)
   Qdrant: Embed resolution_notes for future RAG
   ```

---

## Implementation Components

### A. New Jira Bridge Microservice

**Service**: `jira-bridge` (Python FastAPI)

**Responsibilities**:

- REST endpoint to create tickets (called by orchestrator when confidence ≥80%)
- Webhook receiver for Jira status updates
- Periodic polling for ticket resolution (fallback if webhooks fail)
- Rate limiting & retry logic for Jira API
- Logging & monitoring of sync failures

**Key Endpoints**:

```
POST   /create-incident-ticket
  Input: { incident_id, root_cause, confidence, explanation, impact_chain, anomalies }
  Output: { ticket_id, ticket_url }

POST   /jira-webhook
  Receives: Jira issue.updated event
  Action: Sync ticket status back to CausalIQ

GET    /incident/{incident_id}/ticket
  Returns: Linked Jira ticket details (if exists)

POST   /sync-resolution
  Input: { ticket_id, resolution_action, notes }
  Action: Update ClickHouse + Qdrant
```

### B. Custom Jira Fields

Create three custom fields in Jira Cloud (Administration > Custom Fields):

| Field Name             | Type             | Description                            |
| ---------------------- | ---------------- | -------------------------------------- |
| `CausalIQ_IncidentID`  | Short Text (255) | Links ticket back to CausalIQ incident |
| `CausalIQ_Confidence`  | Number           | Root cause confidence score (0–100)    |
| `CausalIQ_ImpactChain` | Text (long)      | Services affected in order             |

### C. Database Schema Extensions

**ClickHouse** - Add to `incidents` table:

```sql
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS (
  ticket_id String,                    -- Jira key (OPS-1234)
  ticket_url String,                   -- Link to Jira
  ticket_created_at DateTime,          -- When ticket was created
  ticket_acknowledged_at DateTime,     -- When engineer acknowledged
  ticket_resolution_url String,        -- Link to resolution ticket or PR
  ticket_resolution_notes String,      -- Free-form resolution description
  time_to_ticket_seconds UInt32,       -- Detection → ticket creation
  time_to_acknowledge_seconds UInt32,  -- Ticket → acknowledged
  time_to_resolve_seconds UInt32,      -- Detection → resolved
  resolution_outcome Enum8('RESOLVED'=1, 'PARTIAL'=2, 'FAILED'=3, 'MANUAL'=4, 'PENDING'=5)
)
```

### D. Qdrant Payload Extensions

When storing incidents in Qdrant, add:

```json
{
  "incident_id": "3a7f8d9c",
  "root_cause_service": "payment-service",
  "cause_type": "DB_CONNECTION_POOL_EXHAUSTION",
  "confidence_score": 0.94,
  "affected_services": ["order-service", "cart-service"],
  "resolution_action": "Increased max_connections from 50 to 150",
  "resolution_outcome": "RESOLVED",
  "resolution_ticket": "OPS-1234",
  "resolution_notes": "Engineer increased DB connection pool during incident. Monitoring now stable.",
  "time_to_resolve_minutes": 12
}
```

---

## Configuration & Deployment

### Environment Variables (Jira Bridge Service)

```bash
JIRA_CLOUD_URL=https://your-org.atlassian.net
JIRA_API_TOKEN=<personal_api_token>           # Or use OAuth2 flow
JIRA_PROJECT_KEY=OPS                          # Project to create tickets in
JIRA_ASSIGNEE_ON_CALL_ID=<user_id>           # Or query from StatusPage/PagerDuty
JIRA_AUTO_TICKET_THRESHOLD=0.80              # Confidence threshold for auto-creation
JIRA_WEBHOOK_SECRET=<random_secret>          # Validate incoming webhooks
CAUSALIQ_API_URL=http://backend:9000         # CausalIQ backend (internal)
```

### Docker Compose Addition

```yaml
jira-bridge:
  build: ./services/jira-bridge
  ports:
    - "8003:8000"
  environment:
    JIRA_CLOUD_URL: ${JIRA_CLOUD_URL}
    JIRA_API_TOKEN: ${JIRA_API_TOKEN}
    # ... other env vars
  depends_on:
    - backend
    - clickhouse
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
    interval: 30s
    timeout: 10s
    retries: 3
```

---

## Implementation Roadmap

### Phase 1: MVP (2–3 weeks)

- [ ] Create Jira Bridge service with basic ticket creation
- [ ] Hardcode on-call assignee (no dynamic routing yet)
- [ ] Manual webhook setup in Jira (no auto-validation)
- [ ] One-way sync: CausalIQ → Jira
- [ ] Test with 3–5 incidents in staging

### Phase 2: Bidirectional Sync (1–2 weeks)

- [ ] Implement Jira webhook receiver
- [ ] Add ticket_id, ticket_url to ClickHouse incidents table
- [ ] Polling fallback (every 5 min) if webhooks fail
- [ ] Test: ticket status changes reflect in CausalIQ UI

### Phase 3: Smart Routing & Escalation (1 week)

- [ ] Query Jira project structure → map services to teams
- [ ] Intelligent assignee routing (service owner lookup)
- [ ] Escalation rule: unacknowledged >5 min → notify manager

### Phase 4: Resolution Loop & RAG Integration (1–2 weeks)

- [ ] Capture resolution_notes from Jira comments
- [ ] Store resolution_outcome (RESOLVED, PARTIAL, FAILED) in ClickHouse
- [ ] Embed resolution patterns into Qdrant for future RAG
- [ ] Log success rate: "95% of incidents with resolution_notes had <15 min MTTR"

### Phase 5: SLA & Metrics Dashboard (1 week)

- [ ] Prometheus exporters for MTTR, ticket creation time
- [ ] Grafana dashboard for SLA tracking
- [ ] Alerting: if TTT (time to ticket) >5 min → alert Slack

---

## Benefits

| Benefit                     | Impact                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| **Faster ticket lifecycle** | Reduce manual ticket creation by 95%; auto-ticket in <30s                  |
| **Operator context**        | Full RCA + evidence pre-filled; no manual diagnosis needed                 |
| **Better prioritization**   | Route to right team automatically; confidence score guides priority        |
| **Closed loop**             | Resolution notes feed back into ML; Bayesian priors improve week-over-week |
| **SLA compliance**          | Track MTTR, time to acknowledgment; easier to meet SLAs                    |
| **Audit trail**             | All incidents + tickets linked; compliance/post-mortems easier             |
| **Less context switching**  | Engineers see RCA inside Jira; no need to alt-tab to CausalIQ UI           |

---

## Risk Mitigation

| Risk                       | Mitigation                                                                          |
| -------------------------- | ----------------------------------------------------------------------------------- |
| **False positive tickets** | Set confidence threshold to 80%+; auto-dismiss low-confidence tickets after 2 hours |
| **Webhook downtime**       | Polling fallback every 5 min ensures eventual consistency                           |
| **Jira API rate limits**   | Queue incidents; respect 429 responses; batch updates                               |
| **Sensitive data in Jira** | Redact PII from RCA reports; use custom fields instead of free text when possible   |
| **Ticket spam**            | Deduplication: check if same root cause in same service already has open ticket     |

---

## Approval Checklist (Before Implementation)

- [ ] Confirm Jira Cloud instance URL and API access available
- [ ] Agree on confidence threshold for auto-ticket creation (recommend 80%)
- [ ] Identify on-call rotation source (Jira, PagerDuty, Slack Workflow, hardcoded list?)
- [ ] Define Jira project key & issue type (e.g., OPS project, Bug type)
- [ ] Decide on custom fields: create vs. use existing fields?
- [ ] Agree on escalation policy: who gets notified if ticket ignored >5 min?
- [ ] Compliance review: any PII or security concerns with storing resolution notes?
- [ ] Load testing: estimate incident volume & Jira API calls per day

---

## Next Steps

**Once approved, the implementation will**:

1. Create `services/jira-bridge/` microservice
2. Deploy jira-bridge in docker-compose
3. Add Jira config to .env.example
4. Modify orchestrator.py to call jira-bridge on high-confidence RCA
5. Add webhook handler to backend or jira-bridge
6. Update ClickHouse schema for ticket tracking
7. Create Grafana dashboard for incident-to-ticket metrics
8. Write integration tests (mock Jira API)
9. Deploy to staging; run 48-hour canary before production rollout
