# 01 — Architecture

## What this repo is

A single-command, GitOps-driven **Brev launchable** that stands up a production-grade
vLLM inference platform on one GPU node. The main design goal is not throughput —
it is **inference reliability**: predictable latency, safe rollouts, saturation
signals, and the ability to keep a single expensive GPU serving traffic without
starving, evicting, or overwhelming it.

## The stack, one line each

| Layer | Component | Why it's here |
|-------|-----------|---------------|
| Cluster | k3s / kind | Single-node target; kind for CI e2e |
| GitOps | **ArgoCD** v2.13.1 | Every manifest deployed via app-of-apps + sync waves |
| Ingress | **Envoy Gateway** v1.3.2 + Gateway API v1.2.1 | HTTP entrypoint, TLS termination, rate limit |
| Smart routing | **Gateway API Inference Extension** (EPP) v1.5.0 | KV-cache-aware endpoint picking via gRPC ext_proc |
| Inference | **vLLM** v0.7.3 serving `meta-llama/Meta-Llama-3-8B-Instruct` | OpenAI-compatible API |
| GPU | **NVIDIA GPU Operator** v24.9.0 + DCGM exporter | Device plugin + hardware telemetry |
| Autoscaling | **KEDA** v2.15.2 | Prometheus-driven `ScaledObject` on queue depth |
| Policy | **Kyverno** v3.3.0 | 8 `ClusterPolicy` guardrails (runtime, resources, SHM, image signature) |
| Secrets | **HashiCorp Vault** v0.29.1 (dev) + **ESO** v0.10.4 | HF token, vLLM API key, GitHub creds |
| Metrics | **kube-prometheus-stack** 66.3.1 | Prometheus, Alertmanager, Grafana, node/kube-state exporters |
| Logs | **Loki** 6.21.0 (single binary, 7d) | Fed by OTel Collector |
| Traces | **Tempo** 1.16.0 (24h) | Fed by OTel Collector, links to logs and metrics |
| Collector | **OTel Collector** 0.108.0 (DaemonSet) | vLLM OTLP → Tempo, node/vLLM Prometheus → remote-write |
| Push-based | **Prometheus Pushgateway** 2.15.0 | Batch jobs (evals, benchmarks) publish metrics here |
| Batch jobs | **Argo Workflows** 0.45.0 | Nightly benchmark suite, 6-hourly evaluation |
| CI | **GitHub Actions** | yamllint, helm-unittest, kubeconform, Kyverno CLI, pytest, kind e2e |

## Namespaces

```
argocd                   ArgoCD control plane
envoy-gateway-system     Envoy Gateway controller + data plane + Gateway "public"
gpu-operator             NVIDIA device plugin + DCGM exporter
vault                    HashiCorp Vault (dev mode)
external-secrets         External Secrets Operator + vault-token bootstrap secret
kyverno                  Kyverno controllers + ClusterPolicies
keda                     KEDA operator + metrics-server
monitoring               Prometheus, Grafana, Alertmanager, Loki, Tempo, OTel, Pushgateway
argo                     Argo Workflows + eval/loadtest WorkflowTemplates
llama                    vLLM pod, EPP, InferencePool, HPA-adjacent CRDs
```

## Request path: `POST /v1/chat/completions`

The interesting part of this platform is **not** vLLM itself — it's the request
path that makes vLLM *reliable*. Full sequence:

1. **Client** → `Gateway public :8080/v1/chat/completions` with `Authorization: Bearer <api-key>`.
2. **HTTPRoute** `vllm` matches the `/v1` prefix.
3. **BackendTrafficPolicy** `vllm-ratelimit` enforces 60 req/min — the coarse
   safety net before any expensive work happens.
4. **EnvoyExtensionPolicy** `vllm-epp` calls the **EPP** (Endpoint Picker) over
   gRPC ext_proc. EPP has been scraping vLLM's `/metrics` every second and knows:
   - `vllm:num_requests_waiting` — queue depth per pod
   - `vllm:gpu_cache_usage_perc` — KV-cache pressure per pod
   - `vllm:num_requests_running` — active batch size per pod

   EPP returns an `x-gateway-destination-endpoint` header — the pod IP to route to.
5. **NetworkPolicy** on the `llama` namespace allows only Envoy Gateway → vLLM :8000.
6. **vLLM** validates `--api-key`, admits the request, and either:
   - **Prefills** immediately if KV cache has room, or
   - **Queues** if `num_requests_waiting > 0` (`num_scheduler_steps` decides preemption).
7. Response streams back (OpenAI-compatible SSE). Traces go OTLP → Tempo,
   metrics go /metrics → Prometheus, logs go stdout → Loki via OTel.

The Envoy Gateway also proxies `/argocd`, `/grafana`, and `/argo` to the
respective control-plane UIs — reachable through the same `:8080` port.

## Data flow: telemetry

- **Metrics** — Prometheus scrapes:
  - vLLM `:8000/metrics` via `ServiceMonitor` (15s)
  - Envoy proxy `:19001/stats/prometheus` via `PodMonitor`
  - DCGM `:9400/metrics` via GPU Operator's `ServiceMonitor`
  - EPP `:9090/metrics`
  - Pushgateway `:9091/metrics` (evals, loadtests write here)
  - Node exporter, kube-state-metrics, Alertmanager, Grafana itself
- **Traces** — vLLM exports OTLP HTTP → OTel Collector → Tempo. `trace_id` is
  added to Loki logs via Grafana derived fields (jump straight from log to trace).
- **Logs** — OTel Collector DaemonSet tails pod stdout, attaches `k8sattributes`,
  and pushes to Loki gateway.
- **Alerts** — Prometheus rules from `alerts/` (vllm-slo, gpu-health, model-quality)
  → Alertmanager. Alertmanager routes to whatever receiver you configure (default
  install ships a `null` receiver — plug Slack/PagerDuty in `apps/kube-prometheus-stack.yaml`).

## GitOps flow: how a manifest gets deployed

1. **Root Application** (`bootstrap/root-app.yaml`) watches `apps/`.
2. Each file in `apps/*.yaml` is an ArgoCD `Application` with a `sync-wave`
   annotation.
3. ArgoCD applies waves in order: `-6` (Gateway API CRDs) → `-4` (Envoy Gateway)
   → `-3` (Inference Extension CRDs) → `0` (foundational infra) → `3` (KEDA,
   Kyverno) → `5` (observability, ESO, alerts, dashboards) → `7` (Kyverno
   policies) → `10` (vLLM) → `11` (HTTPRoutes, EPP) → `12` (evals) → `20`
   (loadtests). See [`03-bootstrap-and-gitops.md`](03-bootstrap-and-gitops.md)
   for the full table.
4. Every Application has `automated.prune=true` and `selfHeal=true` — drift is
   corrected automatically.

## Why this design (short version)

- **GitOps over kubectl** — every change is a PR, auditable, reversible.
- **Envoy Gateway over LB annotations** — Gateway API is upstream K8s SIG-Network,
  cleanly extensible via `EnvoyExtensionPolicy` for ext_proc (that's how EPP
  plugs in without touching Envoy config).
- **EPP over round-robin** — LLM inference is stateful (KV cache, batch fill).
  Round-robin can send a request to a pod whose cache is 99% full and preempt
  active users. EPP routes to the pod that can serve *this* request without
  eviction.
- **KEDA over HPA** — vLLM's most useful signal is `vllm:num_requests_waiting`,
  not CPU. KEDA reads it straight from Prometheus.
- **Kyverno over PSA** — Pod Security Admission can't mutate `runtimeClassName`
  or require `/dev/shm`. Kyverno can, and it produces PolicyReports.
- **Vault + ESO over Sealed Secrets** — Vault is the production endpoint;
  ESO abstracts it. Dev mode here, swap the `ClusterSecretStore` for prod later.

## Diagrams

Mermaid source lives in [`images/`](../images/):

- [`k8s-infrastructure.mmd`](../images/k8s-infrastructure.mmd) — full namespace/pod topology (rendered inline in [`../README.md`](../README.md))
- [`networking.mmd`](../images/networking.mmd) — NetworkPolicy topology (rendered inline in [`../README.md`](../README.md))
- [`inference-request-path.mmd`](../images/inference-request-path.mmd) — sequence for `POST /v1/chat/completions` (rendered inline in [`07-inference-extension-epp.md`](07-inference-extension-epp.md#inference-request-path))
- [`observability-data-flow.mmd`](../images/observability-data-flow.mmd) — telemetry pipeline (source only; walked through in [`12-observability.md`](12-observability.md))

## Next

Read [`02-reliability.md`](02-reliability.md) — the core design story: what
"inference reliability" actually means and how each of these components
contributes.
