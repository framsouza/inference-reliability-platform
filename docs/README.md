# Documentation

Deep dives for each component of `inference-reliability-platform` — a Brev-launchable, single-GPU
vLLM inference platform on Kubernetes engineered for **inference reliability**.

The root [`README.md`](../README.md) is the fast path (what it is, how to launch,
how to send a request). Everything else lives here.

## Component docs

| Doc | What it covers |
|-----|----------------|
| [**01 — Architecture**](01-architecture.md) | Big-picture: namespaces, request path, data flow, why every piece exists |
| [**02 — Reliability model**](02-reliability.md) | **The core of the repo.** How every layer contributes to inference reliability — SLOs, error budgets, KV-cache pressure, preemption, graceful drain, priority, GPU health |
| [**03 — Bootstrap & GitOps (ArgoCD)**](03-bootstrap-and-gitops.md) | `bootstrap/install.sh`, root-app, sync waves, app-of-apps, how to add a new app |
| [**04 — Kubernetes cluster**](04-kubernetes.md) | Cluster assumptions, storage class, RuntimeClass, priority classes, node layout |
| [**05 — GPU Operator & DCGM**](05-gpu-operator.md) | NVIDIA GPU Operator config, DCGM exporter, XID/ECC alerts, `/dev/shm` and RuntimeClass gotchas |
| [**06 — Inference stack (vLLM Helm chart)**](06-inference-vllm.md) | `charts/llama-8b` — every value, probe, PVC, priority class, network policy, rollout gate |
| [**07 — Gateway API Inference Extension (EPP)**](07-inference-extension-epp.md) | `InferencePool`, endpoint picker, KV-cache-aware routing, RBAC, gRPC ext_proc wiring |
| [**08 — Gateway (Envoy Gateway)**](08-gateway-envoy.md) | GatewayClass, listeners, EnvoyProxy telemetry, PodMonitor, HTTPRoutes, rate-limit policy, ext_proc extension policy |
| [**09 — KEDA autoscaling**](09-keda-autoscaling.md) | The ScaledObject, Prometheus trigger query, activation, why min/max is 1 today, how to scale on multi-GPU |
| [**10 — Secrets (ESO + Vault)**](10-secrets-eso-vault.md) | ClusterSecretStore, ExternalSecrets, seeding Vault, rotating tokens, moving to prod Vault |
| [**11 — Kyverno policies**](11-kyverno-policies.md) | Every ClusterPolicy — what it enforces, why it exists, mutate vs. validate, audit vs. enforce |
| [**12 — Observability stack**](12-observability.md) | Prometheus, Grafana, Loki, Tempo, OTel collector, Alertmanager — wiring and retention |
| [**13 — Dashboards**](13-dashboards.md) | Each Grafana dashboard: what it shows, which metrics, when to use it (vLLM, GPU, gateway, cost, loadtests, model quality) |
| [**14 — Alerts**](14-alerts.md) | Every Prometheus alert with condition, severity, runbook link, and error-budget math |
| [**15 — Load testing**](15-loadtests.md) | `vllm bench serve` wrapper, Argo `WorkflowTemplate`, suite DAG, nightly `CronWorkflow`, Pushgateway metrics, **how to adapt for your workload** |
| [**16 — Model quality evals**](16-evals.md) | `evals/` — prompt set, evaluator, Pushgateway metrics, dashboard, alerts, adding new prompts |
| [**17 — CI/CD (GitHub Actions)**](17-ci-cd.md) | `ci.yml` (lint/test/render), `e2e.yml` (kind cluster), how to add a check |
| [**18 — Operations runbook**](18-operations.md) | Day-2: rollouts, rollbacks, upgrades, incident triage per alert, GPU quarantine, cost levers |
| [**19 — Extending the platform**](19-extending.md) | Add another model, another route, another dashboard, another eval, swap Llama for Mixtral, run on 2+ GPUs |
| [**20 — Making inference reliable (design notes)**](20-making-inference-reliable.md) | Beyond this repo: what else matters — request shaping, canary, saturation, capacity planning, chaos, safety, evaluation cadence |

## Screenshots

Live screenshots of the running platform live under
[`../images/screenshots/`](../images/screenshots/) and are embedded in
the relevant docs:

- **[03-bootstrap-and-gitops](03-bootstrap-and-gitops.md)** — ArgoCD Applications view
- **[13-dashboards](13-dashboards.md)** — every Grafana dashboard (vLLM, GPU, gateway, cost, loadtests, model quality)
- **[14-alerts](14-alerts.md)** — Grafana Alert rules view

## How to read these docs

- Read `01-architecture.md` and `02-reliability.md` first — they frame everything else.
- Each component doc is standalone: cite the files, explain the config, note the gotchas.
- Every doc ends with **"Extending / Operating"** — the levers you'll actually touch.
