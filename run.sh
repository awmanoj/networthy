#!/usr/bin/env bash
#
# run.sh — run Networthy on the server, published on port 8321.
#
# Pulls the image from Docker Hub and runs it, listening on port 8321 both
# inside the container and on the host (published as 8321:8321).
#
# Usage:
#   DOCKERHUB_USER=yourname ./run.sh [tag]
#
#   tag   Optional image tag to run (default: latest).
#
# Data (the SQLite net-worth DB) is persisted in the named volume
# `networthy_data`, mounted at /app/data.

set -euo pipefail

# --- Config -----------------------------------------------------------------
DOCKERHUB_USER="${DOCKERHUB_USER:?Set DOCKERHUB_USER to your Docker Hub username}"
IMAGE_NAME="${IMAGE_NAME:-networthy}"
TAG="${1:-latest}"

REPO="${DOCKERHUB_USER}/${IMAGE_NAME}"
CONTAINER_NAME="${CONTAINER_NAME:-networthy}"
PORT=8321
DATA_VOLUME="${DATA_VOLUME:-networthy_data}"

echo ">> Pulling ${REPO}:${TAG}"
docker pull "${REPO}:${TAG}"

# --- Replace any existing container -----------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  echo ">> Removing existing container ${CONTAINER_NAME}"
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

# --- Run --------------------------------------------------------------------
# APP_PORT makes uvicorn (and the image's healthcheck) bind to ${PORT} inside
# the container; -p publishes that same port on the host.
echo ">> Starting ${CONTAINER_NAME} on port ${PORT}"
docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -e "APP_PORT=${PORT}" \
  -e "RESEND_API_KEY=${RESEND_API_KEY:-}" \
  -e "EMAIL_FROM=${EMAIL_FROM:-}" \
  -e "OWNER_EMAIL=${OWNER_EMAIL:-}" \
  -e "APP_SECRET=${APP_SECRET:-}" \
  -e "COOKIE_SECURE=${COOKIE_SECURE:-true}" \
  -p "${PORT}:${PORT}" \
  -v "${DATA_VOLUME}:/app/data" \
  "${REPO}:${TAG}"

echo ">> Running. Available at http://<server-ip>:${PORT}/"
echo ">> Logs: docker logs -f ${CONTAINER_NAME}"
