#!/usr/bin/env bash
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION before running this script}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPOSITORY_NAME="${ECR_REPOSITORY_NAME:-ai-test-generator-sandbox}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${REPOSITORY_NAME}:${IMAGE_TAG}"

if ! aws ecr describe-repositories --repository-names "$REPOSITORY_NAME" >/dev/null 2>&1; then
  aws ecr create-repository \
    --repository-name "$REPOSITORY_NAME" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256 >/dev/null
fi

aws ecr get-login-password --region "$AWS_REGION" | docker login \
  --username AWS --password-stdin "$REGISTRY"
docker build --pull -f sandbox/Dockerfile.ecs -t "$IMAGE_URI" .
docker push "$IMAGE_URI"
printf 'SandboxImageUri=%s\n' "$IMAGE_URI"
