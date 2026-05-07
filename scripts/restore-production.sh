#!/bin/bash
# Restore script for CausalIQ production databases

set -e

if [ -z "$1" ]; then
  echo "Usage: ./restore-production.sh <backup_timestamp>"
  echo "Example: ./restore-production.sh 20260502_143022"
  exit 1
fi

TIMESTAMP=$1
BACKUP_DIR="/backups/causaliq"
RESTORE_LOG="${BACKUP_DIR}/restore_${TIMESTAMP}.log"

echo "🔄 CausalIQ Restore Started" | tee -a $RESTORE_LOG
echo "Timestamp: $TIMESTAMP" | tee -a $RESTORE_LOG
echo "" | tee -a $RESTORE_LOG

# ── Verify backups exist ──────────────────────────────────────────────────
echo "✓ Verifying backups exist..." | tee -a $RESTORE_LOG
[ -d "${BACKUP_DIR}/neo4j_${TIMESTAMP}" ] || { echo "❌ Neo4j backup not found"; exit 1; }
[ -f "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb" ] || { echo "❌ Redis backup not found"; exit 1; }
[ -d "${BACKUP_DIR}/qdrant_${TIMESTAMP}" ] || { echo "❌ Qdrant backup not found"; exit 1; }

# ── Stop services ──────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "⏹️  Stopping services..." | tee -a $RESTORE_LOG
docker-compose stop backend jira-bridge stream-processor anomaly-detector rca-engine

# ── Restore Neo4j ──────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "🔗 Restoring Neo4j..." | tee -a $RESTORE_LOG
docker exec causaliq-neo4j \
  neo4j-admin database restore \
  --from-path="/backups" "neo4j_${TIMESTAMP}" \
  2>&1 | tee -a $RESTORE_LOG

# ── Restore Redis ──────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "🔴 Restoring Redis..." | tee -a $RESTORE_LOG
docker cp "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb" causaliq-redis:/data/dump.rdb
docker exec causaliq-redis redis-cli SHUTDOWN NOSAVE
docker-compose start redis
sleep 5

# ── Restore Qdrant ────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "📚 Restoring Qdrant..." | tee -a $RESTORE_LOG
docker exec causaliq-qdrant rm -rf /qdrant/snapshots/*
docker cp "${BACKUP_DIR}/qdrant_${TIMESTAMP}" causaliq-qdrant:/qdrant/snapshots/

# ── Start services ────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "▶️  Starting services..." | tee -a $RESTORE_LOG
docker-compose start backend jira-bridge stream-processor anomaly-detector rca-engine

# ── Verify restore ────────────────────────────────────────────────────────
echo "" | tee -a $RESTORE_LOG
echo "✓ Verifying restore..." | tee -a $RESTORE_LOG
sleep 10
curl -f http://localhost:9001/health || { echo "❌ Restore verification failed"; exit 1; }

echo "" | tee -a $RESTORE_LOG
echo "✅ Restore completed successfully at $(date)" | tee -a $RESTORE_LOG
