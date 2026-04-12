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
.PHONY: help install env serve webui test test-v example clean

# ── Help ───────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "SkillfulMCP — available targets"
	@echo ""
	@echo "  make install       Install the package and dev dependencies"
	@echo "  make env           Copy .env.example → .env (skip if .env exists)"
	@echo "  make serve         Start the MCP server  (localhost:$(MCP_PORT))"
	@echo "  make webui         Start the Web UI      (localhost:$(WEBUI_PORT))"
	@echo "  make test          Run the test suite"
	@echo "  make test-v        Run the test suite (verbose)"
	@echo "  make example       Run the multi-agent example network"
	@echo "                       MESSAGE=\"...\" to change the user prompt"
	@echo "  make clean         Remove build artefacts and temp files"
	@echo ""

# ── Install ────────────────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install -e ".[dev]"

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

# ── Example ────────────────────────────────────────────────────────────────────
example:
	$(PYTHON) example/run_network.py --message "$(MESSAGE)"

# ── Clean ──────────────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info"  -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.db"  -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
