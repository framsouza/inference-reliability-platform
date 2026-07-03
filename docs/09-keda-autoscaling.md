# 09 — KEDA autoscaling

[KEDA](https://keda.sh) is a Kubernetes-native event-driven autoscaler. It
extends the HPA with **custom triggers** — including reading a Prometheus
query as the scaling signal. That's what this repo does: scale vLLM on
queue depth, not CPU.

## Files

```
apps/keda.yaml                                     Helm chart v2.15.2 (wave 3)
charts/llama-8b/templates/scaledobject.yaml        The ScaledObject
charts/llama-8b/values.yaml (autoscaling.*)        The knobs
```

## Why not HPA-on-CPU?

An LLM served with continuous batching (vLLM) uses **~100% GPU** whenever
the batch is non-empty. CPU utilization on the pod stays modest and
constant — the tokenizer, scheduler, and OpenAI HTTP layer aren't
compute-bound.

Consequences:

- HPA-on-CPU won't trigger when the model is overloaded.
- HPA-on-GPU is possible with `nvidia.com/gpu` custom metric but doesn't
  correlate with user-visible latency either — 100% GPU is fine as long
  as the batch is filled without preemption.

What actually matters is **queue depth** (`vllm:num_requests_waiting`) —
if requests are queuing, latency is spiking. KEDA reads this from
Prometheus and scales.

## The `ScaledObject`

Rendered from `charts/llama-8b/templates/scaledobject.yaml`:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: llama-llama-8b
  namespace: llama
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: llama-llama-8b
  pollingInterval: 30            # seconds between Prometheus queries
  cooldownPeriod: 300            # 5 min of idle before scale-in
  minReplicaCount: 1
  maxReplicaCount: 1             # single GPU today
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://kps-kube-prometheus-stack-prometheus.monitoring:9090
        metricName: vllm_load
        threshold: "1"
        activationThreshold: "0"
        query: |
          sum(vllm:num_requests_running + vllm:num_requests_waiting)
```

- **`threshold: 1`** — desired value of the metric *per replica*. KEDA
  computes `desiredReplicas = ceil(currentMetric / threshold)`.
- **`activationThreshold: 0`** — below this, KEDA scales to zero. `0`
  effectively means "never scale to zero" (a strictly-greater comparison).
- **`pollingInterval: 30`** — Prometheus query fires every 30s.
- **`cooldownPeriod: 300`** — after the metric drops below threshold,
  wait 5 minutes before scaling in. Prevents flapping during bursty
  traffic.
- **`minReplicaCount: 1`** / **`maxReplicaCount: 1`** — no actual scaling
  today because there is only one GPU. KEDA is still useful as a
  template — flip the max the moment you add hardware.

## Under the hood

KEDA-metrics-server registers as an `APIService` implementing the
`external.metrics.k8s.io` API. When the KEDA operator sees a
`ScaledObject`, it creates a normal `HorizontalPodAutoscaler` under the
hood pointing at that external metric. The HPA does the actual scaling.

`kubectl -n llama get hpa` — you'll see the KEDA-managed HPA there.

## Behavior when maxReplicas > 1

With a Deployment that can go from 1 → 3 replicas:

- Current metric: `sum` across all pods (Prometheus does the aggregation).
- Desired: `ceil(sum / 1)` = number of pods whose combined
  running+waiting equals sum.
- If 3 pods are running with 3 in-flight requests each, sum = 9 → desired
  = 9 pods. KEDA scales to `min(9, maxReplicas)`.

The exact formula is Prometheus-metric dependent — you may want to switch
to `avg` if you want each pod to converge on a target load rather than
scaling on total.

## Reliability implications

- **Reactivity** — 30s polling + 5-minute cooldown means the scale-out
  reaction takes at least one polling cycle. If your traffic is bursty
  in seconds, KEDA is too slow — mitigate with a warm pool
  (`minReplicaCount > 1`).
- **KV-cache pre-warming** — new vLLM pods take minutes to start (image
  pull, weight load, CUDA graph capture). Even after HPA scales, the new
  pod isn't serving traffic for ~90s. Consider using
  `podDisruptionBudget` + oversized `minReplicaCount` for latency-critical
  workloads.
- **Prometheus dependency** — if `kube-prometheus-stack` is unhealthy,
  KEDA can't scale. Ensure Prometheus itself has a high-priority class
  and doesn't share fate with the workload it monitors.

## Debugging

- `kubectl describe scaledobject llama-llama-8b -n llama` — shows the
  computed metric, last scale event, any errors resolving the trigger.
- `kubectl -n keda logs deploy/keda-operator` — controller logs.
- `kubectl -n keda logs deploy/keda-metrics-apiserver` — the external
  metrics API. Common failure: Prometheus query returns no data (metric
  name typo, missing ServiceMonitor label) → the metric resolves to `0`
  and KEDA reports "no traffic".
- `curl http://kps-kube-prometheus-stack-prometheus.monitoring:9090/api/v1/query?query=sum(vllm:num_requests_running+vllm:num_requests_waiting)`
  from a debug pod — sanity-check the query.

## Extending / operating

- **Multi-GPU scale-out** — bump `autoscaling.maxReplicas` in
  `charts/llama-8b/values.yaml`. Also ensure PVC access mode supports
  the replica count (RWO won't).
- **Different trigger** — replace the Prometheus query. Example: scale
  on Envoy request rate:
  ```
  sum(rate(envoy_cluster_upstream_rq_total{envoy_cluster_name=~"llama.*"}[1m]))
  ```
- **Cron-based scaling** — KEDA supports a `cron` trigger to pre-scale
  before predictable peak hours.
- **Scale on TPUs / other accelerators** — no code change; just point at
  a different Prometheus metric.
- **Scale down to zero** — set `activationThreshold: 1` and
  `minReplicaCount: 0`. Cold-start becomes multi-minute; only useful for
  dev environments.

## Related docs

- vLLM chart: [`06-inference-vllm.md`](06-inference-vllm.md)
- Observability (Prometheus): [`12-observability.md`](12-observability.md)
- Multi-node scaling: [`19-extending.md`](19-extending.md)
