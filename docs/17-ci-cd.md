# 17 — CI/CD (GitHub Actions)

CI catches breakage before a manifest lands in the cluster. Two workflows:

- **`ci.yml`** — fast, lint-and-test-only. Runs on every PR and push to
  `main`.
- **`e2e.yml`** — expensive, real k8s (kind). Runs on PRs that touch
  `charts/`, `policies/`, `httproutes/`, `gateway/`, or the workflow
  file itself.

## Files

```
.github/workflows/
├── ci.yml        Lint + unit tests + rendering
└── e2e.yml       kind cluster + Helm install + Kyverno enforcement
```

## `ci.yml` — the fast path

Runs 9 parallel jobs. Any red job blocks the PR.

### `yamllint`

- Runs `yamllint` against all `**/*.yaml` respecting `.yamllint.yaml`.
- Rules: max 200-char lines (warning), truthy values must be
  `true`/`false`, consistent indentation, spaces inside `{}` and `[]`
  limited to 1.
- Ignores: Helm chart templates, GitHub workflow files, dashboards
  (embedded JSON), and the eval script ConfigMap (Python inside).

### `shellcheck`

- Runs `ludeeus/action-shellcheck` on `bootstrap/*.sh`.
- Severity: `error` (won't fail on warnings).

### `helm-lint`

- Sets up Helm v3.15.3.
- `helm lint charts/llama-8b`.

### `kubeconform`

- Downloads `kubeconform v0.6.7`.
- Validates every YAML in the repo against Kubernetes JSON Schemas.
- Includes CRD schemas for Gateway API, Prometheus Operator, KEDA,
  ArgoCD, Argo Workflows, Kyverno, Envoy Gateway, ESO, Inference
  Extension.

### `kyverno`

- Installs Kyverno CLI v1.13.0.
- Renders the vLLM Helm chart to a manifest.
- Runs `kyverno test policies/ --resources <manifest>` — dry-runs every
  policy against the rendered chart.
- Skips `verify-vllm-image` (it's Audit mode and requires network
  access for Cosign signature fetch).

### `helm-unittest`

- Installs `helm-unittest` plugin v0.7.2.
- Runs `helm unittest charts/llama-8b`.
- Test files in `charts/llama-8b/tests/` assert on rendered template
  contents (see [`06-inference-vllm.md`](06-inference-vllm.md)).

### `python-tests`

- Sets up Python 3.11.
- `pip install -r evals/tests/requirements.txt`.
- `pytest evals/tests/` — unit tests for `evaluator.py`.

### `mermaid`

- Installs `@mermaid-js/mermaid-cli` v11.4.0.
- Renders every `images/*.mmd` file to SVG.
- Ensures diagrams parse. On failure you get the mermaid parser error
  in CI logs.

### `argocd-diff`

- Custom Python script.
- Validates that every file in `apps/*.yaml` parses as YAML and has
  `kind: Application` under the `argoproj.io/v1alpha1` API.
- Catches missing `spec.destination` or `spec.source` fields.

## `e2e.yml` — the real kind cluster

Triggers:

- PRs touching `charts/**`, `policies/**`, `httproutes/**`, `gateway/**`,
  or `.github/workflows/e2e.yml`.
- Pushes to `main` (post-merge safety net).
- Manual `workflow_dispatch`.

### Setup steps

1. **Provision kind** — kind v0.24.0 with `kindest/node:v1.30.4`. Waits
   for CoreDNS + kube-proxy ready.
2. **Install Gateway API CRDs** — upstream v1.2.1 experimental channel.
3. **Install KEDA CRDs only** — no operator (we just need the
   `ScaledObject` schema for `helm template` validation).
4. **Install Prometheus Operator CRDs** — needed for `ServiceMonitor`.
5. **Install Gateway API Inference Extension CRDs** — needed for
   `InferencePool`.
6. **Install a minimal Argo Workflow CRD** — a stub CRD (`x-kubernetes-preserve-unknown-fields: true`)
   because the real one is huge and k8s 1.30's structural schema
   validator can trip on it. Enough for `helm template` to reference
   Workflow objects.
7. **Install Kyverno v3.3.0** — real deployment, admission enforcement
   enabled.
8. **Apply the ClusterPolicies** from `policies/`.
9. **Create namespaces**: `llama`, `envoy-gateway-system`, `monitoring`,
   `argo`.
10. **Create fake Secrets** for `hf-token` and `vllm-api-key`.
11. **Create the `nvidia` RuntimeClass** with `handler: runc` (kind has
    no GPU, so runc is a stand-in). Kyverno mutation still checks for
    the class's *presence*, not that it works.

### Test cases

1. **Helm install with fake image**:
   ```yaml
   image: nginx:alpine
   otlp.installSdk: false
   rolloutGate.enabled: false
   ```
   - `nginx:alpine` because we can't pull the 2GB vLLM image in CI.
   - `otlp.installSdk: false` because we don't want pip install in the
     nginx container.
   - `rolloutGate.enabled: false` because the gate would fail — no vLLM
     serving.
2. **Assert resources created**:
   - Deployment, Service, PVC, PriorityClass (cluster-scoped),
     ServiceMonitor, NetworkPolicy × 2, ScaledObject.
   - Deployment has `runtimeClassName: nvidia`.
   - Pod has `/dev/shm` emptyDir with `medium: Memory`.
3. **Kyverno mutation test**: apply a plain Pod requesting
   `nvidia.com/gpu` without `runtimeClassName`. Verify the applied Pod
   has `runtimeClassName: nvidia` (Kyverno mutated it).
4. **Kyverno validation test**: apply a Pod in `llama` ns without
   `priorityClassName`. Verify Kyverno rejects it with the expected
   message.
5. **Dump artifacts on failure**: policy reports, events, pod
   descriptions, container logs — attached to the run for debugging.

## Local pre-flight

Same set of checks, run manually:

```bash
# yamllint
yamllint -c .yamllint.yaml .

# shellcheck
shellcheck bootstrap/*.sh

# helm lint & unittest
helm lint charts/llama-8b
helm unittest charts/llama-8b

# kubeconform
kubeconform -summary -strict -kubernetes-version 1.30.0 \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  .

# kyverno
helm template llama charts/llama-8b > /tmp/rendered.yaml
kyverno test policies/ --resources /tmp/rendered.yaml

# python
pytest evals/tests/
```

## Adding a check

1. Edit `.github/workflows/ci.yml`.
2. Add a new job under `jobs:`.
3. Keep it under 5 minutes wall-clock — CI is on the critical path for
   PRs.

For e2e: only add to `e2e.yml` if the check requires a real cluster.
Prefer `helm template + kubeconform + kyverno test` in `ci.yml`.

## Debugging CI failures

- **`yamllint` failure**: run locally with the same rules
  (`.yamllint.yaml`).
- **`kubeconform` failure**: usually a missing CRD schema. Add the
  schema to the schema-location list.
- **`kyverno test` failure**: often a policy expects a resource shape
  the rendered chart doesn't produce. Reproduce with
  `kyverno test --detailed-results`.
- **`e2e.yml` failure**: check the "dump on failure" step artifacts —
  usually a Kyverno rejection message or an image pull error.

## What CI *doesn't* catch

- Runtime behavior of vLLM itself (needs a real GPU).
- Envoy Gateway config generation from CRDs (needs the controller
  running).
- KEDA scaling behavior (needs a running Prometheus and the operator).

These are covered by the rollout gate (per-sync canary) and the nightly
load tests (post-deploy regression).

## Related docs

- Kyverno policies: [`11-kyverno-policies.md`](11-kyverno-policies.md)
- vLLM Helm chart tests: [`06-inference-vllm.md`](06-inference-vllm.md)
- Rollout gate: [`06-inference-vllm.md`](06-inference-vllm.md)
- Eval tests: [`16-evals.md`](16-evals.md)
