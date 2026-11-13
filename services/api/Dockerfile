# syntax=docker/dockerfile:1.7
# One image, several entry points (api, gateway, worker). Entry point is chosen by the command.

FROM python:3.12.16-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
# Dependencies first: this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

FROM python:3.12.16-slim-bookworm AS runtime
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Not root: the process gets no extra privileges inside the container.
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /srv
COPY --from=builder --chown=app:app /srv /srv
USER app
EXPOSE 8001
# The process is alive and serving: /health/live does not touch any dependency.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8001/health/live', timeout=2)"]
# Exec form: uvicorn is PID 1 and receives SIGTERM directly. On SIGTERM it stops accepting
# connections and waits up to 20 s for in-flight requests (compose gives 30 s before SIGKILL).
CMD ["uvicorn", "app.api_main:app", "--host", "0.0.0.0", "--port", "8001", \
     "--timeout-graceful-shutdown", "20", "--no-access-log"]
