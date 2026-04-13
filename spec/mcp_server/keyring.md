# mcp_server/keyring.py

JWT signing-key ring. One active key for minting new tokens + any number of
verify-only keys so token rotation doesn't invalidate in-flight credentials.

## `KeyRing`

```python
@dataclass(frozen=True)
class KeyRing:
    keys: dict[str, str]    # kid -> secret
    active_kid: str         # which kid signs new tokens
    algorithm: str = "HS256"
```

Accessors:
- `get_secret(kid) -> str | None` — for verification side.
- `active_secret` — the secret used when minting new tokens.
- `known_kids` — sorted list, for log / introspection.

Frozen / immutable — swap the whole ring at rotation time instead of
mutating in place.

## `build_keyring(settings) -> KeyRing`

Two modes based on env:

### Legacy (single secret)

```
MCP_JWT_SECRET=abc123
```

→ `KeyRing(keys={"primary": "abc123"}, active_kid="primary")`.

All tokens carry `kid: primary` in the header, so later switching to
multi-key mode doesn't invalidate them.

### Multi-key (rotation)

```
MCP_JWT_KEYS='{"k1": "secret1", "k2": "secret2"}'
MCP_JWT_ACTIVE_KID=k2
```

- The JSON must be a non-empty `{str: non-empty-str}` object.
- `MCP_JWT_ACTIVE_KID` must be a key present in the blob — otherwise
  `build_keyring` raises `RuntimeError` at startup.

## Rotation playbook

1. Add a new key alongside the current one:
   `MCP_JWT_KEYS='{"old": "…", "new": "…"}'`, leave `MCP_JWT_ACTIVE_KID=old`.
   Deploy. Nothing changes for users — tokens still signed with `old`,
   both keys are accepted for verify.
2. Flip `MCP_JWT_ACTIVE_KID=new`. Deploy. New tokens are signed with
   `new`; old tokens still verify because `old` is still in the ring.
3. After the longest-lived `old`-signed token has expired, remove `old`
   from `MCP_JWT_KEYS`. Deploy.

No in-flight tokens are invalidated at any step.

## Testing

`tests/test_keyring_revocation.py::TestKeyRing` — 4 tests:
- legacy single-secret mode
- multi-key mode (active + verify-only + unknown kid returns None)
- missing active kid rejected
- malformed JSON rejected

Plus `TestTokenServiceValidation::test_old_kid_still_verifies_during_rotation`
covers the end-to-end rotation path through the HTTP surface.

## Future work

- **KMS-backed keys.** Replace HMAC secrets with RS256 / ES256 keys
  pulled from AWS KMS / Google KMS / Azure Key Vault. The `KeyRing`
  shape stays; `active_secret` becomes `active_signer` and verification
  uses public keys.
- **Key auto-reload.** Rotate without a deploy by reloading env on SIGHUP
  or a polling loop.
- **JWKS endpoint.** Serve the public keys at `/.well-known/jwks.json`
  so external verifiers (other services) don't have to share secrets.
