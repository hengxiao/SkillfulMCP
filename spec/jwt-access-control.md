# JWT Access Control

This document defines JWT structure and how the MCP server uses tokens to control skill access.

## JWT Claims

The JWT should include the following claims:

- `sub` — agent identifier.
- `iss` — token issuer identifier.
- `exp` — expiration time.
- `iat` — issued-at time.
- `skills` — optional array of authorized skill identifiers.
- `skillsets` — optional array of authorized skillset identifiers.
- `scope` — optional permission scope for the agent (read-only, read-write, etc.).
- `context` — optional metadata describing the agent environment or allowed context boundaries.

## Authorization Rules

1. Token Validation
   - Verify signature using a shared secret or public key.
   - Reject expired tokens.
   - Confirm the token was issued by the expected issuer.

2. Permission Resolution
   - If `skills` is present, allow only those skill IDs.
   - If `skillsets` is present, allow any skill in the listed skillsets.
   - If both are present, allow the union of explicitly authorized skills and skills within authorized skillsets.

3. Context Limitation
   - When returning skill data, include only fields relevant to the authorized context.
   - Do not expose skill descriptions, metadata, or additional context that falls outside the authorized scope.

## Example Token Payload

```json
{
  "sub": "agent-123",
  "iss": "mcp-server",
  "exp": 1750000000,
  "iat": 1710000000,
  "skillsets": ["sales-assistant", "basic-qa"],
  "scope": "read",
  "context": {
    "region": "us-east",
    "agentType": "chatbot"
  }
}
```

## Access-Control Enforcement

- Skill requests without a valid JWT must be denied.
- Skill responses must be filtered by the resolved authorization set.
- Audit logs should record `sub`, requested skill, resolved permissions, and decision outcome.

## Extensibility

- Additional claims may be added later for feature flags, tenant IDs, or more granular context policies.
- The model supports both agent-level grants and finer-grained skill-level grants.
