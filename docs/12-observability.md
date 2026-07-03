# 12 — Observability stack

**Metrics, logs, and traces** for the platform and the model. All three
signal types are collected by an OpenTelemetry Collector DaemonSet and
routed into their respective backends: **Prometheus**, **Loki**,
**Tempo**. **Grafana** unifies the query experience; **Alertmanager**
routes alerts.

There's also a **Pushgateway** for batch-job metrics (evals, load tests)
that can't be scraped.

## Files

```
apps/kube-prometheus-stack.yaml     Prometheus + Grafana + Alertmanager + kube-state (wave 0)
apps/loki.yaml                       Loki singleBinary v6.21.0 (wave 0)
apps/tempo.yaml                      Tempo v1.16.0 (wave 0)
apps/otel-collector.yaml             OTel Collector DaemonSet 0.108.0 (wave 5)
apps/pushgateway.yaml                Pushgateway v2.15.0 (wave 5)
apps/alerts.yaml                     ArgoCD Application → alerts/ (wave 5)
apps/dashboards.yaml                 ArgoCD Application → dashboards/ (wave 5)
```

## Metrics — Prometheus

`kube-prometheus-stack` v66.3.1 bundles:

- **Prometheus** — the scraper + TSDB. Retention **6 hours** (short by
  design; long-term storage goes to remote-write if you enable it).
- **Alertmanager** — routes alerts to receivers. Default install has a
  null receiver; plug Slack/PagerDuty into `apps/kube-prometheus-stack.yaml`.
- **Grafana** — admin/admin login (change it). Auto-discovers dashboards
  and datasources across all namespaces via the sidecar.
- **kube-state-metrics** — exposes Kubernetes object state (deployments,
  pods, HPAs, etc.).
- **node-exporter** — host-level CPU, memory, disk, network.
- **Prometheus Operator** — runs the whole thing.

### Scrape config

The stack is configured to scrape any ServiceMonitor or PodMonitor with
label `release: kps`:

```yaml
prometheus:
  prometheusSpec:
    serviceMonitorSelector:
      matchLabels:
        release: kps
    podMonitorSelector:
      matchLabels:
        release: kps
    serviceMonitorNamespaceSelector: {}    # scrape all namespaces
    podMonitorNamespaceSelector: {}
```

Every ServiceMonitor/PodMonitor in this repo carries `release: kps`; the
Kyverno `validate-service-monitor-release` policy enforces it.

### Retention & remote-write

**6 hours** is deliberately short — this is a launchable, not a long-term
observability store. Metrics that need durability go to:

- **Pushgateway** (evals, load tests) — Prometheus keeps them in its
  scraper for the 6h window; connect to a durable remote-write endpoint
  if you need history.
- **remote-write** — enable in `apps/kube-prometheus-stack.yaml`:
  ```yaml
  prometheus:
    prometheusSpec:
      remoteWrite:
        - url: https://prometheus-us-central1.grafana.net/api/prom/push
          basicAuth:
            username: {name: rw-creds, key: user}
            password: {name: rw-creds, key: token}
  ```

### Alertmanager routing

Default install has a `null` receiver — alerts fire but go nowhere. To
send to Slack:

```yaml
alertmanager:
  config:
    receivers:
      - name: slack
        slack_configs:
          - api_url: 'https://hooks.slack.com/services/…'
            channel: '#llm-oncall'
    route:
      receiver: slack
      group_by: ['alertname', 'severity']
      group_wait: 10s
      group_interval: 5m
      repeat_interval: 3h
```

The alert rules themselves are in `alerts/` — see [`14-alerts.md`](14-alerts.md).

## Logs — Loki

`loki` v6.21.0 in `singleBinary` mode:

- 1 replica.
- Filesystem storage backend (10Gi PVC).
- Schema v13 with TSDB indexing.
- Retention **168 hours (7 days)**.
- Compactor deletes at a 2h delay.

Log ingestion: OTel Collector's Loki exporter pushes to
`loki-gateway.monitoring`. Every log line carries the `k8sattributes`
processor's labels: `k8s.namespace.name`, `k8s.pod.name`,
`k8s.container.name`, `k8s.node.name`, etc. Plus derived fields for
`trace_id` (added by Grafana in the Loki datasource config).

Log query in Grafana:

```logql
{k8s_namespace_name="llama"} |= "preemption"
```

Or drill in on a single pod:

```logql
{k8s_pod_name="llama-llama-8b-abc123"} | json
```

## Traces — Tempo

`tempo` v1.16.0:

- Local storage, 10Gi PVC.
- Retention **24 hours**.
- OTLP receivers on gRPC 4317 and HTTP 4318.
- ServiceMonitor enabled with `release: kps`.

Tempo is fed by the OTel Collector. Every vLLM request emits a span
carrying:

- `http.method`, `http.url`, `http.status_code`
- `llm.request.model`
- `llm.request.max_tokens`
- Duration of prefill, decode
- Token counts (input, output)

Grafana's Tempo datasource is configured with a **serviceMap** and
`tracesToLogs` / `tracesToMetrics` so you can jump from a slow trace to
the Loki logs it produced and the Prometheus metrics for that pod.

## OpenTelemetry Collector

`otel-collector` v0.108.0 runs as a **DaemonSet** using the
`otel/opentelemetry-collector-contrib` image (the contrib distribution
has the Loki and k8sattributes exporters/processors we need).

Its pipeline:

```yaml
receivers:
  otlp:
    protocols:
      grpc: {endpoint: 0.0.0.0:4317}
      http: {endpoint: 0.0.0.0:4318}
  prometheus:
    config:
      scrape_configs:
        - job_name: vllm
          scrape_interval: 15s
          static_configs:
            - targets: [vllm-llama-8b.llama:8000]

processors:
  batch: {}
  memory_limiter: {check_interval: 1s, limit_mib: 400}
  k8sattributes:
    extract:
      metadata: [k8s.pod.name, k8s.namespace.name, k8s.node.name, ...]
  resource:
    attributes:
      - key: cluster
        value: brev
        action: insert

exporters:
  otlp/tempo:
    endpoint: tempo.monitoring:4317
    tls: {insecure: true}
  loki:
    endpoint: http://loki-gateway.monitoring/loki/api/v1/push
  prometheusremotewrite:
    endpoint: http://kps-kube-prometheus-stack-prometheus.monitoring:9090/api/v1/write

service:
  pipelines:
    traces:  {receivers: [otlp], processors: [memory_limiter, k8sattributes, batch], exporters: [otlp/tempo]}
    metrics: {receivers: [otlp, prometheus], processors: [memory_limiter, k8sattributes, batch], exporters: [prometheusremotewrite]}
    logs:    {receivers: [otlp], processors: [memory_limiter, k8sattributes, batch], exporters: [loki]}
```

Preset `logsCollection.enabled: true` scrapes pod stdout from the node's
container runtime and pushes to Loki.

### Why DaemonSet

- Node-local means low-latency ingestion — apps can send OTLP to
  `localhost:4318` without hopping through a cluster IP.
- Log tailing needs access to the container runtime socket, which is
  per-node.
- Resource-bounded per node — a runaway pod on one node doesn't OOM the
  collector on another.

## Pushgateway

`prometheus-pushgateway` v2.15.0. Used by batch jobs that don't have a
long-running HTTP endpoint for Prometheus to scrape:

- **`evals/evaluator.py`** — pushes `model_eval_pass_rate`,
  `model_eval_latency_seconds`, `model_eval_last_run_timestamp`.
- **`loadtests/argo/workflow-template.yaml`** — pushes
  `loadtest_ttft_p95_seconds`, `loadtest_output_throughput_tokens_per_sec`,
  etc.

Prometheus scrapes Pushgateway on 9091 every 15s. Metrics are
grouped by the `job` label the job sets; overwrite semantics mean each
run replaces the previous run's metrics for the same job.

**Gotcha**: Pushgateway retains metrics *forever* until explicitly
deleted. If a job dies mid-way and never pushes again, the stale metric
stays. Alerts on Pushgateway metrics should filter on freshness
(`last_seen_timestamp`).

## Grafana wiring

`kube-prometheus-stack` Grafana config:

```yaml
sidecar:
  dashboards:
    enabled: true
    label: grafana_dashboard
    labelValue: "1"
    searchNamespace: ALL
  datasources:
    enabled: true
    searchNamespace: ALL
additionalDataSources:
  - name: Loki
    type: loki
    url: http://loki-gateway.monitoring
    jsonData:
      maxLines: 1000
      derivedFields:
        - datasourceUid: tempo
          matcherRegex: "trace_id=(\\w+)"
          name: TraceID
          url: '$${__value.raw}'
  - name: Tempo
    type: tempo
    url: http://tempo.monitoring:3100
    jsonData:
      tracesToLogs: {datasourceUid: loki, tags: [k8s_pod_name]}
      tracesToMetrics: {datasourceUid: prometheus, tags: [k8s_pod_name]}
      serviceMap: {datasourceUid: prometheus}
```

- Any ConfigMap labeled `grafana_dashboard: "1"` gets auto-imported (that's
  how `dashboards/*.yaml` works).
- Loki and Tempo are pre-provisioned as datasources.
- `trace_id=abc` in a Loki log line becomes a clickable link to the Tempo
  trace.

## Reliability guarantees

- **Metrics durability** — Prometheus is 6h in-cluster. Anything longer
  needs remote-write to Grafana Cloud, Mimir, VictoriaMetrics, etc.
- **Logs durability** — Loki is 7d, filesystem. Multi-node needs S3
  or GCS backend.
- **Traces durability** — Tempo is 24h. Same story.
- **Backpressure** — OTel Collector's `memory_limiter` processor drops
  data on OOM. Traces are the first to go under pressure.

These are launchable-friendly defaults. Turn each one up in
`apps/*.yaml` for a real deployment.

## Debugging

- **Prometheus can't scrape**: check `kubectl -n monitoring get
  servicemonitor -A` — does it have `release: kps`?
- **No dashboards showing**: `kubectl -n monitoring logs -c
  grafana-sc-dashboard kps-grafana-<hash>` — sidecar logs.
- **Loki logs empty**: `kubectl -n monitoring logs
  ds/otel-collector` — receiver errors, exporter errors.
- **Tempo trace missing**: check the vLLM container has
  `--otlp-traces-endpoint` set; check OTel Collector received the trace
  (`otelcol_processor_batch_batch_send_size` metric).
- **Alertmanager silent**: check `kubectl -n monitoring get
  alertmanager alertmanager-kps -o yaml` — receiver config.

## Extending / operating

- **Longer retention** — bump `prometheus.prometheusSpec.retention: 6h`
  to `30d` and add a PVC big enough. Or enable remote-write to a hosted
  service.
- **Add a new metric** — expose a Prometheus-format endpoint on your
  workload; create a `ServiceMonitor` with `release: kps`; you're done.
- **Add a receiver** — Alertmanager config supports Slack, PagerDuty,
  Opsgenie, generic webhook. See `alertmanager.config` in
  `apps/kube-prometheus-stack.yaml`.
- **Custom exporter** — deploy alongside vLLM, expose `/metrics`, add a
  ServiceMonitor. Common: cost exporter, GPU allocation exporter, model
  metadata exporter.
- **High cardinality** — vLLM per-request labels can explode metric
  cardinality. Set `otel-collector`'s `attributes/filter` processor to
  drop chatty attributes before exporting.

## Related docs

- Dashboards: [`13-dashboards.md`](13-dashboards.md)
- Alerts: [`14-alerts.md`](14-alerts.md)
- Data flow diagram: [`../images/observability-data-flow.mmd`](../images/observability-data-flow.mmd)
