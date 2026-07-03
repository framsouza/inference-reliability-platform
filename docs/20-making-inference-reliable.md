# 20 — Making AI inference reliable

> A design essay. Not a config reference. Read this if you're building
> your own inference platform or explaining to someone why LLM
> reliability is harder than "just autoscale it".

This repo implements a *slice* of inference reliability. The rest of the
picture is here — the design axes, the failure modes, the tradeoffs, and
the levers that are outside any single YAML manifest.

## 1. The seven reliability axes for LLM inference

Traditional web services have three axes: availability, latency, error
rate. LLM inference has seven. Every one of them is a separate design
decision.

### 1.1 Availability

Standard interpretation: **the server responds with a non-5xx to a
well-formed request.**

For LLMs specifically:

- A `200 OK` streaming a partially-generated response that then errors
  mid-stream is *not* available from the user's point of view.
- A `200` returning a truncated response (`finish_reason: length`
  hitting `max_tokens` prematurely) is degraded availability.
- A `503` returned by the endpoint picker because KV cache is saturated
  is availability failure — even if the response is technically valid.

**Measure**: fraction of requests that complete successfully with a
non-truncated response. Not HTTP 200 rate.

**This repo**: Envoy Gateway 5xx rate → `VLLMErrorBudgetBurnFast/Slow`
alerts. Aborts and length truncations tracked separately.

### 1.2 Latency

Two numbers to care about:

- **TTFT** (time-to-first-token) — how long until the user sees
  something.
- **E2E** (end-to-end) — how long until the response is complete.

TTFT is bounded by prefill (single expensive attention pass over the
prompt). E2E adds decode time (many cheap forward passes, one per
output token).

**Non-obvious**: high TTFT can mean *saturation* (queue) or *long
prompt* (long prefill). They look identical from Envoy but have
completely different fixes.

**Measure**: p50/p95/p99 of both, split by prompt length bucket.

**This repo**: histograms → `vllm.yaml` dashboard, alerts on p95.

### 1.3 Throughput

Tokens-per-second on the GPU. Direct proxy for revenue and unit economics.

Trade-off with latency: higher `--max-num-batched-tokens` fills bigger
batches (better throughput) at the cost of higher TTFT (new requests
wait longer).

**Measure**: `output_throughput_tokens_per_sec` over 5-minute windows.

**This repo**: measured in load tests. Not alerted (it's a business
metric, not a reliability metric).

### 1.4 Correctness (quality)

Two independent failure modes:

- **The model doesn't know**. Baseline model quality.
- **The infrastructure changed how the model responds**. Tokenizer,
  chat template, dtype, sampling params.

Category 2 is the reliability concern. If the platform silently
degrades output quality, HTTP monitoring won't catch it.

**Measure**: continuous evaluation on a held-out prompt set.

**This repo**: 6-hourly eval runs, per-category pass rate, alert on
regression.

### 1.5 Consistency / determinism

Same input → same output. Matters for cached responses, A/B tests,
regression tests, and any user experience where repeating a prompt
"should just work".

vLLM by default is non-deterministic even at `temperature=0` (CUDA
kernel non-determinism, batch-dependent numerical drift).

**Measure**: run identical prompts N times, count unique outputs.

**This repo**: not enforced. Add a determinism eval if you need it.

### 1.6 Fairness / isolation

Users shouldn't be able to starve other users. A single 32k-token
prompt can dominate a batch and delay everyone else's TTFT.

**Measure**: per-tenant p95 TTFT. Alert on the largest tenant's tail
degrading everyone else's.

**This repo**: not implemented (single-tenant assumption). Add
per-tenant labels to Envoy Gateway route metrics + tenant-keyed
ratelimit for real multi-tenancy.

### 1.7 Safety

Content classifier, jailbreak detection, output moderation.
Reliability *of the safety layer* is its own SLO.

**This repo**: not implemented. Add a pre-filter in Envoy
(WASM plugin or ext_proc) and a post-filter on the response body.

## 2. The failure modes LLMs invented

### 2.1 KV cache saturation and preemption

Every in-flight request pins some fraction of the KV cache
(proportional to its current context length). vLLM's continuous
batching schedules new requests into any free cache. When cache runs
out, vLLM **preempts** — the least-recently-scheduled request is
evicted and its cache pages freed. That request either restarts (from
prompt) or aborts.

**Consequences**:

- A user streaming a long response can suddenly get a stall (their
  request was preempted and is now restarting) or an abort.
- p95 TTFT stays low (new prefills are quick when cache is being
  aggressively freed).
- p95 E2E blows up (evicted requests restart and re-run).

**Mitigations**:

- Cap `--max-num-seqs` — fewer concurrent = less pressure.
- Cap `--max-model-len` — shorter contexts = less pin per request.
- KV-cache-aware routing (EPP) — route new requests to the pod with
  headroom instead of triggering preemption.
- Enable **prefix caching** — repeated system prompts share pages.
- Enable **cache offloading** to CPU/NVMe — vLLM 0.8+ supports it,
  slower but avoids preemption.

**Alerts**: `VLLMHighPreemptionRate`, `VLLMKVCacheAlmostFull` in this
repo.

### 2.2 Head-of-line blocking

Long prefill (say, 32k tokens) blocks the decode step for every request
in the batch. Even short-prompt users see decode latency spike during a
long-prompt prefill.

**Mitigations**:

- **Chunked prefill** (vLLM `--enable-chunked-prefill`) — split long
  prefills across scheduler steps.
- **Route long prompts to a dedicated pool** via EPP header inspection.
- **Cap per-request input length** at the ratelimit layer.

**Not in this repo yet**. Enable chunked prefill in v0.8+.

### 2.3 Cold start

- Image pull: ~2 GB vLLM image, ~2 minutes.
- HF download: 16 GB Llama-3-8B, 5-15 minutes.
- Weight load: ~30 seconds.
- CUDA graph capture: ~30 seconds if enabled.
- Total: **5-20 minutes to first token** on a truly cold pod.

This is the *hardest* number to hide from users. Options:

- **Never scale to zero** — keep a warm replica always.
- **Fast image tier** — bake the model into a custom image (2× storage
  cost, but pull is 5 GB instead of 20 GB and skips HF entirely).
- **PVC pre-population** — warm the HF cache PVC from a snapshot at
  pod start.
- **Model preloader DaemonSet** — pulls the weights into the local
  filesystem before the vLLM pod schedules.

**This repo**: HF cache PVC + 30-minute `startupFailureThreshold`.
Doesn't hide the cold start; buys enough time to survive one.

### 2.4 Silent quality regression

Model output subtly worse after a change nobody thought was risky.
Sources:

- Tokenizer version bump changes prompt tokenization.
- Chat template mismatch — model was trained on `<|im_start|>system\n`,
  server sends `<|system|>`.
- Quantization from bf16 → int8 → fp8, each drops a bit of accuracy.
- Beam-search-off, sampling-on, temperature-drift.
- Library version differences — vLLM 0.9 changed default `top_p` for
  chat completions.

Detectable only by **continuous evaluation**. That's the point of
[`16-evals.md`](16-evals.md).

**Mitigation**: run the eval **before every rollout** as part of the
rollout gate. This repo runs it 6-hourly post-deployment; a proper
production rollout would gate on evals inline.

### 2.5 Streaming failure modes

- Server disconnects mid-stream. Client sees partial output.
- Server sends `finish_reason: error` (rare). Client library may or
  may not surface it as an exception.
- Server response encoding is buffered instead of streamed
  (misconfigured Envoy `processingMode`), user sees no output for
  10+ seconds.

**Mitigation**: use SSE (Server-Sent Events) with explicit heartbeat
frames. Envoy's `processingMode.response.body: Streamed` (this repo
uses it) preserves streaming.

### 2.6 GPU hardware failures

Not exclusive to LLMs, but hits harder:

- **XID errors** — GPU driver errors. Some transient, some fatal.
- **ECC errors** — memory bit flips. Single-bit corrected;
  double-bit means quarantine.
- **Thermal throttling** — perf halves, TTFT triples.
- **PCIe link degradation** — slower than expected weight loads.
- **NVLink flapping** — tensor-parallel breaks.

**Mitigation**: DCGM exporter + alerts + operational discipline
(cordoning). This repo covers detection; response is human.

### 2.7 Poisoned prompts / prompt injection

Adversarial content in the *input* can make the model do things it
shouldn't. Reliability angle: a compromised model output is worse than
a 500.

**Mitigation**: not in this repo. Look at:

- Input classification (Anthropic prompt-shield, Llama Guard).
- Output moderation.
- Separation of system-controlled and user-controlled prompt parts.

## 3. Design decisions this repo made (and would remake)

- **KEDA on queue depth, not CPU** — correct choice; queue depth is
  the actual saturation signal for continuous batching.
- **EPP over round-robin** — even single-pod, worth it for
  observability and forward compatibility.
- **Recreate strategy over rolling update** — correct on single GPU;
  wrong on multi-GPU (where you can rolling-update if you have spare
  capacity).
- **Pushgateway for batch metrics** — pragmatic. Prometheus + batch =
  either Pushgateway or a sidecar exporter; Pushgateway is simpler.
- **kubectl-friendly Vault dev mode** — great for a launchable, wrong
  for production. Callout: this is the biggest jump from "demo" to
  "real".
- **Small in-cluster observability** — 6h Prometheus retention, 7d
  Loki, 24h Tempo. Fine for demo; production needs remote storage.

## 4. What we'd add before calling it production

- **Multi-window burn-rate alerts** at multiple time horizons (5m/1h,
  30m/6h, 6h/3d). We have two; production wants three.
- **Per-tenant labels** on request-path metrics.
- **A canary route** — 5% of traffic to the new pod during rollout.
  (Envoy Gateway supports it; not wired up here.)
- **Inline evals in rollout gate**, not just post-deploy 6-hourly.
- **Chunked prefill** in vLLM to defuse long-prompt head-of-line.
- **Cache offload** to CPU for graceful degradation on cache pressure.
- **Circuit breakers per pod** — Envoy already computes outlier
  detection; wire it in.
- **Real Vault** with KMS auto-unseal.
- **Loki + Tempo on object storage** with 30+d retention.
- **Cost attribution per tenant** — request-level accounting.

## 5. The reliability toolkit you already know applies

Everything from traditional SRE still applies:

- **Chaos engineering** — inject pod deletion, network partition,
  disk-full. See if the platform recovers. `kubectl-chaos` or
  chaos-mesh.
- **Failure injection at the model layer** — sabotage the model with a
  bad prompt template in a canary and verify the eval catches it.
- **DR drills** — quarterly, kill Vault. Kill Prometheus. Kill Envoy
  Gateway. Time the recovery.
- **Postmortems** — required after any critical alert. Blameless.
  Store in a searchable place.
- **Capacity planning** — forecast QPS × avg tokens/req; compare to
  measured tokens/sec/GPU; provision GPUs 3 months out.

## 6. The uncomfortable truth about LLM reliability

You can build the world's best observability stack, tune every knob,
and still lose because:

- **Your model provider changed a subtle default.** vLLM 0.9 changed
  `enable_prefix_caching` default. Users noticed lower quality on cache
  hits — hard to find without an eval on identical repeat prompts.
- **Your users use it differently than you expect.** They send prompts
  with 20,000 tokens of context because "the model has 32k window".
  Your baseline load test used 512 tokens.
- **The model itself has bad days.** Distributional queries you didn't
  test on regress silently when someone bumps the temperature default.

The only defense is continuous, evaluation-driven reliability
engineering. Ship telemetry for every axis. Alert on regression at
every layer. Roll back fast. Assume you're wrong. Test in prod.

## Where to go next

- Google SRE Workbook, Chapter 6 (multi-window burn-rate alerts).
- Anthropic / OpenAI production engineering blog posts.
- vLLM documentation on scheduler internals.
- [Gateway API Inference Extension design doc](https://github.com/kubernetes-sigs/gateway-api-inference-extension).
- Talk to your users. Every one of them has a reliability story that
  isn't in your dashboards.

## Related docs

- Reliability model in this repo: [`02-reliability.md`](02-reliability.md)
- Alerts implementing SLOs: [`14-alerts.md`](14-alerts.md)
- Evals: [`16-evals.md`](16-evals.md)
- Load tests: [`15-loadtests.md`](15-loadtests.md)
- Extending: [`19-extending.md`](19-extending.md)
