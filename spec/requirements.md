# Requirements

## Functional Requirements

1. Agent Registration and Identity
   - The MCP server must recognize different agents.
   - Each agent gets a unique identifier and a JWT used for authenticating requests.

2. Skill Catalog
   - The server must maintain a catalog of skills and skillsets.
   - Skillsets group related skills for easier authorization.

3. Skill Versioning
   - Each skill must include version metadata.
   - The server must support retrieving skill metadata by version and tracking changes over time.

4. JWT-Based Authorization
   - Agent requests must include a JWT.
   - JWTs must encode allowed skill or skillset access.
   - The server must validate JWT signature and claims before serving skill information.

4. Scoped Skill Access
   - The server must only return the subset of skill data permitted for the requesting agent.
   - Context for each agent must be limited to the authorized skillset and metadata.

5. Skill Context Control
   - Agents should not see other agents' accessible skillsets or protected skill context.
   - The server should provide context only for authorized skills, avoiding unnecessary exposure.

## Non-Functional Requirements

1. Security
   - JWTs must be signed and optionally encrypted.
   - Access control decisions should be deterministic and auditable.

2. Extensibility
   - The design should support adding new agents and skillsets without breaking existing access rules.

3. Performance
   - The server should validate tokens and filter skill data efficiently.

4. Observability
   - The server should log authorization decisions and rejected requests.

## Constraints

- The spec assumes a centralized MCP server implementation.
- The initial design focuses on access control, not on a full skill execution environment.
