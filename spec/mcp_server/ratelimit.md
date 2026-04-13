# mcp_server/ratelimit.py

Per-key token-bucket rate limiter. In-process, thread-safe, zero external
dependencies.

## `TokenBucket(rate_per_minute, capacity=None)`

- `rate_per_minute` — refill rate. `<= 0` disables the limiter (`allow()`
  always returns `(True, 0.0)`).
- `capacity` — burst size. Defaults to `rate_per_minute` so a fresh client
  can spend up to one minute's worth of tokens immediately before the
  steady-state refill gate takes over.

### `allow(key, *, now=None) -> (bool, float)`

Attempts to consume one token from `key`'s bucket. Returns
`(allowed, retry_after_seconds)`. When not allowed, the caller responds
with `429 Too Many Requests` and `Retry-After: retry_after_seconds`.

`now` is injectable for deterministic tests.

### `reset(key=None)` — test helper to drop per-key state.

## Algorithm

Lazy refill. Tokens are not refilled on a timer — each `allow()` call
recomputes `tokens = min(capacity, tokens + elapsed * rate_per_second)`
using its own timestamp. O(1) per call; no background worker.

## Thread safety

One `Lock` around the per-key state dicts. Fine for the expected
single-process workload; Wave N+ replaces this with Redis' atomic
`INCR`/`EXPIRE` when multi-replica deployments land.

## Known limitations

- **Memory grows with the set of unique keys** seen. No eviction yet.
  Fine for per-IP buckets with a bounded client set; a public endpoint
  facing the open Internet needs eviction before this is shipped widely.
  Tracked in productization §3.3.
- **No sharing across replicas.** Two pods each honor their own bucket
  → effective rate is 2× the configured value. Moving to Redis fixes
  this but needs a deploy step. Prototype ships with single-process
  semantics and a sharp note in the ops docs.

## Testing

`tests/test_rate_limit.py::TestTokenBucketUnit` — 6 tests covering
capacity burst, disabled mode, time-based refill, per-key isolation,
retry-after bounds, and reset.

## Future work

- Redis backend: swap the internal dicts for `INCR` + `EXPIRE` in a
  pipeline. `allow()` signature stays.
- Configurable burst vs steady-state (separate `rate` and `capacity`
  env vars exposed in settings).
- Per-endpoint limits (e.g. `POST /token` much stricter than `GET /skills`
  — currently all share one bucket).
- Soft + hard thresholds — `Retry-After` as warning before actual drop.
