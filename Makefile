# Run from WSL or Linux. Main commands: make help.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.dev.yml
SERVICES := api
SECRETS := postgres_password redis_password rabbitmq_password
# The repo may live on a Windows drive mounted into WSL; hardlinks do not work there.
export UV_LINK_MODE := copy

.DEFAULT_GOAL := help
.PHONY: help env secrets up down logs test lint fmt check-compose pre-commit

help: ## List commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || { cp .env.example .env; echo "created .env"; }

secrets: ## Generate dev secrets in deploy/secrets/dev (existing ones are kept)
	@mkdir -p deploy/secrets/dev
	@for n in $(SECRETS); do \
	  f=deploy/secrets/dev/$$n; \
	  if [ ! -f $$f ]; then \
	    openssl rand -base64 33 | tr -d '/+=\n' > $$f; \
	    chmod 644 $$f; \
	    echo "created $$f"; \
	  fi; \
	done
	# 644, not 600: processes inside containers (postgres, rabbitmq) run as other users.
	# In prod the files live outside the repository with permissions set on the server.

up: env secrets ## Start the core (postgres, redis, rabbitmq) and dev tools (Adminer)
	$(COMPOSE) up -d --wait

down: ## Stop everything
	$(COMPOSE) down

logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

test: ## Unit and integration tests of all services
	@for s in $(SERVICES); do \
	  echo "== $$s"; (cd services/$$s && uv run pytest) || exit 1; \
	done

lint: ## ruff + mypy
	@for s in $(SERVICES); do \
	  echo "== $$s"; \
	  (cd services/$$s && uv run ruff check --config ../../ruff.toml . && uv run ruff format --config ../../ruff.toml --check . && uv run mypy app tests) || exit 1; \
	done

fmt: ## ruff format
	@for s in $(SERVICES); do (cd services/$$s && uv run ruff format --config ../../ruff.toml .); done

check-compose: env secrets ## Verify log rotation and restart policy for every compose service
	python3 scripts/check_compose.py deploy/compose.yml deploy/compose.dev.yml

pre-commit: ## Run all pre-commit hooks on the whole tree
	uvx pre-commit run --all-files
