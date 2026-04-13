# Azure Deployment Plan

How to take SkillfulMCP from `docker compose up` (current state) to a
production-grade Azure deployment. Companion to
[`deployment.md`](deployment.md), which covers the local + Helm
mechanics that this plan composes on top of.

The plan is **phased** so you can land step 1 in a day and decide
whether to go further. Each phase is independently shippable.

---

## 1. Service mapping

Every running piece of the stack maps to an Azure-native counterpart.
Pick the row that matches the operating posture you want.

| Local component | Recommended Azure service | Alternative |
| --------------- | ------------------------ | ----------- |
| **catalog + webui containers** | Azure Container Apps | AKS (Helm chart already exists), Azure App Service for Containers |
| **Postgres 16** | Azure Database for PostgreSQL — Flexible Server | Cosmos DB for PostgreSQL (Citus) for horizontal scale |
| **S3 bundle store** | Azure Blob Storage *via S3-compatible SDK shim* | Stay on AWS S3 (cross-cloud); Azure Container Storage |
| **SMTP invitations** | Azure Communication Services Email | SendGrid (Azure Marketplace), Mailgun, in-VNet Postfix |
| **OIDC identity provider** | Microsoft Entra ID | Auth0/Cognito/Okta still work — codebase is provider-agnostic |
| **JWT signing keys** | Azure Key Vault (HSM-backed) | In-cluster Kubernetes Secret (lower trust tier) |
| **Logs (JSON stdout)** | Container Apps → Log Analytics workspace via `Microsoft.App/managedEnvironments/logsConfiguration` | Azure Monitor + Diagnostic settings |
| **Audit table queries** | already in Postgres; surface via Grafana on App Service | Power BI direct-query if your audit team lives there |
| **Object signing keys (Ed25519 bundle signatures)** | Key Vault (separate from JWT key for blast-radius split) | Azure Managed HSM if you need FIPS 140-2 Level 3 |
| **Container registry** | Azure Container Registry (ACR) | GHCR + ACR pull-through cache |
| **CI/CD** | GitHub Actions → ACR → Container Apps revisions | Azure DevOps Pipelines |
| **Secrets** | Azure Key Vault + Container Apps secret refs | Kubernetes Secrets backed by Key Vault via Secrets Store CSI Driver (AKS only) |
| **TLS + custom domain** | Container Apps custom domain + Azure-managed cert | Azure Front Door (multi-region) or Application Gateway |
| **Observability** | Azure Monitor / Application Insights for metrics + traces; Log Analytics for logs | LGTM stack on AKS (matches local dev) |

The S3 row is the only one with friction. See [§5](#5-bundle-storage-on-azure) for the choice and the trade-offs.

---

## 2. Topology — recommended starting point

```
                 ┌─────────────────────────────────────┐
                 │   Microsoft Entra ID (OIDC IdP)     │
                 └───────────────┬─────────────────────┘
                                 │ id_token via /auth/oidc/callback
                                 ▼
┌─────────────────────────────────────────────────────────────┐
│  Azure Front Door (TLS + WAF)                                │
└────────────────┬───────────────────────────┬────────────────┘
                 │                           │
                 ▼                           ▼
   ┌──────────────────────┐     ┌──────────────────────┐
   │ Container App: webui │     │ Container App: catalog│
   │  - 1-3 replicas      │────►│  - 2-5 replicas (HPA) │
   │  - sticky=off        │     │  - reads JWKS public  │
   └──────────────────────┘     └──────────┬───────────┘
                                            │
                            ┌───────────────┼───────────────┐
                            ▼               ▼               ▼
              ┌────────────────────┐  ┌──────────────┐  ┌─────────────────┐
              │ Azure DB for       │  │ Azure Blob   │  │ Azure Key Vault │
              │ PostgreSQL Flex    │  │ Storage      │  │  - JWT private  │
              │ (private endpoint) │  │ (S3 SDK via  │  │  - bundle Ed25519│
              │                    │  │  Blob S3)    │  │  - SMTP creds   │
              └────────────────────┘  └──────────────┘  └─────────────────┘

              ┌────────────────────────────────────┐
              │ Azure Communication Services Email │
              │ (catalog → invitee MX)             │
              └────────────────────────────────────┘

              ┌────────────────────────────────────┐
              │ Log Analytics workspace            │
              │ Application Insights (optional)    │
              └────────────────────────────────────┘
```

**Why Container Apps as the default**: scales-to-zero on idle, built-in
ingress + revisions, no node management, and the smallest leap from
the existing Dockerfiles. AKS becomes the right answer when you need
sidecars (e.g. an ambassador for VPC-only network egress) or want to
reuse the Helm chart in [`deploy/helm/`](../deploy/helm/) — both work,
this plan picks Container Apps as the lower-friction first deploy.

---

## 3. Phased rollout

Each phase ships independently and stays valuable on its own. The
progression goes from "single-region MVP" to "production-hardened".

### Phase 1 — minimum viable Azure deployment (1–2 days)

**Goal**: app reachable on a `*.azurecontainerapps.io` URL with
managed Postgres. Login with the env-bootstrap superadmin works.
Bundle storage stays inline (no Blob yet).

1. Provision via Bicep / Terraform / portal:
   - 1 Resource Group (`rg-mcp-prod`).
   - 1 Azure Container Registry (basic tier).
   - 1 Log Analytics workspace.
   - 1 Container Apps environment.
   - 1 Azure Database for PostgreSQL — Flexible Server (Burstable
     B1ms is fine for dev/staging; private endpoint, AAD admin
     enabled).
   - 1 Key Vault with the secrets:
     - `MCP-JWT-SECRET` (random 64-byte URL-safe string)
     - `MCP-ADMIN-KEY`
     - `MCP-WEBUI-SESSION-SECRET`
     - `MCP-SUPERADMIN-PASSWORD-HASH`
     - `MCP-DATABASE-URL` (with the connection string)

2. Build + push images:
   ```bash
   az acr login --name mcpacr
   docker build -f deploy/Dockerfile.catalog -t mcpacr.azurecr.io/catalog:0.1.0 .
   docker build -f deploy/Dockerfile.webui   -t mcpacr.azurecr.io/webui:0.1.0   .
   docker push mcpacr.azurecr.io/catalog:0.1.0
   docker push mcpacr.azurecr.io/webui:0.1.0
   ```

3. Create the two container apps. Bind each Key Vault secret as a
   Container Apps secret reference (uses managed identity, no
   plaintext in env). Catalog app gets:
   - `secretRef` for the four MCP_* env vars.
   - `MCP_DATABASE_URL` from secret (Postgres connection string with
     `?sslmode=require`).
   - Min 1 replica, max 5; HTTP scaling rule on requests.
   - Ingress: internal-only OR external (depending on whether you
     want catalog reachable from outside).

4. Webui app: same shape, `MCP_SERVER_URL=https://catalog.<env>.<region>.azurecontainerapps.io`.

5. Smoke test from the Container Apps URL:
   ```bash
   curl https://webui-...azurecontainerapps.io/login   # 200
   curl https://catalog-...azurecontainerapps.io/readyz # ready
   ```

**Out of scope at Phase 1**: custom domain, Blob storage, SMTP, OIDC,
asymmetric JWT keys.

### Phase 2 — Blob bundle storage (1 day)

Switch `MCP_BUNDLE_STORE` from `inline` to `s3` and route to Azure Blob.

1. Create a Storage account (`stmcpprod`) + container (`mcp-bundles`).
2. Enable the Blob storage S3-compatible API:
   - Currently only available as a preview via the
     [`Microsoft.Storage` S3-compat extension](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-s3-protocol).
     Decide between (a) using the preview, (b) using AWS S3 directly
     across clouds (works fine — boto3 doesn't care which cloud the
     endpoint lives in), or (c) writing a thin `AzureBlobBundleStore`
     class. See [§5](#5-bundle-storage-on-azure) for the trade-offs.
3. If using S3-compat preview: store the access key in Key Vault as
   `MCP-BUNDLE-S3-KEY-ID` + `MCP-BUNDLE-S3-SECRET`. Set
   `MCP_BUNDLE_S3_ENDPOINT_URL` to the S3-compat endpoint.
4. Update catalog env:
   - `MCP_BUNDLE_STORE=s3`
   - `MCP_BUNDLE_S3_BUCKET=mcp-bundles`
   - `MCP_BUNDLE_S3_REGION=<region>`
   - `MCP_BUNDLE_S3_ENDPOINT_URL=<endpoint>`
   - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from secrets.
5. Roll the catalog revision. Verify a bundle upload writes to Blob
   (`az storage blob list -c mcp-bundles -o table`).

### Phase 3 — secrets out of env, into Key Vault (½ day)

Container Apps already pulls our secrets via `secretRef` in Phase 1.
The hardening step is:

1. Add a User-Assigned Managed Identity (`umi-mcp`).
2. Grant the UMI **Get** + **List** on Key Vault secrets via RBAC.
3. Reconfigure each Container App to use the UMI and reference
   secrets directly with the Key Vault URL syntax
   (`@Microsoft.KeyVault(SecretUri=...)`). No more secrets stored on
   the Container App resource itself.
4. Rotate every secret you put in plaintext during Phase 1 — they
   live in Container App revisions until purged.

### Phase 4 — Microsoft Entra OIDC (½ day)

The OIDC code path (item G) works against any standards-compliant
provider. For Entra:

1. Register a single-page-app or web-app in Entra ID:
   - Redirect URI: `https://webui-…azurecontainerapps.io/auth/oidc/callback`
   - ID token claims: `email`, `name`, `oid`.
2. Add a client secret (or use certificate auth — but the current
   code path only supports client_secret).
3. Set webui env:
   ```
   MCP_OIDC_ISSUER_URL     = https://login.microsoftonline.com/<tenant-id>/v2.0
   MCP_OIDC_CLIENT_ID      = <app id>
   MCP_OIDC_CLIENT_SECRET  = <Key Vault secret>
   MCP_OIDC_REDIRECT_URI   = https://webui-…/auth/oidc/callback
   MCP_OIDC_SCOPES         = openid email profile
   ```
4. The "Sign in with SSO" button appears on `/login`. First sign-in
   auto-creates the user via the existing `/admin/signup` flow.

### Phase 5 — asymmetric JWT + JWKS endpoint (½ day)

Move from HMAC `MCP_JWT_SECRET` to RSA + Key Vault.

1. Generate an RSA-2048 key in Key Vault: `JWT-SIGNING-KEY`.
2. Export the **private** PEM into a separate secret
   (`MCP-JWT-PRIVATE-KEY-PEM`) — Container Apps' Key Vault secret
   refs return strings, not certificate bundles. Mount it via
   `secretRef`.
3. Set `MCP_JWT_PRIVATE_KEY_PEM` from the secret. The keyring auto-
   switches to RS256.
4. `/.well-known/jwks.json` now publishes the public JWK; external
   verifiers (gateways, partner services) fetch it once.
5. Operational note: rotation is a Key Vault operation —
   re-import a new private key, delete the old one, deploy. Wave 9
   item I has the design seam for a real KMS provider; until then
   this Key-Vault-as-PEM-store path is the bridge.

### Phase 6 — SMTP invitations (Azure Communication Services Email) (1 day)

1. Provision an Azure Communication Services resource +
   Email Communication Service.
2. Verify a sender domain (DNS records: SPF + DKIM).
3. Create a Communication Services connection string; store as
   `MCP-ACS-CONNECTION-STRING` in Key Vault.
4. Two options:
   - **Easy path**: ACS exposes SMTP relay. Use:
     ```
     MCP_SMTP_HOST=smtp.azurecomm.net
     MCP_SMTP_PORT=587
     MCP_SMTP_TLS=1
     MCP_SMTP_USER=<acs username>
     MCP_SMTP_PASSWORD=<from secret>
     MCP_SMTP_FROM=noreply@your-verified-domain.com
     MCP_WEBUI_PUBLIC_URL=https://webui-….azurecontainerapps.io
     ```
   - **Better path** (future): replace `mcp_server.mailer.SMTPMailer`
     with an `ACSMailer` that uses the ACS Email SDK directly;
     gives you delivery callbacks and templated bodies.
5. Verify by inviting an unknown email → ACS Email Insights shows
   the message.

### Phase 7 — observability (½ day)

Container Apps streams stdout to Log Analytics by default when the
environment is wired to a workspace. The structured-JSON formatter
(now load-bearing after the recent migrations/env.py fix) lets
Log Analytics parse `request_id`, `level`, `action`, etc. as
KQL columns:

```kql
ContainerAppConsoleLogs_CL
| where ContainerName_s == "catalog"
| where Log_s contains "audit"
| extend p = parse_json(Log_s)
| project Time = todatetime(p.ts), action = p.action,
          actor = p.actor_email, account = p.account_id, request = p.request_id
| order by Time desc
| take 100
```

Optional: enable Application Insights on each app for
distributed-tracing visibility (the catalog and webui already emit
`X-Request-ID` headers that AI can stitch into a single trace).

### Phase 8 — production-hardening (1 week, do once)

Items that aren't blocking but should land before "real" prod traffic:

- **Custom domain** on each app (`api.skillful-mcp.example.com`,
  `app.skillful-mcp.example.com`) with managed certificates.
- **Front Door** in front of webui for WAF + multi-region failover.
- **Private endpoint** for Postgres + Storage (catalog runs in a
  Container Apps environment with `internalLoadBalancerEnabled` and
  reaches data services privately).
- **Backup policy** on Postgres (built-in, just confirm retention).
- **Diagnostic settings** on every resource → Log Analytics +
  optionally archive to Blob.
- **Alerts**: `/readyz` 5xx-ratio > 1%/5min; Postgres CPU > 80% sustained;
  Key Vault auth failures > 0; SMTP send failures.
- **Rotation calendar**: superadmin password (90d), JWT signing key
  (180d), admin key (90d).

---

## 4. Bicep skeleton (Phase 1 starter)

```bicep
// main.bicep
param location string = resourceGroup().location
param env string = 'prod'

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-mcp-${env}'
  location: location
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'mcpacr${env}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: 'pg-mcp-${env}'
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: 'mcpadmin'
    administratorLoginPassword: pgPassword  // pass as @secure() param
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
  }
}

resource caEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-mcp-${env}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: listKeys(law.id, '2023-09-01').primarySharedKey
      }
    }
  }
}

// catalog + webui Container Apps follow — see the full Bicep in
// deploy/azure/main.bicep (to be added).
```

A complete Bicep template lives in [`deploy/azure/main.bicep`](../deploy/azure/main.bicep)
when this plan is implemented. Wire it into the existing GitHub
Actions workflow with `azure/login@v2` + `azure/arm-deploy@v2`.

---

## 5. Bundle storage on Azure

The mismatch: `mcp_server.bundles.S3BundleStore` uses boto3 against
an S3 endpoint. Azure Blob isn't S3 by default. Three viable paths:

**(a) AWS S3 directly.** Cross-cloud is fine — boto3 connects to
whichever endpoint you point it at. Cost is data egress fees if your
Container Apps live in Azure but the bucket is in AWS. Easy
day-one path; revisit when traffic justifies removing the dependency.

**(b) Azure Blob S3-compatible API (preview).** Microsoft ships an
S3-compat layer for Blob. As of writing it's preview; check GA
status before committing. When GA, this is the no-code-change path.

**(c) Native `AzureBlobBundleStore`.** ~150 LoC: subclass
`BundleStore`, use `azure-storage-blob` SDK, register under
`MCP_BUNDLE_STORE=azure_blob`. Most ergonomic for Azure-native
deployments; least portable.

**Recommendation**: start with (a) (AWS S3 if you have one;
otherwise the preview (b)). When there's a real production volume
to optimize, ship (c) and document the migration story.

---

## 6. CI/CD wiring

Existing CI (`.github/workflows/ci.yml`) covers tests + Docker
builds. For Azure, add a **deploy** workflow gated on `master`:

```yaml
name: Deploy to Azure
on:
  push:
    branches: [master]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write           # OIDC federated creds
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      - name: Build + push images
        run: |
          az acr login --name mcpacr
          tag="${GITHUB_SHA::8}"
          docker build -f deploy/Dockerfile.catalog -t mcpacr.azurecr.io/catalog:$tag .
          docker build -f deploy/Dockerfile.webui   -t mcpacr.azurecr.io/webui:$tag   .
          docker push mcpacr.azurecr.io/catalog:$tag
          docker push mcpacr.azurecr.io/webui:$tag
      - name: Roll Container App revisions
        run: |
          az containerapp update -n ca-catalog -g rg-mcp-prod \
            --image mcpacr.azurecr.io/catalog:${GITHUB_SHA::8}
          az containerapp update -n ca-webui   -g rg-mcp-prod \
            --image mcpacr.azurecr.io/webui:${GITHUB_SHA::8}
```

Use **federated credentials** on a GitHub Actions service principal
(no client secrets in GitHub).

---

## 7. Cost ballpark (single-region, light traffic)

| Component | Estimated monthly USD |
| --------- | --------------------: |
| Container Apps (2 apps, 0.5 vCPU + 1Gi each, 24/7) | ~$30 |
| Postgres Flexible Burstable B1ms + 32 GB | ~$25 |
| Storage Account (10 GB Blob + minimal ops) | ~$2 |
| Key Vault (standard tier, low ops) | ~$3 |
| Log Analytics (5 GB ingest/mo) | ~$15 |
| ACR Basic | ~$5 |
| **Total** | **~$80/mo** |

Costs scale most aggressively with Container Apps replicas + Log
Analytics ingest. A 5-replica catalog with verbose logging easily
hits $200–$400/mo.

---

## 8. Security posture checklist

- [ ] All secrets in Key Vault, never inline in Container App env.
- [ ] Postgres reachable only via private endpoint.
- [ ] catalog admin-key path only reachable from inside the
      Container Apps environment (set ingress to internal).
- [ ] webui ingress is external; gated by Microsoft Entra OIDC.
- [ ] JWT signing keys are RS256 in Key Vault, not HMAC env strings.
- [ ] Bundle signing public keys deployed via env from Key Vault;
      private signing keys live OUTSIDE Azure (with the skill
      authors).
- [ ] `MCP_SUPERADMIN_PASSWORD_HASH` rotated every 90 days; rotation
      tested via `mcp-cli superadmin rotate --dry-run`.
- [ ] CI deployment uses federated creds, not stored client secrets.
- [ ] Diagnostic settings forward Container Apps logs + Postgres
      audit logs to Log Analytics with 90-day retention minimum.
- [ ] Alerts on: 5xx ratio, Postgres CPU + storage, Key Vault auth
      failures, missed SMTP deliveries.

---

## 9. DR + rollback

- **Postgres**: Flexible Server includes built-in PITR (point-in-time
  restore) for the retention window; the Bicep above sets 7 days.
  Bump to 35 for production.
- **Container Apps revisions**: every push creates a new revision;
  rolling back is `az containerapp revision activate -n ... --revision <previous>`.
- **Bundles**: enable Blob soft-delete + container soft-delete on the
  storage account. 7 days minimum; 30 days for prod.
- **Multi-region**: replicate Postgres via read replicas + use Azure
  Front Door's origin failover. Webui sessions are cookie-signed so
  they survive backend swaps as long as `MCP_WEBUI_SESSION_SECRET`
  is the same across regions.

---

## 10. Open decisions before kickoff

1. **Container Apps vs AKS.** This plan picks Container Apps. AKS
   makes sense if (a) you already operate AKS, (b) you need
   sidecars, or (c) you want to run the existing Helm chart
   verbatim. Pick before Phase 1.
2. **S3 path** — see [§5](#5-bundle-storage-on-azure). Pick before
   Phase 2.
3. **Identity provider** — Microsoft Entra is the natural choice on
   Azure but the codebase is OIDC-generic. If your org uses a
   different IdP, set `MCP_OIDC_ISSUER_URL` to that one.
4. **SMTP provider** — ACS Email vs SendGrid vs in-VNet relay. ACS
   keeps everything in-tenant; SendGrid often has better deliverability.
5. **Custom domain** — book the cert + DNS work for Phase 8 even if
   you start on `*.azurecontainerapps.io` URLs.

---

## 11. What's already wired

The repo is closer to Azure-ready than the plan implies because every
external dependency is gated by env vars:

| Capability | Env knob | Production mode |
| ---------- | -------- | --------------- |
| Postgres   | `MCP_DATABASE_URL` | `postgresql://...?sslmode=require` |
| S3 bundles | `MCP_BUNDLE_STORE=s3` + `MCP_BUNDLE_S3_*` | Blob via S3-compat OR direct AWS |
| SMTP       | `MCP_SMTP_HOST` + `MCP_SMTP_*` | ACS / SendGrid |
| OIDC       | `MCP_OIDC_*` | Entra / Auth0 / etc. |
| RS256 JWT  | `MCP_JWT_PRIVATE_KEY_PEM` | Key Vault secret |
| Audit log  | always on; in `audit_events` Postgres table | Power BI / Grafana / KQL |
| Rate limit | `MCP_RATE_LIMIT_PER_MINUTE` | per-IP token bucket; tighten in prod |
| Bundle sigs| `MCP_BUNDLE_SIGNING_PUBLIC_KEYS` | JSON of trusted Ed25519 pubkeys |

So the Azure deployment work is mostly **provisioning + secret
plumbing**, not code. Phase 1 lands within a working day for a
single operator.

---

## 12. Where to read next

- [`spec/deployment.md`](deployment.md) — the underlying Helm + Docker
  mechanics this plan composes on.
- [`spec/productization.md`](productization.md) §3.7 — original
  packaging / deploy roadmap.
- [`spec/visibility-and-accounts.md`](visibility-and-accounts.md) +
  [`spec/user-management.md`](user-management.md) — operator + tenant
  story; everything in Phase 4 + 6 references their flows.
- [`deploy/helm/skillful-mcp/`](../deploy/helm/skillful-mcp/) — Helm
  chart usable on AKS if you go that route.
