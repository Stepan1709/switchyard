# Run from WSL or Linux. Main commands: make help.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.dev.yml
SERVICES := api mock_llm web
SECRETS := postgres_password redis_password rabbitmq_password jwt_secret key_pepper admin_password master_keys
# The repo may live on a Windows drive mounted into WSL; hardlinks do not work there.
export UV_LINK_MODE := copy

.DEFAULT_GOAL := help
.PHONY: help env secrets up down logs migrate create-admin check-shutdown bench test lint fmt check-compose pre-commit

help: ## List commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || { cp .env.example .env; echo "created .env"; }

secrets: ## Generate dev secrets in deploy/secrets/dev (existing ones are kept)
	@mkdir -p deploy/secrets/dev
	@for n in $(SECRETS); do \
	  f=deploy/secrets/dev/$$n; \
	  if [ ! -f $$f ]; then \
	    if [ "$$n" = master_keys ]; then \
	      echo "k1:$$(openssl rand -base64 32)" > $$f; \
	    else \
	      openssl rand -base64 33 | tr -d '/+=\n' > $$f; \
	    fi; \
	    chmod 644 $$f; \
	    echo "created $$f"; \
	  fi; \
	done
	# 644, not 600: processes inside containers (postgres, rabbitmq) run as other users.
	# In prod the files live outside the repository with permissions set on the server.

up: env secrets ## Start the stack: postgres, redis, rabbitmq, migrations, api, mock_llm + Adminer
	$(COMPOSE) up -d --build --wait

down: ## Stop everything
	$(COMPOSE) down

logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

migrate: env secrets ## Apply database migrations (one-shot container)
	$(COMPOSE) run --rm --build migrate

create-admin: ## Create an administrator: make create-admin EMAIL=you@example.com [ROLE=viewer]
	$(COMPOSE) run --rm -it api python -m app.cli create-admin --email $(EMAIL) --role $(or $(ROLE),admin)

bench: ## Gateway overhead vs the mock provider (start with: MOCK_TIME_SCALE=0 make up)
	cd services/api && uv run python ../../scripts/bench_gateway.py

check-shutdown: ## Time `docker compose stop api`: graceful shutdown must take a few seconds
	python3 scripts/check_shutdown.py

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
