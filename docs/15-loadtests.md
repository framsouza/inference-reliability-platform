# 15 — Load testing

Automated performance testing runs on the cluster itself, using Argo
Workflows. The engine is vLLM's own `benchmark_serving.py`, so results
match how the vLLM project measures performance.

Every scenario pushes metrics to **Pushgateway** → Prometheus → Grafana
(`dashboards/loadtests.yaml`) → alerts on regression.

## Files

```
loadtests/argo/
├── workflow-template.yaml           WorkflowTemplate vllm-bench — one scenario
├── suite-workflow-template.yaml     WorkflowTemplate vllm-bench-suite — 4-scenario DAG
├── suite-cronworkflow.yaml          CronWorkflow — nightly suite run
└── cli-pod.yaml                     Interactive Pod for ad-hoc runs
apps/loadtests.yaml                  ArgoCD Application → loadtests/argo/ (wave 20)
```

## Anatomy of a run

Under the hood every scenario does the same thing:

1. Start a Pod with `vllm/vllm-openai:v0.9.2` (the benchmark image).
2. `apt-get install git jq`.
3. `git clone vllm-project/vllm --branch v0.9.2`.
4. `pip install pandas Pillow datasets`.
5. Run `benchmark_serving.py` against
   `http://llama-llama-8b.llama.svc.cluster.local:8000/v1/chat/completions`
   with the OpenAI backend.
6. Parse the resulting `result.json` and push a bundle of Prometheus
   metrics to Pushgateway keyed by `job=vllm-bench, scenario=<name>`.

The image is pinned at **v0.9.2** even though vLLM serves on v0.7.3 —
`benchmark_serving.py` is forward-compatible with older servers and has
richer output (per-percentile TTFT, per-request timings, error
breakdown) in newer versions.

## The primary WorkflowTemplate: `vllm-bench`

`loadtests/argo/workflow-template.yaml`. Parameters:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `scenario` | `baseline` | Label on all pushed metrics. |
| `model` | `meta-llama/Meta-Llama-3-8B-Instruct` | Passed as `--model`; must match served model. |
| `base-url` | `http://llama-llama-8b.llama.svc.cluster.local:8000` | vLLM Service URL. |
| `dataset` | `random` | `--dataset-name`. Also supports `sharegpt`, `sonnet`, `hf`. |
| `num-prompts` | `200` | How many requests. |
| `request-rate` | `5` | req/s. `inf` = as fast as possible. |
| `input-len` | `512` | For random dataset — input tokens. |
| `output-len` | `128` | For random dataset — output tokens. |
| `seed` | `42` | Reproducibility. |
| `image` | `vllm/vllm-openai:v0.9.2` | Benchmark image. |
| `pushgateway-url` | in-cluster Pushgateway | Where to send metrics. |

Env from secrets:

- `HUGGING_FACE_HUB_TOKEN` from `hf-token` — needed if the dataset
  requires HF hub auth.
- `OPENAI_API_KEY` from `vllm-api-key` — the benchmark auths against vLLM
  as an OpenAI client. This one has bit users before — a "authenticated
  endpoint returns 401" error usually means this env var isn't set.

Resources: 500m / 1Gi request, 2 CPU / 4Gi limit. Kyverno's
`validate-argo-pod-limits` enforces the limits.

### Pushed metrics

For each scenario:

| Metric | Meaning |
|--------|---------|
| `loadtest_ttft_p95_seconds` | TTFT p95 |
| `loadtest_ttft_p99_seconds` | TTFT p99 |
| `loadtest_ttft_mean_seconds` | Mean TTFT |
| `loadtest_e2e_p95_seconds` | End-to-end p95 |
| `loadtest_e2e_p99_seconds` | End-to-end p99 |
| `loadtest_e2e_mean_seconds` | Mean E2E |
| `loadtest_output_throughput_tokens_per_sec` | Output tok/s |
| `loadtest_request_throughput_per_sec` | Requests/s |
| `loadtest_completed` | Requests that completed |
| `loadtest_prompts_total` | Total prompts issued |
| `loadtest_errors` | prompts − completed |
| `loadtest_error_rate` | errors / prompts |
| `loadtest_duration_seconds` | Wall clock |
| `loadtest_last_run_timestamp` | Unix time of run end |

All labeled `scenario=<name>, model=<name>`.

## The suite: `vllm-bench-suite`

`loadtests/argo/suite-workflow-template.yaml` — a DAG of four scenarios
that run in sequence:

| Scenario | Prompts | Rate | Input tok | Output tok | Purpose |
|----------|---------|------|-----------|------------|---------|
| **warmup** | 20 | 2/s | 128 | 32 | Fill KV cache, JIT CUDA graphs |
| **baseline** | 200 | 5/s | 512 | 128 | Steady-state performance |
| **burst** | 500 | ∞ | 512 | 128 | Stress test — batch fill under saturation |
| **long-context** | 50 | 2/s | 4096 | 128 | Starvation test — long prompts pin KV pages |

They run **sequentially** (`dependencies:`) so they don't compete for
the single GPU. Total wall clock: ~15-25 minutes.

### Why sequence, not parallel

You want scenarios to run against a **known state**. Warmup fills the KV
cache from cold. Baseline measures a healthy pod. Burst deliberately
overloads it. Long-context tests starvation.

Running them in parallel on one GPU would just meas "how do 4 clients
compete" — a different question, and less useful.

## Nightly schedule

`loadtests/argo/suite-cronworkflow.yaml`:

```yaml
schedule: "23 2 * * *"          # 02:23 UTC every day
concurrencyPolicy: Forbid       # skip if previous run still going
startingDeadlineSeconds: 900
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 5
workflowSpec:
  workflowTemplateRef:
    name: vllm-bench-suite
```

02:23 UTC was chosen to avoid clashes with typical model quality eval
runs (`:17 */6 * * *`) and typical human working hours.

## Ad-hoc / interactive runs

`loadtests/argo/cli-pod.yaml` is a Pod running `vllm/vllm-openai:v0.9.2`
with `sleep infinity`. To run a one-off benchmark:

```bash
kubectl -n argo exec -it vllm-bench-cli -- bash
# inside the pod:
git clone --depth 1 --branch v0.9.2 https://github.com/vllm-project/vllm.git
cd vllm/benchmarks
python3 benchmark_serving.py \
  --backend openai-chat \
  --model meta-llama/Meta-Llama-3-8B-Instruct \
  --base-url http://llama-llama-8b.llama:8000 \
  --endpoint /v1/chat/completions \
  --dataset-name random --num-prompts 100 --request-rate 3 \
  --random-input-len 1024 --random-output-len 256
```

## Running a scenario from Argo UI

1. Go to `https://<host>:8080/argo`.
2. Templates → `vllm-bench` → Submit.
3. Override any parameters (e.g. `num-prompts=1000`).
4. Watch pod logs from the UI.

## Adapting for your workload

The nightly suite is a good *shape* — steady state, burst, long tail —
but the exact numbers should match how *your* clients actually use the
model.

### 1. Match the input/output distribution

If your production workload is retrieval-augmented generation with 4k
tokens of context and 200-token answers, `--random-input-len 4096
--random-output-len 200`. Don't benchmark 512/128 and be surprised in
production.

Use **real prompts** where possible: `--dataset-name sharegpt
--dataset-path sharegpt.json` for chat, or `--dataset-name hf
--hf-name <your-dataset>` for a custom dataset.

### 2. Match the arrival rate

- **Constant rate** — `--request-rate 5` fires at a Poisson rate of 5/s.
- **Bursty** — pair `--request-rate inf` with `--burstiness 1.0` for
  Poisson bursts.
- **Session-based** — set `--num-prompts` low and repeat the workflow to
  simulate short-session clients.

### 3. Match the concurrency ceiling

Real clients have `--max-concurrent-requests`. The benchmark script's
`--request-rate` doesn't cap concurrency — set `--max-concurrency N` if
your users would.

### 4. Match the priorities

Add multiple concurrent workflow runs at different rates to mimic mixed
traffic (e.g. one interactive workload + one batch workload).

### 5. Pick the right SLO targets

Once you know what your workload looks like:

- Run the tuned suite in dev.
- Read the p95/p99 TTFT, E2E from the dashboard.
- Set `alerts/vllm-slo.yaml` thresholds to **1.5×** the observed p95 in a
  healthy state. That's tight enough to catch regressions without
  flapping on normal variance.

### 6. Compare across models / versions

The `scenario` label distinguishes runs. Add a second dimension via a
new label — either edit the workflow template to set `model=` per run,
or push to a different Pushgateway `job` name and filter in Grafana.

### 7. Long-running soak tests

For catch-runs of subtle regressions (memory leak, cache fragmentation),
use a very long baseline scenario:

```
num-prompts: 100000
request-rate: 5
```

Runs for ~5.5h. Set `activeDeadlineSeconds` in the workflow to guard
against runaway jobs.

## Interpreting results

Reading the `loadtests` Grafana dashboard:

- **TTFT p95 stable across scenarios** → healthy. TTFT should be similar
  in `baseline` and `long-context` (long context slows prefill, not
  TTFT for cached prefills).
- **TTFT p95 spikes in `burst`** → expected. Burst deliberately
  saturates.
- **TTFT p95 spikes in `long-context` but not `baseline`** → the pod
  isn't handling long prompts efficiently. Check
  `--max-model-len` in vLLM args; check KV cache usage during that
  scenario.
- **`e2e_p95_seconds > 4× ttft_p95_seconds`** → decode is slow relative
  to prefill. Check GPU utilization during decode; could indicate memory
  bandwidth bottleneck.
- **`error_rate > 0.01`** → real errors. Grep pod logs from that
  timeframe:
  ```logql
  {k8s_namespace_name="llama"} |= "error" | line_format "{{.}}"
  ```

## Debugging

- **Workflow stuck** — `kubectl -n argo get wf`. Look for `Message:` in
  the phase — often a Kyverno rejection or an image pull failure.
- **Benchmark returns 401** — the `OPENAI_API_KEY` isn't matching what
  vLLM expects. Verify with:
  ```bash
  kubectl -n argo get secret vllm-api-key -o jsonpath='{.data.token}' | base64 -d
  kubectl -n llama get secret vllm-api-key -o jsonpath='{.data.token}' | base64 -d
  # These must match. If they don't, re-run bootstrap/seed-vault.sh.
  ```
- **`result.json` missing fields** — you upgraded `image:` to a newer
  vLLM tag; the JSON schema changed. Update the `jq` extractors in the
  script.
- **Pushgateway 200 but Grafana shows nothing** — Prometheus scrapes
  Pushgateway; check `up{job="pushgateway"}` in Prometheus. If Prometheus
  can't reach Pushgateway, check the `ServiceMonitor` label.

## Related docs

- Loadtests dashboard: [`13-dashboards.md`](13-dashboards.md)
- Pushgateway: [`12-observability.md`](12-observability.md)
- Alerts on regression: [`14-alerts.md`](14-alerts.md)
- Rollout gate (pre-flight benchmark): [`06-inference-vllm.md`](06-inference-vllm.md)
