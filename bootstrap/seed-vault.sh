#!/usr/bin/env bash
# Seed Vault with every secret ExternalSecrets consume. Idempotent —
# re-run whenever Vault dev mode loses its in-memory state.
#
# Required env vars:
#   GITHUB_TOKEN   PAT with Contents:Read on this repo
#   HF_TOKEN       HuggingFace token with Llama-3-8B access
#
# Optional:
#   GITHUB_USER    default: framsouza
#   REPO_URL       default: this repo
#   VLLM_API_KEY   if unset: reuse existing key from Vault, else generate
#
# Usage:
#   export GITHUB_TOKEN=ghp_...
#   export HF_TOKEN=hf_...
#   ./bootstrap/seed-vault.sh

set -euo pipefail

: "${GITHUB_TOKEN:?export GITHUB_TOKEN first}"
: "${HF_TOKEN:?export HF_TOKEN first}"
GITHUB_USER="${GITHUB_USER:-framsouza}"
REPO_URL="${REPO_URL:-https://github.com/framsouza/nvidia-brev-vllm.git}"

echo "waiting for vault-0 to be ready..."
until kubectl -n vault get statefulset vault >/dev/null 2>&1; do sleep 5; done
kubectl -n vault rollout status statefulset/vault --timeout=5m

# Reuse existing vLLM API key if one is already in Vault and none was passed in.
# Prevents rotating the key (and needing a pod restart) on a routine re-seed.
if [[ -z "${VLLM_API_KEY:-}" ]]; then
  EXISTING=$(kubectl -n vault exec vault-0 -- sh -c \
    'VAULT_TOKEN=root vault kv get -field=apiKey secret/vllm 2>/dev/null' 2>/dev/null || true)
  if [[ -n "${EXISTING}" ]]; then
    VLLM_API_KEY="${EXISTING}"
    echo "reusing existing vllm apiKey from vault"
  else
    VLLM_API_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
    echo "generated new vllm apiKey (save this): ${VLLM_API_KEY}"
  fi
fi

echo "seeding vault..."
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/hf token='${HF_TOKEN}'"
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/github \
     url='${REPO_URL}' username='${GITHUB_USER}' password='${GITHUB_TOKEN}'"
kubectl -n vault exec -i vault-0 -- sh -c \
  "VAULT_TOKEN=root vault kv put secret/vllm apiKey='${VLLM_API_KEY}'"

echo "force-syncing every ExternalSecret so they pick up the fresh vault state..."
for ns in llama argocd argo; do
  for es in $(kubectl -n "$ns" get externalsecret -o name 2>/dev/null); do
    kubectl -n "$ns" annotate "$es" force-sync="$(date +%s)" --overwrite
  done
done

# Restart any pod whose env is populated from the secrets at boot (vLLM does this).
# The kubelet auto-reflects updated Secret payloads for mounts, but Deployment env
# vars are read once at container start.
echo "restarting vLLM so it picks up any rotated api key..."
kubectl -n llama rollout restart deploy/llama-llama-8b 2>/dev/null || true

echo
echo "vault re-seeded. vLLM API key:"
echo "  ${VLLM_API_KEY}"
