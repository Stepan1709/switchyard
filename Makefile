# Run from WSL or Linux. Main commands: make help.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.dev.yml
SERVICES := api mock_llm web
SECRETS := postgres_password redis_password rabbitmq_password jwt_secret key_pepper admin_password master_keys
# The repo may live on a Windows drive mounted into WSL; hardlinks do not work there.
export UV_LINK_MODE := copy

.DEFAULT_GOAL := help
.PHONY: help env secrets up up-full chaos load e2e backup-check logs-dump down logs migrate create-admin check-shutdown bench test clean-test-containers lint fmt check-compose pre-commit

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

down: ## Stop everything (including the monitoring and tracing profiles)
	$(COMPOSE) --profile '*' down

logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

up-full: env secrets ## make up + Prometheus, Grafana (:3000) and Jaeger (:16686), with tracing switched on
	OTEL_ENABLED=true $(COMPOSE) --profile monitoring --profile tracing up -d --build --wait

# The mock headers (x-mock-fail ...) must reach the mock provider, so the gateway is recreated
# with them allowed. Everything restarts the services it breaks before it ends.
chaos: env secrets ## Failure scenarios against the running stack (start it with `make up` first)
	FORWARD_HEADER_PREFIXES=x-mock- $(COMPOSE) up -d --wait gateway
	cd services/api && uv run python ../../scripts/chaos.py

# 400 concurrent calls per provider (the default is 200) so that 300 open streams are not refused.
LOAD_ENV := FORWARD_HEADER_PREFIXES=x-mock- MOCK_TIME_SCALE=0 GATEWAY_PROVIDER_MAX_CONCURRENCY=400

load: env secrets ## Load tests with container limits of a small VPS; prints the README table
	$(LOAD_ENV) $(COMPOSE) -f deploy/compose.load.yml up -d --build --wait
	$(LOAD_ENV) COMPOSE_FILES="deploy/compose.yml deploy/compose.dev.yml deploy/compose.load.yml" \
	  uv run --project loadtest python scripts/load.py

e2e: ## The whole path of the product on the running stack (make up first)
	cd services/api && uv run python ../../scripts/e2e.py

backup-check: env secrets ## Restore the newest database backup into a scratch database and compare it with the live one
	$(COMPOSE) --profile backup up -d --wait backup
	@for i in $$(seq 1 30); do \
	  $(COMPOSE) exec -T backup sh -c 'ls /backups/switchyard-*.dump' >/dev/null 2>&1 && break; sleep 2; \
	done
	$(COMPOSE) exec -T backup sh /backup/verify.sh

logs-dump: ## Print the last lines of every container's log (for a failed CI run)
	$(COMPOSE) --profile '*' logs --no-color --tail=200

migrate: env secrets ## Apply database migrations (one-shot container)
	$(COMPOSE) run --rm --build migrate

create-admin: ## Create an administrator: make create-admin EMAIL=you@example.com [ROLE=viewer]
	$(COMPOSE) run --rm -it api python -m app.cli create-admin --email $(EMAIL) --role $(or $(ROLE),admin)

bench: ## Gateway overhead vs the mock provider (start with: MOCK_TIME_SCALE=0 make up)
	cd services/api && uv run python ../../scripts/bench_gateway.py

check-shutdown: ## Time `docker compose stop api`: graceful shutdown must take a few seconds
	python3 scripts/check_shutdown.py

test: ## Unit and integration tests of all services (test containers are removed afterwards)
	@status=0; \
	for s in $(SERVICES); do \
	  echo "== $$s"; (cd services/$$s && uv run pytest) || { status=$$?; break; }; \
	done; \
	$(MAKE) --no-print-directory clean-test-containers; \
	exit $$status

# The integration tests start PostgreSQL, Redis and RabbitMQ with testcontainers and remove them
# at the end of the session; a run that is interrupted leaves them behind. Everything they start
# carries this label, so the cleanup cannot touch the containers of `make up` or of other
# projects. Do not run two test sessions at once: the first to finish would remove the other's.
clean-test-containers: ## Remove containers left behind by tests (testcontainers label)
	@ids=$$(docker ps -aq --filter label=org.testcontainers=true); \
	if [ -n "$$ids" ]; then echo "removing leftover test containers: $$(echo $$ids | wc -w)"; \
	  docker rm -f $$ids > /dev/null; fi

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
