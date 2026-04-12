# example/network.yaml

Shared topology consumed by every framework runner. One file to edit when
you want every runner to agree on which skills / skillsets / agents to
register.

## Schema

```yaml
network:
  name: <str>
  description: <str>
  mcp_server_url: <str>          # optional, overrides the runner's --server-url

skillsets:
  - id: <str>
    name: <str>
    description: <str>
    skills:
      - id: <str>
        name: <str>
        version: <semver>
        description: <str>
        metadata:
          input_schema: <JSON schema>
          output_schema: <JSON schema>   # currently informational only

agents:
  - id: <str>
    name: <str>
    skillsets: [<str>, ...]
    skills: [<str>, ...]
    scope: [<str>, ...]            # {"read", "execute"}
    role: "orchestrator" | "worker"
    model: <str>                   # default claude-sonnet-4-6
    openai_model: <str>            # optional override for the OpenAI runner
    system_prompt: <str>           # block-literal, free text
```

## Prototype topology

- Three skillsets: `routing`, `billing`, `technical-support`.
- Three agents:
  - `intent-router` — orchestrator; holds `routing`.
  - `billing-agent` — worker; holds `billing`.
  - `tech-support-agent` — worker; holds `technical-support`.

The orchestrator calls `classify_intent`, then delegates via the
`route_to_agent` meta-tool to the matching worker.

## Contract with runners

- Every runner calls `common.bootstrap_mcp(config, ...)` which reads
  `skillsets` + `agents` and idempotently upserts them via the admin API.
- Runners identify the orchestrator by `role == "orchestrator"` and wire
  the `route_to_agent` tool into it via `skillful._network.wire_orchestrator`.
- Runners build one `Skillful<framework>Agent` per entry in `agents`,
  passing `system_prompt` and `model` through.

## Future work

- Split into multiple YAMLs (per scenario) and pick via runner `--config`.
- Add `executor_url` / `executor_mcp` fields on skills to replace the
  simulated dispatcher (productization §3.4).
