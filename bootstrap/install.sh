#!/usr/bin/env bash
set -euo pipefail

ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.1}"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
GITHUB_USER="${GITHUB_USER:-git}"
REPO_URL="${REPO_URL:-https://github.com/framsouza/nvidia-brev-vllm.git}"
HF_TOKEN="${HF_TOKEN:-}"
VLLM_API_KEY="${VLLM_API_KEY:-$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)}"

if [[ -z "${GITHUB_TOKEN}" ]]; then
  echo "GITHUB_TOKEN not set — export it and re-run" >&2
  exit 1
fi
if [[ -z "${HF_TOKEN}" ]]; then
  echo "HF_TOKEN not set — export it and re-run" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"
kubectl apply -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

kubectl -n argocd rollout status deploy/argocd-server --timeout=5m

kubectl -n argocd patch configmap argocd-cmd-params-cm \
  --type merge -p '{"data":{"controller.diff.server.side":"true","server.insecure":"true","server.rootpath":"/argocd","server.basehref":"/argocd"}}'
kubectl -n argocd patch configmap argocd-cm \
  --type merge -p '{"data":{"timeout.reconciliation":"30s"}}'
kubectl -n argocd rollout restart deploy/argocd-server
kubectl -n argocd rollout restart statefulset/argocd-application-controller

kubectl -n argocd apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: repo-nvidia-brev-vllm
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${REPO_URL}
  username: ${GITHUB_USER}
  password: ${GITHUB_TOKEN}
EOF

kubectl apply -f "${SCRIPT_DIR}/root-app.yaml"

echo "waiting for vault-0 to come up (root app must sync the vault Application first)..."
until kubectl -n vault get statefulset vault >/dev/null 2>&1; do sleep 5; done
kubectl -n vault rollout status statefulset/vault --timeout=10m

echo "seeding vault..."
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/hf token='${HF_TOKEN}'"
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/github \
     url='${REPO_URL}' username='${GITHUB_USER}' password='${GITHUB_TOKEN}'"
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/vllm apiKey='${VLLM_API_KEY}'"
echo "vLLM API key seeded. Save this — clients must send it as \`Authorization: Bearer <key>\`:"
echo "  ${VLLM_API_KEY}"

echo "waiting for ExternalSecret CRDs to be registered..."
until kubectl get crd externalsecrets.external-secrets.io >/dev/null 2>&1; do sleep 5; done

echo "waiting for ExternalSecret objects to appear (secrets app sync)..."
until kubectl -n llama  get externalsecret hf-token              >/dev/null 2>&1 \
   && kubectl -n argocd get externalsecret repo-nvidia-brev-vllm >/dev/null 2>&1; do
  sleep 5
done

kubectl -n llama  annotate externalsecret hf-token              force-sync=$(date +%s) --overwrite
kubectl -n argocd annotate externalsecret repo-nvidia-brev-vllm force-sync=$(date +%s) --overwrite

echo
echo "argocd ${ARGOCD_VERSION} up. seeded vault. ESO reconciling."
echo "admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d && echo
