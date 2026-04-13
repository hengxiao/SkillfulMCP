.DEFAULT_GOAL := help

# Load .env if it exists so targets can use env vars directly
-include .env
export

# ── Variables ──────────────────────────────────────────────────────────────────
PYTHON      ?= python
PYTEST      ?= pytest
MCP_HOST    ?= 0.0.0.0
MCP_PORT    ?= 8000
WEBUI_HOST  ?= 127.0.0.1
WEBUI_PORT  ?= 8080

# Used by the example runner; override on the command line if needed:
#   make example MESSAGE="Why is my bill so high?"
MESSAGE     ?= My internet has been dropping every night for a week

# ── Phony targets ──────────────────────────────────────────────────────────────
.PHONY: help install install-examples env serve webui test test-v example \
        example-anthropic example-openai example-langchain example-langgraph clean \
        docker-build docker-up docker-up-detach docker-down \
        helm-lint helm-template

# ── Help ───────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "SkillfulMCP — available targets"
	@echo ""
	@echo "  make install           Install the package and dev dependencies"
	@echo "  make install-examples  Also install deps for the framework examples"
	@echo "  make env               Copy .env.example → .env (skip if .env exists)"
	@echo "  make serve             Start the MCP server (localhost:$(MCP_PORT))"
	@echo "  make webui             Start the Web UI     (localhost:$(WEBUI_PORT))"
	@echo "  make test              Run the test suite"
	@echo "  make test-v            Run the test suite (verbose)"
	@echo "  make test-cov          Run the suite with coverage (fails if <85%)"
	@echo "  make example           Default example network (Anthropic SDK)"
	@echo "  make example-anthropic Same as above (explicit)"
	@echo "  make example-openai    OpenAI SDK runner"
	@echo "  make example-langchain LangChain runner"
	@echo "  make example-langgraph LangGraph runner"
	@echo "                         MESSAGE=\"...\" to change the user prompt"
	@echo "  make clean             Remove build artefacts and temp files"
	@echo ""
	@echo "  make docker-build      Build catalog + webui images (skillful-mcp/*:dev)"
	@echo "  make docker-up         Run the local stack (catalog+webui+postgres) in foreground"
	@echo "  make docker-up-detach  Same, in background"
	@echo "  make docker-down       Stop + remove the local stack"
	@echo ""
	@echo "  make helm-lint         Lint the Helm chart under deploy/helm/skillful-mcp"
	@echo "  make helm-template     Render chart locally with stub secret (debugging)"
	@echo ""

# ── Install ────────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e ".[dev]"

install-examples:
	$(PYTHON) -m pip install -e ".[dev,examples]"

# ── Environment ────────────────────────────────────────────────────────────────
env:
	@if [ -f .env ]; then \
		echo ".env already exists — skipping. Edit it directly to change settings."; \
	else \
		cp .env.example .env; \
		echo ".env created from .env.example. Set MCP_JWT_SECRET and MCP_ADMIN_KEY before running."; \
	fi

# ── Servers ────────────────────────────────────────────────────────────────────
serve:
	uvicorn "mcp_server.main:create_app" --factory \
		--host $(MCP_HOST) --port $(MCP_PORT) --reload

webui:
	uvicorn "webui.main:create_app" --factory \
		--host $(WEBUI_HOST) --port $(WEBUI_PORT) --reload

# ── Tests ──────────────────────────────────────────────────────────────────────
test:
	$(PYTEST)

test-v:
	$(PYTEST) -v

# Coverage run + report. `fail_under=85` in pyproject.toml enforces the
# floor — regressions cause this target to exit non-zero, which is what
# CI checks.
test-cov:
	$(PYTEST) --cov --cov-report=term-missing

# ── Examples ───────────────────────────────────────────────────────────────────
example: example-anthropic

example-anthropic:
	$(PYTHON) -m example.anthropic_sdk.run_network --message "$(MESSAGE)"

example-openai:
	$(PYTHON) -m example.openai_sdk.run_network --message "$(MESSAGE)"

example-langchain:
	$(PYTHON) -m example.langchain_app.run_network --message "$(MESSAGE)"

example-langgraph:
	$(PYTHON) -m example.langgraph_app.run_network --message "$(MESSAGE)"

# ── Docker ─────────────────────────────────────────────────────────────────────
docker-build:
	docker build -f deploy/Dockerfile.catalog -t skillful-mcp/catalog:dev .
	docker build -f deploy/Dockerfile.webui   -t skillful-mcp/webui:dev   .

docker-up:
	docker compose up --build

docker-up-detach:
	docker compose up --build -d

docker-down:
	docker compose down -v

# ── Helm ───────────────────────────────────────────────────────────────────────
helm-lint:
	helm lint deploy/helm/skillful-mcp

helm-template:
	helm template mcp deploy/helm/skillful-mcp --set existingSecret=mcp-test

# ── Clean ──────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info"  -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.db"  -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
