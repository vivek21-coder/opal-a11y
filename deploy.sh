#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# --- Configuration ---
REGION="europe-west2"
REPO_NAME="opal-repo"
# Set your project ID here to prevent accidental deployment to the wrong project
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"

# Colors for output
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo -e "${GREEN}Starting Opal Deployment for Project: $PROJECT_ID${NC}"

# --- 1. Prerequisites Check ---
gcloud config set project "$PROJECT_ID"

# --- 2. Collect Secrets (So we don't save them to disk) ---
if [ -f "infra/terraform.tfvars" ]; then
  echo "Loading secrets from infra/terraform.tfvars..."
else
  if [ -z "$TF_VAR_google_api_key" ]; then
    read -s -p "Enter Google API Key (Gemini): " TF_VAR_google_api_key
    echo ""
    export TF_VAR_google_api_key
  fi

  if [ -z "$TF_VAR_groq_api_key" ]; then
    read -s -p "Enter Groq API Key: " TF_VAR_groq_api_key
    echo ""
    export TF_VAR_groq_api_key
  fi
fi

# --- 3. Infrastructure Setup ---
echo -e "${GREEN}Enabling required APIs...${NC}"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com iam.googleapis.com cloudbuild.googleapis.com

echo -e "${GREEN}Checking Artifact Registry...${NC}"
if ! gcloud artifacts repositories describe $REPO_NAME --location=$REGION &>/dev/null; then
    echo "Creating Artifact Registry repository..."
    gcloud artifacts repositories create $REPO_NAME \
        --repository-format=docker \
        --location=$REGION \
        --description="Docker repository for Opal"
else
    echo "Artifact Registry repository already exists."
fi

echo -e "${GREEN}Configuring Docker auth...${NC}"
gcloud auth configure-docker ${REGION}-docker.pkg.dev --quiet

# --- 4. Build and Push Docker Images ---
BACKEND_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/backend:latest"
FRONTEND_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/frontend:latest"

echo -e "${GREEN}Building and Pushing Backend...${NC}"
cd backend
docker build --platform linux/amd64 -t $BACKEND_IMAGE .
docker push $BACKEND_IMAGE
# Use the digest to ensure Terraform detects the change
export TF_VAR_backend_image=$(docker inspect --format='{{index .RepoDigests 0}}' $BACKEND_IMAGE)
cd ..

echo -e "${GREEN}Building and Pushing Frontend...${NC}"
# Frontend uses runtime BACKEND_URL injection via entrypoint.sh — no build-arg needed
cd frontend
docker build --platform linux/amd64 -t $FRONTEND_IMAGE .
docker push $FRONTEND_IMAGE
# Use the digest to ensure Terraform detects the change
export TF_VAR_frontend_image=$(docker inspect --format='{{index .RepoDigests 0}}' $FRONTEND_IMAGE)
cd ..

# --- 5. Deploy with Terraform ---
echo -e "${GREEN}Initializing and Applying Terraform...${NC}"
cd infra
terraform init -upgrade

# Export image variables for Terraform to pick up automatically
export TF_VAR_project_id=$PROJECT_ID
export TF_VAR_region=$REGION

# Apply changes (auto-approve skips the "yes" prompt)
terraform apply -var="project_id=$PROJECT_ID" -auto-approve

echo -e "${GREEN}Deployment Complete!${NC}"
echo "---------------------------------------------------"
terraform output
echo "---------------------------------------------------"
cd ..