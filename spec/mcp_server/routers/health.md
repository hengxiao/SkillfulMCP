# mcp_server/routers/health.py

Liveness + readiness probes.

## Endpoints

| Path       | What it checks                                              | Codes |
| ---------- | ----------------------------------------------------------- | ----- |
| `/health`  | Legacy alias. Always 200 as long as the worker responds.    | 200   |
| `/livez`   | Process alive. No dependency checks.                        | 200   |
| `/readyz`  | Settings loaded + DB reachable. Per-component status in body. | 200 / 503 |

## `/readyz` body

```json
{
  "status": "ready" | "not_ready",
  "components": {
    "settings": "ok" | "fail: ...",
    "db":       "ok" | "fail: ..."
  }
}
```

The `db` check runs `SELECT 1` through the normal `get_db` session. The
`settings` check ensures `get_settings()` succeeds and `jwt_secret` is
non-empty. Returns 503 if either fails.

## Usage with Kubernetes

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 8000 }
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
```

Liveness failure restarts the pod; readiness failure de-pools it from the
service endpoint. Keeping them separate means a DB hiccup won't restart
pods, just drain traffic.

## Future work

- Add a bundle-store reachability check once S3 storage lands
  (productization §3.2).
- Add a JWT key-ring check once rotation lands (productization §3.1).
- Emit readiness metrics for dashboards.

## Testing

`tests/test_observability.py::TestHealthEndpoints` — `/livez`, `/readyz`
success, legacy `/health` kept working.
