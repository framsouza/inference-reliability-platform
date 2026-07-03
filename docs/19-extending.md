# 19 — Extending the platform

The launchable is opinionated for one model on one GPU. Here's how to
push it in the four directions people usually want.

## 1. Add a second model

Two ways: separate release of the same chart, or add a route to a single
larger vLLM Deployment.

### Preferred: separate Helm release

```
apps/
├── llama.yaml          release: llama, model: meta-llama/Meta-Llama-3-8B-Instruct
└── mistral.yaml        release: mistral, model: mistralai/Mistral-7B-Instruct-v0.3
```

Duplicate `apps/llama.yaml` as `apps/mistral.yaml`:

```yaml
spec:
  source:
    repoURL: https://github.com/framsouza/inference-reliability-platform.git
    path: charts/llama-8b            # same chart
    targetRevision: HEAD
    helm:
      releaseName: mistral
      values: |
        model: mistralai/Mistral-7B-Instruct-v0.3
        persistence:
          hfCache:
            size: 60Gi
        rolloutGate:
          maxP95TtftSeconds: 2.0
```

Add:

- Add an HTTPRoute for the new model — either `/v2/mistral/**` or
  header-based routing.
- Add an `InferencePool` selecting the mistral pods (labels differ by
  release name).
- Add an `EnvoyExtensionPolicy` targeting the new HTTPRoute pointing at
  a second EPP.

**Single-GPU caveat**: you can't run two models on one GPU
simultaneously without MIG partitioning. Either:

- Set `nodeSelector` for each release to a different node.
- Use MIG to split the GPU (`gpu-operator` `migManager`).
- Keep one at `replicas: 0` and swap by scaling.

### Alternative: one vLLM Deployment serving multiple models

vLLM v0.7+ supports `--served-model-name` with multiple names. Requires
loading multiple checkpoints — significant memory. Not recommended
below 40 GB GPU memory.

## 2. Add a second route

For a new endpoint (say, `/embeddings` on a different model):

1. Deploy the model (see above).
2. Add `httproutes/embeddings.yaml`:
   ```yaml
   apiVersion: gateway.networking.k8s.io/v1
   kind: HTTPRoute
   metadata:
     name: embeddings
     namespace: llama
   spec:
     parentRefs: [{name: public, namespace: envoy-gateway-system}]
     rules:
       - matches: [{path: {type: PathPrefix, value: /embeddings}}]
         backendRefs: [{name: embeddings-svc, port: 8000}]
   ```
3. Rate-limit and ext_proc policies work the same way.

Details: [`08-gateway-envoy.md`](08-gateway-envoy.md).

## 3. Add a new dashboard, alert, or eval

- **Dashboard**: create `dashboards/my.yaml` (ConfigMap with
  `grafana_dashboard: "1"` label). See [`13-dashboards.md`](13-dashboards.md).
- **Alert**: create a `PrometheusRule` under `alerts/`. Label
  `release: kps`. See [`14-alerts.md`](14-alerts.md).
- **Eval prompt**: add a line to `evals/prompts-configmap.yaml`. See
  [`16-evals.md`](16-evals.md).

## 4. Multi-GPU / multi-node scaling

Single-node is the launchable's baseline. Going multi-GPU or multi-node:

### Multi-GPU on one node (tensor parallelism)

- `values.yaml`:
  ```yaml
  gpu: 2
  resources:
    limits:
      nvidia.com/gpu: 2
  ```
- Deployment template: add `--tensor-parallel-size 2` to the vLLM args.
  (Currently not exposed as a value — templatize it if you use this
  often.)
- Requires NVLink or PCIe P2P between the GPUs.

### Multi-GPU across nodes (data parallelism, more replicas)

- `values.yaml`:
  ```yaml
  replicas: 2
  autoscaling:
    maxReplicas: 4
  ```
- Storage: `local-path` is per-node and RWO. Switch to a shared class
  (Longhorn, EFS-CSI, GCE PD). Or split the HF cache per replica by
  using a `StatefulSet` with a `volumeClaimTemplates`, giving each
  replica its own PVC — model files download once per pod, but you keep
  isolation.
- Deployment strategy: switch from `Recreate` to `RollingUpdate`
  (maxSurge: 1, maxUnavailable: 0). You need one spare GPU for the
  rolling update.
- Placement: `podAntiAffinity` to spread replicas across nodes:
  ```yaml
  affinity:
    podAntiAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        - labelSelector:
            matchLabels: {app.kubernetes.io/name: llama-8b}
          topologyKey: kubernetes.io/hostname
  ```
- InferencePool will pick up all replicas automatically via label selector.

### Making observability multi-node ready

- Loki: swap `singleBinary` for distributed (`microservices` in the chart).
- Tempo: same, or switch to `tempo-distributed` chart.
- Prometheus: remote-write to a durable backend.
- Vault: real Raft cluster; see [`10-secrets-eso-vault.md`](10-secrets-eso-vault.md).

## Adapting for a bigger model (Llama-3-70B, etc.)

Rough compatibility matrix (bf16, no quantization):

| Model | Params | Min GPU memory | vLLM `--tensor-parallel-size` |
|-------|--------|----------------|--------------------------------|
| Llama-3-8B | 8B | 16 GB | 1 |
| Llama-3-70B | 70B | 160 GB | 4× A100 80GB / 2× H100 80GB |
| Mixtral-8x7B | 47B activated | 96 GB | 4× A100 40GB |

For a 70B model on 4× A100:

1. `gpu: 4`, `--tensor-parallel-size 4`, `replicas: 1`.
2. Bump PVC to 400 Gi.
3. Bump `startupFailureThreshold` further — download takes 30+ min.
4. Bump `resources.requests.cpu` to 16 (tokenization + scheduler
   overhead).

## Removing a component

Every app is one file in `apps/`. To remove (say, the eval CronWorkflow):

1. Delete `apps/evals.yaml`.
2. Delete `evals/` (or keep for later).
3. Delete `dashboards/model-quality.yaml`.
4. Delete `alerts/model-quality.yaml`.
5. Commit; ArgoCD prunes the Argo Workflow objects.

## Common pitfalls

- **Changing the chart's `releaseName`** — retriggers a full resource
  reprovision. All pod names change; PVCs may orphan.
- **Reducing PVC size** — Kubernetes doesn't support shrinking PVCs.
  Create a new one and copy data.
- **Changing `PriorityClass.value`** after workloads exist — running
  pods keep their priority; only new pods pick up the new value.
- **Renaming an ArgoCD Application** — old one keeps its resources.
  Delete the old Application first (with `--cascade`).

## Related docs

- Overall architecture: [`01-architecture.md`](01-architecture.md)
- Component-by-component: index in [`README.md`](README.md)
- Reliability essay: [`20-making-inference-reliable.md`](20-making-inference-reliable.md)
