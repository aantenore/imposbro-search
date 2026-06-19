# IMPOSBRO Search - root-level targets

PYTHON ?= python3
HELM ?= $(shell command -v helm 2>/dev/null || printf "%s/.local/bin/helm" "$$HOME")
HELM_TEST_VALUES ?= -f helm/ci-values.yaml

SCALE_COMPOSE_FILE ?= docker-compose.yml:docker-compose.scale.yml
SCALE_QUERY_API_REPLICAS ?= 3
SCALE_INDEXING_REPLICAS ?= 3

.PHONY: help test test-api test-ui lint build-ui compose-config compose-config-scale helm smoke-vector smoke-outage smoke-load smoke-state smoke-alias smoke-scale benchmark-k8s smoke-docker smoke-docker-outage smoke-docker-load smoke-docker-state smoke-docker-alias smoke-docker-scale ci

help:
	@echo "IMPOSBRO Search – available targets:"
	@echo "  make test           Run backend/indexing pytest and Admin UI tests"
	@echo "  make test-api       Run Query API and indexing pytest"
	@echo "  make test-ui        Run Admin UI unit tests"
	@echo "  make lint           Run Admin UI lint"
	@echo "  make build-ui       Build Admin UI"
	@echo "  make compose-config Validate docker compose config"
	@echo "  make compose-config-scale Validate multi-instance docker compose overlay"
	@echo "  make helm           Lint and render Helm chart"
	@echo "  make smoke-vector   Run vector smoke against an already running stack"
	@echo "  make smoke-outage   Run partial-outage smoke against an already running stack"
	@echo "  make smoke-load     Run Kafka/indexing load smoke against an already running stack"
	@echo "  make smoke-state    Run control-plane backup/restore smoke against an already running stack"
	@echo "  make smoke-alias    Run collection alias switching smoke against an already running stack"
	@echo "  make smoke-scale    Run multi-instance rolling smoke against an already running scaled stack"
	@echo "  make benchmark-k8s  Run sustained ingest/search benchmark against Query API"
	@echo "  make smoke-docker   Build/start Docker stack, run vector smoke, then stop it"
	@echo "  make smoke-docker-outage Build/start Docker stack, run outage smoke, then stop it"
	@echo "  make smoke-docker-load Build/start Docker stack, run load smoke, then stop it"
	@echo "  make smoke-docker-state Build/start Docker stack, run state backup/restore smoke, then stop it"
	@echo "  make smoke-docker-alias Build/start Docker stack, run alias switching smoke, then stop it"
	@echo "  make smoke-docker-scale Build/start scaled Docker stack, run rolling smoke, then stop it"
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

compose-config-scale:
	COMPOSE_FILE=$(SCALE_COMPOSE_FILE) docker compose config --quiet

helm:
	$(HELM) lint ./helm $(HELM_TEST_VALUES)
	$(HELM) template imposbro-release ./helm $(HELM_TEST_VALUES) >/tmp/imposbro-helm-rendered.yaml

smoke-vector:
	$(PYTHON) scripts/smoke-vector-search.py

smoke-outage:
	$(PYTHON) scripts/smoke-partial-outage.py

smoke-load:
	$(PYTHON) scripts/smoke-load.py

smoke-state:
	$(PYTHON) scripts/smoke-state-backup.py

smoke-alias:
	$(PYTHON) scripts/smoke-aliases.py

smoke-scale:
	$(PYTHON) scripts/smoke-scale.py

benchmark-k8s:
	$(PYTHON) scripts/benchmark-k8s.py

smoke-docker:
	@set -e; \
	docker compose up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-vector-search.py

smoke-docker-outage:
	@set -e; \
	docker compose up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-partial-outage.py

smoke-docker-load:
	@set -e; \
	docker compose up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-load.py

smoke-docker-state:
	@set -e; \
	docker compose up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-state-backup.py

smoke-docker-alias:
	@set -e; \
	docker compose up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-aliases.py

smoke-docker-scale:
	@set -e; \
	export COMPOSE_FILE=$(SCALE_COMPOSE_FILE); \
	docker compose up -d --build --scale query_api=$(SCALE_QUERY_API_REPLICAS) --scale indexing_service=$(SCALE_INDEXING_REPLICAS) query_api indexing_service query_api_lb admin_ui; \
	trap 'docker compose down' EXIT; \
	$(PYTHON) scripts/smoke-scale.py

ci: test lint build-ui compose-config compose-config-scale helm
