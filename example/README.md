# Example agentic networks

Each subdirectory here runs the **same** customer-support agentic network
(orchestrator → billing-agent / tech-support-agent) on top of SkillfulMCP —
but uses a different agent framework on the agent side. The MCP server stays
the same across all of them; it's just the tool-calling layer that swaps.

| Runner                                                   | Framework          | Backend model          | Extra install              |
| -------------------------------------------------------- | ------------------ | ---------------------- | -------------------------- |
| [`anthropic_sdk/run_network.py`](anthropic_sdk/run_network.py) | Anthropic SDK      | Claude                 | `anthropic` (in base deps) |
| [`openai_sdk/run_network.py`](openai_sdk/run_network.py)       | OpenAI SDK         | GPT-4o / GPT-4o-mini   | `openai`                   |
| [`langchain_app/run_network.py`](langchain_app/run_network.py) | LangChain          | Claude (via `langchain-anthropic`) | `langchain`, `langchain-anthropic` |
| [`langgraph_app/run_network.py`](langgraph_app/run_network.py) | LangGraph          | Claude                 | `langgraph`, `langchain-anthropic` |
| [`cursor/`](cursor/README.md)                                 | Cursor IDE (MCP protocol) | Whatever Cursor uses | see directory README |

Each runner uses a framework-specific **agent class** under
[`skillful/`](skillful/) — `SkillfulAnthropicAgent`, `SkillfulOpenAIAgent`,
`SkillfulLangChainAgent`, `SkillfulLangGraphAgent`. You instantiate one with
`(agent_id, server_url, admin_key, client_or_llm, system_prompt=...)` and it
fetches its JWT, loads the authorized skills, and translates them into that
framework's native tool format on first use. LangChain and LangGraph classes
subclass `langchain_core.runnables.Runnable`, so instances drop into any
chain or graph.

```python
from example.skillful import SkillfulAnthropicAgent
import anthropic

agent = SkillfulAnthropicAgent(
    agent_id="billing-agent",
    server_url="http://localhost:8000",
    admin_key=os.environ["MCP_ADMIN_KEY"],
    client=anthropic.Anthropic(),
    system_prompt="You are a billing specialist.",
)
print(agent.run("Look up invoice INV-1234"))
```

Everything under [`common/`](common/) is framework-agnostic infrastructure —
MCP catalog bootstrap, JWT issuance, and the simulated `dispatch_skill` used
as the default tool-execution handler. Override by passing
`on_skill_call=my_handler` to any `Skillful*` constructor.

[`network.yaml`](network.yaml) defines the shared topology (skills, skillsets,
agents, system prompts). Each runner ingests it identically.

## Installing the extras

Framework dependencies are optional — installed via the `examples` extra so
`make install` doesn't drag them in by default:

```bash
pip install -e ".[examples]"
```

## Running one

Pick the framework, set the right API key, and go.

```bash
# Make sure the MCP server is running first.
#   make serve        # in another terminal

# Anthropic SDK
export ANTHROPIC_API_KEY=sk-ant-...
make example ARGS='--message "I have a billing question about invoice #1234"'

# OpenAI SDK
export OPENAI_API_KEY=sk-...
python example/openai_sdk/run_network.py \
    --message "I have a billing question about invoice #1234"

# LangChain (uses Claude by default; set ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
python example/langchain_app/run_network.py \
    --message "My internet has been dropping every night for a week"

# LangGraph (also Claude-backed by default)
export ANTHROPIC_API_KEY=sk-ant-...
python example/langgraph_app/run_network.py \
    --message "My internet has been dropping every night for a week"
```

The Makefile also exposes per-framework targets:

```bash
make example-anthropic     MESSAGE="..."
make example-openai        MESSAGE="..."
make example-langchain     MESSAGE="..."
make example-langgraph     MESSAGE="..."
```

## How to add a new framework

Create a new `example/<your_framework>/run_network.py` that:

1. Loads the network config with `common.load_network_config()`.
2. Bootstraps the catalog with `common.bootstrap_mcp(...)`.
3. For each agent defined in the config, calls `common.get_agent_token(...)`
   and `common.load_agent_skills(token)`.
4. Translates each skill's `metadata.input_schema` into whatever your
   framework's tool-definition format is.
5. Runs the orchestrator's tool-use loop; when the `route_to_agent` tool is
   called, invoke the matching worker agent and feed its response back as the
   tool result.
6. Uses `common.dispatch_skill(name, args)` to produce a (simulated) response
   for any regular skill tool call.

See any of the existing runners for a ~200-line template.
