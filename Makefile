# IMPOSBRO Search - root-level targets

PYTHON ?= python3
HELM ?= $(shell command -v helm 2>/dev/null || printf "%s/.local/bin/helm" "$$HOME")

.PHONY: help test test-api test-ui lint build-ui compose-config helm ci

help:
	@echo "IMPOSBRO Search – available targets:"
	@echo "  make test           Run backend/indexing pytest and Admin UI tests"
	@echo "  make test-api       Run Query API and indexing pytest"
	@echo "  make test-ui        Run Admin UI unit tests"
	@echo "  make lint           Run Admin UI lint"
	@echo "  make build-ui       Build Admin UI"
	@echo "  make compose-config Validate docker compose config"
	@echo "  make helm           Lint and render Helm chart"
	@echo "  make ci             Run local release gate"

test: test-api test-ui

test-api:
	npm run test:api

test-ui:
	npm run test:ui

lint:
	cd admin_ui && npm run lint

build-ui:
	cd admin_ui && npm run build

compose-config:
	docker compose config --quiet

helm:
	$(HELM) lint ./helm
	$(HELM) template imposbro-release ./helm >/tmp/imposbro-helm-rendered.yaml

ci: test lint build-ui compose-config helm
