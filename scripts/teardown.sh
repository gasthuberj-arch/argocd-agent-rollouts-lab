#!/usr/bin/env bash
set -euo pipefail

HUB_CLUSTER="hub"
SPOKE_CLUSTER="spoke"
NETWORK_NAME="k3d-net"

echo "=== Deleting Clusters ==="
k3d cluster delete "${HUB_CLUSTER}" 2>/dev/null || true
k3d cluster delete "${SPOKE_CLUSTER}" 2>/dev/null || true

echo "=== Deleting Network ==="
docker network rm "${NETWORK_NAME}" 2>/dev/null || true

echo "=== Cleanup complete ==="
