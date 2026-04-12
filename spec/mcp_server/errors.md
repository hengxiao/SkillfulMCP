# mcp_server/errors.py

Global exception handlers that enforce a consistent error envelope and
always propagate `X-Request-ID` on error responses.

## Envelope shape

```json
{
  "detail":     "<original string or list>",
  "code":       "HTTP_404",
  "request_id": "abcdef..."
}
```

- `detail` — kept verbatim from the original source (`HTTPException.detail`,
  pydantic validation errors, etc.). Preserves backwards compatibility with
  every existing caller (Web UI, tests, CLI) that reads `detail`.
- `code` — stable machine-readable. Today it's `HTTP_<status>` for
  `HTTPException`, `VALIDATION_ERROR` for pydantic 422s, `INTERNAL_ERROR`
  for catch-all 500s. The set will grow with typed-error adoption
  (productization §3.3).
- `request_id` — matches the `X-Request-ID` header for log-to-user
  correlation.

## Handlers

### `http_exception_handler(request, exc)`

Converts any `HTTPException` into the envelope. Preserves `exc.headers`
(merged with our `X-Request-ID`).

### `validation_exception_handler(request, exc)`

Handles pydantic `RequestValidationError`. Uses `jsonable_encoder(exc.errors())`
for the `detail` because pydantic v2 wraps caught `ValueError`s into its
`ctx` which aren't JSON-serializable by default. Code: `VALIDATION_ERROR`.

### `unhandled_exception_handler(request, exc)`

Catch-all for programming errors. Logs at EXCEPTION level (with request
path) and returns a scrubbed generic 500: `detail: "Internal Server Error"`
+ `code: "INTERNAL_ERROR"`. Never leaks the underlying exception message —
a real message in the log stream is fine (operator-visible), but never in
a user response.

## Wiring

Registered in `main.create_app` via `app.add_exception_handler(...)`:

```python
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
```

## Testing

`tests/test_observability.py::TestErrorEnvelope` — 4 tests:
- 404 from `/admin/skills/<missing>` carries `code=HTTP_404`, `request_id=…`,
  and `X-Request-ID` header matches.
- 403 when admin key missing on `POST /skills` → `code=HTTP_403`.
- 401 on bad JWT → `code=HTTP_401`.
- 422 on invalid semver → `code=VALIDATION_ERROR`, `detail` is a list.

## Future work (tracked in productization plan)

- Custom error codes beyond `HTTP_*` (`SKILL_NOT_FOUND`, `SKILL_DUPLICATE`,
  `BUNDLE_TOO_LARGE`, etc.) with documented semantics so clients can branch
  on `code` instead of status + string matching.
- Drop the `detail` field under a new `/v1/` API — keep it only on the
  unversioned legacy routes.
- Integrate Sentry / equivalent error tracker in `unhandled_exception_handler`.
