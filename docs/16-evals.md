# 16 — Model quality evals

Load tests measure *performance*. Evals measure *quality* — did the model
give the right answer? A production LLM stack without continuous quality
measurement is flying blind: a silent tokenizer bug, a wrong chat template,
a bad dtype quantization can drop accuracy without any latency or error
symptom.

This repo runs a small eval every 6 hours and pushes results to
Prometheus.

## Files

```
evals/
├── prompts-configmap.yaml       12 prompts across 5 categories (JSONL)
├── script-configmap.yaml        evaluator.py embedded in a ConfigMap
├── evaluator.py                 The actual eval script (source of truth for the ConfigMap)
├── workflow-template.yaml       WorkflowTemplate model-quality-eval
├── cronworkflow.yaml            Every 6 hours schedule
└── tests/test_evaluator.py      Unit tests (pytest)
apps/evals.yaml                  ArgoCD Application → evals/ (wave 12)
```

## The prompt set

`evals/prompts-configmap.yaml` — 12 prompts across 5 categories:

| Category | Prompts | Example |
|----------|---------|---------|
| `factual` | 3 | "What is the capital of France? Answer in one word." → `(?i)paris` |
| `math` | 3 | "What is 15 * 23? Answer with only the number." → `\\b345\\b` |
| `code` | 2 | "Write a Python function called reverse_string..." → regex checking for `[::-1]` or `reversed` |
| `instruction` | 2 | "Reply with exactly the word HELLO in uppercase, nothing else." → `^\\s*HELLO\\s*\\.?\\s*$` |
| `reasoning` | 2 | "Alice has 3 brothers and 2 sisters. How many siblings does Alice's brother have?" → `\\b4\\b` |

Each prompt is:

```json
{
  "id": "factual-france",
  "category": "factual",
  "prompt": "What is the capital of France? Answer in one word.",
  "expected_regex": "(?i)paris"
}
```

**Design intent**:

- Small and cheap — full eval runs in ~1 minute.
- Regex-scored — no LLM-as-judge (deterministic, no external LLM
  dependency).
- Categories — regression in one category isolates the failure to a
  capability class.
- **Explicit output format instructions** — reduces false negatives from
  chatty responses. "Answer with only the number" makes "\b345\b"
  reliable.

This is not a benchmark set — it won't tell you if your model is
"better" than another. It's a **regression detector** — quick, cheap,
runs constantly, and catches "someone broke it".

## The evaluator

`evals/evaluator.py`. Logic:

```python
for prompt in prompts:
    response = call_vllm(prompt.prompt)
    passed = re.search(prompt.expected_regex, response) is not None
    results.append(EvalResult(
        category=prompt.category,
        passed=passed,
        latency=response_time,
        tokens=token_count,
    ))

# Aggregate
pass_rate = passed_count / total_count
per_category_pass_rate = {cat: cat_passed / cat_total for cat, ...}

# Push to Pushgateway
push_metrics({
    "model_eval_pass_rate": pass_rate,
    "model_eval_latency_seconds": ...,
    "model_eval_response_tokens": ...,
    "model_eval_last_run_timestamp": now,
    "model_eval_prompts_total": total_count,
}, labels={"category": ..., "model": ...})
```

Calls vLLM via the OpenAI chat completions API, using `OPENAI_API_KEY`
from the `vllm-api-key` Secret in the `argo` namespace.

## The WorkflowTemplate

`evals/workflow-template.yaml` — parameters:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `model` | `meta-llama/Meta-Llama-3-8B-Instruct` | Model name passed to `/v1/chat/completions`. |
| `vllm-url` | `http://llama-llama-8b.llama.svc.cluster.local:8000` | vLLM Service URL. |
| `pushgateway-url` | in-cluster | Where to push metrics. |
| `max-tokens` | `200` | vLLM `max_tokens`. |
| `temperature` | `0.1` | Near-deterministic; small temp for tie-breaking. |
| `min-pass-rate` | `0.4` | Below this, the eval Workflow exits non-zero. |
| `image` | `python:3.11-slim` | Slim Python container. |

Container:

- Image: `python:3.11-slim`
- Command: `pip install requests prometheus_client && python /scripts/evaluator.py`
- Mounts: `model-eval-prompts` ConfigMap at `/prompts`, `model-eval-script`
  ConfigMap at `/scripts`.
- Env: `VLLM_URL`, `MODEL`, `MAX_TOKENS`, `TEMPERATURE`, `PUSHGATEWAY_URL`,
  `VLLM_API_KEY` (from Secret).
- Resources: 200m/256Mi req, 1000m/512Mi limit.

## The schedule

`evals/cronworkflow.yaml`:

```yaml
schedule: "17 */6 * * *"        # 00:17, 06:17, 12:17, 18:17 UTC
concurrencyPolicy: Forbid
startingDeadlineSeconds: 600
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 5
```

Every 6 hours means 4 samples/day — enough for daily trend, sparse enough
to be cheap. The `:17` offset avoids top-of-hour Grafana/Prometheus
cluster scrape spikes.

## Pushed metrics

Under `job=model-eval, model=<name>`:

| Metric | Labels | Meaning |
|--------|--------|---------|
| `model_eval_pass_rate` | `model` | Overall pass rate (0-1) |
| `model_eval_pass_rate` | `model, category` | Per-category pass rate |
| `model_eval_latency_seconds` | `model, category, quantile` | Per-category p50/p95 latency |
| `model_eval_response_tokens` | `model, category` | Mean tokens in responses |
| `model_eval_last_run_timestamp` | `model` | Unix epoch of last run |
| `model_eval_prompts_total` | `model, category` | Total prompts run |

## Dashboards & alerts

- **Dashboard**: [`13-dashboards.md`](13-dashboards.md#model-qualityyaml---continuous-eval)
- **Alerts**: [`14-alerts.md`](14-alerts.md#model-qualityyaml)

## Unit tests

`evals/tests/test_evaluator.py` uses `pytest` and `requests-mock` to
test:

- Regex matching (positive and negative cases)
- API response parsing
- Metric formatting
- Pushgateway HTTP layer

Runs as part of CI (`.github/workflows/ci.yml`).

## Adding a new prompt

1. Edit `evals/prompts-configmap.yaml`:

   ```yaml
   {"id":"factual-boiling","category":"factual","prompt":"...","expected_regex":"..."}
   ```

2. Test the regex locally:

   ```bash
   python3 -c 'import re; print(bool(re.search("your-regex", "sample response")))'
   ```

3. Commit. ArgoCD applies the ConfigMap. The next CronWorkflow run picks
   it up.

**Tip**: regex-only scoring works best for constrained outputs. If you
need semantic evaluation:

- Add an LLM-as-judge step in the evaluator (calls a second, stronger
  model to grade).
- Use `evaluator.py`'s existing framework and just swap the scoring
  function.

## Adding a new category

- Update prompts with the new `category` value.
- The evaluator groups by category automatically — no code change
  needed.
- Add a corresponding `ModelQualityCategoryRegressed` alert filter if you
  want per-category paging.

## Adapting for your workload

The default 12 prompts are a smoke test. For your production use case:

1. **Sample your traffic**: pick 50-100 real prompts spanning your
   categories. Anonymize.
2. **Score with regex** where possible ("does the answer contain X?").
3. **Score with LLM-as-judge** where not — but budget the extra API
   calls.
4. **Set the pass-rate threshold** — run the eval once against known-good
   output to baseline; alert at ~10% below baseline.

## When quality alerts fire

`ModelQualityLowOverallPassRate` firing usually means one of:

- **Someone edited `values.yaml`** — `model:` or `image:` changed.
- **Prompt template changed** — the chat template used by tokenizer
  differs from the one prompts assume.
- **Dtype quantization** — swapped bf16 → int8 or fp8 without re-eval.
- **vLLM version bump** — new version changed sampling defaults.
- **Model actually degraded** — very rare, but possible after a
  `--served-model-name` change.

Diagnosis: check `model_eval_pass_rate{category=...}` — which category
regressed tells you the shape of the failure.

## Related docs

- Model quality dashboard: [`13-dashboards.md`](13-dashboards.md)
- Model quality alerts: [`14-alerts.md`](14-alerts.md)
- Pushgateway wiring: [`12-observability.md`](12-observability.md)
- CI (evaluator.py tests): [`17-ci-cd.md`](17-ci-cd.md)
