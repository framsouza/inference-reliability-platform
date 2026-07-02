#!/usr/bin/env bash
set -euo pipefail

ARGOCD_VERSION="${ARGOCD_VERSION:-v2.13.1}"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
GITHUB_USER="${GITHUB_USER:-git}"
REPO_URL="${REPO_URL:-https://github.com/framsouza/nvidia-brev-vllm.git}"

if [[ -z "${GITHUB_TOKEN}" ]]; then
  echo "GITHUB_TOKEN not set — export it and re-run" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"
kubectl apply -n argocd \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

kubectl -n argocd rollout status deploy/argocd-server --timeout=5m

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

echo
echo "argocd ${ARGOCD_VERSION} up. next: seed vault, then port-forward argocd-server."
echo "admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d && echo
