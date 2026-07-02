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

Sync waves: `0` gpu-operator, vault, external-secrets → `5` secrets → `10` llama.

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
repo). Then:

```bash
export GITHUB_TOKEN=ghp_...
export GITHUB_USER=framsouza
export REPO_URL=https://github.com/framsouza/nvidia-brev-vllm.git
./bootstrap/install.sh
```

Script installs ArgoCD (`ARGOCD_VERSION`, default `v2.13.1`), writes the repo
credential Secret, applies the root app. The `ExternalSecret` in
`secrets/argocd-repo-external-secret.yaml` takes ownership of the same Secret
once Vault is seeded — after that, rotating the PAT is a `vault kv put`.

## 3. Seed Vault

```bash
kubectl -n vault rollout status statefulset/vault --timeout=5m

kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/hf token='${HF_TOKEN}'"

kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/github \
     url='${REPO_URL}' username='${GITHUB_USER}' password='${GITHUB_TOKEN}'"
```

| ExternalSecret          | Vault path      | Target                          |
|-------------------------|-----------------|---------------------------------|
| `hf-token`              | `secret/hf`     | `llama/hf-token`                |
| `repo-nvidia-brev-vllm` | `secret/github` | `argocd/repo-nvidia-brev-vllm`  |

Force a refresh instead of waiting the hour:

```bash
kubectl -n llama annotate externalsecret hf-token force-sync=$(date +%s) --overwrite
kubectl -n argocd annotate externalsecret repo-nvidia-brev-vllm force-sync=$(date +%s) --overwrite
```

## 4. Verify

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443 &
kubectl -n argocd get applications
kubectl -n gpu-operator get pods
kubectl -n vault get pods
kubectl -n external-secrets get pods
kubectl -n llama get externalsecret,secret,pods
```

DCGM diag:

```bash
kubectl run dcgm-diag --rm -it --restart=Never \
  --image=nvcr.io/nvidia/cloud-native/dcgm:3.3.5-1-ubuntu22.04 \
  --overrides='{"spec":{"runtimeClassName":"nvidia","containers":[{"name":"dcgm-diag","image":"nvcr.io/nvidia/cloud-native/dcgm:3.3.5-1-ubuntu22.04","command":["dcgmi","diag","-r","2"],"resources":{"limits":{"nvidia.com/gpu":1}}}]}}'
```

## Notes

- `gpu-operator/values.yaml` disables the operator-managed driver + toolkit
  (host handles both) and disables CDI. CDI segfaults on driver <570; flip
  `cdi.enabled: true` and drop `DEVICE_LIST_STRATEGY` once you're on 570+.
- Vault dev mode is in-memory. Every vault pod restart = re-seed. For anything
  beyond a dev box: switch to `server.standalone.enabled: true` with a PVC,
  drop the static root token, use k8s auth, delete `secrets/vault-token.yaml`.
