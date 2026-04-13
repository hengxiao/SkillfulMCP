# mcp_server/revocation.py

In-process `jti` deny list. Small, thread-safe, self-purging.

## `RevocationList`

| Method                                    | Purpose                                       |
| ----------------------------------------- | --------------------------------------------- |
| `revoke(jti, *, expires_at=None)`          | Mark `jti` as revoked until `expires_at` (default: now + 24h). |
| `is_revoked(jti) -> bool`                 | True if `jti` is listed AND its TTL hasn't elapsed. Lazily evicts expired entries on lookup. |
| `purge_expired() -> int`                  | Bulk cleanup (e.g. from a scheduled task).    |
| `reset()`                                 | Drop everything. Test helper.                 |
| `len(list)`                               | Current entry count. Used by the admin debug endpoint. |

Empty-string jti is never revoked (no-op on `revoke("")`, False on
`is_revoked("")`).

## Lifetime model

Every revocation carries a wall-clock `expires_at`. Once past, the entry
is dropped on the next `is_revoked(jti)` call for that jti. Intent: a
revoked token that would have expired anyway shouldn't permanently take
memory.

Default TTL (24h) is longer than the default `max_token_lifetime_seconds`
(also 24h) so a token can't outlive its revocation entry.

## Concurrency

One `Lock` around the `dict[str, float]`. Fine for a single-process
worker. Not suitable for multi-replica deployments — every replica would
have its own list, so a revocation on pod A wouldn't affect pod B.

## Swappable backend

The interface is intentionally tiny (`revoke`, `is_revoked`) so moving
to Redis is a one-file change:

```python
class RedisRevocationList:
    def revoke(self, jti, *, expires_at=None): ...
    def is_revoked(self, jti): ...
```

`TokenService.__init__` accepts whatever honors this shape; nothing else
needs to know.

## Testing

`tests/test_keyring_revocation.py::TestRevocationList` — 5 tests:
- Revoke + check round-trip.
- Expired entries purge themselves on lookup.
- `purge_expired` bulk cleanup.
- Empty jti is a no-op.
- `reset` clears everything.

End-to-end HTTP behavior tested in `TestTokenServiceValidation` and
`TestAdminRevokeEndpoint`.

## Future work

- **Redis backend** — `INCR` + `EXPIRE` + `EXISTS`. Multi-replica reliable.
- **Revoke-by-agent.** Epoch field on the `agents` table; any token with
  `iat < agent.revoke_not_before` is rejected. Cheap bulk revoke without
  knowing every jti.
- **Audit log** on every revoke/unrevoke event.
