# IMPOSBRO Search - root-level targets

PYTHON ?= python3
UV ?= uv
HELM ?= $(shell command -v helm 2>/dev/null || printf "%s/.local/bin/helm" "$$HOME")
HELM_TEST_VALUES ?= -f helm/ci-values.yaml
COMPOSE_ENV_FILE ?= $(if $(wildcard .env),.env,.env.example)
PIP_AUDIT_REQUIREMENTS ?= requirements-audit.txt

SCALE_COMPOSE_FILE ?= docker-compose.yml:docker-compose.scale.yml
SCALE_QUERY_API_REPLICAS ?= 3
SCALE_INDEXING_REPLICAS ?= 3

BENCHMARK_DOCKER_DOCUMENTS ?= 500
BENCHMARK_DOCKER_INGEST_CONCURRENCY ?= 16
BENCHMARK_DOCKER_INGEST_BATCH_SIZE ?= 1
BENCHMARK_DOCKER_SEARCH_REQUESTS ?= 100
BENCHMARK_DOCKER_SEARCH_CONCURRENCY ?= 8
BENCHMARK_DOCKER_TIMEOUT_SECONDS ?= 180
BENCHMARK_DOCKER_OUTPUT_JSON ?= artifacts/benchmark-docker.json
BENCHMARK_DOCKER_OUTPUT_MARKDOWN ?= artifacts/benchmark-docker.md

.PHONY: help test test-api test-ui lint build-ui lock-python audit compose-config compose-config-scale helm smoke-vector smoke-outage smoke-load smoke-state smoke-alias smoke-scale benchmark-k8s benchmark-docker smoke-docker smoke-docker-outage smoke-docker-load smoke-docker-state smoke-docker-alias smoke-docker-scale ci

help:
	@echo "IMPOSBRO Search – available targets:"
	@echo "  make test           Run backend/indexing pytest and Admin UI tests"
	@echo "  make test-api       Run Query API and indexing pytest"
	@echo "  make test-ui        Run Admin UI unit tests"
	@echo "  make lint           Run Admin UI lint"
	@echo "  make build-ui       Build Admin UI"
	@echo "  make lock-python    Refresh hashed production dependency locks with uv"
	@echo "  make audit          Run npm and Python dependency audits"
	@echo "  make compose-config Validate docker compose config"
	@echo "  make compose-config-scale Validate multi-instance docker compose overlay"
	@echo "  make helm           Lint, render, and validate Helm chart scenarios"
	@echo "  make smoke-vector   Run vector smoke against an already running stack"
	@echo "  make smoke-outage   Run partial-outage smoke against an already running stack"
	@echo "  make smoke-load     Run Kafka/indexing load smoke against an already running stack"
	@echo "  make smoke-state    Run control-plane backup/restore smoke against an already running stack"
	@echo "  make smoke-alias    Run collection alias switching smoke against an already running stack"
	@echo "  make smoke-scale    Run multi-instance rolling smoke against an already running scaled stack"
	@echo "  make benchmark-k8s  Run sustained ingest/search benchmark against Query API"
	@echo "  make benchmark-docker Build/start Docker stack, run local benchmark, then stop it"
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

lock-python:
	cd query_api && $(UV) pip compile requirements.txt --python-version 3.11 --generate-hashes --output-file requirements.lock
	cd indexing_service && $(UV) pip compile requirements.txt --python-version 3.11 --generate-hashes --output-file requirements.lock

audit:
	npm audit --omit=dev
	npm --prefix admin_ui audit --omit=dev
	$(PYTHON) -m pip install --disable-pip-version-check -r $(PIP_AUDIT_REQUIREMENTS)
	$(PYTHON) -m pip_audit --requirement query_api/requirements.lock --require-hashes --disable-pip
	$(PYTHON) -m pip_audit --requirement indexing_service/requirements.lock --require-hashes --disable-pip

compose-config:
	COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE) docker compose --env-file $(COMPOSE_ENV_FILE) config --quiet

compose-config-scale:
	COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE) COMPOSE_FILE=$(SCALE_COMPOSE_FILE) docker compose --env-file $(COMPOSE_ENV_FILE) config --quiet

helm:
	HELM="$(HELM)" HELM_TEST_VALUES="$(HELM_TEST_VALUES)" $(PYTHON) scripts/test-helm-chart.py

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

benchmark-docker:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	BENCHMARK_DOCUMENTS=$(BENCHMARK_DOCKER_DOCUMENTS) \
	BENCHMARK_INGEST_CONCURRENCY=$(BENCHMARK_DOCKER_INGEST_CONCURRENCY) \
	BENCHMARK_INGEST_BATCH_SIZE=$(BENCHMARK_DOCKER_INGEST_BATCH_SIZE) \
	BENCHMARK_SEARCH_REQUESTS=$(BENCHMARK_DOCKER_SEARCH_REQUESTS) \
	BENCHMARK_SEARCH_CONCURRENCY=$(BENCHMARK_DOCKER_SEARCH_CONCURRENCY) \
	BENCHMARK_TIMEOUT_SECONDS=$(BENCHMARK_DOCKER_TIMEOUT_SECONDS) \
	BENCHMARK_OUTPUT_JSON=$(BENCHMARK_DOCKER_OUTPUT_JSON) \
	BENCHMARK_OUTPUT_MARKDOWN=$(BENCHMARK_DOCKER_OUTPUT_MARKDOWN) \
	$(PYTHON) scripts/benchmark-k8s.py

smoke-docker:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-vector-search.py

smoke-docker-outage:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-partial-outage.py

smoke-docker-load:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-load.py

smoke-docker-state:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-state-backup.py

smoke-docker-alias:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build query_api indexing_service admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-aliases.py

smoke-docker-scale:
	@set -e; \
	export COMPOSE_ENV_FILE=$(COMPOSE_ENV_FILE); \
	export COMPOSE_FILE=$(SCALE_COMPOSE_FILE); \
	docker compose --env-file $(COMPOSE_ENV_FILE) up -d --build --scale query_api=$(SCALE_QUERY_API_REPLICAS) --scale indexing_service=$(SCALE_INDEXING_REPLICAS) query_api indexing_service query_api_lb admin_ui; \
	trap 'docker compose --env-file $(COMPOSE_ENV_FILE) down' EXIT; \
	$(PYTHON) scripts/smoke-scale.py

ci: test lint build-ui compose-config compose-config-scale helm
