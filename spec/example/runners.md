# example/*/run_network.py

Per-framework runner scripts. Each is ~60 lines of bootstrap + agent
instantiation + orchestrator wiring. Differences across frameworks are
limited to the specific `SkillfulXxxAgent` class instantiated and the
client/llm passed to it.

## Common runner structure

```python
def main():
    config = load_network_config()
    server_url = config.get("network", {}).get("mcp_server_url") or args.server_url
    bootstrap_mcp(config, server_url, args.admin_key)

    # Build one Skillful<framework>Agent per agent definition.
    agents, roles = build_agents(config, server_url, args.admin_key, client_or_llm)

    orch_id = next(i for i, r in roles.items() if r == "orchestrator")
    orchestrator = agents[orch_id]
    workers = {i: a for i, a in agents.items() if roles[i] != "orchestrator"}
    wire_orchestrator(orchestrator, workers)

    result = orchestrator.run(args.message)
    print(result)
```

## `anthropic_sdk/run_network.py`

- Imports `anthropic`.
- Client: `anthropic.Anthropic()`.
- Agent class: `SkillfulAnthropicAgent`.
- Models default to `claude-sonnet-4-6` (from network.yaml `agents[].model`).

## `openai_sdk/run_network.py`

- Imports `openai.OpenAI`.
- Client: `OpenAI()`.
- Agent class: `SkillfulOpenAIAgent`.
- Per-agent model comes from `openai_model` in network.yaml, falling back to `gpt-4o-mini`.

## `langchain_app/run_network.py`

- Imports `langchain_anthropic.ChatAnthropic`.
- `llm = ChatAnthropic(model=args.model, max_tokens=1024)`.
- Agent class: `SkillfulLangChainAgent`.
- Default model: `claude-sonnet-4-6`.

## `langgraph_app/run_network.py`

- Imports `langchain_anthropic.ChatAnthropic`.
- Same `llm` setup as LangChain.
- Agent class: `SkillfulLangGraphAgent`.

## Top-level shim — `example/run_network.py`

```python
from example.anthropic_sdk.run_network import main
if __name__ == "__main__":
    main()
```

Keeps the legacy entry point (`python example/run_network.py ...`, `make example`) working by forwarding to the Anthropic runner.

## CLI args (all runners)

| Flag            | Default                                                 |
| --------------- | ------------------------------------------------------- |
| `--message`     | required                                                |
| `--server-url`  | `MCP_SERVER_URL` env, else `http://localhost:8000`      |
| `--admin-key`   | `MCP_ADMIN_KEY` env                                     |
| `--config`      | `example/network.yaml` (when wired)                     |
| `--model`       | framework-appropriate default                           |

## Makefile targets

```
make example            # alias for example-anthropic
make example-anthropic  MESSAGE="..."
make example-openai     MESSAGE="..."
make example-langchain  MESSAGE="..."
make example-langgraph  MESSAGE="..."
```

Each invokes the runner as a module
(`python -m example.<framework>.run_network`).

## Failure envelope

Bootstrap + agent instantiation happen before the first LLM call. If the
catalog is down or the admin key is wrong, the runner fails before
consuming any LLM budget. If an API key for the backing LLM is missing,
the runner reaches the first `client.messages.create` / `llm.invoke` and
fails there.

## Future work

- Generalize `build_agents` into a shared helper (it's the same structure
  in all four runners modulo the class name).
- Accept multiple `--message` calls in sequence for interactive use.
- Emit structured run transcripts (JSONL) for offline analysis.
