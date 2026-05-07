# 🚀 CausalIQ Production Deployment Guide

**Version**: 2.0.0-production  
**Last Updated**: May 2, 2026  
**Status**: Production Ready

---

## Table of Contents

1. [Pre-Deployment Checklist](#pre-deployment-checklist)
2. [Security Hardening](#security-hardening)
3. [Kubernetes Deployment](#kubernetes-deployment)
4. [Production Configuration](#production-configuration)
5. [Monitoring & Alerting](#monitoring--alerting)
6. [Backup & Disaster Recovery](#backup--disaster-recovery)
7. [Jira Integration Setup](#jira-integration-setup)
8. [Runbooks & Troubleshooting](#runbooks--troubleshooting)

---

## Pre-Deployment Checklist

### Infrastructure Requirements

- [ ] Kubernetes 1.25+ cluster (3+ nodes, 8+ GB RAM each)
- [ ] Persistent storage: 100+ GB (ClickHouse), 50+ GB (Neo4j), 20+ GB (Qdrant)
- [ ] Docker registry (DockerHub, ECR, GCR, or self-hosted)
- [ ] Load balancer or ingress controller
- [ ] Secrets management (AWS Secrets, Vault, or K8s Secrets)
- [ ] Log aggregation system (ELK, Splunk, Datadog)
- [ ] Monitoring stack (Prometheus, Grafana, or Cloud Provider)

### Access & Credentials

- [ ] Jira Cloud account + API token (not email-based)
- [ ] Slack workspace + bot token (for notifications)
- [ ] AWS/GCP/Azure account (for backups and secrets)
- [ ] Git repository with write access
- [ ] Docker registry credentials

### Pre-Production Testing

- [ ] Load test (1000+ incidents/hour)
- [ ] Jira webhook integration test
- [ ] Database backup/restore test
- [ ] Failover scenario test
- [ ] Security vulnerability scan
- [ ] MTTR SLA verification

---

## Security Hardening

### 1. Rotate All Credentials

**Before deployment:**

```bash
# Generate new passwords (min 32 chars, mixed case, symbols)
openssl rand -base64 32

# Update .env.production with new values
export NEO4J_PASSWORD=$(openssl rand -base64 32)
export CLICKHOUSE_PASSWORD=$(openssl rand -base64 32)
export REDIS_PASSWORD=$(openssl rand -base64 32)
export JIRA_API_TOKEN="<new-token-from-jira-admin>"
export SLACK_BOT_TOKEN="<new-token-from-slack-admin>"
```

### 2. Secrets Management

**Recommended: AWS Secrets Manager**

```bash
# Store secrets
aws secretsmanager create-secret \
  --name causaliq/production \
  --secret-string file://secrets.json \
  --region us-east-1

# Reference in K8s
kubectl create secret aws-secret \
  --from-literal=access-key=$AWS_ACCESS_KEY \
  --from-literal=secret-key=$AWS_SECRET_KEY \
  -n causaliq
```

**Alternative: HashiCorp Vault**

```bash
vault kv put secret/causaliq/production \
  neo4j_password="$NEO4J_PASSWORD" \
  clickhouse_password="$CLICKHOUSE_PASSWORD" \
  jira_api_token="$JIRA_API_TOKEN"
```

### 3. Network Security

**Enable mTLS between services:**

```yaml
# k8s/network-policy.yml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: causaliq-network-policy
  namespace: causaliq
spec:
  podSelector: {}
  policyTypes:
    - Ingress
    - Egress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              name: causaliq
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              name: causaliq
    - to:
        - podSelector: {}
      ports:
        - protocol: TCP
          port: 53 # DNS
```

### 4. RBAC Configuration

```yaml
# k8s/rbac.yml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: causaliq
  name: causaliq-service
rules:
  - apiGroups: [""]
    resources: ["pods", "services", "configmaps"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["get", "list"]
```

### 5. Container Security

```dockerfile
# Dockerfile best practices
FROM python:3.11-slim as base

# Run as non-root
RUN useradd -m -u 1000 causaliq
USER causaliq

# Minimal attack surface
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=causaliq:causaliq main.py ./

# Security scanning in CI/CD
ENTRYPOINT ["python", "main.py"]
```

---

## Kubernetes Deployment

### Step 1: Create Namespace & Secrets

```bash
# Create namespace
kubectl create namespace causaliq

# Create secrets from .env.production
kubectl create secret generic causaliq-secrets \
  --from-env-file=.env.production \
  -n causaliq

# Verify secrets
kubectl get secrets -n causaliq
```

### Step 2: Deploy Infrastructure

```bash
# Apply manifests in order
kubectl apply -f k8s/k8s-manifest.yml

# Wait for stateful sets
kubectl rollout status statefulset/neo4j -n causaliq --timeout=5m
kubectl rollout status statefulset/clickhouse -n causaliq --timeout=5m

# Verify all pods are running
kubectl get pods -n causaliq
```

### Step 3: Deploy Applications

```bash
# Deploy backend, frontend, jira-bridge
kubectl apply -f k8s/k8s-manifest.yml

# Watch rollout
kubectl rollout status deployment/backend -n causaliq --timeout=10m

# Check logs
kubectl logs -f deployment/backend -n causaliq
```

### Step 4: Configure Ingress (HTTPS)

```yaml
# k8s/ingress.yml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: causaliq-ingress
  namespace: causaliq
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - causaliq.your-domain.com
      secretName: causaliq-tls
  rules:
    - host: causaliq.your-domain.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: frontend
                port:
                  number: 80
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: backend
                port:
                  number: 9000
```

---

## Production Configuration

### Environment Variables

```bash
# Copy .env.production and update values
cp .env.production .env.prod
source .env.prod

# Critical values to change:
JIRA_CLOUD_URL=https://your-org.atlassian.net
JIRA_EMAIL=causaliq-bot@your-org.com
JIRA_API_TOKEN=ATATT_xxxxx  # Generate new token
JIRA_PROJECT_KEY=YOUR_KEY

NEO4J_AUTH=neo4j/$(openssl rand -base64 32)
CLICKHOUSE_PASSWORD=$(openssl rand -base64 32)
REDIS_PASSWORD=$(openssl rand -base64 32)

SLACK_BOT_TOKEN=xoxb-xxxxx
SLACK_SIGNING_SECRET=xxxxx
SLACK_CHANNEL_ID=C_xxxxx

# LLM & AI
LLM_MODEL=llama3
EMBED_MODEL=nomic-embed-text
ANOMALY_SCORE_THRESHOLD=-0.30
JIRA_AUTO_TICKET_THRESHOLD=0.85

# Observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
LOG_LEVEL=INFO
DEBUG=false

# Deployment
ENVIRONMENT=production
VERSION=2.0.0-production
```

### Resource Limits (Per Pod)

| Service     | CPU Request | CPU Limit | Memory Request | Memory Limit |
| ----------- | ----------- | --------- | -------------- | ------------ |
| Backend     | 500m        | 1000m     | 1Gi            | 2Gi          |
| Frontend    | 100m        | 500m      | 256Mi          | 512Mi        |
| Jira Bridge | 200m        | 500m      | 512Mi          | 1Gi          |
| RCA Engine  | 1000m       | 2000m     | 2Gi            | 4Gi          |
| Neo4j       | 1000m       | 2000m     | 3Gi            | 4Gi          |
| ClickHouse  | 2000m       | 4000m     | 4Gi            | 8Gi          |
| Redpanda    | 1000m       | 2000m     | 2Gi            | 4Gi          |

### Autoscaling

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: backend-hpa
  namespace: causaliq
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: backend
  minReplicas: 3
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: 80
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 50
          periodSeconds: 60
    scaleUp:
      stabilizationWindowSeconds: 0
      policies:
        - type: Percent
          value: 100
          periodSeconds: 15
```

---

## Monitoring & Alerting

### 1. Prometheus Setup

```bash
# Update prometheus.yml with alert rules
kubectl apply -f otel/prometheus-alerts.yml

# Access Prometheus
kubectl port-forward svc/prometheus 9090:9090 -n causaliq
# Open: http://localhost:9090
```

### 2. Grafana Dashboards

**Create dashboards for:**

- RCA Pipeline Health (detection → ticket creation)
- Jira Integration Metrics
- Database Performance (ClickHouse, Neo4j)
- Service Latencies & Error Rates
- MTTR & SLA Compliance
- Incident Trends & Root Cause Distribution

**Default credentials:**

```
Username: admin
Password: causaliq123 (CHANGE THIS)
```

### 3. Alert Channels

**PagerDuty:**

```yaml
alertmanager:
  route:
    receiver: "pagerduty"
  receivers:
    - name: "pagerduty"
      pagerduty_configs:
        - service_key: ${{ secrets.PAGERDUTY_KEY }}
          severity: "{{ .Alerts.Firing | len | if gt 0 }}critical{{ else }}info{{ end }}"
```

**Slack:**

```yaml
receivers:
  - name: "slack"
    slack_configs:
      - api_url: ${{ secrets.SLACK_WEBHOOK_URL }}
        channel: "#causaliq-alerts"
        title: "[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }}"
```

---

## Backup & Disaster Recovery

### Daily Backup Schedule

```bash
# Add to crontab
0 2 * * * /scripts/backup-production.sh >> /var/log/causaliq-backup.log 2>&1
0 3 * * * aws s3 sync /backups/causaliq s3://company-backups/causaliq/
```

### Backup Targets

| Database   | Target | Retention | Schedule   |
| ---------- | ------ | --------- | ---------- |
| ClickHouse | AWS S3 | 90 days   | Daily 2 AM |
| Neo4j      | AWS S3 | 30 days   | Daily 2 AM |
| Redis      | AWS S3 | 7 days    | Daily 2 AM |
| Qdrant     | AWS S3 | 14 days   | Daily 2 AM |

### Restore Procedure

```bash
# 1. Identify backup timestamp
aws s3 ls s3://company-backups/causaliq/

# 2. Run restore script
./scripts/restore-production.sh 20260502_143022

# 3. Verify services
kubectl get pods -n causaliq
curl http://localhost:9001/health

# 4. Check incident count
curl http://localhost:9001/incidents?limit=1
```

### Disaster Recovery SLA

| Scenario            | RTO    | RPO                        |
| ------------------- | ------ | -------------------------- |
| Single pod failure  | 5 min  | 0 (replicas)               |
| Node failure        | 10 min | 0 (multi-node)             |
| Database corruption | 30 min | 24 hrs (daily backup)      |
| Region failure      | 2 hrs  | 1 hr (cross-region backup) |

---

## Jira Integration Setup

### Step 1: Create Custom Fields

1. Go to **Jira Administration** → **Custom Fields**
2. Create 3 fields:

   **Field 1: CausalIQ_IncidentID**
   - Type: Text (Short)
   - Configure for: Your project

   **Field 2: CausalIQ_Confidence**
   - Type: Number
   - Configure for: Your project

   **Field 3: CausalIQ_ImpactChain**
   - Type: Text (Long)
   - Configure for: Your project

### Step 2: Register Webhook

1. Go to **Jira Administration** → **Webhooks**
2. Create New Webhook:
   - Name: `CausalIQ Webhook`
   - URL: `https://your-domain/jira/webhook`
   - Events: `issue.updated`
   - Active: Yes

3. Test webhook:
   ```bash
   curl -X POST https://your-domain/jira/webhook \
     -H "Content-Type: application/json" \
     -d '{"webhookEvent":"issue.updated","issue":{"key":"TEST-1"}}'
   ```

### Step 3: Configure Ticket Routing

```yaml
# In Jira: Project Settings → Automation
Rule: "Route CausalIQ Tickets"
Trigger: Issue Created
Condition: Labels contains "causaliq"
Action: Assign to Team Lead
Action: Add label "automated"
```

---

## Runbooks & Troubleshooting

### Incident: High Incident Creation Rate

**Symptoms:**

- > 5 incidents/sec detected
- Alert: `HighIncidentRate`

**Diagnosis:**

```bash
# Check backend logs
kubectl logs -f deployment/backend -n causaliq | grep ERROR

# Check stream processor
kubectl logs -f deployment/stream-processor -n causaliq

# Query incident rate
curl http://localhost:9001/incidents?limit=100 | jq length
```

**Resolution:**

1. **If false positives:** Increase `ANOMALY_SCORE_THRESHOLD` (default: -0.30)
2. **If real incidents:** Engage SRE team for investigation
3. **If Kafka lag:** Scale stream-processor replicas
4. **If Jira bridge failing:** Check bridge logs and API quota

### Incident: Jira API Errors

**Symptoms:**

- Alert: `JiraAPIErrorRate` > 5%
- Tickets not being created

**Diagnosis:**

```bash
# Check jira-bridge logs
kubectl logs -f deployment/jira-bridge -n causaliq | grep ERROR

# Check Jira API quota
curl -u $JIRA_EMAIL:$JIRA_API_TOKEN \
  https://your-org.atlassian.net/rest/api/2/ratelimit/status

# Test Jira connectivity
curl -u $JIRA_EMAIL:$JIRA_API_TOKEN \
  https://your-org.atlassian.net/rest/api/2/project
```

**Resolution:**

1. **If 401 Unauthorized:** Rotate Jira API token
2. **If 429 Rate Limited:** Implement backoff in bridge
3. **If 400 Bad Request:** Check custom field IDs
4. **If timeout:** Increase Jira bridge timeout

### Incident: Database Disk Full

**Symptoms:**

- Alert: `ClickhouseDiskUsage` > 90%
- Queries failing with "no space"

**Resolution:**

```bash
# Check disk usage
kubectl exec clickhouse-0 -n causaliq -- df -h

# Enable compression
kubectl exec clickhouse-0 -n causaliq -- clickhouse-client \
  --query "ALTER TABLE incidents MODIFY SETTING compression_codec = 'ZSTD'"

# Archive old incidents (>90 days)
kubectl exec clickhouse-0 -n causaliq -- clickhouse-client \
  --query "INSERT INTO incidents_archive SELECT * FROM incidents WHERE created_at < now() - interval 90 day"

# Delete archived incidents
kubectl exec clickhouse-0 -n causaliq -- clickhouse-client \
  --query "DELETE FROM incidents WHERE created_at < now() - interval 90 day"

# Expand PVC
kubectl patch pvc clickhouse-data-0 -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'
```

### Incident: Pod Crash Loop

**Symptoms:**

- Pods in `CrashLoopBackOff` state
- Alert: Container OOM killed

**Resolution:**

```bash
# Check logs
kubectl logs deployment/backend -n causaliq --tail=50

# Increase memory limits
kubectl set resources deployment backend \
  -c backend \
  --limits=memory=4Gi,cpu=2000m \
  -n causaliq

# Rolling restart
kubectl rollout restart deployment/backend -n causaliq
```

---

## Maintenance & Operations

### Weekly Tasks

- [ ] Review alert logs
- [ ] Check backup completion
- [ ] Verify disk space usage
- [ ] Review RCA accuracy metrics

### Monthly Tasks

- [ ] Rotate credentials (API tokens, passwords)
- [ ] Security vulnerability scans
- [ ] Performance optimization review
- [ ] Database maintenance (vacuum, optimize)

### Quarterly Tasks

- [ ] Disaster recovery drill
- [ ] Load testing
- [ ] Capacity planning review
- [ ] Major version upgrades

---

## Support & Escalation

**On-Call Escalation:**

1. **P1 (Critical):** < 5 min response
   - Database down
   - Jira integration broken
   - RCA engine not detecting incidents

2. **P2 (High):** < 30 min response
   - High error rate (> 1%)
   - High latency (> 1s P95)
   - Low RCA accuracy (< 75%)

3. **P3 (Medium):** < 4 hrs response
   - Disk usage high (> 80%)
   - Deprecated warnings
   - Performance degradation

---

**Questions? Check the wiki or contact the platform team!**

🚀 **Happy Incident Hunting!**
