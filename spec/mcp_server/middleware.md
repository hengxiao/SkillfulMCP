# mcp_server/middleware.py

HTTP middleware stack. Currently holds `RequestIDMiddleware`; future auth /
tenant / rate-limit middleware will layer on top.

## `RequestIDMiddleware`

A Starlette `BaseHTTPMiddleware`. Per request:

1. Reads `X-Request-ID` from the incoming request; if absent, generates
   a UUID4 hex.
2. `set_request_id(rid)` — puts it on the `ContextVar` that the
   JSONFormatter reads.
3. Runs the handler; measures `time.perf_counter()` latency.
4. On success: attaches `X-Request-ID` to the response, logs one INFO
   line `"request"` with `{method, path, status, latency_ms}`.
5. On unhandled exception: logs at EXCEPTION level with the same fields
   (minus `status`), clears the ContextVar, re-raises so FastAPI's
   exception handler produces the actual response.
6. Always clears the ContextVar after the request exits so it never leaks
   between requests on the same worker.

Header name is exported as `HEADER = "X-Request-ID"` so other modules
(`errors.py`) reuse the constant.

## Interaction with exception handlers

The middleware's `try/except` re-raises unhandled exceptions so Starlette's
exception handler chain runs. The `errors.unhandled_exception_handler`
registered in `main.py` returns the 500 response. This is a deliberate
two-stage setup: the middleware owns logging, the exception handler owns
response shape.

## Future middleware to add here

- `AuthMiddleware` — validates OIDC session on operator-facing paths and
  sets `tenant_id` / `operator_id` ContextVars. (productization §3.1)
- `TenantFilterMiddleware` — rejects requests where the resolved tenant
  doesn't match the path parameter. (productization §3.1)
- `RateLimitMiddleware` — Redis-backed token bucket per
  `(tenant_id, route)`. (productization §3.3)

## Testing

`tests/test_observability.py::TestRequestIDMiddleware` — 4 tests:
- New request gets a generated id.
- Inbound `X-Request-ID` is echoed unchanged.
- Ids differ across successive requests.
- ContextVar is cleared between requests (no leakage).
