#!/usr/bin/env bash
#
# deploy.sh — build the Networthy image and push it to Docker Hub.
#
# Usage:
#   DOCKERHUB_USER=yourname ./deploy.sh [tag]
#
#   tag   Optional image tag (default: latest). The image is also always
#         tagged with the short git commit SHA for traceability.
#
# Requires: docker, and a prior `docker login` (or DOCKERHUB_TOKEN set below).

set -euo pipefail

# --- Config -----------------------------------------------------------------
DOCKERHUB_USER="${DOCKERHUB_USER:?Set DOCKERHUB_USER to your Docker Hub username}"
IMAGE_NAME="${IMAGE_NAME:-networthy}"
TAG="${1:-latest}"

REPO="${DOCKERHUB_USER}/${IMAGE_NAME}"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

cd "$(dirname "$0")"

# --- Login (non-interactive if a token is provided) -------------------------
if [[ -n "${DOCKERHUB_TOKEN:-}" ]]; then
  echo ">> Logging in to Docker Hub as ${DOCKERHUB_USER}"
  echo "${DOCKERHUB_TOKEN}" | docker login -u "${DOCKERHUB_USER}" --password-stdin
fi

# --- Build ------------------------------------------------------------------
echo ">> Building ${REPO}:${TAG} (and :${GIT_SHA})"
docker build \
  -t "${REPO}:${TAG}" \
  -t "${REPO}:${GIT_SHA}" \
  .

# --- Push -------------------------------------------------------------------
echo ">> Pushing ${REPO}:${TAG}"
docker push "${REPO}:${TAG}"

echo ">> Pushing ${REPO}:${GIT_SHA}"
docker push "${REPO}:${GIT_SHA}"

echo ">> Done. Pushed:"
echo "     ${REPO}:${TAG}"
echo "     ${REPO}:${GIT_SHA}"
