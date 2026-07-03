# 05 — GPU Operator & DCGM

The [NVIDIA GPU Operator](https://github.com/NVIDIA/gpu-operator) sets up
GPUs in Kubernetes: device plugin (advertises `nvidia.com/gpu` resource),
DCGM exporter (metrics), MIG manager (partitioning), driver (kernel module).
This repo runs a **minimal** operator config — only the pieces you need on
a Brev host that already has driver + toolkit installed.

## Files

```
apps/gpu-operator.yaml           ArgoCD Application → Helm chart v24.9.0
policies/mutate-nvidia-runtime-class.yaml   Ensures RuntimeClass is set
policies/require-gpu-pod-shm.yaml           Ensures /dev/shm is large enough
alerts/gpu-health.yaml           XID, ECC, thermal, memory alerts
dashboards/gpu.yaml              Grafana dashboard for GPU health
```

## Helm chart config (what's on, what's off, and why)

From `apps/gpu-operator.yaml`:

```yaml
driver:
  enabled: false        # Brev images already have driver installed
toolkit:
  enabled: false        # Brev images already have container-toolkit
cdi:
  enabled: false        # Not using Container Device Interface path
devicePlugin:
  env:
    - name: DEVICE_LIST_STRATEGY
      value: volume-mounts
dcgmExporter:
  enabled: true
  image:
    repository: nvcr.io/nvidia/k8s/dcgm-exporter
    tag: 3.3.9-3.6.1-ubuntu22.04
  serviceMonitor:
    enabled: true
    interval: 15s
```

### Why driver + toolkit are disabled

Brev's launchable images pre-install the NVIDIA driver (`nvidia-smi` works
before k3s starts) and the `nvidia-container-toolkit` package (needed for
containerd to know how to inject GPU devices into containers). Letting the
operator try to install them again would either fail (driver already
loaded) or waste minutes on every launch.

If you deploy this repo on a **bare** cluster where nothing NVIDIA is
installed, flip:

```yaml
driver:
  enabled: true
toolkit:
  enabled: true
```

### Why `DEVICE_LIST_STRATEGY=volume-mounts`

The default (`envvar`) sets `NVIDIA_VISIBLE_DEVICES` inside the container.
`volume-mounts` uses `/dev` bind-mounts, which is more compatible with
newer containerd versions and doesn't require the container image to
know about NVIDIA.

### Why DCGM Exporter is on

[DCGM](https://developer.nvidia.com/dcgm) (Data Center GPU Manager) is
NVIDIA's canonical monitoring stack. The exporter turns DCGM counters into
Prometheus metrics. Without it you have **no** GPU visibility — vLLM's own
metrics only tell you what the software thinks, not what the hardware is
doing.

Metrics exposed (partial):

- `DCGM_FI_DEV_GPU_UTIL` — SM (Streaming Multiprocessor) utilization %
- `DCGM_FI_DEV_MEM_COPY_UTIL` — memory bandwidth utilization %
- `DCGM_FI_DEV_FB_USED` / `_FREE` — frame buffer (GPU memory) usage
- `DCGM_FI_DEV_GPU_TEMP` — die temperature °C
- `DCGM_FI_DEV_POWER_USAGE` — power draw W
- `DCGM_FI_DEV_SM_CLOCK` / `_MEM_CLOCK` — clocks MHz
- `DCGM_FI_DEV_XID_ERRORS` — XID error counter (any bump = incident)
- `DCGM_FI_DEV_ECC_DBE_VOL_TOTAL` — double-bit ECC errors
- `DCGM_FI_DEV_THERMAL_VIOLATION` — thermal throttle time counter
- `DCGM_FI_DEV_POWER_VIOLATION` — power-cap throttle time counter

### `ServiceMonitor` label

The chart's `serviceMonitor.enabled: true` creates a `ServiceMonitor` with
the release label the exporter uses. Prometheus's kube-prometheus-stack is
configured to scrape *all* namespaces (`serviceMonitorNamespaceSelector: {}`)
so no cross-namespace tweaks are needed.

## Two GPU pod gotchas the platform solves

### 1. `runtimeClassName: nvidia`

Every GPU pod must have `runtimeClassName: nvidia`. If it doesn't, the
containerd runtime doesn't inject the `libnvidia-*` binaries — `nvidia-smi`
fails inside the container, and vLLM crashes at startup.

`policies/mutate-nvidia-runtime-class.yaml` (Kyverno) matches Pods with a
container that requests `nvidia.com/gpu`, and *mutates* them to add
`runtimeClassName: nvidia` if missing. It's a safety net: the vLLM Helm
chart sets it explicitly, but a hand-rolled pod (e.g. a debug shell)
benefits from the auto-mutation.

### 2. `/dev/shm` size

PyTorch multiprocessing uses shared memory for CUDA tensor sharing between
worker processes. The default `/dev/shm` inside a container is **64 MiB**
(set by containerd). vLLM prefill can easily need >1 GiB — you get:

```
Bus error: shared memory segment ran out of space
```

Halfway through a request. The pod crashes.

Fix: mount an `emptyDir` with `medium: Memory` and `sizeLimit: 2Gi` at
`/dev/shm`. The vLLM chart does this by default (`values.yaml` →
`shmSize: 2Gi`), and Kyverno policy `require-gpu-pod-shm` enforces it on
any other GPU pod.

## Alerts on GPU health

`alerts/gpu-health.yaml` — 6 rules covering the hardware failure modes:

| Alert | Threshold | Severity | Action |
|-------|-----------|----------|--------|
| `GPUXIDError` | Any increase in XID counter | **critical** | Cordon, reboot, RMA if reproducing |
| `GPUECCDoubleBitError` | Any DBE | **critical** | Cordon, quarantine, RMA |
| `GPUHighTemperature` | >85°C for 5m | warning | Check airflow, throttle downstream |
| `GPUHighMemoryUsage` | FB used >95% for 5m | warning | Reduce `--max-num-seqs` or context length |
| `GPUThermalThrottling` | Throttle active for 5m | warning | Same as high temp |
| `GPUExporterDown` | DCGM unreachable 10m | warning | Restart DCGM daemonset; dashboards are blind until fixed |

XID errors are the ones you page for. See NVIDIA's
[XID error reference](https://docs.nvidia.com/deploy/xid-errors/index.html):
XID 79 = GPU fell off the bus, XID 43 = uncorrectable ECC, XID 63 = ECC
row remapping event. All require intervention.

## Reading the DCGM dashboard

`dashboards/gpu.yaml` (see [`13-dashboards.md`](13-dashboards.md)). Panels
cover: GPU count, SM utilization (max across a batch), power draw,
temperature, FB memory used, SM/memory clocks, throttling event count,
ECC errors, XID errors.

**When SM utilization is low but queue depth is high** (`VLLMGPUUnderutilized`
alert fires):
- Either `--max-num-batched-tokens` is too low (batches don't fill up)
- Or requests are I/O bound (network to client, tokenizer overhead)
- Check the vLLM dashboard `vllm:num_running` vs `vllm:num_waiting`

## Extending / operating

- **MIG partitioning** — enable `migManager.enabled: true` in the operator
  and split an A100 into e.g. 3× 20GB partitions to serve 3 replicas per
  card.
- **Multiple GPUs per pod** — vLLM supports tensor parallelism. Set
  `--tensor-parallel-size 2` in the Helm values and request 2 GPUs; you
  need NVLink or PCIe P2P.
- **Custom DCGM fields** — the exporter's default field set is fine; if you
  need extras, override the `dcgm-exporter.metricsConfig` ConfigMap.
- **Older drivers** — pin `dcgmExporter.image.tag` to a version compatible
  with your driver. See
  [DCGM compatibility matrix](https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/getting-started.html#supported-platforms).

## Related docs

- Kubernetes-level GPU config: [`04-kubernetes.md`](04-kubernetes.md)
- Kyverno enforcement: [`11-kyverno-policies.md`](11-kyverno-policies.md)
- GPU dashboard: [`13-dashboards.md`](13-dashboards.md)
- Alerts: [`14-alerts.md`](14-alerts.md)
