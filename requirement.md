# Python Dependency Installation

SkillfulMCP requires **Python ≥ 3.11**. All dependencies are declared in
[`pyproject.toml`](pyproject.toml); this document summarizes them, lists
the optional extras, and shows how to install.

## Quick install

Pick one of the following.

### Using `make` (recommended)

```bash
make install            # base + [dev] (test suite)
make install-examples   # base + [dev,examples] (LangChain/OpenAI/LangGraph runners)
```

`make install` runs `python -m pip install -e ".[dev]"` under the hood —
package in editable mode, with the test toolchain included.

### Using `uv`

Install directly with uv (fastest; skips pip entirely):

```bash
uv pip install -e ".[dev]"
```

Or create an isolated environment first. **Use `--seed`** so pip is
available inside the venv — `make install` calls `python -m pip`, which
fails in a pip-less uv venv:

```bash
uv venv --seed
source .venv/bin/activate
make install        # or: uv pip install -e ".[dev]"
```

### Using plain `pip`

```bash
python -m pip install -e ".[dev]"
```

Omit `[dev]` for a runtime-only install:

```bash
python -m pip install -e .
```

---

## Optional extras

Install combinations like `pip install -e ".[dev,postgres,s3,examples]"`.

| Extra      | What you get | When you need it |
| ---------- | ------------ | ---------------- |
| `dev`      | `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `moto[s3]` | Running the test suite. |
| `postgres` | `psycopg2-binary` | Pointing `MCP_DATABASE_URL` at Postgres. |
| `s3`       | `boto3` | `MCP_BUNDLE_STORE=s3` (Wave 5 bundle store). |
| `examples` | `openai`, `langchain`, `langchain-anthropic`, `langchain-core`, `langgraph` | Running the framework runners under `example/`. |

Without any extras you still get a working catalog + Web UI + CLI; only
the listed integrations are gated.

---

## Runtime dependencies (always installed)

| Package | Version | Purpose |
| --- | --- | --- |
| fastapi | ≥ 0.111 | Web framework for catalog + Web UI |
| uvicorn[standard] | ≥ 0.29 | ASGI server |
| sqlalchemy | ≥ 2.0 | ORM / database layer |
| alembic | ≥ 1.13 | Schema migrations (run automatically at startup for non-`:memory:` URLs) |
| pydantic | ≥ 2.0 | Request / response validation |
| pydantic-settings | ≥ 2.0 | Settings loaded from environment / `.env` |
| python-jose[cryptography] | ≥ 3.3 | JWT signing and verification |
| bcrypt | ≥ 4.0 | Web UI operator password hashing |
| itsdangerous | ≥ 2.2 | Signed session cookies (used via Starlette's SessionMiddleware) |
| typer | ≥ 0.12 | CLI framework (`mcp-cli`) |
| semver | ≥ 3.0 | Semantic version handling for skills |
| python-dotenv | ≥ 1.0 | `.env` file loading |
| pyyaml | ≥ 6.0 | Skill manifest parsing + catalog import |
| httpx | ≥ 0.27 | Async HTTP client (Web UI → catalog) |
| anthropic | ≥ 0.30 | Claude API client (also used by the Anthropic example runner) |
| jinja2 | ≥ 3.1 | Web UI templating |
| python-multipart | ≥ 0.0.9 | Form uploads + multipart bundle uploads |

---

## Dev dependencies (`[dev]` extra)

| Package | Version | Purpose |
| --- | --- | --- |
| pytest | ≥ 8.0 | Test runner |
| pytest-asyncio | ≥ 0.23 | Async test support |
| pytest-cov | ≥ 5.0 | Coverage reporting; CI gate at 85% |
| httpx | ≥ 0.27 | TestClient + MockTransport for unit tests |
| moto[s3] | ≥ 5.0 | Mock S3 for `test_bundle_store_s3.py` |

---

## Generating an operator password hash

The Web UI requires `MCP_WEBUI_OPERATORS` to be a JSON list of operator
records. Each record has an email and a bcrypt password hash. Generate
one with:

```bash
python -c "from webui.auth import hash_password; print(hash_password('your-password'))"
```

Then drop it into `.env`:

```bash
MCP_WEBUI_OPERATORS=[{"email":"alice@example.com","password_hash":"$2b$12$..."}]
```

(Single-quoted in shell so `$` characters in the hash aren't expanded.)

---

## Note on the `python` command

The Makefile defaults to `PYTHON ?= python`. On systems where only
`python3` exists (common on Debian / Ubuntu), activate a virtualenv
first (which aliases `python`) or override:

```bash
make install PYTHON=python3
```

---

## Verify

After installing, confirm the console scripts are on your PATH:

```bash
mcp-server --help        # catalog
mcp-cli --help           # admin CLI
webui-server --help      # Web UI
```

Run the test suite:

```bash
make test                # 379 tests, ~20s
make test-cov            # + coverage report (fails below 85%)
```

The Postgres-gated migration parity tests run when
`MCP_TEST_POSTGRES_URL=postgresql://...` is set in the environment;
they're skipped otherwise so the default install needs no extra services.
