# 06 — Inference stack (vLLM Helm chart)

The `charts/llama-8b` Helm chart is where every knob that matters for
serving Llama-3-8B lives. It renders a `Deployment`, `Service`, `PVC`,
`PriorityClass`, `ServiceMonitor`, two `NetworkPolicy` resources, an
optional `KEDA ScaledObject`, and an optional `PostSync` rollout gate
`Job`.

## Chart layout

```
charts/llama-8b/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── pvc.yaml
│   ├── priorityclass.yaml
│   ├── servicemonitor.yaml
│   ├── networkpolicy.yaml
│   ├── scaledobject.yaml
│   ├── secret.yaml
│   ├── rollout-gate-job.yaml
│   └── _helpers.tpl
└── tests/                   # helm-unittest suite
    ├── deployment_test.yaml
    ├── pvc_priorityclass_test.yaml
    ├── networkpolicy_test.yaml
    └── rollout_gate_test.yaml
```

## `values.yaml` — the knobs

### Model & image

| Key | Default | Notes |
|-----|---------|-------|
| `model` | `meta-llama/Meta-Llama-3-8B-Instruct` | HF model ID. Change here to try a new model. |
| `image` | `vllm/vllm-openai:v0.7.3` | vLLM version. Bumping requires re-testing the eval suite. |
| `imagePullPolicy` | `IfNotPresent` | |
| `replicas` | `1` | Single GPU — must stay 1 unless you have MIG or multiple GPUs. |
| `gpu` | `1` | GPUs per pod (tensor parallelism > 1 goes here). |
| `enablePrefixCaching` | `false` | Off in v0.7.3 (known-flaky); flip once you upgrade. |

### Networking

| Key | Default | Notes |
|-----|---------|-------|
| `service.type` | `ClusterIP` | Envoy Gateway is the ingress; don't LB directly. |
| `service.port` | `8000` | vLLM's OpenAI server port. |
| `networkPolicy.enabled` | `true` | Creates default-deny + allow policies (see below). |

### Persistence — the HF cache

| Key | Default | Notes |
|-----|---------|-------|
| `persistence.hfCache.enabled` | `true` | If `false`, the pod re-downloads Llama-3-8B on every restart. |
| `persistence.hfCache.size` | `100Gi` | Room for the model + tokenizer + a few checkpoints. |
| `persistence.hfCache.storageClass` | `local-path` | k3s default. |
| `persistence.hfCache.accessMode` | `ReadWriteOnce` | Fine for single replica. |

Mounted at `/root/.cache/huggingface` — vLLM's default cache path.

### Secrets

| Key | Default | Notes |
|-----|---------|-------|
| `hfToken.existingSecret` | `hf-token` | Populated by ESO from Vault; keyed on `token`. |
| `apiKey.enabled` | `true` | Adds `--api-key` to the vLLM command. |
| `apiKey.existingSecret` | `vllm-api-key` | Populated by ESO from Vault. |
| `apiKey.secretKey` | `token` | The key inside the Secret. |

### OpenTelemetry

| Key | Default | Notes |
|-----|---------|-------|
| `otlp.tracesEndpoint` | `http://otel-collector.monitoring:4318` | OTLP HTTP path — vLLM's built-in exporter. |
| `otlp.serviceName` | `vllm` | Appears as `service.name` in Tempo. |
| `otlp.installSdk` | `true` | `pip install opentelemetry-*` in an entrypoint wrapper. Set to `false` if you bake it into a custom image. |

### Resources — QoS Guaranteed by intent

| Key | Default | Notes |
|-----|---------|-------|
| `resources.requests.cpu` | `4` | 4 cores for tokenization + scheduler + I/O. |
| `resources.requests.memory` | `16Gi` | Model + KV cache + working set. |
| `resources.limits.memory` | `32Gi` | Absorbs spikes without OOM eviction. |
| `resources.requests.ephemeral-storage` | `20Gi` | Argo-side scratch for tokenizer files. |

Kyverno's `require-inference-pod-resources` will reject a Pod that omits
CPU or memory requests.

### Priority

| Key | Default | Notes |
|-----|---------|-------|
| `priorityClass.enabled` | `true` | Creates the `gpu-inference` PriorityClass and assigns it to the Deployment. |
| `priorityClass.value` | `1000000` | Above any workload the user might add. |

### Probes — reliability-critical

| Key | Default | Notes |
|-----|---------|-------|
| `probes.startupInitialDelaySeconds` | `10` | Model download & load takes minutes. |
| `probes.startupPeriodSeconds` | `10` | |
| `probes.startupFailureThreshold` | `180` | 10s × 180 = **30-minute** window for first-time HF download. |
| `probes.readinessInitialDelaySeconds` | `10` | |
| `probes.readinessPeriodSeconds` | `5` | |
| `probes.livenessInitialDelaySeconds` | `60` | |
| `probes.livenessPeriodSeconds` | `15` | |
| `probes.livenessFailureThreshold` | `4` | 4 × 15s = 60s before restart. |
| `terminationGracePeriodSeconds` | `120` | Drain window for in-flight requests. |
| `preStopSleepSeconds` | `15` | Wait for endpoint removal to propagate before SIGTERM. |

### Shared memory

| Key | Default | Notes |
|-----|---------|-------|
| `shmSize` | `2Gi` | `/dev/shm` — see [`05-gpu-operator.md`](05-gpu-operator.md) for why. |

### Metrics

| Key | Default | Notes |
|-----|---------|-------|
| `serviceMonitor.enabled` | `true` | Prometheus scrapes `/metrics` on 8000. |
| `serviceMonitor.interval` | `15s` | Fast enough for KEDA and EPP; slow enough not to overwhelm Prometheus. |

### Autoscaling (KEDA)

| Key | Default | Notes |
|-----|---------|-------|
| `autoscaling.enabled` | `true` | Renders a `ScaledObject`. |
| `autoscaling.minReplicas` | `1` | |
| `autoscaling.maxReplicas` | `1` | Single-GPU today; bump when you add nodes. |
| `autoscaling.pollingInterval` | `30` | Seconds. |
| `autoscaling.idleCooldownSeconds` | `300` | Time queue must stay empty before scale-in. |
| `autoscaling.prometheusUrl` | `http://kps-kube-prometheus-stack-prometheus.monitoring:9090` | The in-cluster Prom URL. |
| `autoscaling.threshold` | `"1"` | Trigger fires when `running + waiting >= 1`. Given `maxReplicas=1`, this just keeps the pod alive; matters when maxReplicas > 1. |
| `autoscaling.activationThreshold` | `"0"` | Below this KEDA scales to zero — not used here. |

Query used:
```promql
sum(vllm:num_requests_running + vllm:num_requests_waiting)
```

See [`09-keda-autoscaling.md`](09-keda-autoscaling.md).

### Rollout gate — the PostSync canary

| Key | Default | Notes |
|-----|---------|-------|
| `rolloutGate.enabled` | `true` | Runs a `Job` after every ArgoCD sync. |
| `rolloutGate.image` | `vllm/vllm-openai:v0.9.2` | Newer than the serving image — `benchmark_serving.py` in 0.9.2 has richer output. |
| `rolloutGate.numPrompts` | `30` | Small enough to be quick. |
| `rolloutGate.requestRate` | `2` | req/s. |
| `rolloutGate.maxP95TtftSeconds` | `3.0` | SLO gate — fails the sync if p95 TTFT > 3s. |
| `rolloutGate.maxErrorRate` | `0.02` | 2% max. |
| `rolloutGate.dataset` | `random` | Synthetic prompts. |
| `rolloutGate.inputLen` | `512` | Tokens. |
| `rolloutGate.outputLen` | `128` | Tokens. |

Runs as `argocd.argoproj.io/hook: PostSync` with `delete-policy:
HookSucceeded` — it lingers on failure so you can `kubectl logs` it.

If the gate fails, ArgoCD marks the Application as **degraded**. Rolling
forward is intentional — vLLM's Deployment is `Recreate` strategy (single
GPU), so the old pod is already gone.

## `templates/deployment.yaml` — highlights

### Command wrapper

```yaml
command: ["bash", "-euo", "pipefail", "-c"]
args:
  - |
    if [ -n "$OTLP_TRACES_ENDPOINT" ] && [ "$OTLP_INSTALL_SDK" = "true" ]; then
      pip install --no-cache-dir opentelemetry-sdk opentelemetry-exporter-otlp
    fi
    exec python -m vllm.entrypoints.openai.api_server \
      --model $MODEL \
      --api-key $VLLM_API_KEY \
      --otlp-traces-endpoint $OTLP_TRACES_ENDPOINT \
      --served-model-name $MODEL \
      ...
```

The `pip install` is optional (`otlp.installSdk`) and only runs on
first-startup — cached on subsequent restarts.

### Recreate strategy

`strategy.type: Recreate` — the old pod must fully release the GPU before
the new one starts, or CUDA will fail with "device already in use". Rolling
update requires 2 GPUs.

Trade-off: brief downtime during rollouts. Mitigated by the PostSync gate
(which fires *after* the new pod is up) and by clients being expected to
retry idempotent requests.

### PreStop hook

```yaml
lifecycle:
  preStop:
    exec:
      command: ["/bin/sh", "-c", "sleep 15"]
```

15-second sleep before SIGTERM. During this window:

- Endpoints controller removes the pod from the Service.
- Envoy Gateway's endpoint-discovery picks up the removal.
- EPP stops routing to the pod.
- New requests go elsewhere (or 503 if this is the only pod).
- SIGTERM fires; vLLM completes in-flight requests within
  `terminationGracePeriodSeconds`.

## `templates/pvc.yaml`

Boring but critical: 100Gi RWO PVC for the HF cache. Without it, every pod
restart re-downloads ~16 GB from Hugging Face (rate-limited by them, slow
for you, breaks the startup probe).

## `templates/priorityclass.yaml`

Cluster-scoped `PriorityClass gpu-inference` with `value: 1000000`. Created
by the chart, but note: cluster-scoped resources don't uninstall with
`helm uninstall`. Removing the chart leaves the PriorityClass behind (fine;
harmless).

## `templates/networkpolicy.yaml`

Two policies:

1. **`llama-default-deny`** — matches all pods in the namespace, denies all
   ingress and egress.
2. **`llama-allow-vllm`** — matches the vLLM pod, allows:
   - **Ingress**: namespace selectors for `envoy-gateway-system`,
     `monitoring`, `argo` on port 8000.
   - **Egress**: DNS to kube-system, TCP 8200 to Vault, OTLP (4317/4318) to
     `monitoring`, HTTPS to 0.0.0.0/0 excluding private CIDRs
     (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).

The egress exclusion of private CIDRs is deliberate: the vLLM pod needs
to reach `huggingface.co` for weights, but you don't want a compromised
pod to pivot into `kubernetes.default.svc:443` or another internal service.

## `templates/servicemonitor.yaml`

Standard Prometheus Operator `ServiceMonitor` with `release: kps` label
(matched by the kube-prometheus-stack's `serviceMonitorSelector`). Scrapes
port `http` (8000) at path `/metrics` every 15 seconds. Metrics all carry
the `vllm:` prefix.

## `templates/rollout-gate-job.yaml`

An ArgoCD `PostSync` hook — runs after every sync completes successfully.
The Job:

1. Waits for the vLLM service to be ready (`kubectl wait` on Deployment).
2. Clones `vllm-project/vllm` at `v0.9.2` (SHA-pinned).
3. Installs `pandas Pillow datasets`.
4. Runs `vllm bench serve` with:
   ```
   --backend openai-chat
   --model $MODEL
   --dataset-name random --random-input-len 512 --random-output-len 128
   --num-prompts 30 --request-rate 2
   --percentile-metrics ttft,tpot,itl,e2el --metric-percentiles 50,90,95,99
   --save-result /tmp/result.json
   ```
5. Parses `result.json` with `jq` and checks:
   - `ttft_p95_ms/1000 <= maxP95TtftSeconds`
   - `errors / (successes + errors) <= maxErrorRate`
6. Exits non-zero if either fails — ArgoCD marks the Application degraded.

`backoffLimit: 0` — fail fast, don't retry. `ttlSecondsAfterFinished: 300`
— clean up after 5 min.

## Helm-unittest tests

`charts/llama-8b/tests/` uses [`helm-unittest`](https://github.com/helm-unittest/helm-unittest)
plugin. Tests assert:

- Deployment has `runtimeClassName: nvidia`.
- SHM is a `Memory` emptyDir with the correct size.
- PVC is 100Gi RWO with `local-path`.
- PriorityClass value is 1000000.
- NetworkPolicy has the expected `from`/`to` selectors.
- Rollout gate Job has `backoffLimit: 0`.

Run locally: `helm unittest charts/llama-8b`.

## Extending / operating

- **Swap the model**: change `model:` and `image:` in `values.yaml`. Bump
  the PVC size if the new model is bigger. Update `evals/prompts-configmap.yaml`
  if the prompt formatting changes (chat template).
- **Enable prefix caching**: set `enablePrefixCaching: true` after
  upgrading to vLLM ≥ v0.8.
- **Add tensor parallelism**: set `gpu: 2`, add
  `--tensor-parallel-size 2` to the vLLM args in the Deployment template.
- **Longer context**: adjust `--max-model-len` (chart doesn't expose it —
  templatize if you need to change often), plus grow `resources.limits.memory`.
- **Tighter rollout gate**: lower `rolloutGate.maxP95TtftSeconds` once
  you know your baseline. Raise `numPrompts` for stronger statistical
  signal.
- **Custom sampling parameters**: add
  `--guided-decoding-backend outlines` etc. to the args in the Deployment
  template.

## Related docs

- Kyverno enforcement of pod shape: [`11-kyverno-policies.md`](11-kyverno-policies.md)
- Endpoint Picker (EPP): [`07-inference-extension-epp.md`](07-inference-extension-epp.md)
- Autoscaling: [`09-keda-autoscaling.md`](09-keda-autoscaling.md)
- vLLM dashboard: [`13-dashboards.md`](13-dashboards.md)
- SLO alerts: [`14-alerts.md`](14-alerts.md)
