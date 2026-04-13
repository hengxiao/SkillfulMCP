# Deployment

Covers how SkillfulMCP gets packaged, run, and deployed. Closes the
Wave 7 slice of [`productization.md`](productization.md) §3.7.

## Components

Two services:

| Service  | Image                             | Exposes | Role                                   |
| -------- | --------------------------------- | ------- | -------------------------------------- |
| catalog  | `skillful-mcp/catalog:<tag>`      | `:8000` | FastAPI API; `/livez`, `/readyz`, REST + JWT auth + bundle store |
| webui    | `skillful-mcp/webui:<tag>`        | `:8080` | Operator console; calls catalog over HTTP using an admin key |

Both images ship the same Python package (`pyproject.toml` declares both
`mcp_server` and `webui`). Splitting into separate packages is tracked
as a future optimization; the size cost is marginal today.

## Dockerfiles

`deploy/Dockerfile.catalog` and `deploy/Dockerfile.webui`. Both follow
the same pattern:

1. **Builder stage** (`python:<ver>-slim`) installs the project + runtime
   deps into a venv at `/venv`. Build-essential is installed only in the
   builder to compile any sdists (bcrypt wheels, etc.).
2. **Runtime stage** copies `/venv` + application code, drops privileges
   to `uid 10000:gid 10000` (`mcp:mcp`), exposes the service port, and
   declares a `HEALTHCHECK` against `/livez` (catalog) / `/login`
   (webui).
3. `PATH` pre-prepended with `/venv/bin` so the image doesn't need pip
   at runtime.

### The `.dockerignore`

Strips everything outside what the image actually needs: `.git`, tests,
`spec/`, `example/`, caches, local env files. Keeps the build context
small and avoids baking local secrets into layers.

## docker-compose (local dev + integration)

`docker-compose.yml` at the repo root defines a 3-service stack:

- **postgres** (Postgres 16, health-checked with `pg_isready`).
- **catalog** (depends on a healthy postgres, reads `MCP_DATABASE_URL`
  pointing at it).
- **webui** (depends on a healthy catalog, reads `MCP_SERVER_URL`
  pointing at the catalog service name).

Secrets are **dev defaults only**: the compose file is never used for
staging or production. Override secrets by creating a
`docker-compose.override.yml` that sources them from a secret manager.

Ports published: `8000` (catalog), `8080` (webui), `5432` (postgres).

Targets:

```bash
make docker-build       # build both images
make docker-up          # run stack in foreground
make docker-up-detach   # background
make docker-down        # stop + drop volumes
```

## Helm chart

`deploy/helm/skillful-mcp/`. A minimal but production-shaped chart —
deploys the two services with sane defaults, integrates with an operator-
managed Secret, and includes the basics for scale + resilience.

### Resources rendered

| Template                    | Kind                     | Notes                                              |
| --------------------------- | ------------------------ | -------------------------------------------------- |
| `configmap.yaml`            | ConfigMap                | Non-secret env (JWT issuer, rate limit, bundle store, S3 prefix, log level) |
| `catalog-deployment.yaml`   | Deployment               | liveness `/livez`, readiness `/readyz`, pod/container security contexts, `tmp` emptyDir for readOnlyRootFilesystem |
| `catalog-service.yaml`      | Service (ClusterIP)      |                                                    |
| `catalog-hpa.yaml`          | HorizontalPodAutoscaler  | CPU-based, opt-out via `catalog.hpa.enabled=false` |
| `catalog-pdb.yaml`          | PodDisruptionBudget      | `minAvailable: 1` by default                       |
| `webui-deployment.yaml`     | Deployment               | Same shape as catalog; liveness/readiness `/login` |
| `webui-service.yaml`        | Service                  |                                                    |
| `webui-hpa.yaml`            | HPA                      |                                                    |
| `webui-pdb.yaml`            | PDB                      |                                                    |
| `ingress.yaml`              | Ingress                  | Opt-in. Routes `/` → webui, `/api/*` → catalog.    |
| `serviceaccount.yaml`       | ServiceAccount           | Created by default; override via `serviceAccount.name` |
| `NOTES.txt`                 | post-install notes       |                                                    |

### Secrets

**The chart does NOT create the Secret that holds sensitive values.**
This is deliberate — you want those rotated independently by External
Secrets / SealedSecrets / your cloud's KMS, not embedded in a Helm
release.

Provision a Secret named `{{ .Values.existingSecret }}` (default:
`skillful-mcp-secrets`) with keys:

```
MCP_JWT_SECRET               JWT signing secret (legacy single-key mode)
MCP_ADMIN_KEY                admin-key for catalog write endpoints
MCP_DATABASE_URL             postgresql://user:pass@host:5432/db
MCP_WEBUI_SESSION_SECRET     session cookie signing secret
MCP_WEBUI_OPERATORS          JSON array, e.g. [{"email":"...","password_hash":"..."}]
```

For multi-key JWT rotation add `MCP_JWT_KEYS` + `MCP_JWT_ACTIVE_KID`
(see [`mcp_server/keyring.md`](mcp_server/keyring.md)).

### Install

```bash
# Create the secret out-of-band, e.g.:
kubectl create secret generic skillful-mcp-secrets \
  --from-literal=MCP_JWT_SECRET=$(openssl rand -hex 32) \
  --from-literal=MCP_ADMIN_KEY=$(openssl rand -hex 32) \
  --from-literal=MCP_DATABASE_URL="postgresql://..." \
  --from-literal=MCP_WEBUI_SESSION_SECRET=$(openssl rand -hex 32) \
  --from-literal=MCP_WEBUI_OPERATORS='[{"email":"alice@example.com","password_hash":"$2b$..."}]'

helm upgrade --install mcp deploy/helm/skillful-mcp \
  --set image.registry=ghcr.io \
  --set image.repository=youraccount/skillful-mcp \
  --set image.catalogTag=0.1.0 \
  --set image.webuiTag=0.1.0 \
  --set ingress.enabled=true \
  --set ingress.host=mcp.example.com \
  --set ingress.tls.enabled=true
```

### Security defaults shipped

- Pods run as `uid 10000` (non-root), with `readOnlyRootFilesystem`,
  `allowPrivilegeEscalation: false`, all capabilities dropped,
  `seccompProfile: RuntimeDefault`.
- A `tmp` emptyDir is mounted at `/tmp` since several deps need scratch
  space that conflicts with `readOnlyRootFilesystem`.

## CI

`.github/workflows/ci.yml`. Five jobs, in rough order of runtime:

1. **lint** — `ruff check` (soft-fail for now; a dedicated pass will
   tighten rules).
2. **test-sqlite** — matrix on Python 3.11 + 3.12. Runs the full
   `pytest` suite against the default `:memory:` SQLite.
3. **test-postgres** — same suite against a Postgres 16 service
   container. `MCP_TEST_POSTGRES_URL` flips the Postgres-gated
   migration tests on.
4. **docker-build** — builds both images with `buildx` + GHA cache.
   Does not push — that's a separate release job owned by whoever
   controls the registry.
5. **helm-lint** — `helm lint` + a `helm template` dry-render with
   `existingSecret=mcp-test` so the `required` guard passes.

## Observability

Both services emit JSON logs to stdout via
`mcp_server.logging_config.JSONFormatter`. Every log line carries
`request_id`, matching the `X-Request-ID` response header, so a ticket
that includes the header maps directly to server logs.

Health probes:

- **`/livez`** — catalog only. Always 200 while the worker is up. Map
  to `livenessProbe`.
- **`/readyz`** — catalog. Verifies DB + settings. Map to
  `readinessProbe`. Fails 503 with a per-component breakdown, so a dead
  DB removes the pod from the service endpoint set without restarting
  the pod.
- **`/login`** — webui. Cheap 200 that bypasses the auth redirect; good
  enough for both liveness and readiness until Wave 8 lands a proper
  webui `/readyz`.

## Runbook

### Rotating the JWT secret (no multi-key)

1. `kubectl edit secret skillful-mcp-secrets` — change `MCP_JWT_SECRET`.
2. `kubectl rollout restart deployment/<name>-catalog`.
3. Every existing JWT immediately stops verifying. Clients re-auth via
   `POST /token`.

### Rotating the JWT secret (multi-key, zero-downtime)

Documented in [`mcp_server/keyring.md`](mcp_server/keyring.md#rotation-playbook).
TL;DR: add new kid alongside old → deploy → flip active kid → deploy
→ wait for old-kid tokens to expire → remove old kid → deploy.

### Rotating the session secret

`MCP_WEBUI_SESSION_SECRET` change + webui deploy. Every logged-in
operator gets bumped to `/login` on their next request. Expected — use
when investigating a compromise.

### Revoking a specific agent's token

```bash
# Get the jti of the token from server logs.
curl -X POST https://mcp.example.com/api/admin/tokens/revoke \
  -H "X-Admin-Key: $MCP_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jti": "abc123..."}'
```

In-process deny list (see [`mcp_server/revocation.md`](mcp_server/revocation.md))
— multi-replica deployments need the Redis backend, still TODO.

### Migrating bundle bytes from inline → S3

Code path shipped in Wave 5; the one-shot data migration is still a
follow-up. When implemented, it reads every `SkillFile` row, uploads
`content` to S3 under `{prefix}/pk{skill_pk}/{path}`, then clears the
`content` column. Flip `MCP_BUNDLE_STORE=s3` + `MCP_BUNDLE_S3_BUCKET`
and restart.

### Reading logs for a specific request

Every log line JSON carries `request_id`. Clients get the id in the
`X-Request-ID` response header. Correlate:

```bash
kubectl logs -l app.kubernetes.io/component=catalog | jq -c 'select(.request_id == "abc123...")'
```

## Known gaps (future waves)

- **Push to registry in CI.** Currently `docker-build` runs on every
  PR but doesn't publish. A release workflow should build on tag, push
  to a registry the cluster can pull from, and optionally trigger a
  Helm upgrade in staging.
- **External Secrets wiring in the chart.** The chart expects a Secret
  to already exist; for production-grade it should template an
  `ExternalSecret` resource pointing at your cloud's KMS.
- **Prometheus metrics.** Structured logs are in; metrics export isn't.
  Productization §3.6.
- **Blue/green deploys.** HPA + PDB give safe rolling updates, but
  there's no automated rollback on SLO burn. Productization §3.7 P1.
- **Managed offerings.** No Cloud Run / Lambda / Azure Container Apps
  packaging; teams that don't run Kubernetes do it themselves. §3.7 P2.
- **Rate limiting is per-process.** Multi-replica deployments need a
  Redis-backed bucket (§3.3); today each replica honors its own limit
  so the effective rate is N× configured.
- **Revocation list is per-process.** Same Redis dependency
  (§3.1).
