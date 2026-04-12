"""
Reusable per-framework agent classes that fetch their tools from SkillfulMCP.

Each class handles:
  1. Minting a JWT for the configured agent id (lazy, on first run).
  2. Fetching the skills that JWT authorizes via GET /skills.
  3. Translating the skill list into the framework's native tool format.
  4. Running the framework's normal agent loop.

Users don't have to know anything about the catalog's HTTP surface — they
instantiate one of these classes and invoke it like a native agent of that
framework. For LangChain / LangGraph the class subclasses
`langchain_core.runnables.Runnable` so it can drop into any chain or graph.

All four classes accept an optional `on_skill_call(name, args) -> dict` hook
so runtime skill execution can be overridden (defaults to the simulated
`example.common.dispatch_skill` used by the demo).
"""

# Imports are lazy per-framework so importing the package doesn't pull in
# every SDK. Each submodule does its own `import`.

__all__ = [
    "SkillfulAnthropicAgent",
    "SkillfulOpenAIAgent",
    "SkillfulLangChainAgent",
    "SkillfulLangGraphAgent",
]


def __getattr__(name: str):
    if name == "SkillfulAnthropicAgent":
        from .anthropic_agent import SkillfulAnthropicAgent
        return SkillfulAnthropicAgent
    if name == "SkillfulOpenAIAgent":
        from .openai_agent import SkillfulOpenAIAgent
        return SkillfulOpenAIAgent
    if name == "SkillfulLangChainAgent":
        from .langchain_agent import SkillfulLangChainAgent
        return SkillfulLangChainAgent
    if name == "SkillfulLangGraphAgent":
        from .langgraph_agent import SkillfulLangGraphAgent
        return SkillfulLangGraphAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
