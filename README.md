# Argo CD Agent (Managed Mode) & Argo Rollouts Lab

A complete, self-contained multi-cluster environment demonstrating **Argo CD Agent in Managed Mode** paired with **Argo Rollouts**.

This repository answers key production unknowns regarding:
1. **Rollout Concurrency & Blast Radius Control:** Throttling deployments to strictly 1 rollout at a time (rather than 20 concurrent rollouts) to avoid load spikes on infrastructure/databases during roll-forwards.
2. **Automated vs. Manual Rollback Reliability:** Validating automated HTTP canary analysis rollbacks, examining how Argo CD GitOps `selfHeal: true` interacts with manual operator aborts (`kubectl argo rollouts abort`), and detailing the correct recovery procedures.

---

## Architecture Overview

```
                      Shared Docker Network (k3d-net)
┌────────────────────────────────────────┐  ┌────────────────────────────────────────┐
│         k3d-hub (Control Plane)        │  │         k3d-spoke (Edge Workload)      │
│                                        │  │                                        │
│  ┌──────────────────┐                  │  │  ┌──────────────────┐                  │
│  │ Local Git Server │ (Port 30941)     │  │  │ Argo CD Agent    │ (Worker Client)  │
│  └────────▲─────────┘                  │  │  └────────┬─────────┘                  │
│           │                            │  │           │ In-cluster                 │
│  ┌────────┴─────────┐                  │  │  ┌────────▼─────────┐                  │
│  │ Argo CD Server   │ (API & Web UI)   │  │  │ Argo CD App Ctrl │                  │
│  └──────────────────┘                  │  │  └────────┬─────────┘                  │
│  ┌──────────────────┐                  │  │           │ Reconciles CRDs            │
│  │ Agent Principal  │ (NodePort 30443) │  │  ┌────────▼─────────────────────────┐  │
│  └────────▲─────────┘                  │  │  │ Argo Rollouts Controller         │  │
│           │                            │  │  │                                  │  │
│           │   Outbound mTLS gRPC       │  │  │  • Wave 0: service-a (Canary)    │  │
│           └────────────────────────────┼──┼──┤  • Wave 1: service-b (Canary)    │  │
│               (No inbound to Spoke)    │  │  │  • Wave 2: service-c (Canary)    │  │
│                                        │  │  └──────────────────────────────────┘  │
└────────────────────────────────────────┘  └────────────────────────────────────────┘
```

### Key Security & Multi-Cluster Properties
- **Zero Inbound Credentials:** The Hub cluster possesses **no kubeconfig or cluster-admin secrets** for the Spoke cluster.
- **Outbound mTLS Tunnel:** The Spoke agent establishes an outbound gRPC tunnel to the Principal on `k3d-hub-serverlb:30443`.
- **Decentralized Execution:** The Argo CD Application Controller and Argo Rollouts Controller run locally on the Spoke cluster, eliminating latency and control plane SPOF.

---

## Directory Structure

```
.
├── charts/
│   ├── argocd-agent-principal/       # Official Principal Helm chart
│   ├── argocd-agent-agent/           # Official Agent Worker Helm chart
│   ├── principal-values.yaml         # Values enabling mTLS & allowedNamespaces: "*"
│   └── agent-values.yaml             # Values for managed mode & outbound gRPC
├── git-server/
│   └── deployment.yaml               # In-cluster bare Git daemon (port 30941 on Hub)
├── gitops-repo/                      # Working tree synced to in-cluster Git server
│   ├── kustomization.yaml
│   └── services/
│       ├── service-a.yaml            # Wave 0 canary rollout + AnalysisTemplate
│       ├── service-b.yaml            # Wave 1 canary rollout + AnalysisTemplate
│       └── service-c.yaml            # Wave 2 canary rollout + AnalysisTemplate
├── scripts/
│   ├── setup_clusters.sh             # Creates k3d-net, k3d-hub, and k3d-spoke
│   └── teardown.sh                   # Deletes clusters and Docker network
├── verify_rollouts.py                # Automated end-to-end test runner
└── README.md                         # Documentation
```

---

## Quickstart & Verification

### 1. Prerequisites
- Docker
- `k3d`
- `kubectl`
- `helm`
- Python 3 with `requests` (`pip install requests`)
- Argo Rollouts CLI plugin (`kubectl-argo-rollouts`)

### 2. Run the Automated Verification Suite
To execute all four test scenarios automatically:
```bash
python3 verify_rollouts.py
```

The script performs:
1. **Scenario 1 (Blast Radius Control):** Simultaneous 3-microservice roll-forward across sync waves; verifies that deployments are serialized one-by-one.
2. **Scenario 2 (Automated Canary Rollback):** Injects faulty image (`argoproj/rollouts-demo:bad-red`), verifies automated HTTP 500 canary failure, instant traffic cutback, and downstream wave protection.
3. **Scenario 3 (Manual Abort vs. Self-Heal):** Tests emergency manual abort (`kubectl argo rollouts abort`) against Argo CD `selfHeal: true`, validating status subresource behavior and recovery.
4. **Scenario 4 (Hub-Controlled Rollout via Resource Proxy):** Introduces a manual approval gate `pause: {}`, pauses rollout on Spoke, discovers actions, streams canary pod logs, and imperatively promotes the rollout from the central Hub over the reverse gRPC tunnel with ZERO inbound firewall access into the spoke!

### 3. Live Inspection Commands

**Inspect Rollout Status:**
```bash
# Check all rollouts on the spoke cluster
kubectl get rollouts -n demo --context k3d-spoke

# Watch interactive canary dashboard for service-a
kubectl argo rollouts get rollout service-a -n demo --watch --context k3d-spoke
```

**Access Argo CD UI (Hub Cluster):**
```bash
kubectl port-forward svc/argocd-server -n argocd 8443:443 --context k3d-hub
```
- Open `https://localhost:8443`
- Username: `admin`
- Password:
  ```bash
  kubectl get secret argocd-initial-admin-secret -n argocd --context k3d-hub -o jsonpath="{.data.password}" | base64 -d; echo
  ```

---

## Key Production Takeaways

### 1. Blast Radius Control via Sync Waves
- Assigning `argocd.argoproj.io/sync-wave: "0"`, `"1"`, `"2"` to microservices effectively serializes rollouts.
- Because Argo CD natively tracks the health of `argoproj.io/Rollout` custom resources, Wave 1 will **never start** until Wave 0 reaches `Healthy` status.
- Empirical test result: Concurrency remained strictly capped at 1 rollout at any given second.

### 2. AnalysisTemplate Configuration for HTTP 500 Responses
- When testing canary failures with HTTP 500 responses, Argo Rollouts categorizes 500 status codes as metric `Error` (not `Failed`).
- Default configuration uses `consecutiveErrorLimit: 4` with 10-second exponential retry delays, causing a 40+ second abort latency.
- Setting `consecutiveErrorLimit: 1` ensures instantaneous abort and traffic diversion back to the stable ReplicaSet.

### 3. Manual Abort vs. Argo CD Self-Heal
- `kubectl argo rollouts abort` mutates `status.abort: true`.
- Argo CD GitOps `selfHeal: true` only reconciles the resource `spec`. It **does not** override or fight manual aborts.
- **Operational Trap:** If a developer pushes a new Git commit to an aborted rollout without running `kubectl argo rollouts retry`, the rollout remains stalled in `RolloutAborted`.
- **Recommended Incident Runbook:**
  1. Operator executes: `kubectl argo rollouts abort <name>` (cuts traffic immediately).
  2. Team commits Git revert (`git revert <bad-commit>`).
  3. Operator or CI re-arms controller: `kubectl argo rollouts retry rollout <name>`.

### 4. Argo CD Sync Retry Lock Trap
- When an automated sync fails due to degraded rollouts, Argo CD enters a retry backoff loop locked onto the failing Git commit SHA (`operationState.operation.sync.revision`).
- Pushing a fix commit to Git will **not** immediately be applied because Argo CD prioritizes completing the backoff retry sequence for the older failing revision.
- **Remediation:** Automated pipelines or operators should terminate the hung operation (`argocd app terminate-op <app>` or patch out `/status/operationState` on the spoke application) before or upon pushing fix commits.

### 5. Multi-Cluster Agent Managed Mode UI & API Requirements
When running `argocd-agent` in Managed Mode where each spoke cluster maps to its own namespace (e.g. `spoke`), Argo CD's control plane requires three key configurations to render applications in the UI/API:
1. **`argocd-cmd-params-cm`**: Must set `application.namespaces: "*"` (or `spoke`) so the API server and UI index non-control-plane namespaces.
2. **`argocd-rbac-cm`**: Add `p, role:admin, applications, *, *, allow` and `p, role:admin, applications, *, */*/*, allow` to support 3-segment resource paths (`<namespace>/<project>/<app>`).
3. **`AppProject`**: Must declare `spec.sourceNamespaces: ["*"]` (or `["spoke"]`), otherwise Argo CD rejects applications residing outside `argocd`.

### 6. UI Resource Tree & Redis Proxy
In Managed Mode, application resources are cached in the Spoke cluster's Redis. Without the Principal's Redis proxy (`argocd-agent-redis-proxy`), the Hub UI fails with `500: cache: key is missing` when viewing resource trees.
- Enable `principal.redisProxy.enabled: true` in the Principal Helm chart.
- Configure `argocd-cmd-params-cm` on the Hub: `redis.server: "argocd-agent-redis-proxy:6379"`.

### 7. Centralized Imperative Control via Resource Proxy (Approach C)
To inspect live manifests, stream pod logs, and trigger manual rollout promotions (e.g. promoting past a `pause: {}` approval gate) directly from the Hub Argo CD UI without direct inbound access into the Spoke:
- Enable `principal.resourceProxy.enabled: true` (listens on `:9090`) and `enableResourceProxy: true` on the Spoke agent.
- Map the cluster secret `cluster-spoke` on the Hub to `server: https://argocd-agent-resource-proxy:9090?agentName=spoke`.
- The Resource Proxy intercepts Kubernetes API requests from Argo CD, serializes them over the outbound reverse gRPC tunnel to the Spoke agent, and returns responses back to the Hub UI/API. Zero firewall holes or inbound load balancers required!

---

## Teardown
To cleanly tear down both clusters and remove all containers:
```bash
./scripts/teardown.sh
```
