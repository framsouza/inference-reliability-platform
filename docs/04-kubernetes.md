# 04 — Kubernetes cluster

The whole platform targets a **single-node cluster with one NVIDIA GPU** —
optimized for the Brev "one-click launchable" experience. It's not a
distributed HA setup by design, but every choice has been made so the same
manifests scale to a proper multi-node cluster with minimal edits.

## Target cluster shape

| Property | Value | Why |
|----------|-------|-----|
| Distro | k3s or kind | k3s ships `local-path` StorageClass out of the box; kind is used by CI |
| K8s version | 1.29+ | Gateway API v1.2.1 & Inference Extension v1.5 require ≥1.29 |
| Nodes | 1 | Single GPU host; multi-node needs new placement rules |
| GPU | 1× NVIDIA (Ampere or newer for bf16) | Meta-Llama-3-8B-Instruct fits on ~20GB with bf16 |
| Container runtime | containerd + NVIDIA container toolkit | GPU Operator ships toolkit disabled — expects it pre-installed |
| Storage | `local-path` StorageClass (default) | 100Gi PVC for HF model cache; must be RWO |
| Ingress | Envoy Gateway (`:8080`) | No cloud LB assumed; Brev port-forwards this |

CI (`.github/workflows/e2e.yml`) uses `kindest/node:v1.30.4` as the pinned
reference for testing manifests.

## Namespaces

Namespaces are created by their owning ArgoCD Application via
`syncOptions: CreateNamespace=true`. No manual `kubectl create namespace` is
needed after `bootstrap/install.sh`.

| Namespace | Owner | Purpose |
|-----------|-------|---------|
| `argocd` | bootstrap | ArgoCD control plane |
| `argo` | `apps/argo-workflows.yaml` | Argo Workflows + eval/loadtest CRDs |
| `envoy-gateway-system` | `apps/envoy-gateway.yaml` | Envoy Gateway controller + data plane + Gateway `public` |
| `external-secrets` | `apps/external-secrets.yaml` | ESO operator + `vault-token` bootstrap Secret |
| `gpu-operator` | `apps/gpu-operator.yaml` | NVIDIA GPU Operator (device plugin + DCGM exporter) |
| `keda` | `apps/keda.yaml` | KEDA operator + metrics adapter |
| `kyverno` | `apps/kyverno.yaml` | Kyverno controllers + ClusterPolicies |
| `llama` | `secrets/llama-namespace.yaml` | vLLM pod + EPP + InferencePool (created by ESO app so ExternalSecrets can target it) |
| `monitoring` | `apps/kube-prometheus-stack.yaml` | Prometheus, Grafana, Alertmanager + Loki, Tempo, OTel, Pushgateway |
| `vault` | `apps/vault.yaml` | HashiCorp Vault (dev mode) |

## Storage

- **StorageClass**: `local-path` (k3s default; provided by
  `rancher.io/local-path`). RWO only. On multi-node clusters, replace with
  something RWX-capable (Longhorn, NFS, EFS CSI) if you need HF cache
  sharing across nodes.
- **PVCs used**:
  - `llama-hf-cache` — 100Gi, mounted at `/root/.cache/huggingface` in the
    vLLM pod. Holds the ~16GB Llama-3-8B weights.
  - Loki, Tempo, Grafana, Prometheus, Alertmanager, Vault — small PVCs from
    their charts (see individual values).

## `RuntimeClass`

Pods that consume `nvidia.com/gpu` must set `runtimeClassName: nvidia`.
Otherwise:

- kind/k3s use `runc` by default, which lacks `libnvidia-*` bind-mounts.
- Container will start but nvidia-smi returns "no devices found".
- vLLM crashes with `RuntimeError: CUDA error: no CUDA-capable device is detected`.

Kyverno policy `policies/mutate-nvidia-runtime-class.yaml` auto-mutates any
Pod requesting `nvidia.com/gpu` to add `runtimeClassName: nvidia`.

The `nvidia` `RuntimeClass` resource is provided by the NVIDIA GPU
Operator when it installs the container-toolkit runtime. On CI (kind), the
e2e workflow creates a fake `nvidia` RuntimeClass with `handler: runc` so
Kyverno tests pass without a real GPU.

## Priority classes

`gpu-inference` (value `1000000`) is created by the vLLM Helm chart
(`charts/llama-8b/templates/priorityclass.yaml`). It exists so:

- The vLLM pod is **never evicted** in favor of an evaluation or load-test
  pod during resource pressure.
- The scheduler wakes it up first after a node reboot.

Kyverno policy `require-inference-pod-priorityclass` enforces that any pod
in the `llama` namespace must set `priorityClassName` — this stops someone
from adding a debug pod that could kick out the model.

For load-test / eval pods there is no priority class assigned — they are
best-effort compared to inference. This is intentional.

## NetworkPolicy

Two layers of NetworkPolicy protect the vLLM pod:

1. **Default-deny in `llama`** — `charts/llama-8b/templates/networkpolicy.yaml`
   creates a default-deny policy that blocks everything not explicitly
   allowed.
2. **Explicit allow** — same file creates an allow policy admitting:
   - Ingress: `envoy-gateway-system` (port 8000), `monitoring` (port 8000
     for ServiceMonitor scrape), `argo` (port 8000 for evals/loadtests)
   - Egress: `kube-system` DNS (53), `vault` (8200), OTel Collector in
     `monitoring` (4317/4318), `443` to public IPs but not private CIDRs
     (so HF Hub works but internal services aren't reachable).

The EPP has its own `NetworkPolicy` (`inference/epp-networkpolicy.yaml`)
with symmetric rules.

## Resource sizing (single-GPU baseline)

The following requests/limits fit a single node with **one A10 (24 GB) or
larger** GPU and **32 GB system RAM**:

| Workload | CPU req | Mem req | Mem limit |
|----------|---------|---------|-----------|
| vLLM pod | 4 | 16Gi | 32Gi |
| EPP | 200m | 256Mi | 512Mi |
| Prometheus | 100m | 512Mi | (unbounded) |
| Grafana | 50m | 128Mi | (chart default) |
| Loki (singleBinary) | (chart default) | ~256Mi | ~1Gi |
| Tempo | (chart default) | ~256Mi | ~1Gi |
| OTel Collector | 100m | 256Mi | 512Mi |
| ArgoCD server | (chart default) | ~200Mi | ~500Mi |
| Argo Workflows server | 50m | 128Mi | (chart default) |
| KEDA operator | 50m | 128Mi | (chart default) |
| Kyverno admission | 100m | 128Mi | (chart default) |

Total control-plane footprint ~2-3 GB — leaves ~28 GB for vLLM headroom.

## Scaling to multi-node

The manifests are single-node-friendly but not single-node-locked. To go
multi-node:

1. Replace `local-path` StorageClass with RWX-capable storage (or split HF
   cache per node with an initContainer).
2. Add a `nodeSelector` or `tolerations` to the vLLM Deployment targeting
   your GPU nodes (`values.yaml` has hooks for both).
3. Set `autoscaling.maxReplicas > 1` in `charts/llama-8b/values.yaml` — KEDA
   will scale on `vllm:num_requests_waiting`.
4. Consider affinity rules to spread replicas across GPU nodes.
5. Loki, Tempo, ArgoCD, Vault — swap `singleBinary`/`dev` modes for their
   HA equivalents.

Details in [`19-extending.md`](19-extending.md).

## Related docs

- GPU-specific configuration: [`05-gpu-operator.md`](05-gpu-operator.md)
- Policies enforcing pod shape: [`11-kyverno-policies.md`](11-kyverno-policies.md)
- NetworkPolicy topology: [`../images/networking.mmd`](../images/networking.mmd)
