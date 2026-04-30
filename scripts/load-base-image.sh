#!/usr/bin/env bash
# Load the saved platform base image (includes Jaeger binary) so that
# Dockerfile.incremental can build without internet access.
#
# The base image is NOT stored in git (too large). It lives at:
#   /home/ubuntu/docker-images/amaze-platform-base.tar.gz
#
# Run this once after a fresh clone or after Docker prunes the image cache:
#   ./scripts/load-base-image.sh
#
# To save a fresh copy (after the main Dockerfile is rebuilt with internet):
#   docker save amaze-amaze:latest | gzip > /home/ubuntu/docker-images/amaze-platform-base.tar.gz

set -euo pipefail

BASE_IMAGE_PATH="${AMAZE_BASE_IMAGE:-/home/ubuntu/docker-images/amaze-platform-base.tar.gz}"

if [ ! -f "$BASE_IMAGE_PATH" ]; then
  echo "ERROR: Base image not found at $BASE_IMAGE_PATH"
  echo "  Either build from scratch (needs internet):"
  echo "    docker build -f Dockerfile -t amaze-amaze:latest ."
  echo "  Or set AMAZE_BASE_IMAGE to the path of the saved tar.gz."
  exit 1
fi

echo "Loading base image from $BASE_IMAGE_PATH ..."
docker load < "$BASE_IMAGE_PATH"
echo "Done. amaze-amaze:latest is now available for incremental builds."
