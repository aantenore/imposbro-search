# IMPOSBRO Search – root-level targets
# Use: make test-api | test (alias) | help

.PHONY: help test test-api lint

help:
	@echo "IMPOSBRO Search – available targets:"
	@echo "  make test-api   Run Query API pytest (requires TESTING=1)"
	@echo "  make test       Alias for test-api"
	@echo "  make lint       Run lint on query_api and admin_ui (if available)"

test: test-api

test-api:
	cd query_api && TESTING=1 python -m pytest tests -v

lint:
	@command -v ruff >/dev/null 2>&1 && cd query_api && ruff check app || true
	@command -v npm >/dev/null 2>&1 && cd admin_ui && npm run lint 2>/dev/null || true
