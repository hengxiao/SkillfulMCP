# mcp_server/middleware.py

HTTP middleware. Three middlewares ship today; auth / tenant middleware
will layer on top later.

## Stack order

`app.add_middleware` prepends to Starlette's user-middleware list, so the
**last-added** middleware is outermost (runs first on request ingress).

```
RequestIDMiddleware             outermost  (added last)
  → RequestSizeLimitMiddleware
    → RateLimitMiddleware       innermost  (added first)
      → handler
```

Rationale:
- RequestID first so every downstream log line and error response carries
  the id — including the 413 / 429 responses the next two middlewares
  emit.
- SizeLimit before RateLimit so oversize bodies are rejected without
  burning a rate-limit token.
- RateLimit is the last gate before the handler runs.

## `RequestIDMiddleware`

Per request:

1. Reads `X-Request-ID` from the request; if absent, generates a UUID4 hex.
2. `set_request_id(rid)` — stores in the ContextVar that `JSONFormatter`
   reads.
3. Runs the handler, measuring `time.perf_counter()` latency.
4. On success: attaches `X-Request-ID` to the response, logs an INFO
   `"request"` line with `{method, path, status, latency_ms}`.
5. On unhandled exception: logs at EXCEPTION level (without `status`),
   clears the ContextVar, re-raises so FastAPI's exception chain runs.
6. Always clears the ContextVar so no leakage between requests.

Header name is exported as `HEADER = "X-Request-ID"` so `errors.py` and
the 429/413 handlers reuse the constant.

## `RequestSizeLimitMiddleware(app, *, max_bytes)`

Rejects requests whose `Content-Length` exceeds `max_bytes` with a `413`
response carrying the typed envelope + `X-Request-ID`. `max_bytes <= 0`
disables the middleware.

**Known gap**: chunked transfers have no `Content-Length`; this middleware
does NOT read the body stream to enforce a cap on such requests. The
reverse proxy / ingress should handle those. Documented alongside
productization §3.3.

Coexists with the bundle endpoint's own 100 MB archive check:
- App-level cap (this middleware) — infra safety, default 101 MB.
- Bundle endpoint cap — business rule, 100 MB. Runs once the request
  gets to `routers/bundles.py`.

## `RateLimitMiddleware(app, *, limiter, exempt_paths)`

Per-IP token-bucket throttling, delegating to `ratelimit.TokenBucket`.

- **Key** is `request.client.host` (`_client_key` helper). Proxy-aware
  resolution (`X-Forwarded-For`) is deferred behind a future
  `MCP_TRUST_PROXY_HEADERS` knob — blindly trusting that header lets
  anyone spoof the rate key.
- **Exempt paths** default to `/livez`, `/readyz`, `/health`. Probes must
  always respond, otherwise a throttle can take pods out of the load
  balancer rotation.
- **Over limit** → 429 with the standard `{detail, code, request_id}`
  envelope plus `Retry-After: <seconds>` and `X-Request-ID`.
- If `limiter.enabled` is False (`rate_per_minute <= 0`) the middleware
  short-circuits and never touches the bucket.

## Wiring

See `mcp_server/main.create_app`:

```python
app.state.rate_limiter = TokenBucket(settings.rate_limit_per_minute)
app.add_middleware(RateLimitMiddleware, limiter=app.state.rate_limiter)
app.add_middleware(RequestSizeLimitMiddleware,
                   max_bytes=settings.max_request_body_mb * 1024 * 1024)
app.add_middleware(RequestIDMiddleware)
```

Exposing the limiter on `app.state` keeps one instance per app (fresh
for every test that calls `create_app`) and makes it addressable for
future admin endpoints (`POST /admin/ratelimit/reset`).

## Testing

- `tests/test_observability.py::TestRequestIDMiddleware` — 4 tests on
  RequestID behavior.
- `tests/test_rate_limit.py::TestRateLimitMiddleware` — 3 tests on 429
  generation, health exemption, request-id echo on error.
- `tests/test_rate_limit.py::TestRequestSizeLimit` — 2 tests on oversize
  413 and within-limit pass-through.

## Future middleware

- `AuthMiddleware` — OIDC session validation for operator routes
  (productization §3.1).
- `TenantFilterMiddleware` — reject requests whose resolved tenant
  doesn't match the path parameter (§3.1).
- Replace in-process `TokenBucket` with a Redis-backed variant once
  multi-replica deployments land (§3.3).
