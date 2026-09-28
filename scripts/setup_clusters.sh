#!/usr/bin/env bash
set -euo pipefail

NETWORK_NAME="k3d-net"
HUB_CLUSTER="hub"
SPOKE_CLUSTER="spoke"

echo "=== [1/3] Creating Docker Network: ${NETWORK_NAME} ==="
docker network create "${NETWORK_NAME}" 2>/dev/null || true

echo "=== [2/3] Creating Hub Cluster: ${HUB_CLUSTER} ==="
if ! k3d cluster list | grep -q "^${HUB_CLUSTER} "; then
  k3d cluster create "${HUB_CLUSTER}" \
    --image "rancher/k3s:v1.30.2-k3s1" \
    --network "${NETWORK_NAME}" \
    --k3s-arg "--disable=traefik@server:0" \
    --k3s-arg "--disable=metrics-server@server:0" \
    --port "8080:80@loadbalancer" \
    --port "30443:30443@loadbalancer" \
    --wait
else
  echo "Cluster ${HUB_CLUSTER} already exists."
fi

echo "=== [3/3] Creating Spoke Cluster: ${SPOKE_CLUSTER} ==="
if ! k3d cluster list | grep -q "^${SPOKE_CLUSTER} "; then
  k3d cluster create "${SPOKE_CLUSTER}" \
    --image "rancher/k3s:v1.30.2-k3s1" \
    --network "${NETWORK_NAME}" \
    --k3s-arg "--disable=traefik@server:0" \
    --k3s-arg "--disable=metrics-server@server:0" \
    --wait
else
  echo "Cluster ${SPOKE_CLUSTER} already exists."
fi

echo "=== Clusters ready! ==="
kubectl config get-contexts
