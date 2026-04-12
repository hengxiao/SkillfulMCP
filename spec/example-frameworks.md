# Framework-agnostic Agent Examples

This document describes how SkillfulMCP is consumed by different agent
frameworks, and the layout of the example runners that demonstrate each one.

## Premise

SkillfulMCP defines three things:

1. A **skill catalog** — id, name, version, description, JSON-schema input
   shape, bundle file tree.
2. A **JWT-scoped authorization model** — each agent token grants a set of
   skill ids and scopes.
3. A **small HTTP surface** — `GET /skills` (JWT-scoped), `GET /skills/{id}/…`
   for bundle files, plus admin endpoints for mutation.

Everything above the HTTP layer — how an agent *uses* a skill, whether it's
through the Anthropic SDK, OpenAI function calling, LangChain, LangGraph, a
custom agent, or an IDE like Cursor — is the framework's problem, not the
catalog's. This spec keeps the catalog lean and lets each framework project a
native-feeling tool surface onto the same underlying skills.

## Non-goals

- The catalog does **not** execute skills. Skill handlers live in whatever
  backend the runner connects to (or, in the current examples, in the shared
  `dispatch_skill` stub under `example/common/`).
- The catalog does **not** know which framework a token is being used with.
  Tokens are scoped by skill ids, not by consumer type.

## Directory layout

```
example/
├── network.yaml                     shared topology (skills, skillsets, agents)
├── common/                          framework-agnostic helpers
│   ├── mcp_bootstrap.py             catalog provisioning + JWT issuance
│   └── skill_dispatcher.py          simulated skill responses (demo default)
├── skillful/                        reusable Skillful* agent classes
│   ├── _base.py                     SkillFetcher (lazy JWT + skill cache)
│   ├── anthropic_agent.py           SkillfulAnthropicAgent
│   ├── openai_agent.py              SkillfulOpenAIAgent
│   ├── langchain_agent.py           SkillfulLangChainAgent  (Runnable)
│   ├── langgraph_agent.py           SkillfulLangGraphAgent  (Runnable)
│   └── _network.py                  orchestrator/worker wiring helper
├── anthropic_sdk/run_network.py     ~60-line runner: bootstrap + Skillful*
├── openai_sdk/run_network.py        same pattern, OpenAI
├── langchain_app/run_network.py     same pattern, LangChain
├── langgraph_app/run_network.py     same pattern, LangGraph
├── cursor/README.md                 bridging via MCP stdio protocol
└── run_network.py                   compat shim → anthropic_sdk
```

### Skillful* agent classes — the main abstraction

Each framework gets one class under `example/skillful/` that handles skill
fetching + tool translation + the framework's agent loop. A user writes:

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
reply = agent.run("Look up invoice INV-1234")
```

The class lazily:
1. Mints a JWT via `POST /token` for `agent_id`.
2. Calls `GET /skills` to list the authorized skills.
3. Translates each skill into the framework's native tool format.
4. Runs the framework's standard agent loop on each `.run(message)`.

Override tool execution with `on_skill_call=my_handler` — defaults to the
simulated `common.dispatch_skill` for the demo. For LangChain / LangGraph the
class subclasses `langchain_core.runnables.Runnable`, so instances compose
with `|`, `RunnableParallel`, graph edges, etc.

### Runner responsibilities

With Skillful* doing the heavy lifting, each runner collapses to:

1. Load `network.yaml` + bootstrap the catalog (`common.bootstrap_mcp`).
2. Instantiate one `Skillful<framework>Agent` per agent definition.
3. Call `skillful._network.wire_orchestrator(orch, workers)` to attach the
   `route_to_agent` meta-tool to the orchestrator.
4. `orchestrator.run(user_message)`.

Each runner is ~60 lines.

## Topology used by all runners

`network.yaml` defines:

- **Skillsets:** `routing`, `billing`, `technical-support`
- **Agents:**
  - `intent-router` (orchestrator, holds `routing` skillset)
  - `billing-agent` (worker, holds `billing`)
  - `tech-support-agent` (worker, holds `technical-support`)

Flow:

1. The user message enters the orchestrator.
2. The orchestrator calls `classify_intent`.
3. Based on the classified intent, it calls the **meta-tool `route_to_agent`**
   with `{"agent_id": "…", "request_summary": "…"}`.
4. The runner intercepts that call in-process, invokes the matching worker
   agent with the summary, and returns the worker's reply as the tool result.
5. The orchestrator composes a final answer and returns.

The `route_to_agent` schema lives in `common.orchestrator_routing_tool_schema()`
so the orchestrator system prompt can stay framework-agnostic. Each runner
adapts that JSON-Schema-style dict into its native tool type.

## Shared translation rules

| MCP field                         | Framework tool field                            |
| --------------------------------- | ----------------------------------------------- |
| `skill.id` with hyphens           | tool name with `-` → `_` (frameworks restrict names to `[a-zA-Z0-9_]`) |
| `skill.description` (or `name` if absent) | tool description                        |
| `skill.metadata.input_schema`     | tool parameters / input_schema / args_schema    |

If `input_schema` is missing, each runner falls back to an empty object schema
(`{"type": "object", "properties": {}}`), which the LLM is free to call with
no arguments.

## Per-framework implementations

### Anthropic SDK — `example/anthropic_sdk/run_network.py`

- Tool shape: `{name, description, input_schema}` — identical to MCP metadata.
- Loop: `client.messages.create(tools=…)` → `stop_reason == "tool_use"` →
  run each `tool_use` block → append `tool_result` blocks → repeat.
- Orchestrator hook: an `AnthropicAgent._tool_override` callback intercepts
  `route_to_agent` before the canned dispatcher runs.

### OpenAI SDK — `example/openai_sdk/run_network.py`

- Tool shape: `{type: "function", function: {name, description, parameters}}`.
  `parameters` takes JSON Schema directly.
- Loop: `chat.completions.create(tools=…)` → `message.tool_calls` → append
  `role: "tool"` messages keyed by `tool_call_id` → repeat.
- OpenAI requires the assistant message containing `tool_calls` to be present
  in history before the tool results; the runner appends both.

### LangChain — `example/langchain_app/run_network.py`

- Tool shape: `StructuredTool.from_function(func, name, description, args_schema)`
  where `args_schema` accepts a raw JSON-Schema dict in LangChain 1.x.
- Loop: `langchain.agents.create_agent(model, tools, system_prompt)` returns
  a compiled graph that runs the tool-use loop internally. Invoked with
  `{"messages": [HumanMessage(content=…)]}`; the final AIMessage without
  `tool_calls` is the answer.
- LangChain 1.0 reorganized the agents API: `AgentExecutor` /
  `create_tool_calling_agent` are gone. The new entry point is
  `create_agent` returning a compiled graph.

### LangGraph — `example/langgraph_app/run_network.py`

- Tool shape: same `StructuredTool` as LangChain.
- Loop: per-agent `StateGraph(AgentState)` with:
  - a `chatbot` node that calls `llm.bind_tools(tools).invoke(messages)`,
  - a `tools` node (`langgraph.prebuilt.ToolNode`),
  - a conditional edge: if the last AIMessage has `tool_calls`, go to `tools`; else END.
- The orchestrator's graph is rebuilt at call time to include the
  `route_to_agent` tool in addition to its skills.
- Routing is implemented as a closure captured by `route_to_agent`'s tool
  function, which calls back into the matching worker's graph.

### Cursor — `example/cursor/README.md`

Cursor is an IDE that consumes tools through the **MCP protocol over stdio**.
Since SkillfulMCP is a REST catalog, plugging it into Cursor requires a small
stdio bridge:

1. Cursor is registered to launch `cursor_mcp_adapter.py` (via `~/.cursor/mcp.json`).
2. The adapter calls `GET /skills` with a per-Cursor JWT and publishes each
   skill as an MCP `tool` (`list_tools()` handler).
3. On `call_tool(name, arguments)`, the adapter dispatches to the skill
   backend — either embedded handlers or a call to the execution gateway the
   skill's metadata points at.

Token rotation is manual (update `mcp.json`) in the starter; a production
setup would either mint short-lived tokens and refresh, or front the adapter
with a local proxy that handles auth.

## Extending: adding another framework

Create a new `example/<framework>/run_network.py` that:

1. `config = common.load_network_config()`
2. `common.bootstrap_mcp(config, server_url, admin_key)` (idempotent)
3. For each `agent_def` in `config["agents"]`:
   - `token = common.get_agent_token(server_url, agent_id, admin_key)`
   - `skills = common.load_agent_skills(server_url, token)`
   - Translate each skill into your framework's tool format.
4. Run the orchestrator's loop. When the `route_to_agent` tool fires, invoke
   the matching worker agent and return its reply as the tool result.
5. Use `common.dispatch_skill(name, args)` for any regular skill tool call
   unless you've wired real backends.

Each existing runner is ~170 lines; that's the budget to aim for.

## Packaging

Framework SDKs are installed via an optional extra so the base install stays
lean:

```toml
[project.optional-dependencies]
examples = [
    "openai>=1.40",
    "langchain>=1.0",
    "langchain-anthropic>=1.0",
    "langchain-core>=1.0",
    "langgraph>=1.0",
]
```

```bash
pip install -e ".[examples]"
# or
make install-examples
```

Makefile targets: `make example-anthropic`, `make example-openai`,
`make example-langchain`, `make example-langgraph`. `make example` stays as
an alias for the Anthropic runner.

## Open questions / future work

- Real skill execution. All runners currently use the same canned responses
  from `common.skill_dispatcher` for demo purposes. Two options for the real
  thing:
  - Each skill's `metadata` declares an endpoint URL; a shared dispatcher
    makes the HTTP call.
  - A separate execution gateway service owns handlers; the runner only needs
    to know `skill id → (endpoint, schema)`.
- A **streaming** runner for frameworks that support it (all four do,
  natively). The current examples run synchronously for clarity.
- A **multi-turn** conversation loop. The runners currently handle a single
  user message at a time.
