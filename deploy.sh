#!/bin/bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <PROJECT_ID> <REDIS_IP>"
    exit 1
fi

PROJECT=$1
REDIS_IP=$2
SERVICE="promptwars"
REGION="us-central1"
IMAGE="gcr.io/$PROJECT/$SERVICE"

echo "=========================================="
echo " Deploying PromptWars API"
echo " Project: $PROJECT"
echo " Redis:   $REDIS_IP"
echo "=========================================="

echo ""
echo ">>> [1/3] Building and pushing Docker image..."
gcloud builds submit --tag "$IMAGE" .

echo ""
echo ">>> [2/3] Deploying to Cloud Run..."
gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --region "$REGION" \
    --platform managed \
    --no-allow-unauthenticated \
    --memory 512Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 20 \
    --vpc-connector scoring-connector \
    --service-account "scoring-api-sa@$PROJECT.iam.gserviceaccount.com" \
    --set-env-vars "GCP_PROJECT=$PROJECT,GCP_REGION=$REGION,REDIS_HOST=$REDIS_IP,CACHE_TTL_SEC=3600"

echo ""
echo ">>> [3/3] Fetching deployed service URL..."
SERVICE_URL=$(gcloud run services describe "$SERVICE" --platform managed --region "$REGION" --format "value(status.url)")

echo ""
echo "✅ Deployment successful!"
echo "URL: $SERVICE_URL"
