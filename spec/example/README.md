# spec/example — Example Runner Submodule Specs

Per-file specs for the framework-integration examples under `example/`.
High-level rationale is in
[`../example-frameworks.md`](../example-frameworks.md); these docs describe
the implementation at the module level.

## Module map

| Directory / file                   | Spec                                                      | Purpose                                                 |
| ---------------------------------- | --------------------------------------------------------- | ------------------------------------------------------- |
| `network.yaml`                     | [network-config.md](network-config.md)                    | Shared topology consumed by every runner                |
| `common/`                          | [common.md](common.md)                                    | Catalog bootstrap, JWT issuance, dispatcher stub        |
| `skillful/`                        | [skillful.md](skillful.md)                                | Per-framework `SkillfulXxxAgent` classes                |
| `anthropic_sdk/run_network.py`     | [runners.md](runners.md)                                  | Anthropic-SDK runner                                    |
| `openai_sdk/run_network.py`        | [runners.md](runners.md)                                  | OpenAI-SDK runner                                       |
| `langchain_app/run_network.py`     | [runners.md](runners.md)                                  | LangChain runner                                        |
| `langgraph_app/run_network.py`     | [runners.md](runners.md)                                  | LangGraph runner                                        |
| `cursor/`                          | [cursor.md](cursor.md)                                    | Cursor IDE integration notes                            |
| `run_network.py`                   | [runners.md](runners.md) (shim section)                   | Compat entry point → anthropic_sdk                      |
