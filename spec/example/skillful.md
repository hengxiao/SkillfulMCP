# example/skillful/

Reusable per-framework agent classes. The goal is that a user writes one
line of framework-appropriate code and gets an agent whose tools come from
SkillfulMCP.

## Module map

| File                    | Class / helper                           |
| ----------------------- | ---------------------------------------- |
| `__init__.py`           | Lazy re-exports of all four classes      |
| `_base.py`              | `SkillFetcher`, `SkillCallHook`, `default_skill_call` |
| `anthropic_agent.py`    | `SkillfulAnthropicAgent`                 |
| `openai_agent.py`       | `SkillfulOpenAIAgent`                    |
| `langchain_agent.py`    | `SkillfulLangChainAgent` (extends `Runnable`) |
| `langgraph_agent.py`    | `SkillfulLangGraphAgent`  (extends `Runnable`) |
| `_network.py`           | `wire_orchestrator(orch, workers)` glue  |

## Shared contract

Every `SkillfulXxxAgent` exposes:

- `agent_id: str` — the catalog agent being impersonated.
- `skills() -> list[dict]` — lazily fetched, cached MCP skill list.
- `tools() -> list[<native tool shape>]` — translated from skills; **Anthropic / OpenAI** only. LangChain/LangGraph instantiate `StructuredTool`s internally.
- `bind_extra_tool(schema, handler)` — attach an out-of-catalog tool in the framework-agnostic shape (same as `orchestrator_routing_tool_schema` returns). The agent converts it internally.
- `run(message: str) -> str` — convenience wrapper that runs one user message through the framework's loop and returns the final text.
- Optional constructor kwarg `on_skill_call=fn` — override the default simulated dispatcher.

LangChain / LangGraph classes additionally:
- Subclass `langchain_core.runnables.Runnable`.
- Expose `invoke(input, config=None)` per the Runnable protocol.

## `_base.py`

### `SkillFetcher(agent_id, server_url, admin_key)`

Lazily mints a JWT on first `.token` access and caches it. Lazily fetches
skills on first `.skills()` call.

```python
self._token: str | None
self._skills: list[dict] | None
```

Token rotation: none in the prototype. If the token expires mid-run the
next call fails; user restarts. Productization §3.4 adds refresh.

### `SkillCallHook = Callable[[str, dict], dict]`

Every framework class accepts one of these to override tool execution.

### `default_skill_call(name, args) -> dict`

Delegates to `common.dispatch_skill`. Swap for real handlers in prod.

## `anthropic_agent.py` — `SkillfulAnthropicAgent`

Plain class (no base). Constructor kwargs: `agent_id`, `server_url`,
`admin_key`, `client`, `model`, `system_prompt`, `max_tokens=1024`,
`max_steps=10`, `on_skill_call`.

Internal state: `_fetcher: SkillFetcher`, `_extra_tools: list[dict]`,
`_extra_tool_handlers: dict[str, SkillCallHook]`.

Run loop: standard Anthropic tool-use loop. `max_steps` is a safety
against infinite tool-calling.

Tool translation: `skills_to_anthropic_tools` — just renames `-` to `_`
and passes schema through.

## `openai_agent.py` — `SkillfulOpenAIAgent`

Plain class. Constructor kwargs: same as Anthropic but `model` defaults to
`gpt-4o-mini`.

Key quirk: OpenAI requires the assistant message carrying `tool_calls` to
be present in history before the tool-result messages. The agent appends
both explicitly.

Tool translation wraps each skill in `{"type": "function", "function": {...}}`.

## `langchain_agent.py` — `SkillfulLangChainAgent`

Subclasses `langchain_core.runnables.Runnable`. Constructor kwargs:
`agent_id`, `server_url`, `admin_key`, `llm`, `system_prompt`,
`on_skill_call`.

Tool translation: each skill becomes a `StructuredTool` via
`StructuredTool.from_function(func, name, description, args_schema=<json-schema>)`.
`args_schema` accepts a raw dict in LangChain 1.x.

Lazy build: the inner `CompiledStateGraph` (from `create_agent(...)`) is
built on first `invoke()`. `bind_extra_tool` invalidates the cache so the
next invoke rebuilds with the new tool set.

Runnable surface: implements `invoke(input, config=None, **kwargs)`.
Convenience `run(message)` accepts a plain string and returns the final
AIMessage content.

## `langgraph_agent.py` — `SkillfulLangGraphAgent`

Same ergonomics as LangChain class, but builds an **explicit** StateGraph
(chatbot ↔ tools) internally instead of calling `create_agent`. Useful
when you want direct access to the graph for checkpointing, custom nodes,
or persistence — exposed as `agent.graph`.

State shape:

```python
class _AgentState(TypedDict):
    messages: Annotated[list, add_messages]
```

Graph:
```
START → chatbot → (tool_calls?) → tools → chatbot
                         \
                          \──▶ END
```

`ToolNode(tools)` from `langgraph.prebuilt` executes tool calls.

## `_network.py` — `wire_orchestrator(orchestrator, workers)`

Calls `orchestrator.bind_extra_tool(schema, handler)` with
`orchestrator_routing_tool_schema(worker_ids)` and a handler that invokes
the matching worker in-process:

```python
def handler(name, args):
    worker = workers[args["agent_id"]]
    return {"worker_response": worker.run(args["request_summary"])}
```

Runners import this and pass their `orchestrator` + `workers` dict.

## Testing

`tests/test_skillful_agents.py` — 11 tests, no LLM calls. Covers:
- Skill → tool translation shape for each framework.
- Extra-tool binding and (for OpenAI) shape conversion to function spec.
- Lazy token fetch / skill fetch.
- `bind_extra_tool` invalidates the LangChain compiled graph.
- LangChain / LangGraph classes are `Runnable` subclasses.
- `on_skill_call` override path.

Catalog interactions are monkey-patched on `SkillFetcher`'s imports so
tests run without a live MCP server.

## Future work

- Async `.arun(message)` for all four classes.
- Streaming: expose tokens / tool events as they arrive (requires framework-specific APIs).
- Background skill-list refresh on a TTL.
- Graceful handling of token expiry (refresh + retry).
- Per-agent metrics hooks (request counter, tool-call counter, latency).
