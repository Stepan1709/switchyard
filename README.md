# Switchyard

A self-hosted, OpenAI-compatible LLM gateway. It records real application traffic, replays it on local and external models of different sizes, compares quality, latency and cost, and lets a human choose routing rules: which kind of request is handled by which model. The gateway then applies the rules in production and reports spend, savings, server utilisation and user feedback.

> Status: early development. The infrastructure and the control-plane service skeleton (config, logging, health checks, migrations, graceful shutdown) are in place; the gateway, worker and admin UI are being built step by step.

## Architecture

```mermaid
flowchart LR
    app["Client app"] -->|"OpenAI API"| gw["gateway"]
    gw -->|"SSE proxy"| prov[("Providers: local + external LLMs")]
    gw -.->|"request.completed"| mq(["RabbitMQ"])
    mq --> wk["worker"]
    wk --> pg[("PostgreSQL")]
    api["api: control plane"] --> pg
    api -.->|"rules.changed via outbox"| mq
    mq -.->|"rules.changed"| gw
    gw --- rd[("Redis")]
    api --- rd
    web["web: admin UI"] -->|"HTTP"| api
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
make up        # postgres, redis, rabbitmq, migrations, api, worker, mock_llm + Adminer; waits for healthchecks
make test      # unit and integration tests (integration tests start containers via Docker)
make lint      # ruff + mypy
make migrate   # apply database migrations (also runs automatically before api on `make up`)
make create-admin EMAIL=you@example.com   # create an administrator (password prompt)
make down
```

Dev endpoints (localhost only): API `:8001` (`/docs`, `/health/live`, `/health/ready`), mock provider `:8003`, worker health `:8004`, Adminer `:8080`, RabbitMQ UI `:15672`, Postgres `:5433`, Redis `:6379`, AMQP `:5672`.
Run `make help` for all commands.

## Admin API: first steps

On `make up` the first administrator is created from `ADMIN_EMAIL` (see `.env`) and the dev secret
`deploy/secrets/dev/admin_password`, if no administrator exists yet. Interactive docs: `http://localhost:8001/docs`.

```bash
PW=$(cat deploy/secrets/dev/admin_password)
TOKEN=$(curl -s localhost:8001/auth/login -H 'content-type: application/json'   -d "{\"email\":\"admin@example.com\",\"password\":\"$PW\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8001/projects -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"name":"demo"}'
# then POST /projects/{id}/keys: the response contains the client key (swy_...) exactly once
```

Access model: administrators (`admin` can change things, `viewer` is read-only) see only the projects they are members of.
Client API keys look like `swy_<prefix>_<secret>`; only the prefix and an HMAC of the secret are stored.

## Providers and models

`mock_llm` is an OpenAI-compatible provider simulator (`mock-tiny`, `mock-mid`, `mock-strong`, `mock-judge`) with
deterministic answers of different quality, streaming, `usage`, tool calls, context limits and failure injection
(`x-mock-fail: 500|503|429|timeout`, `x-mock-delay-ms`, `x-mock-output-tokens`, `x-mock-drop-after-chunks`).

```bash
# register the mock as a self-hosted provider, add a model, check the connection
curl -s -X POST localhost:8001/providers -H "authorization: Bearer $TOKEN" -H 'content-type: application/json'   -d '{"name":"mock","base_url":"http://mock_llm:8003/v1","auth_type":"none","is_local":true}'
# POST /models, POST /models/{id}/prices (price history), POST /providers/{id}/test
```

Provider access keys are stored AES-256-GCM encrypted (key ring from the `master_keys` secret) and are only ever
returned masked. Provider addresses are validated against SSRF: cloud metadata and link-local ranges are always
rejected, private addresses are allowed only for providers marked `local` whose host is listed in
`LOCAL_PROVIDER_HOSTS`, and the connection goes to the address that was checked (not to the name again).

## Messaging, outbox and the worker

Events and jobs travel through RabbitMQ (`switchyard.events` topic exchange, `switchyard.dlx` for retries and dead
letters). Every work queue `Q` comes with `Q.retry.1..3` (delays 5 s, 30 s, 5 min through queue TTLs and the
dead-letter exchange) and `Q.dlq`:

- a handler that raises `TemporaryError` is retried with a growing delay, then dead-lettered;
- `PermanentError` (or any unexpected exception) goes to the DLQ at once, with the reason in the message headers;
- publishing waits for the broker's confirmation and is `mandatory`: an event nobody is subscribed to is reported;
- delivery is at-least-once, so consumers drop duplicates through the inbox (`processed_messages`, written in the same
  transaction as the business effect);
- events that must not be lost are written to the `outbox` table in the same transaction as the data, and a relay
  inside the worker publishes them (`FOR UPDATE SKIP LOCKED`, safe to run in several processes);
- unsafe admin `POST`s (projects, providers, models, prices) accept an `Idempotency-Key` header: a retry returns the
  stored response instead of repeating the action.

The `worker` container runs the consumers, the outbox relay and hourly cleanup, and stops gracefully on SIGTERM
(running handlers finish, unfinished messages go back to the queue).

```bash
python -m app.cli dlq list --queue worker.request_log            # what failed and why (no payloads)
python -m app.cli dlq requeue --queue worker.request_log --limit 10
```

## Repository layout

```
services/api/   application code (api, gateway and worker entry points), migrations, tests, Dockerfile
services/mock_llm/   OpenAI-compatible provider simulator
deploy/         compose files, secret templates
scripts/        helper scripts
docs/adr/       architecture decision records
```

## License

MIT
