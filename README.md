# nvidia-brev-vllm

vLLM serving Llama-3-8B on a single-GPU k3s node. ArgoCD does the deploys, Vault
(dev) + ESO handle secrets.

```
apps/            argocd Applications
bootstrap/       argocd install + root app
charts/llama-8b/ vllm helm chart
gpu-operator/    gpu-operator values
secrets/         ClusterSecretStore + ExternalSecrets
```

Sync waves: `0` gpu-operator, vault, external-secrets, kube-prometheus-stack,
loki, tempo → `5` secrets, otel-collector → `10` llama.

## 1. Host

```bash
nvidia-smi
curl -sfL https://get.k3s.io | sh -
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
export KUBECONFIG=~/.kube/config
echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc
kubectl get nodes
```

NVIDIA container toolkit so k3s picks up the `nvidia` runtime:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart k3s
```

## 2. Bootstrap

Repo is private, so ArgoCD needs a fine-grained PAT (Contents: Read on this
repo). Export both secrets and run the script — it installs ArgoCD, waits for
Vault, seeds `secret/hf` + `secret/github`, and force-syncs the ExternalSecrets.

```bash
export GITHUB_TOKEN=ghp_...
export GITHUB_USER=framsouza
export REPO_URL=https://github.com/framsouza/nvidia-brev-vllm.git
export HF_TOKEN=hf_...
./bootstrap/install.sh
```

The `ExternalSecret` in `secrets/argocd-repo-external-secret.yaml` takes
ownership of the bootstrap-created `repo-nvidia-brev-vllm` Secret once ESO
comes up. From then on, rotating the PAT is a `vault kv put`:

```bash
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/github \
     url='${REPO_URL}' username='${GITHUB_USER}' password='${NEW_TOKEN}'"
kubectl -n argocd annotate externalsecret repo-nvidia-brev-vllm force-sync=$(date +%s) --overwrite
```

Same shape for rotating the HF token via `secret/hf`.

| ExternalSecret          | Vault path      | Target                          |
|-------------------------|-----------------|---------------------------------|
| `hf-token`              | `secret/hf`     | `llama/hf-token`                |
| `repo-nvidia-brev-vllm` | `secret/github` | `argocd/repo-nvidia-brev-vllm`  |

**Vault pod restarted?** Dev mode is in-memory — re-seed:

```bash
kubectl -n vault rollout status statefulset/vault --timeout=5m
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/hf token='$HF_TOKEN'"
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/github \
     url='$REPO_URL' username='$GITHUB_USER' password='$GITHUB_TOKEN'"
kubectl -n llama  annotate externalsecret hf-token              force-sync=$(date +%s) --overwrite
kubectl -n argocd annotate externalsecret repo-nvidia-brev-vllm force-sync=$(date +%s) --overwrite
```

## 3. Verify

```bash
kubectl -n argocd get applications
kubectl -n gpu-operator get pods
kubectl -n vault get pods
kubectl -n external-secrets get pods
kubectl -n llama get externalsecret,secret,pods
```

### ArgoCD UI on Brev

`bootstrap/install.sh` already patches `argocd-cmd-params-cm` with
`server.insecure: "true"` (Brev's port publisher is HTTP-only),
`controller.diff.server.side: "true"` (avoids `terminatingReplicas: field not
declared in schema` on k8s ≥1.33), and sets `timeout.reconciliation: 30s` in
`argocd-cm` so a git push turns into a sync within ~30 seconds instead of the
default 3 minutes. Port-forward on port 80:

```bash
kubectl -n argocd port-forward --address 0.0.0.0 svc/argocd-server 8080:80
```

Expose port `8080` in the Brev UI, then hit the Brev-provided URL over
`http://`. Login: `admin` / password from `argocd-initial-admin-secret`
(printed by `bootstrap/install.sh`, or `kubectl -n argocd get secret
argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`).

If you bootstrapped before these patches were added, run them manually:

```bash
kubectl -n argocd patch configmap argocd-cmd-params-cm --type merge \
  -p '{"data":{"controller.diff.server.side":"true","server.insecure":"true"}}'
kubectl -n argocd patch configmap argocd-cm --type merge \
  -p '{"data":{"timeout.reconciliation":"30s"}}'
kubectl -n argocd rollout restart deploy/argocd-server
kubectl -n argocd rollout restart statefulset/argocd-application-controller
```

DCGM diag:

```bash
kubectl run dcgm-diag --rm -it --restart=Never \
  --image=nvcr.io/nvidia/cloud-native/dcgm:3.3.5-1-ubuntu22.04 \
  --overrides='{"spec":{"runtimeClassName":"nvidia","containers":[{"name":"dcgm-diag","image":"nvcr.io/nvidia/cloud-native/dcgm:3.3.5-1-ubuntu22.04","command":["dcgmi","diag","-r","2"],"resources":{"limits":{"nvidia.com/gpu":1}}}]}}'
```

## Observability

All in the `monitoring` namespace:

| Component               | Chart                                | Purpose                                     |
|-------------------------|--------------------------------------|---------------------------------------------|
| kube-prometheus-stack   | prometheus-community/kube-prometheus-stack | Prometheus (with remote-write receiver) + Grafana + alertmanager |
| loki                    | grafana/loki                         | log store                                   |
| tempo                   | grafana/tempo                        | trace store                                 |
| otel-collector          | open-telemetry/opentelemetry-collector | DaemonSet: OTLP + filelog + prometheus receivers; exports to tempo/loki/prometheus |

Signal flow:

```
vLLM ── OTLP traces ──▶ otel-collector ── OTLP ─▶ tempo
vLLM /metrics ─ scrape ▶ otel-collector ─ remote_write ▶ prometheus
pod stdout/stderr ────▶ otel-collector (filelog) ── push ▶ loki
kube-state / node-exporter / cadvisor ── scrape ▶ prometheus
```

vLLM tracing is wired via chart values (`otlp.tracesEndpoint`) which set both
`--otlp-traces-endpoint` and `OTEL_EXPORTER_OTLP_ENDPOINT`. Every span is
enriched with `service.name=vllm`, k8s attributes (pod, namespace, node), and
`cluster=brev`.

Grafana access:

```bash
kubectl -n monitoring port-forward --address 0.0.0.0 svc/kps-grafana 3000:80
```

Expose `3000` in Brev, login `admin` / `admin`. Prometheus, Loki, and Tempo
datasources are preconfigured; Explore → pick one.

Sanity checks:

```bash
kubectl -n monitoring get pods
kubectl -n monitoring logs -l app.kubernetes.io/name=opentelemetry-collector --tail=50
# a trace should appear in Tempo once you hit vLLM
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "content-type: application/json" \
  -d '{"model":"meta-llama/Meta-Llama-3-8B-Instruct","messages":[{"role":"user","content":"hi"}]}'
```

## Notes

- `gpu-operator/values.yaml` disables the operator-managed driver + toolkit
  (host handles both) and disables CDI. CDI segfaults on driver <570; flip
  `cdi.enabled: true` and drop `DEVICE_LIST_STRATEGY` once you're on 570+.
- `charts/llama-8b/values.yaml` pins `vllm/vllm-openai:v0.7.3` because newer
  vLLM images (v0.9+) ship PyTorch built against CUDA 12.8, which needs driver
  570+. On driver 570+ you can bump the tag to `latest`.
- Vault dev mode is in-memory. Every vault pod restart = re-seed. For anything
  beyond a dev box: switch to `server.standalone.enabled: true` with a PVC,
  drop the static root token, use k8s auth, delete `secrets/vault-token.yaml`.
