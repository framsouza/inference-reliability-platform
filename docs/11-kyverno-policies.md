# 11 — Kyverno policies

[Kyverno](https://kyverno.io) is the admission controller. It sits between
`kubectl apply` and the etcd write, and can **validate**, **mutate**, or
**generate** Kubernetes objects. This repo uses Kyverno to codify eight
opinionated invariants that the vLLM platform depends on.

Every policy exists because it prevented a specific failure mode.

## Files

```
apps/kyverno.yaml                       Kyverno Helm chart v3.3.0 (wave 3)
apps/policies.yaml                      ArgoCD Application → policies/ (wave 7)
policies/
├── mutate-nvidia-runtime-class.yaml           Mutate — GPU pods get nvidia RuntimeClass
├── require-gpu-pod-shm.yaml                   Validate — GPU pods must mount /dev/shm ≥ 1Gi
├── require-inference-pod-priorityclass.yaml   Validate — llama ns pods must have priorityClassName
├── require-inference-pod-resources.yaml       Validate — llama ns pods must set CPU + memory requests
├── validate-argo-pod-limits.yaml              Validate — argo ns pods must have CPU + memory limits
├── validate-service-monitor-release.yaml      Validate — ServiceMonitors must have label release=kps
├── mutate-workflow-ttl.yaml                   Mutate — Workflows get ttlStrategy.secondsAfterCompletion: 3600
└── verify-vllm-image.yaml                     Verify (Audit) — vllm images must be Cosign-signed
```

All policies are `ClusterPolicy` (cluster-scoped). Each carries the label
`argocd.argoproj.io/instance: policies` so ArgoCD tracks it.

## Policy-by-policy

### `mutate-nvidia-runtime-class`

**Type**: Mutate  
**Mode**: Enforce  
**Scope**: Pods with any container requesting `nvidia.com/gpu`  
**Action**: Adds `spec.runtimeClassName: nvidia` if not set.

Why: containerd's default runtime (`runc`) doesn't inject NVIDIA driver
libraries. Without `runtimeClassName: nvidia`, the container starts but
`nvidia-smi` returns "no devices". vLLM crashes at CUDA init with a
confusing error. The mutation is a safety net so someone dropping a
one-off GPU debug pod doesn't hit this.

Test: apply a Pod requesting `nvidia.com/gpu` without `runtimeClassName`;
kubectl get shows `runtimeClassName: nvidia` set by Kyverno.

### `require-gpu-pod-shm`

**Type**: Validate  
**Mode**: Enforce  
**Scope**: Pods with any container requesting `nvidia.com/gpu`  
**Action**: Rejects pods that don't mount `/dev/shm` as an `emptyDir`
with `medium: Memory` and `sizeLimit ≥ 1Gi`.

Why: containerd's default `/dev/shm` is 64 MiB. PyTorch/vLLM shared-memory
tensor sharing exhausts it and crashes with `Bus error` mid-request.
[The vLLM chart mounts 2Gi by default](06-inference-vllm.md), but this
policy guards against custom pods bypassing that.

### `require-inference-pod-priorityclass`

**Type**: Validate  
**Mode**: Enforce  
**Scope**: Pods in namespace `llama`  
**Action**: Requires `spec.priorityClassName` to be set (any value).

Why: without a `priorityClassName`, a Pod is `system-priority: 0`. If the
node runs out of memory, kubelet evicts by priority — a `BestEffort` load
test pod would kick out the vLLM Deployment. The chart sets
`gpu-inference` (value 1M). Policy makes sure no one forgets.

### `require-inference-pod-resources`

**Type**: Validate  
**Mode**: Enforce  
**Scope**: Pods in namespace `llama`  
**Action**: Requires every container to set `resources.requests.cpu` and
`resources.requests.memory`.

Why: pods without requests get QoS `BestEffort`, which are the first
evicted under pressure. Setting requests on every container gives the
pod `Burstable` (or `Guaranteed` if requests == limits) QoS. The vLLM
chart sets both; this policy catches a rogue sidecar someone adds later.

### `validate-argo-pod-limits`

**Type**: Validate  
**Mode**: Enforce  
**Scope**: Pods in namespace `argo`  
**Action**: Requires every container to set `resources.limits.cpu` and
`resources.limits.memory`.

Why: Argo Workflows launches pods for evals and load tests. Without limits,
a runaway workflow (bad input, infinite loop) would OOM the entire node,
taking vLLM down with it. Requiring limits makes each pod bounded.

### `validate-service-monitor-release`

**Type**: Validate  
**Mode**: Enforce  
**Scope**: `ServiceMonitor` resources  
**Action**: Requires the label `release: kps` on any ServiceMonitor.

Why: `kube-prometheus-stack` is installed with release name `kps`, and
Prometheus is configured to only scrape ServiceMonitors matching that
label. This policy catches ServiceMonitors that would silently not be
scraped. Prevents "why is my new metric not appearing" investigations.

### `mutate-workflow-ttl`

**Type**: Mutate  
**Mode**: Enforce  
**Scope**: `argoproj.io/v1alpha1/Workflow`  
**Action**: Sets `spec.ttlStrategy.secondsAfterCompletion: 3600` if not
set.

Why: Argo Workflows keeps completed Workflow objects in etcd until
manually cleaned up. Nightly load tests accumulate. 1-hour TTL keeps a
window for debugging and prevents etcd bloat.

### `verify-vllm-image`

**Type**: Image verification (Cosign)  
**Mode**: **Audit** (not Enforce)  
**Scope**: containers pulling `vllm/vllm-openai:*`  
**Action**: Verifies the image has a valid Cosign signature from
`vllm-project` GitHub Actions. On failure, generates a `PolicyReport`
entry but doesn't block admission.

Why Audit and not Enforce: upstream vLLM signing has been inconsistent
across releases; requiring verification would occasionally block a
legitimate release. Audit mode surfaces the risk without breaking the
platform.

Flip to `Enforce` for regulated environments once your image supply
chain is stable.

## Enforce vs. Audit mode

- **Enforce** (default) — policy failure = admission rejection. `kubectl`
  gets an error.
- **Audit** — policy failure = a `PolicyReport` entry. Resource is
  admitted. Query with:
  ```bash
  kubectl get policyreport -A
  kubectl describe policyreport <name>
  ```

Only `verify-vllm-image` runs in Audit today.

## Testing policies locally

The CI workflow (`.github/workflows/ci.yml`) runs `kyverno test` on the
rendered Helm chart against the policies. To run locally:

```bash
helm template llama charts/llama-8b > /tmp/rendered.yaml
kyverno test /tmp/rendered.yaml --policies policies/ \
  --resources /tmp/rendered.yaml
```

`kyverno test` in dry-run mode won't apply the mutation to files, but it
will report which policies matched and their result.

To e2e-test in a real (kind) cluster:

```bash
kind create cluster
kubectl apply -k https://github.com/kyverno/kyverno/config/release?ref=v1.13.0
kubectl apply -f policies/
kubectl apply -f test-pod.yaml
# Expect either admission rejection or auto-mutation
```

`e2e.yml` (`.github/workflows/e2e.yml`) does this end-to-end on every PR
touching `policies/` or `charts/`.

## Debugging

- **Application blocked by a policy** — read the `kubectl apply` error;
  Kyverno prepends `[Kyverno]` and names the failing rule.
- **Kyverno itself failing** — `kubectl -n kyverno logs
  deploy/kyverno-admission-controller`. Common: TLS webhook cert expired
  (Kyverno rotates automatically but drift after downtime can happen).
- **Policy not triggering** — check the `spec.rules[*].match` selector.
  `namespaceSelector` and `resources.kinds` are the usual culprits.
- **PolicyReport growing** — `kubectl get policyreport -A -o
  json | jq '.items[] | .summary'` for a per-report summary.

## Extending / operating

- **Add a new invariant** — copy an existing policy file, adjust
  `match`, `preconditions`, `validate` / `mutate` block, commit. Test
  locally with `kyverno test`.
- **Roll out gradually** — start in `Audit`, watch PolicyReports for
  false positives, flip to `Enforce` when stable.
- **Exception mechanism** — Kyverno supports `PolicyException` CRs that
  temporarily exempt specific workloads. Useful during migrations.
- **Cluster policy vs. namespaced policy** — this repo uses only
  ClusterPolicies. Move to `Policy` (namespaced) if you have per-tenant
  namespaces with different rules.

## Related docs

- GPU pod requirements: [`05-gpu-operator.md`](05-gpu-operator.md)
- Inference pod shape: [`06-inference-vllm.md`](06-inference-vllm.md)
- Argo workload sizing: [`15-loadtests.md`](15-loadtests.md)
- CI validation: [`17-ci-cd.md`](17-ci-cd.md)
