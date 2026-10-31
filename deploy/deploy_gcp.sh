#!/usr/bin/env bash
# Deploy Plant Debugger to Google Cloud (Submission Mode)
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
BUCKET_NAME="${PROJECT_ID}-plant-debugger-evidence"
TOPIC_NAME="plant-anomaly-events"

echo "=== Deploying Plant Debugger to Project: ${PROJECT_ID} in Region: ${REGION} ==="

# 1. Enable Required Services
echo "Enabling Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  pubsub.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}"

# 2. Setup GCS Evidence Bucket
echo "Configuring Evidence Storage Bucket..."
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" --location="${REGION}" --uniform-bucket-level-access --project="${PROJECT_ID}"
fi

# 3. Setup Pub/Sub Topic
echo "Configuring Pub/Sub Topic: ${TOPIC_NAME}..."
if ! gcloud pubsub topics describe "${TOPIC_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud pubsub topics create "${TOPIC_NAME}" --project="${PROJECT_ID}"
fi

# 4. Service Account for Plant Debugger
SA_NAME="plant-debugger-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" --display-name="Plant Debugger Runtime" --project="${PROJECT_ID}"
fi

# Assign IAM roles
for ROLE in roles/datastore.user roles/storage.objectAdmin roles/aiplatform.user roles/iam.serviceAccountTokenCreator; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" --quiet
done

# 5. Build and Deploy Private plant-sim
echo "Building and Deploying plant-sim (Private Cloud Run)..."
gcloud run deploy plant-sim \
  --source=. \
  --dockerfile=deploy/Dockerfile.sim \
  --region="${REGION}" \
  --no-allow-unauthenticated \
  --set-env-vars="ANOMALY_PUBLISHER=pubsub,PUBSUB_TOPIC=${TOPIC_NAME},GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --project="${PROJECT_ID}"

SIM_URL=$(gcloud run services describe plant-sim --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
echo "plant-sim deployed at: ${SIM_URL}"

# Grant plant-debugger-sa invoker access to plant-sim
gcloud run services add-iam-policy-binding plant-sim \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --project="${PROJECT_ID}"

# 6. Build and Deploy Public plant-debugger
echo "Building and Deploying plant-debugger (Public Cloud Run)..."
gcloud run deploy plant-debugger \
  --source=. \
  --dockerfile=deploy/Dockerfile.debugger \
  --region="${REGION}" \
  --allow-unauthenticated \
  --service-account="${SA_EMAIL}" \
  --set-env-vars="DECISION_ENGINE=adk,ADK_FALLBACK_POLICY=false,ADK_SESSION_BACKEND=firestore,VISION_ENGINE=gemini,VISION_FALLBACK_POLICY=false,FIELD_STATE_BACKEND=firestore,FIELD_EVIDENCE_BACKEND=gcs,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GCS_EVIDENCE_BUCKET=${BUCKET_NAME},SIM_URL=${SIM_URL},SIM_AUTH=google" \
  --project="${PROJECT_ID}"

DEBUGGER_URL=$(gcloud run services describe plant-debugger --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')
echo "plant-debugger deployed at: ${DEBUGGER_URL}"

# 7. Setup Pub/Sub Push Subscription to plant-debugger
SUB_NAME="plant-debugger-push"
echo "Configuring Pub/Sub push subscription to ${DEBUGGER_URL}/api/pubsub/anomaly..."
if gcloud pubsub subscriptions describe "${SUB_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud pubsub subscriptions delete "${SUB_NAME}" --project="${PROJECT_ID}" --quiet
fi

gcloud pubsub subscriptions create "${SUB_NAME}" \
  --topic="${TOPIC_NAME}" \
  --push-endpoint="${DEBUGGER_URL}/api/pubsub/anomaly" \
  --project="${PROJECT_ID}"

echo "=== Deployment Complete! ==="
echo "Commander UI: ${DEBUGGER_URL}/"
echo "Operator UI:  ${DEBUGGER_URL}/operator"

