# 08 — Gateway (Envoy Gateway + Gateway API)

Envoy Gateway is the L7 ingress. It's installed via Helm (v1.3.2) and
configured via upstream Gateway API resources (`GatewayClass`, `Gateway`,
`HTTPRoute`) plus Envoy Gateway-specific policies (`BackendTrafficPolicy`,
`EnvoyExtensionPolicy`, `EnvoyProxy`).

Everything the outside world sees enters through **one Envoy Deployment**
listening on `:8080`.

## Files

```
apps/gateway-api-crds.yaml      Upstream Gateway API v1.2.1 CRDs (wave -6)
apps/envoy-gateway.yaml         Envoy Gateway Helm chart v1.3.2 (wave -4)
apps/gateway.yaml               ArgoCD Application pointing at gateway/ (wave -2)
gateway/
├── gatewayclass.yaml           GatewayClass eg
├── gateway.yaml                Gateway public listening on 8080
├── envoyproxy-config.yaml      EnvoyProxy config (Prometheus enabled)
└── envoy-podmonitor.yaml       PodMonitor for Envoy stats
apps/httproutes.yaml            ArgoCD Application → httproutes/ (wave 11)
httproutes/
├── vllm.yaml                   /v1/** → vLLM
├── vllm-ratelimit.yaml         BackendTrafficPolicy: 60 req/min
├── vllm-epp-extension.yaml     EnvoyExtensionPolicy: ext_proc → EPP
├── argocd.yaml                 /argocd/** → argocd-server
├── grafana.yaml                /grafana/** → Grafana
├── argo-workflows.yaml         /argo/** → Argo Workflows UI
└── root-redirect.yaml          / → /argocd/ (302)
```

## The Gateway resource

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: envoy-gateway-system
spec:
  gatewayClassName: eg
  listeners:
    - name: http
      protocol: HTTP
      port: 8080
      allowedRoutes:
        namespaces:
          from: All
```

- **Single listener** on 8080 (HTTP). Brev port-forwards this to the
  user's browser; no cloud LB, no TLS certificate needed at this layer.
- **`allowedRoutes.namespaces.from: All`** — HTTPRoutes in *any*
  namespace can attach. Needed because the `vllm` HTTPRoute lives in
  `llama`, `grafana` HTTPRoute in `monitoring`, etc.

For production behind a real domain, add:

- A second `listener` on `443` with a TLS `Secret` reference.
- A `ReferenceGrant` in the namespace holding the cert.

## EnvoyProxy config — Prometheus metrics

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata:
  name: default
  namespace: envoy-gateway-system
spec:
  telemetry:
    metrics:
      prometheus:
        disable: false
```

Envoy exposes its full stats page (~1200 counters, gauges, histograms) at
`/stats/prometheus` on the admin port. The `EnvoyProxy` CR tells Envoy
Gateway to configure that endpoint so `PodMonitor` can scrape it.

`gateway/envoy-podmonitor.yaml` — `PodMonitor` selecting
`app.kubernetes.io/managed-by=envoy-gateway` pods, scraping
`/stats/prometheus` every 15s with `release: kps` label.

## HTTPRoutes

### `vllm` — the inference route

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: vllm
  namespace: llama
spec:
  parentRefs:
    - name: public
      namespace: envoy-gateway-system
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /v1
      backendRefs:
        - name: llama-llama-8b
          port: 8000
```

- `parentRefs` — attaches to the `public` Gateway across namespaces.
  Requires the Gateway's `allowedRoutes.namespaces.from: All`.
- Matches `/v1/**` (OpenAI paths: `/v1/chat/completions`,
  `/v1/completions`, `/v1/models`).
- Backend is the vLLM Service, port 8000.

### `vllm-ratelimit` — coarse safety net

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: vllm-ratelimit
  namespace: llama
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: vllm
  rateLimit:
    type: Local
    local:
      rules:
        - limit:
            requests: 60
            unit: Minute
```

- **Local** (not distributed) — Envoy tracks the counter in-process, no
  Redis. Fine for single-pod Envoy Gateway.
- 60 req/min hard cap. Not per-tenant, not per-model — just a floor to
  prevent stampedes from overwhelming the platform during an incident.

For per-tenant limits you'd add `clientSelectors` filtering on a header
(e.g. `X-Tenant-ID`).

### `vllm-epp-extension` — the ext_proc wiring

See [`07-inference-extension-epp.md`](07-inference-extension-epp.md#how-epp-plugs-into-envoy-gateway).

### `argocd`, `grafana`, `argo-workflows` — UI routes

Each is a prefix match with a URL rewrite. Example (`argo-workflows.yaml`):

```yaml
rules:
  - matches:
      - path:
          type: PathPrefix
          value: /argo
    filters:
      - type: URLRewrite
        urlRewrite:
          path:
            type: ReplacePrefixMatch
            replacePrefixMatch: /
    backendRefs:
      - name: argo-workflows-server
        namespace: argo
        port: 2746
```

- Strip `/argo` from the path before forwarding, because Argo Workflows
  server expects to serve from `/`.
- ArgoCD is configured to serve from `/argocd` natively (via the
  `argocd-cmd-params-cm` patch in `install.sh`), so no rewrite is needed.
- Grafana uses `GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/grafana` and
  `GF_SERVER_SERVE_FROM_SUB_PATH=true` in `apps/kube-prometheus-stack.yaml`.

### `root-redirect` — landing page

```yaml
rules:
  - matches:
      - path:
          type: Exact
          value: /
    filters:
      - type: RequestRedirect
        requestRedirect:
          path:
            type: ReplaceFullPath
            replaceFullPath: /argocd/
          statusCode: 302
```

Bare `/` sends the user to the ArgoCD UI as the default landing page.

## Envoy Gateway Helm config

Highlights from `apps/envoy-gateway.yaml`:

```yaml
config:
  envoyGateway:
    provider:
      type: Kubernetes
      kubernetes:
        rateLimitDeployment:
          replicas: 1        # single-node — no HA
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    memory: 512Mi
```

The controller ("envoy-gateway") reconciles Gateway CRDs into Envoy config
and provisions the data-plane Deployment (named after the Gateway:
`envoy-envoy-gateway-system-public-<hash>`).

## How a request flows through Envoy

1. Client → Envoy `:8080`.
2. Envoy matches the Route by hostname and path prefix.
3. **BackendTrafficPolicy** rate limit — reject if over 60 req/min.
4. **EnvoyExtensionPolicy** ext_proc:
   - Envoy buffers the request body.
   - Sends `ProcessRequestBody` to EPP over gRPC.
   - EPP returns `x-gateway-destination-endpoint: <pod-ip>:8000`.
5. Envoy sets the destination endpoint and forwards.
6. Response streams back through Envoy to the client.

## Debugging

- **Envoy config** — `kubectl -n envoy-gateway-system exec deploy/envoy-envoy-gateway-system-public-<hash> -c envoy -- curl -s localhost:19000/config_dump | jq .` shows the live Envoy config.
- **Stats** — same pod, `curl localhost:19001/stats/prometheus`.
- **Access logs** — Envoy Gateway ships default access logs to stdout;
  visible in Loki.
- **HTTPRoute status** — `kubectl describe httproute vllm -n llama` shows
  `parentRef` conditions (`Accepted`, `ResolvedRefs`). If either is
  `False`, the route isn't attached.
- **BackendTrafficPolicy not applying** — check `targetRefs` name and
  namespace. Envoy Gateway logs (`deploy/envoy-gateway` in
  `envoy-gateway-system`) show translation errors.

## Extending / operating

- **HTTPS listener** — add a second listener with `protocol: HTTPS`, a
  `tls.certificateRefs`, and a matching `Secret`. Point Route to the new
  listener with `parentRefs[0].sectionName`.
- **Rate limit per client IP** — add
  `local.rules[0].clientSelectors[0].sourceCIDR` matches.
- **Global (distributed) rate limit** — switch policy from `type: Local`
  to `type: Global` and enable Envoy Gateway's Redis-based rate limit
  service (extra Deployment).
- **Access log to JSON** — add `logging: level: json` under
  `envoyGateway.telemetry`.
- **Multiple hostnames** — add `spec.hostnames` on each HTTPRoute; add
  `hostname:` to the Gateway listener.

## Related docs

- Endpoint Picker: [`07-inference-extension-epp.md`](07-inference-extension-epp.md)
- Gateway dashboard: [`13-dashboards.md`](13-dashboards.md)
- Request path diagram: [`../images/inference-request-path.mmd`](../images/inference-request-path.mmd)
