# 10 — Secrets (External Secrets Operator + Vault)

Secrets live in **HashiCorp Vault** (dev mode in this launchable). The
**External Secrets Operator (ESO)** reconciles them into Kubernetes
`Secret` resources that pods can consume as env vars or volume mounts.

The point is: **no plaintext secrets in Git**. The manifests reference
`ExternalSecret` CRs; the secret material only exists in Vault + the live
cluster.

## Files

```
apps/vault.yaml                                Vault Helm chart 0.29.1 (wave 0)
apps/external-secrets.yaml                     ESO Helm chart 0.10.4 (wave 5)
apps/secrets.yaml                              ArgoCD Application → secrets/
bootstrap/seed-vault.sh                        Populates Vault on install
secrets/
├── llama-namespace.yaml                       Creates the llama namespace
├── vault-token.yaml                           Root-token Secret in external-secrets ns (bootstraps ESO's auth)
├── cluster-secret-store.yaml                  ClusterSecretStore vault-dev
├── vllm-api-key-external-secret.yaml          → llama/vllm-api-key
├── vllm-api-key-external-secret-argo.yaml     → argo/vllm-api-key
├── hf-external-secret.yaml                    → llama/hf-token
├── hf-external-secret-argo.yaml               → argo/hf-token
└── argocd-repo-external-secret.yaml           → argocd/repo-github
```

## The Vault setup

`apps/vault.yaml` installs the community Vault chart in **dev mode**:

```yaml
server:
  dev:
    enabled: true
    devRootToken: root
```

Dev mode:

- Runs as a single pod (`vault-0`).
- Storage is in-memory (loses everything on pod restart).
- Auto-unseals.
- Uses `root` as the root token (also the only token).
- HTTP only (`TLS_DISABLE=true`).

Perfect for a launchable / demo. **Not** production. See "Moving to
production Vault" below.

The chart's `readinessProbe` path is
`/v1/sys/health?standbyok=true&sealedcode=204&uninitcode=204` so the pod
reports ready as soon as it's up and unsealed.

## The `ClusterSecretStore`

`secrets/cluster-secret-store.yaml`:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: vault-dev
spec:
  provider:
    vault:
      server: http://vault.vault:8200
      path: secret
      version: v2
      auth:
        tokenSecretRef:
          name: vault-token
          namespace: external-secrets
          key: token
```

- **Cluster-scoped** — any namespace's `ExternalSecret` can reference it.
- Uses the KV v2 mount at path `secret`.
- Authenticates with a static token pulled from a Secret in the
  `external-secrets` namespace.

The `vault-token` Secret (`secrets/vault-token.yaml`) is committed to Git
with `stringData.token: "root"` — **only safe because dev-mode Vault has
no real secrets to protect**. In production, replace with a Kubernetes
Auth `kubernetes.serviceAccountRef` pointing at ESO's SA.

## `ExternalSecret` resources

One `ExternalSecret` per credential the platform needs. Structure of each:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: vllm-api-key
  namespace: llama
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault-dev
  target:
    name: vllm-api-key            # created Secret name
    creationPolicy: Owner
    template:
      type: Opaque
      data:
        token: "{{ .apiKey }}"
  data:
    - secretKey: apiKey
      remoteRef:
        key: vllm                 # secret/vllm in Vault
        property: apiKey          # the .apiKey field
```

- ESO polls Vault every `refreshInterval` (1h here).
- Creates and owns the target Secret.
- If Vault changes, the Secret is updated — but **pods don't restart**
  automatically. env-var-based Secret consumers need explicit rollout.
- The `template` field lets you rename fields between Vault and K8s.

### The five ExternalSecrets

| ExternalSecret | Target namespace | Vault path | Secret name | Consumer |
|----------------|------------------|------------|-------------|----------|
| `vllm-api-key` | `llama` | `secret/vllm/apiKey` | `vllm-api-key` | vLLM Deployment env var |
| `vllm-api-key` | `argo` | `secret/vllm/apiKey` | `vllm-api-key` | Argo eval/loadtest workflows |
| `hf-token` | `llama` | `secret/hf/token` | `hf-token` | vLLM Deployment env var (HF hub auth) |
| `hf-token` | `argo` | `secret/hf/token` | `hf-token` | Argo workflows (only needed if downloading a dataset) |
| `repo-github` | `argocd` | `secret/github/{url,username,password}` | `repo-github` | ArgoCD repo credentials |

## Seeding — `bootstrap/seed-vault.sh`

Run once after install (already called by `install.sh`). Steps:

1. **Wait for `vault-0` ready** — `kubectl -n vault wait --for=condition=Ready pod/vault-0 --timeout=300s`.
2. **Reuse existing vLLM API key** if `vault kv get -field=apiKey secret/vllm`
   returns non-empty. Prevents rotating a working key on every re-seed.
3. **Store HF token**:
   ```bash
   vault kv put secret/hf token="$HF_TOKEN"
   ```
4. **Store GitHub repo creds**:
   ```bash
   vault kv put secret/github url="$REPO_URL" \
     username="$GITHUB_USER" password="$GITHUB_TOKEN"
   ```
5. **Store vLLM API key**:
   ```bash
   vault kv put secret/vllm apiKey="$VLLM_API_KEY"
   ```
6. **Force ESO sync**: annotate each `ExternalSecret` with `force-sync=$now`
   to bypass the 1h refresh interval.
7. **Restart vLLM Deployment** — `kubectl -n llama rollout restart
   deploy/llama-llama-8b`. The container reads `VLLM_API_KEY` at startup;
   updating the Secret alone doesn't affect a running pod.

The script is idempotent — safe to re-run.

## Rotation

To rotate any credential:

1. **Update Vault**: `vault kv put secret/vllm apiKey=new-key`.
2. **Force sync**: `kubectl -n llama annotate externalsecret vllm-api-key
   force-sync=$(date +%s)` (or just wait an hour).
3. **Restart consumers** for env-var-mounted secrets:
   `kubectl -n llama rollout restart deploy/llama-llama-8b`.

For file-mounted secrets (volume projection with `subPath` etc.),
kubelet updates the file automatically within ~60 seconds; no restart
needed. The vLLM chart uses env vars, so restart is required.

## Moving to production Vault

Checklist:

- **Vault mode**: swap `server.dev.enabled: true` for Raft storage:
  ```yaml
  server:
    ha:
      enabled: true
      replicas: 3
      raft:
        enabled: true
    dataStorage:
      enabled: true
      storageClass: <your-persistent-sc>
      size: 10Gi
    auditStorage:
      enabled: true
  ```
- **TLS**: enable Vault's TLS listener; issue certs via cert-manager.
- **Auto-unseal**: use cloud KMS (AWS KMS, GCP KMS) — see the chart's
  `server.seal` block.
- **ESO auth**: switch `ClusterSecretStore` to Kubernetes Auth:
  ```yaml
  auth:
    kubernetes:
      role: eso
      mountPath: kubernetes
      serviceAccountRef:
        name: external-secrets
  ```
  On the Vault side, `vault auth enable kubernetes` and bind ESO's SA.
- **RBAC & policies**: create Vault policies per secret path; bind to
  the ESO role.
- **Backup**: `vault operator raft snapshot save` on a schedule.

## Debugging

- `kubectl -n llama describe externalsecret vllm-api-key` — shows the
  last sync status. `SyncSucceeded` condition is the one to check.
- `kubectl -n external-secrets logs deploy/external-secrets` — operator
  logs; look for `failed to authenticate` (token expired) or `secret not
  found` (bad Vault path).
- `kubectl -n vault exec vault-0 -- vault kv get secret/vllm` — sanity
  check the source data.
- Missing `token` field on the created Secret → check the `template`
  block. `{{ .apiKey }}` needs the key `apiKey` in `data.secretKey`, not
  the Vault property name.

## Extending / operating

- **Add a new secret** — put it in Vault, create an `ExternalSecret`
  under `secrets/`, commit. ArgoCD applies within 3 minutes.
- **Wait less on refresh** — reduce `refreshInterval` to `5m` for hotly
  rotated secrets. Trade Vault load for freshness.
- **Cross-account credentials** — one `ClusterSecretStore` per account,
  reference from `ExternalSecret.spec.secretStoreRef`.
- **Signed workload identity (SPIFFE)** — replace the token-based auth
  with `spiffe` provider in the SecretStore; requires a SPIRE deployment.

## Related docs

- Bootstrap flow: [`03-bootstrap-and-gitops.md`](03-bootstrap-and-gitops.md)
- Consuming the secrets in vLLM: [`06-inference-vllm.md`](06-inference-vllm.md)
- Consuming secrets in workflows: [`15-loadtests.md`](15-loadtests.md), [`16-evals.md`](16-evals.md)
