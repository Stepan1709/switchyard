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

FROM python:3.12.16-slim-bookworm AS runtime
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Not root: the process gets no extra privileges inside the container.
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /srv
COPY --from=builder --chown=app:app /srv /srv
USER app
EXPOSE 8001
# The /health/live endpoint appears together with the FastAPI app (stage 1).
# Until then the check only confirms that the interpreter and package import work.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import app"]
# Exec form: the process is PID 1 and receives SIGTERM directly (graceful shutdown).
CMD ["python", "-m", "app"]
