#!/bin/bash
# Backup script for CausalIQ production databases

BACKUP_DIR="/backups/causaliq"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${BACKUP_DIR}/backup_${TIMESTAMP}.log"

mkdir -p $BACKUP_DIR
echo "Starting CausalIQ backup at $(date)" | tee -a $LOG_FILE

# ── Backup ClickHouse ──────────────────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "📊 Backing up ClickHouse..." | tee -a $LOG_FILE
docker exec causaliq-clickhouse \
  clickhouse-client --user default --password "${CLICKHOUSE_PASSWORD}" \
  --query "BACKUP DATABASE causaliq TO 's3://backups/causaliq/clickhouse_${TIMESTAMP}'" \
  2>&1 | tee -a $LOG_FILE

# ── Backup Neo4j ─────────────────────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "🔗 Backing up Neo4j..." | tee -a $LOG_FILE
docker exec causaliq-neo4j \
  neo4j-admin database backup --to-path="/backups" causaliq_${TIMESTAMP} neo4j \
  2>&1 | tee -a $LOG_FILE

# ── Backup Redis ─────────────────────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "🔴 Backing up Redis..." | tee -a $LOG_FILE
docker exec causaliq-redis \
  redis-cli BGSAVE \
  2>&1 | tee -a $LOG_FILE
docker cp causaliq-redis:/data/dump.rdb "${BACKUP_DIR}/redis_${TIMESTAMP}.rdb" \
  2>&1 | tee -a $LOG_FILE

# ── Backup Qdrant ─────────────────────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "📚 Backing up Qdrant..." | tee -a $LOG_FILE
docker exec causaliq-qdrant \
  curl -X POST http://localhost:6333/snapshots \
  2>&1 | tee -a $LOG_FILE
docker cp causaliq-qdrant:/qdrant/snapshots "${BACKUP_DIR}/qdrant_${TIMESTAMP}" \
  2>&1 | tee -a $LOG_FILE

# ── Upload to S3 ──────────────────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "☁️  Uploading backups to S3..." | tee -a $LOG_FILE
aws s3 sync "${BACKUP_DIR}" \
  "s3://${BACKUP_BUCKET}/causaliq/${TIMESTAMP}/" \
  --region "${AWS_REGION}" \
  2>&1 | tee -a $LOG_FILE

# ── Cleanup old backups (>30 days) ──────────────────────────────────────────
echo "" | tee -a $LOG_FILE
echo "🧹 Cleaning up old backups..." | tee -a $LOG_FILE
find "${BACKUP_DIR}" -type d -mtime +30 -exec rm -rf {} \; 2>/dev/null || true
aws s3 ls "s3://${BACKUP_BUCKET}/causaliq/" \
  | awk '{print $1}' \
  | while read date; do
    if [[ $(date -d "$date" +%s) -lt $(date -d "30 days ago" +%s) ]]; then
      aws s3 rm "s3://${BACKUP_BUCKET}/causaliq/$date" --recursive
    fi
  done

echo "" | tee -a $LOG_FILE
echo "✅ Backup completed at $(date)" | tee -a $LOG_FILE
echo "" | tee -a $LOG_FILE
echo "Backup summary:" | tee -a $LOG_FILE
du -sh "${BACKUP_DIR}" | tee -a $LOG_FILE
