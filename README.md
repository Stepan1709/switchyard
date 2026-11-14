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
make up        # postgres, redis, rabbitmq, migrations, api, gateway, worker, mock_llm + Adminer; waits for healthchecks
make test      # unit and integration tests (integration tests start containers via Docker)
make lint      # ruff + mypy
make migrate   # apply database migrations (also runs automatically before api on `make up`)
make create-admin EMAIL=you@example.com   # create an administrator (password prompt)
make down
```

Dev endpoints (localhost only): gateway `:8000`, API `:8001` (`/docs`, `/health/live`, `/health/ready`), mock provider `:8003`, worker health `:8004`, Adminer `:8080`, RabbitMQ UI `:15672`, Postgres `:5433`, Redis `:6379`, AMQP `:5672`.
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

## Gateway

An OpenAI-compatible proxy on port `8000`. An application only changes its `base_url` and API key:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1", api_key="swy_..."
)  # key from POST /projects/{id}/keys
client.chat.completions.create(model="anything", messages=[...], stream=True)
```

In `record` mode (the only mode so far) every request goes to the project's **reference model**, whatever `model` the
application sends; the answer, including streams, tool calls and `usage`, comes back as the provider produced it.

- **Hot path without the database.** Keys, projects and routes live in an immutable in-memory snapshot, rebuilt in the
  background every 15 s (so a new or revoked key takes effect within that time). The last snapshot is also copied to Redis
  (provider keys only as ciphertext) so a gateway can start while PostgreSQL is down.
- **Streaming** is forwarded chunk by chunk without buffering. The gateway turns on `stream_options.include_usage` to
  learn the token counts and removes the extra usage-only event again if the client did not ask for it.
- **Client disconnect** closes the provider connection at once, so no tokens are generated for nobody.
- **Limits:** request body size, a per-key rate limit (`rate_limit_rpm`, default 600/min; `x-ratelimit-*` headers),
  concurrent requests per provider. Timeouts are separate for connect, first byte, idle time and total time.
- **Errors** use the OpenAI shape. Provider answers about the request itself (400, 413, 422, 429) are passed through;
  provider-side failures (401/403 of the gateway's own key, 404, 5xx, timeouts, unreachable) become 502/504 without the
  provider's body.
- Response headers: `x-request-id`, `x-switchyard-model` (which model answered), `x-switchyard-mode`. `x-task-type`
  sets the task type (default: the key's `default_task_type`).
- `GET /v1/models`, `GET /health/live`, `GET /health/ready` (ready as soon as a snapshot is loaded).

### Overhead

Measured with `MOCK_TIME_SCALE=0 make up && make bench` (instant mock, everything on one laptop under WSL2, so read the
numbers as relative; a proper load test comes later). Direct = client to the mock; gateway = client to the gateway to the
mock, so the difference includes the second HTTP hop.

| non-streaming, 300 sequential requests | p50 | p95 | p99 |
|---|---|---|---|
| direct to mock (ms) | 1.94 | 2.99 | 4.42 |
| through the gateway (ms) | 5.10 | 6.67 | 7.64 |
| overhead (ms) | 3.15 | 3.68 | 3.22 |

Streaming time to the first byte: 2.0 ms direct, 5.1 ms through the gateway (p50). With 32 concurrent clients one gateway
process handled about 160 requests/s against about 360 for the mock itself (the load generator shares the machine).

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
