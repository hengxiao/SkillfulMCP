# mcp_server/authorization.py

Resolves the set of skill ids a JWT authorizes.

## `resolve_allowed_skill_ids(claims: dict, db: Session) -> set[str]`

### Rule — union, no denies

```
allowed = set(claims.skills)
       ∪ { skill_id for (skill_id, skillset_id) in SkillSkillset
                     where skillset_id in claims.skillsets }
```

A skill is allowed if it appears directly in `claims.skills` **or** if any
of `claims.skillsets` contains it. There is **no deny mechanism** — this
keeps the resolver trivially cacheable and easy to reason about. If you
need to revoke access, remove the grant.

### SQL shape

Single query: `SELECT skill_id FROM skill_skillsets WHERE skillset_id IN (...)`.
No join with `skills` — the skill row's existence is verified later (when the
caller tries to fetch it). This is deliberate: an agent can be granted access
to a skill id that no longer exists, and `GET /skills/{id}` will just 404.

### Used by

- `routers/skills.py` — `GET /skills`, `GET /skills/{id}`, `GET /skills/{id}/versions`.
- `routers/skillsets.py::list_skillset_skills` — filters members by this set.
- `routers/bundles.py::_require_read_access` — guards bundle file reads.

### Not used for writes

Write endpoints are admin-key gated (`require_admin`) and are not scoped to
a specific agent. The `scope` claim (`read`, `execute`) exists in the token
but is **not currently enforced** anywhere — the prototype treats all
catalog reads as `read` and has no execution paths.

## Testing

`tests/test_authorization.py` covers:
- skills-only claim returns those ids
- skillsets-only claim returns member skill ids from the DB
- both claims → union, deduplicated
- empty claims → empty set
- unknown skillset id in claim → no effect (silent drop, matches the
  "nonexistent grant" semantics above)

## Future work

- Enforce `scope` — `execute` required for bundle downloads; `read` sufficient
  for metadata.
- Add explicit **deny** support for "blocklist then revoke" incident response.
- Tenant check — when multi-tenancy lands (productization §3.1), this function
  must intersect with the caller's tenant.
- Cache resolution per-token (memoize by JWT `jti`) once revocation lands.
