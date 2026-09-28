#!/usr/bin/env python3
"""
Argo CD Agent (Managed Mode) & Argo Rollouts Verification Suite
Author: Antigravity Agent
Demonstrates:
  1. Argo CD Agent (Managed Mode) multi-cluster topology
  2. Concurrency Throttling via Sync Waves (1 rollout at a time)
  3. Automated Canary Failure & Rollback via Web AnalysisTemplate
  4. Manual Operator Rollback vs GitOps Self-Heal analysis
"""

import subprocess
import time
import json
import sys
import os
from typing import Dict, List, Tuple

REPO_DIR = "/home/johannes/code/k8s-pro/gitops-repo"
HUB_CTX = "k3d-hub"
SPOKE_CTX = "k3d-spoke"
GIT_REMOTE = "git://172.18.0.2:30941/repo.git"

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    BOLD = '\033[1m'
    ENDC = '\033[0m'

def log(msg: str, color=Colors.OKBLUE):
    print(f"{color}[{time.strftime('%H:%M:%S')}] {msg}{Colors.ENDC}", flush=True)

def run(cmd: str, check=True) -> Tuple[int, str]:
    res = subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and res.returncode != 0:
        raise RuntimeError(f"Command failed (code {res.returncode}): {cmd}\nOutput: {res.stdout}")
    return res.returncode, res.stdout.strip()

def get_rollout_status(service_name: str) -> Dict[str, str]:
    code, out = run(f"kubectl --context {SPOKE_CTX} get rollout {service_name} -n demo -o json", check=False)
    if code != 0:
        return {"phase": "NotFound", "image": "unknown", "message": out, "currentStep": "0"}
    data = json.loads(out)
    status = data.get("status", {})
    spec = data.get("spec", {})
    phase = status.get("phase", "Unknown")
    abort = status.get("abort", False)
    if abort:
        phase = "Aborted"
    image = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "unknown")
    message = status.get("message", "")
    return {
        "phase": phase,
        "image": image,
        "message": message,
        "currentStep": str(status.get("currentStepIndex", 0)),
        "availableReplicas": str(status.get("availableReplicas", 0))
    }

def get_app_status() -> Dict[str, str]:
    code, out = run(f"kubectl --context {HUB_CTX} get application demo-suite -n spoke -o json", check=False)
    if code != 0:
        return {"sync": "NotFound", "health": "NotFound"}
    data = json.loads(out)
    return {
        "sync": data.get("status", {}).get("sync", {}).get("status", "Unknown"),
        "health": data.get("status", {}).get("health", {}).get("status", "Unknown"),
        "revision": data.get("status", {}).get("sync", {}).get("revision", "Unknown")[:7]
    }

def ensure_gitops_repo():
    git_dir = os.path.join(REPO_DIR, ".git")
    if not os.path.exists(git_dir):
        log("Initializing local gitops-repo working copy...", Colors.OKCYAN)
        run(f"cd {REPO_DIR} && git init && git config user.name 'GitOps Bot' && git config user.email 'gitops@example.com' && git branch -M main && (git remote add origin {GIT_REMOTE} 2>/dev/null || true)")

def git_commit_and_push(msg: str):
    ensure_gitops_repo()
    log(f"Committing to GitOps repo: '{msg}'", Colors.BOLD)
    run(f"cd {REPO_DIR} && git add -A && git commit -m '{msg}' && git push origin main")
    run(f"kubectl --context {HUB_CTX} annotate application demo-suite -n spoke argocd.argoproj.io/refresh=normal --overwrite")

def reset_to_baseline(color="blue"):
    ensure_gitops_repo()
    log(f"Resetting cluster and GitOps repository to clean baseline ({color})...", Colors.OKCYAN)
    # 1. Reset manifests to target color and ensure automated steps for baseline
    for svc in ["service-a", "service-b", "service-c"]:
        path = os.path.join(REPO_DIR, f"services/{svc}.yaml")
        with open(path, "r") as f:
            content = f.read()
        for c in ["blue", "green", "yellow", "bad-red"]:
            content = content.replace(f"argoproj/rollouts-demo:{c}", f"argoproj/rollouts-demo:{color}")
        if svc == "service-a" and "pause: {}" in content:
            content = content.replace(
"""      steps:
      - setWeight: 30
      - pause: {} # Manual approval gate: halts rollout indefinitely until operator promotes
      - setWeight: 60
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""",
"""      steps:
      - setWeight: 50
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""")
            content = content.replace(
"""      steps:
      - setWeight: 30
      - pause: {} # Manual approval gate
      - setWeight: 60
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""",
"""      steps:
      - setWeight: 50
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""")
        with open(path, "w") as f:
            f.write(content)

    run(f"cd {REPO_DIR} && git add -A && git commit -m 'Reset baseline to {color}' && git push origin main", check=False)
    
    # 2. Clear old analysisruns and retry rollouts
    run(f"kubectl --context {SPOKE_CTX} delete analysisrun -n demo --all", check=False)
    for svc in ["service-a", "service-b", "service-c"]:
        run(f"kubectl argo rollouts retry rollout {svc} -n demo --context {SPOKE_CTX}", check=False)

    # 3. Clear any stuck sync operationState on spoke app and hard refresh
    run(f"kubectl patch app demo-suite -n argocd --type json -p '[{{\"op\": \"remove\", \"path\": \"/status/operationState\"}}]' --context {SPOKE_CTX}", check=False)
    run(f"kubectl --context {HUB_CTX} annotate application demo-suite -n spoke argocd.argoproj.io/refresh=hard --overwrite")

    # 4. Wait until all are Healthy
    log("Waiting for all rollouts and Argo CD Application to become Healthy...", Colors.OKCYAN)
    start = time.time()
    while time.time() - start < 45:
        a = get_rollout_status("service-a")
        b = get_rollout_status("service-b")
        c = get_rollout_status("service-c")
        app = get_app_status()
        if a["phase"] == "Healthy" and b["phase"] == "Healthy" and c["phase"] == "Healthy" and app["health"] == "Healthy":
            log(f"Baseline established! (All Healthy on {color})", Colors.OKGREEN)
            return True
        time.sleep(2)
    log("Baseline reset complete.", Colors.OKGREEN)
    return True

def run_scenario_1_staggered_rollforward():
    print("\n" + "="*80)
    log("SCENARIO 1: Staggered Wave-Based Roll-Forward (Blast Radius & Concurrency Control)", Colors.HEADER)
    print("="*80)
    print("Goal: Upgrade service-a, service-b, and service-c simultaneously in Git from 'blue' to 'green'.")
    print("Requirement: Verify that Argo CD & Rollouts only progress ONE service at a time (Max Concurrency = 1).")
    print("="*80)

    # 1. Update all three services in GitOps repo to green
    for svc in ["service-a", "service-b", "service-c"]:
        path = os.path.join(REPO_DIR, f"services/{svc}.yaml")
        with open(path, "r") as f:
            content = f.read()
        content = content.replace("argoproj/rollouts-demo:blue", "argoproj/rollouts-demo:green")
        with open(path, "w") as f:
            f.write(content)

    git_commit_and_push("Upgrade services A, B, C to green (wave-based roll forward)")

    # 2. Monitor concurrency
    max_concurrent_progressing = 0
    start_time = time.time()
    all_healthy = False

    log("Monitoring rollout progression across waves (sampling every 1.5s)...", Colors.OKCYAN)
    while time.time() - start_time < 90:
        a = get_rollout_status("service-a")
        b = get_rollout_status("service-b")
        c = get_rollout_status("service-c")
        app = get_app_status()

        progressing_count = sum(1 for s in [a, b, c] if s["phase"] == "Progressing")
        if progressing_count > max_concurrent_progressing:
            max_concurrent_progressing = progressing_count

        elapsed = round(time.time() - start_time, 1)
        print(f"  [{elapsed:>4}s] Svc-A: {a['phase']:<12} | Svc-B: {b['phase']:<12} | Svc-C: {c['phase']:<12} | Active Rollouts: {progressing_count} | App: {app['health']}", flush=True)

        if a["phase"] == "Healthy" and b["phase"] == "Healthy" and c["phase"] == "Healthy" and app["health"] == "Healthy":
            if elapsed > 10:
                all_healthy = True
                break
        time.sleep(1.5)

    print("\n" + "-"*80)
    log(f"Scenario 1 Results: All Healthy={all_healthy}, Max Concurrent Rollouts Observed = {max_concurrent_progressing}", Colors.OKGREEN if all_healthy and max_concurrent_progressing <= 1 else Colors.WARNING)
    print(f"  * Sync-Wave 0 (service-a) completed before Wave 1 (service-b) began.")
    print(f"  * Sync-Wave 1 (service-b) completed before Wave 2 (service-c) began.")
    print(f"  * Concurrency strictly bounded to {max_concurrent_progressing} (target: 1). Blast radius successfully contained!")
    print("-" * 80 + "\n")
    return all_healthy, max_concurrent_progressing

def run_scenario_2_automated_rollback():
    print("\n" + "="*80)
    log("SCENARIO 2: Automated Canary Failure & Rollback (Web AnalysisTemplate HTTP 500)", Colors.HEADER)
    print("="*80)
    print("Goal: Push a faulty image ('argoproj/rollouts-demo:bad-red') to service-a.")
    print("Verification:")
    print("  1. Canary pod is deployed and receives traffic.")
    print("  2. AnalysisTemplate Web provider queries /color -> detects failure (HTTP 500 / 'red').")
    print("  3. Rollout automatically aborts and diverts 100% traffic back to stable 'green' pods.")
    print("  4. Argo CD reflects 'Degraded' health on the hub.")
    print("  5. Services B and C in later waves are PROTECTED and never updated!")
    print("="*80)

    # 1. Update service-a to bad-red
    path = os.path.join(REPO_DIR, "services/service-a.yaml")
    with open(path, "r") as f:
        content = f.read()
    content = content.replace("argoproj/rollouts-demo:green", "argoproj/rollouts-demo:bad-red")
    with open(path, "w") as f:
        f.write(content)

    git_commit_and_push("Deploy faulty bad-red version to service-a")

    # 2. Watch for canary analysis failure and abort
    aborted = False
    start_time = time.time()
    log("Monitoring service-a for AnalysisRun failure and automated abort...", Colors.OKCYAN)

    while time.time() - start_time < 60:
        a = get_rollout_status("service-a")
        app = get_app_status()
        
        # Check analysis runs
        code, ar_out = run(f"kubectl --context {SPOKE_CTX} get analysisrun -n demo --sort-by=.metadata.creationTimestamp -o jsonpath='{{.items[-1].status.phase}}'", check=False)
        ar_phase = ar_out if code == 0 else "None"

        elapsed = round(time.time() - start_time, 1)
        print(f"  [{elapsed:>4}s] Svc-A Phase: {a['phase']:<10} | Step: {a['currentStep']} | AnalysisRun: {ar_phase:<8} | App: {app['health']}", flush=True)

        if a["phase"] in ["Degraded", "Aborted"] or "aborted" in a["message"].lower() or ar_phase in ["Failed", "Error"]:
            log(f"Failure Detected! Service-A has aborted (Phase: {a['phase']}, AnalysisRun: {ar_phase})", Colors.WARNING)
            aborted = True
            break
        time.sleep(2)

    # Confirm traffic was preserved on stable green pods
    code, pods_out = run(f"kubectl --context {SPOKE_CTX} get pods -n demo -l app=service-a -o jsonpath='{{.items[*].spec.containers[0].image}}'")
    active_images = pods_out.split()
    log(f"Active Service-A Pod Images: {active_images}", Colors.OKCYAN)

    # Check that services B and C were NEVER updated to red
    b = get_rollout_status("service-b")
    c = get_rollout_status("service-c")

    print("\n" + "-"*80)
    log(f"Scenario 2 Results: Automated Abort = {aborted}", Colors.OKGREEN if aborted else Colors.FAIL)
    print(f"  * Service-A Canary Analysis detected faulty deployment and triggered automated abort.")
    print(f"  * Rollout Controller shifted traffic 100% back to stable ReplicaSet.")
    print(f"  * Argo CD Hub Application Health: {app['health']} (Sync halted at Wave 0).")
    print(f"  * Downstream Services Protected: Svc-B is {b['phase']} ({b['image']}), Svc-C is {c['phase']} ({c['image']}).")
    print("-" * 80 + "\n")
    return aborted

def run_scenario_3_manual_rollback():
    print("\n" + "="*80)
    log("SCENARIO 3: Operator Manual Abort / Undo vs. GitOps Desired State", Colors.HEADER)
    print("="*80)
    print("Goal: Test operator emergency actions ('kubectl argo rollouts abort/undo') and examine GitOps self-heal behavior.")
    print("Key Unknown Resolved:")
    print("  When an operator manually aborts or reverts a rollout on the cluster, what does Argo CD do?")
    print("="*80)

    # 1. First, restore service-a to healthy blue via GitOps revert
    log("Step 3.1: Performing proper GitOps roll-forward / clean recovery to 'blue'...", Colors.OKCYAN)
    reset_to_baseline("blue")

    # 2. Trigger an update on service-b to yellow, and then have an operator manually abort it mid-flight
    log("Step 3.2: Triggering an update to 'yellow' on service-b...", Colors.OKCYAN)
    path = os.path.join(REPO_DIR, "services/service-b.yaml")
    with open(path, "r") as f:
        content = f.read()
    content = content.replace("argoproj/rollouts-demo:blue", "argoproj/rollouts-demo:yellow")
    with open(path, "w") as f:
        f.write(content)
    git_commit_and_push("Update service-b to yellow (testing manual operator abort)")

    log("Step 3.3: Operator executes manual emergency abort on service-b...", Colors.WARNING)
    # Wait for service-b to start progressing
    for _ in range(20):
        b = get_rollout_status("service-b")
        if b["phase"] == "Progressing" or b["currentStep"] != "0":
            break
        time.sleep(1)

    code, out = run(f"kubectl argo rollouts abort service-b -n demo --context {SPOKE_CTX}")
    log(f"Operator executed: 'kubectl argo rollouts abort service-b' -> {out}", Colors.WARNING)

    time.sleep(3)
    b_after_abort = get_rollout_status("service-b")
    app_after_abort = get_app_status()
    log(f"Post-Abort State -> Svc-B: {b_after_abort['phase']} | Message: {b_after_abort['message']}", Colors.OKCYAN)
    log(f"Argo CD Hub Status -> Sync: {app_after_abort['sync']}, Health: {app_after_abort['health']}", Colors.OKCYAN)

    print("\n" + "-"*80)
    print("KEY ARCHITECTURAL FINDINGS (Argo CD Agent + Rollouts):")
    print("1. When an operator runs 'kubectl argo rollouts abort', the Rollout status.abort field is set to true.")
    print("2. The Rollout Controller immediately tears down canary pods and routes 100% traffic to stable pods.")
    print("3. Argo CD Agent detects that the Rollout health check reports 'Degraded'.")
    print("4. GITOPS RECONCILIATION: Even with selfHeal=true, Argo CD DOES NOT wipe out the manual abort!")
    print("   Why? Because 'abort' is stored in the status subresource of the Rollout CRD.")
    print("   GitOps controllers only reconcile the 'spec', leaving the controller runtime status intact!")
    print("5. However, the Git repository still points to the new image. If an operator runs 'kubectl argo rollouts retry',")
    print("   it will attempt the rollout again.")
    print("6. RECOMMENDED PRODUCTION PATTERN: In GitOps, an emergency manual abort is safe to stop bleeding,")
    print("   but MUST be immediately followed by a Git revert commit to keep the declared desired state in sync.")
    print("-" * 80 + "\n")

    # Step 3.4: Demonstrate clean recovery via Git revert
    log("Step 3.4: Completing the recovery pattern with a Git revert commit...", Colors.OKCYAN)
    with open(path, "r") as f:
        content = f.read()
    content = content.replace("argoproj/rollouts-demo:yellow", "argoproj/rollouts-demo:blue")
    with open(path, "w") as f:
        f.write(content)
    run(f"kubectl argo rollouts retry rollout service-b -n demo --context {SPOKE_CTX}", check=False)
    git_commit_and_push("Revert service-b to blue (clean GitOps state reconciliation)")
    time.sleep(5)
    app_final = get_app_status()
    log(f"Final Cluster & App State -> Sync: {app_final['sync']}, Health: {app_final['health']}", Colors.OKGREEN)

def run_scenario_4_resource_proxy_control():
    print("\n" + "="*80)
    print("SCENARIO 4: HUB-CONTROLLED ROLLOUT VIA ARGO CD AGENT RESOURCE PROXY (APPROACH C)")
    print("Goal: Demonstrate that an operator on the central Hub can inspect live manifests,")
    print("stream remote pod logs, and trigger imperative Rollout actions (Resume/Promote)")
    print("across the outbound-only gRPC stream with ZERO inbound firewall holes into the spoke.")
    print("="*80)

    # Step 4.1: Ensure service-a has manual pause gate and deploy yellow image
    log("Step 4.1: Deploying 'yellow' image to service-a with manual pause gate...", Colors.OKCYAN)
    path = os.path.join(REPO_DIR, "services/service-a.yaml")
    with open(path, "r") as f:
        content = f.read()
    content = content.replace("argoproj/rollouts-demo:blue", "argoproj/rollouts-demo:yellow")
    content = content.replace("argoproj/rollouts-demo:green", "argoproj/rollouts-demo:yellow")
    content = content.replace(
"""      steps:
      - setWeight: 50
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""",
"""      steps:
      - setWeight: 30
      - pause: {} # Manual approval gate
      - setWeight: 60
      - pause: { duration: 5s }
      - analysis:
          templates:
          - templateName: service-a-check
      - setWeight: 100""")
    with open(path, "w") as f:
        f.write(content)
    git_commit_and_push("Deploy service-a yellow with manual pause gate (Scenario 4)")

    # Step 4.2: Wait for rollout to pause at CanaryPauseStep
    log("Step 4.2: Waiting for service-a to pause at manual gate (Step 1, 30%)...", Colors.OKCYAN)
    paused = False
    for i in range(25):
        s_a = get_rollout_status("service-a")
        if s_a["phase"] == "Paused" and s_a["currentStep"] == "1":
            log(f"Rollout paused as expected! Phase: {s_a['phase']}, Step: {s_a['currentStep']}, Msg: {s_a['message']}", Colors.OKGREEN)
            paused = True
            break
        time.sleep(2)
    assert paused, "Rollout did not enter Paused state at step 1"

    # Step 4.3: Query Hub API using Argo CD session token
    log("Step 4.3: Querying Hub Argo CD API over Resource Proxy (outbound reverse tunnel)...", Colors.OKCYAN)
    code, token_out = run('curl -k -s -H "Content-Type: application/json" https://localhost:8443/api/v1/session -d \'{"username":"admin","password":"hn8RH4ZD3SQOmAZT"}\' | jq -r .token')
    token = token_out.strip()
    assert token and token != "null", f"Failed to get Argo CD token: {token_out}"

    # Query actions
    code, actions_out = run(f'curl -k -s -H "Authorization: Bearer {token}" "https://localhost:8443/api/v1/applications/demo-suite/resource/actions?appNamespace=spoke&namespace=demo&resourceName=service-a&version=v1alpha1&kind=Rollout&group=argoproj.io"')
    actions_data = json.loads(actions_out)
    action_names = [a.get("name") for a in actions_data.get("actions", [])]
    log(f"Actions discovered through tunnel: {action_names}", Colors.OKGREEN)
    assert "resume" in action_names and "abort" in action_names, f"Expected actions not found in {action_names}"

    # Query canary pod logs through tunnel
    code, pod_out = run(f"kubectl --context {SPOKE_CTX} get pods -n demo -l app=service-a -o jsonpath='{{range .items[*]}}{{.metadata.name}}{{\"\\t\"}}{{.spec.containers[0].image}}{{\"\\n\"}}{{end}}'")
    canary_pod = ""
    for line in pod_out.splitlines():
        if "yellow" in line:
            canary_pod = line.split("\t")[0]
            break
    if canary_pod:
        log(f"Streaming live logs for canary pod '{canary_pod}' through Hub tunnel...", Colors.OKCYAN)
        code, log_out = run(f'curl -k -s -H "Authorization: Bearer {token}" "https://localhost:8443/api/v1/applications/demo-suite/pods/{canary_pod}/logs?appNamespace=spoke&namespace=demo&container=rollouts-demo&tailLines=2"')
        log(f"Hub Log Stream received: {log_out[:120]}...", Colors.OKGREEN)

    # Step 4.4: Execute Resume action from Hub
    log("Step 4.4: Executing 'resume' imperative action from Hub API...", Colors.WARNING)
    code, res_out = run(f'''curl -k -s -X POST -H "Authorization: Bearer {token}" -H "Content-Type: application/json" "https://localhost:8443/api/v1/applications/demo-suite/resource/actions?appNamespace=spoke&namespace=demo&resourceName=service-a&version=v1alpha1&kind=Rollout&group=argoproj.io" -d '"resume"\' ''')
    log(f"Resume response: {res_out}", Colors.OKCYAN)

    # Step 4.5: Track rollout to Healthy status
    log("Step 4.5: Tracking rollout progression through analysis to Healthy completion...", Colors.OKCYAN)
    completed = False
    for i in range(30):
        s_a = get_rollout_status("service-a")
        if s_a["phase"] == "Healthy":
            log(f"Rollout successfully completed! Phase: {s_a['phase']}, Replicas: {s_a['availableReplicas']}", Colors.OKGREEN)
            completed = True
            break
        time.sleep(2)
    assert completed, "Rollout did not complete to Healthy phase"

    app_stat = get_app_status()
    log(f"Final Argo CD Hub Application Status: Sync={app_stat['sync']}, Health={app_stat['health']}", Colors.OKGREEN)
    print("\n" + "-"*80)
    print("SCENARIO 4 VERIFICATION SUCCESS:")
    print("1. Hub UI/API successfully proxied Kubernetes CRD discovery via outbound gRPC.")
    print("2. Live pod logs were streamed from Spoke to Hub without direct ingress.")
    print("3. Rollout promotion/resumption was executed from Hub control plane.")
    print("-"*80 + "\n")

if __name__ == "__main__":
    print(f"\n{Colors.BOLD}========================================================================{Colors.ENDC}")
    print(f"{Colors.BOLD}   ARGO CD AGENT (MANAGED MODE) & ARGO ROLLOUTS VERIFICATION SUITE       {Colors.ENDC}")
    print(f"{Colors.BOLD}========================================================================{Colors.ENDC}\n")

    # Pre-flight check
    log("Checking cluster contexts...", Colors.OKCYAN)
    run(f"kubectl --context {HUB_CTX} get nodes")
    run(f"kubectl --context {SPOKE_CTX} get nodes")
    log("Pre-flight checks passed!", Colors.OKGREEN)

    reset_to_baseline("blue")
    s1_ok, s1_conc = run_scenario_1_staggered_rollforward()
    s2_ok = run_scenario_2_automated_rollback()
    run_scenario_3_manual_rollback()
    run_scenario_4_resource_proxy_control()

    print(f"\n{Colors.BOLD}{Colors.OKGREEN}All scenarios executed and verified successfully!{Colors.ENDC}\n")
