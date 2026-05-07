#!/bin/bash
# Production deployment guide for CausalIQ

set -e

echo "🚀 CausalIQ Production Deployment Guide"
echo "========================================"

# Step 1: Prerequisites
echo ""
echo "1️⃣  Prerequisites Check"
echo "   - Kubernetes 1.25+ cluster"
echo "   - kubectl configured"
echo "   - Docker registry access"
echo "   - 16+ GB RAM per node"
echo ""
read -p "   Press enter to continue..."

# Step 2: Build images
echo ""
echo "2️⃣  Building Production Docker Images"
docker-compose build --no-cache backend frontend jira-bridge
echo "   ✅ Images built"

# Step 3: Push to registry
echo ""
echo "3️⃣  Pushing images to registry (optional)"
read -p "   Enter registry URL (or skip): " REGISTRY
if [ ! -z "$REGISTRY" ]; then
  docker tag causaliq-backend:latest $REGISTRY/causaliq-backend:latest
  docker push $REGISTRY/causaliq-backend:latest
  echo "   ✅ Images pushed"
fi

# Step 4: Create namespace
echo ""
echo "4️⃣  Creating Kubernetes namespace"
kubectl apply -f k8s/k8s-manifest.yml --dry-run=client -o yaml | kubectl apply -f -
echo "   ✅ Namespace created"

# Step 5: Secrets management
echo ""
echo "5️⃣  Setting up secrets"
echo "   Create a secrets.env file with:"
echo "   - NEO4J_PASSWORD (strong)"
echo "   - CLICKHOUSE_PASSWORD (strong)"
echo "   - JIRA_API_TOKEN"
echo "   - SLACK_BOT_TOKEN"
read -p "   Enter path to secrets.env: " SECRETS_FILE
if [ -f "$SECRETS_FILE" ]; then
  kubectl create secret generic causaliq-secrets \
    --from-env-file=$SECRETS_FILE \
    -n causaliq \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "   ✅ Secrets created"
fi

# Step 6: Deploy
echo ""
echo "6️⃣  Deploying CausalIQ"
kubectl apply -f k8s/k8s-manifest.yml
echo "   ✅ Deployment started"

# Step 7: Wait for rollout
echo ""
echo "7️⃣  Waiting for services to be ready..."
kubectl rollout status deployment/backend -n causaliq --timeout=5m
kubectl rollout status deployment/frontend -n causaliq --timeout=5m
echo "   ✅ All services ready"

# Step 8: Verify
echo ""
echo "8️⃣  Verification"
kubectl get pods -n causaliq
kubectl get services -n causaliq
echo ""
echo "   Frontend URL:"
kubectl get service frontend -n causaliq --output jsonpath='{.status.loadBalancer.ingress[0].hostname}'
echo ""
echo ""
echo "✅ Production deployment complete!"
echo ""
echo "Next steps:"
echo "1. Configure ingress for HTTPS"
echo "2. Set up monitoring (Prometheus, Grafana)"
echo "3. Configure backup schedules"
echo "4. Set up log aggregation"
echo "5. Register Jira webhook"
