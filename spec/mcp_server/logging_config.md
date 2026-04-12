# mcp_server/logging_config.py

Structured JSON logging for the catalog server. Stdlib only — no
`structlog` / `python-json-logger` dependency.

## Public API

### `configure_logging(level=None)`

Installs `JSONFormatter` on the root logger. Idempotent — subsequent calls
are no-ops. Called once at app-factory time and guarded by a module-level
`_CONFIGURED` flag so tests that call `create_app` repeatedly don't pile up
handlers.

Level comes from (in order): explicit arg → `MCP_LOG_LEVEL` env →
`INFO`.

Also reroutes uvicorn's own loggers (`uvicorn`, `uvicorn.error`,
`uvicorn.access`) through the root handler so their lines appear in the
same JSON stream.

### `get_logger(name) -> Logger`

Thin wrapper around `logging.getLogger` that ensures `configure_logging` has
run. Use everywhere instead of `logging.getLogger` directly.

### `set_request_id(rid)` / `get_request_id()`

ContextVar-backed setters/getters for the current request ID. The
`JSONFormatter` reads the ContextVar on each format call so `logger.info(...)`
anywhere in request context automatically emits the correct request_id.

## `JSONFormatter`

Produces one JSON object per line. Fields emitted:

```json
{
  "ts":         "2026-04-12T23:39:09.503561+00:00",
  "level":      "INFO",
  "logger":     "mcp.access",
  "msg":        "request",
  "request_id": "abc123...",
  ... extras ...
}
```

`extras` come from `logger.info(..., extra={"path": "/skills", "status": 200})`
calls. Non-JSON-serializable values are rendered via `repr()` so a stray
object in an extra dict never breaks the log stream.

## Testing

`tests/test_observability.py::TestJSONFormatter` — 5 tests:
- Basic shape (level, logger, msg, ts always present).
- Request ID pulled from ContextVar.
- Missing request ID renders as `"-"`.
- `extra={}` fields merged into payload.
- Non-serializable extras fall back to `repr`.

## Future work

- Add `sampling_rate` knob for debug-level logs in prod.
- Plug into OpenTelemetry so trace/span IDs ride alongside `request_id`
  (productization §3.6 P1).
- Emit to a persistent store (Loki / Cloud Logging / Datadog) via the
  infra, not in-process — keep the app emitting stdout.
