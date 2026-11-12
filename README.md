# Switchyard

A self-hosted, OpenAI-compatible LLM gateway. It records real application traffic, replays it on local and external models of different sizes, compares quality, latency and cost, and lets a human choose routing rules: which kind of request is handled by which model. The gateway then applies the rules in production and reports spend, savings, server utilisation and user feedback.

> Status: early development. The infrastructure skeleton is in place; the gateway, control plane and admin UI are being built step by step.

## Architecture

```mermaid
flowchart LR
    app[Client app] -->|OpenAI API| gw[gateway]
    gw -->|SSE proxy| prov[(Providers: local + external LLMs)]
    gw -. request.completed .-> mq[[RabbitMQ]]
    mq --> wk[worker]
    wk --> pg[(PostgreSQL)]
    api[api: control plane] --> pg
    api -. rules.changed via outbox .-> mq
    mq -. rules.changed .-> gw
    gw --- rd[(Redis)]
    api --- rd
    web[web: admin UI] -->|HTTP| api
```

- **gateway** - data plane: key check, model choice from an in-memory snapshot, streaming proxy, traffic events. No database on the hot path.
- **api** - control plane: projects, keys, providers, models, calibrations, rules, analytics.
- **worker** - same image, background consumers: traffic log writer, replay and judge jobs, aggregates.
- **web** - admin UI (FastAPI + Jinja2 + HTMX).
- PostgreSQL stores configuration, traffic logs and results; Redis holds caches, rate limits and sessions; RabbitMQ carries events and jobs.

## Quick start

Requirements: Linux or WSL2, Docker with Compose v2, `make`, `uv`, `git`.

```bash
make secrets   # random dev passwords in deploy/secrets/dev (not committed)
make up        # postgres, redis, rabbitmq + Adminer; waits for healthchecks
make test      # unit and integration tests
make lint      # ruff + mypy
make down
```

Dev endpoints (localhost only): Adminer `:8080`, RabbitMQ UI `:15672`, Postgres `:5433`, Redis `:6379`, AMQP `:5672`.
Run `make help` for all commands.

## Repository layout

```
services/api/   application code (api, gateway and worker entry points), tests, Dockerfile
deploy/         compose files, secret templates
scripts/        helper scripts
docs/adr/       architecture decision records
```

## License

MIT
