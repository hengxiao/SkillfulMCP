# Python Dependency Installation

SkillfulMCP requires **Python ≥ 3.11**. All dependencies are declared in [pyproject.toml](pyproject.toml); this document summarizes them and shows how to install.

## Quick install

Pick one of the following.

### Using `make` (recommended)

```bash
make install
```

This runs `python -m pip install -e ".[dev]"`, installing the package in editable mode with dev extras.

### Using `uv`

Install directly with uv (fastest; skips pip entirely):

```bash
uv pip install -e ".[dev]"
```

Or create an isolated environment first. **Use `--seed`** so pip is available inside the venv — `make install` calls `python -m pip`, which fails in a pip-less uv venv:

```bash
uv venv --seed
source .venv/bin/activate
make install        # or: uv pip install -e ".[dev]"
```

### Using plain `pip`

```bash
python -m pip install -e ".[dev]"
```

Omit `[dev]` if you don't need the test dependencies:

```bash
python -m pip install -e .
```

## Runtime dependencies

| Package | Version | Purpose |
| --- | --- | --- |
| fastapi | ≥ 0.111 | Web framework for MCP server and Web UI |
| uvicorn[standard] | ≥ 0.29 | ASGI server |
| sqlalchemy | ≥ 2.0 | ORM / database layer |
| pydantic | ≥ 2.0 | Data validation and models |
| pydantic-settings | ≥ 2.0 | Settings loaded from environment / `.env` |
| python-jose[cryptography] | ≥ 3.3 | JWT signing and verification |
| typer | ≥ 0.12 | CLI framework (`mcp-cli`) |
| semver | ≥ 3.0 | Semantic version handling for skills |
| python-dotenv | ≥ 1.0 | `.env` file loading |
| pyyaml | ≥ 6.0 | Skill manifest parsing |
| httpx | ≥ 0.27 | Async HTTP client |
| anthropic | ≥ 0.30 | Claude API client for the example network |
| jinja2 | ≥ 3.1 | Web UI templating |
| python-multipart | ≥ 0.0.9 | Form uploads for the Web UI |

## Dev dependencies (`[dev]` extra)

| Package | Version | Purpose |
| --- | --- | --- |
| pytest | ≥ 8.0 | Test runner |
| pytest-asyncio | ≥ 0.23 | Async test support |
| httpx | ≥ 0.27 | Test HTTP client |

## Note on the `python` command

The Makefile defaults to `PYTHON ?= python`. On systems where only `python3` exists (common on Ubuntu/Debian), activate a virtualenv first (which aliases `python`) or override:

```bash
make install PYTHON=python3
```

## Verify

After installing, confirm the console scripts are on your PATH:

```bash
mcp-server --help
mcp-cli --help
webui-server --help
```

Run the test suite:

```bash
make test
```
