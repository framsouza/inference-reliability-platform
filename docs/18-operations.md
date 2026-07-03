# 18 — Operations runbook

Day-2 for the platform. Rollouts, rollbacks, upgrades, incident triage.

## Rollouts

Every change goes through Git + ArgoCD:

1. Edit YAML.
2. Open PR. CI runs (see [`17-ci-cd.md`](17-ci-cd.md)).
3. Merge. ArgoCD picks it up within 3 minutes.
4. **PostSync rollout gate** runs `benchmark_serving.py` — if p95 TTFT
   > 3s or error rate > 2%, Application is marked degraded.
5. If gate passes, sync completes. If not: see "Rollback" below.

For urgent hotfixes:

```bash
argocd app sync llama --prune
```

To watch a sync in progress:

```bash
argocd app get llama --refresh
kubectl -n llama get pods -w
```

## Rollback

**Preferred**: `git revert <bad-sha>`, push. ArgoCD reconciles within
3 minutes.

**Fast**:

```bash
argocd app rollback llama <previous-revision>
```

`argocd app history llama` lists prior revisions.

**Emergency** (bad CRD upgrade, cluster-wide issue):

1. Disable ArgoCD auto-sync so it doesn't re-apply the bad state:
   ```bash
   argocd app set llama --sync-policy none
   ```
2. Fix manually with `kubectl`.
3. `git revert`, push, `argocd app set llama --sync-policy automated`.

**Never**:

- `helm rollback` directly — ArgoCD would just re-apply the Git state.
- Delete the `Application` — it prunes all workloads.

## Upgrades

### vLLM version

1. Edit `charts/llama-8b/values.yaml`:
   ```yaml
   image: vllm/vllm-openai:v0.8.0
   ```
2. Run local eval to see if prompt format changed:
   ```bash
   docker run --rm --gpus all vllm/vllm-openai:v0.8.0 --help | grep chat
   ```
3. Optionally update `rolloutGate.image` to match.
4. PR → merge → rollout gate runs → if pass, done.
5. Watch dashboards for the next 6 hours; especially the model quality
   dashboard (regression from tokenizer changes surfaces on the next
   6-hourly eval).

### ArgoCD / Envoy Gateway / any Helm chart

- Bump `targetRevision` in `apps/<app>.yaml`.
- Review the chart's release notes for breaking changes.
- Merge; ArgoCD syncs.

### K8s version (single node k3s)

Out of band from this repo — reinstall k3s, redeploy from git. The
manifests are all forward-compatible with 1.29-1.32 as of writing.

### GPU driver

Also out of band — the GPU Operator has driver disabled because Brev
images pre-install it. When Brev updates the base image, no repo change.
If you customize:

```yaml
# apps/gpu-operator.yaml
driver:
  enabled: true
  version: "550.144.03"
```

## Incident triage — by alert

### `VLLMPodDown` / `VLLMCrashLooping` / `VLLMStartupFailing`

1. `kubectl -n llama describe pod`:
   - `ImagePullBackOff` → image tag doesn't exist, or HF hub rate-limit.
   - `OOMKilled` → bump `resources.limits.memory` in values.
   - `Init:Error` → the SDK install fetch failed; retry or disable
     `otlp.installSdk` and bake into a custom image.
2. `kubectl -n llama logs deploy/llama-llama-8b --previous`:
   - `CUDA out of memory` → reduce `--max-num-seqs` / `--max-model-len`.
   - `Failed to load tokenizer` → HF token missing/wrong.
3. If `StartupFailing`: the pod is Pending for >35m. Usually HF download
   hanging. `kubectl -n llama exec -it <pod> -- ls
   /root/.cache/huggingface/hub/` to inspect. Delete the incomplete
   snapshot dir and let it retry.

### `VLLMHighTTFT` / `VLLMHighQueueDepth`

1. Check `vllm.yaml` dashboard: is `waiting > 0`?
   - Yes → saturated. Reduce request rate at Envoy Gateway
     ratelimit, or scale out (multi-GPU).
2. Check GPU utilization on `gpu.yaml` dashboard:
   - <50% → not compute-bound. Check for network/tokenizer bottleneck.
   - Thermal throttling? See `GPUHighTemperature`.
3. Check preemption rate — if non-zero, the queue is oscillating.

### `VLLMKVCacheAlmostFull` / `VLLMHighPreemptionRate`

Reduce concurrency by any of:

- Lower `--max-num-seqs` (currently the vLLM default 256).
- Lower `--max-num-batched-tokens` (default 4096).
- Reduce `--max-model-len` if long contexts are the culprit (check the
  `long-context` scenario latency spike).
- Add an EnvoyExtensionPolicy fallback that rejects when EPP reports
  `x-gateway-server-status: no-endpoint` (not enabled by default).

### `VLLMHighAbortRate`

Clients are timing out. Check their timeout settings; usually
easier than server-side. If you can't control clients, raise
`--max-num-batched-tokens` to reduce queue wait.

### `VLLMErrorBudgetBurnFast`

- Look at `envoy_cluster_upstream_rq{response_code_class="5xx"}` in
  Grafana `gateway.yaml`.
- 503s → vLLM is unhealthy; see `VLLMPodDown` above.
- 429s → ratelimit is firing. Legitimate load spike, or a bad client?
- 400s → schema mismatch. Client is sending garbage.

### `GPUXIDError` / `GPUECCDoubleBitError`

**Critical hardware event.**

1. Cordon the node:
   ```bash
   kubectl cordon <node>
   ```
2. Drain (evict all pods but leave DaemonSets):
   ```bash
   kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
   ```
3. Consult the [XID error reference](https://docs.nvidia.com/deploy/xid-errors/).
   Some XIDs (79 "GPU has fallen off the bus", 63 "row remapping") mean
   the GPU is dying. Others (43 "kernel launch failure") can be
   transient.
4. Reboot the node. If the error recurs, RMA the GPU.

### `GPUHighTemperature` / `GPUThermalThrottling`

Not a hardware fault. Data center or airflow issue. Reduce sustained
load until fixed.

### `ModelQualityLowOverallPassRate`

1. Open the `model-quality.yaml` dashboard.
2. Which category regressed?
   - `factual` → tokenizer or template change (very common)
   - `math` → sampling/temperature drift, or true model change
   - `code` → same
   - `instruction` → chat template broken
   - `reasoning` → true model degradation (rare)
3. `git log --since="24h" -- charts/ apps/llama.yaml` — anything
   changed?
4. Compare a specific prompt manually:
   ```bash
   curl -s http://llama-llama-8b.llama:8000/v1/chat/completions \
     -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"model": "meta-llama/...", "messages": [{"role":"user","content":"What is 15 * 23?"}]}'
   ```
   Compare to the eval expectation.

### `ModelQualityEvalStale`

The CronWorkflow isn't running. Check `argo` namespace:

```bash
kubectl -n argo get workflows | tail
kubectl -n argo describe cronworkflow model-quality-eval
```

Usually: pod couldn't schedule (resource pressure), or the vLLM URL was
unreachable (network policy misconfig).

## Cost levers

If you're bleeding money on this stack:

- **`--gpu-memory-utilization`** (vLLM flag; not exposed in values) —
  lower it to leave GPU memory for spikes, at the cost of throughput.
- **KV cache size** — governed indirectly by
  `--max-model-len` × `--max-num-seqs`.
- **Autoscale to zero** — requires a warm-pool proxy (not in this repo).
- **Move Loki/Tempo to object storage** — filesystem is limiting scale;
  the extra retention pays for itself in incident post-mortems.

## Regular maintenance

- **Weekly**: check the model quality dashboard. Baseline shouldn't drift.
- **Weekly**: check the loadtests dashboard. p95 TTFT trend.
- **Monthly**: rotate the vLLM API key. Update Vault; ESO syncs; rollout
  restart the vLLM Deployment.
- **Quarterly**: upgrade vLLM. Run rollout gate + evals.
- **Quarterly**: DR drill — apply a `kubectl scale --replicas=0` and
  verify the on-call gets paged within alertmanager's `group_wait`.

## Related docs

- Alert catalog: [`14-alerts.md`](14-alerts.md)
- Dashboards: [`13-dashboards.md`](13-dashboards.md)
- Bootstrap flow: [`03-bootstrap-and-gitops.md`](03-bootstrap-and-gitops.md)
- Extending the platform: [`19-extending.md`](19-extending.md)
