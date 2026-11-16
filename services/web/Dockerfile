# syntax=docker/dockerfile:1.7
# The web interface: a Python server that renders pages. The CSS is built in its own stage with
# the Tailwind standalone binary, so neither Node nor a CDN is needed anywhere.

# ---- stage 1: the stylesheet -----------------------------------------------------------------
FROM python:3.12.16-slim-bookworm AS css
ARG TARGETARCH
ARG TAILWIND_VERSION=v4.3.5
# The binary is checked against a known checksum: a changed release fails the build.
ARG TAILWIND_SHA256_AMD64=e47695cee3f0d90623a91f3939b08bdee610eb5ad99340adff9d4145b284d6cc
ARG TAILWIND_SHA256_ARM64=ffcd0d21dbd7c605dc12743ff0c0b47debc963c86b399674084c1c8f2c96ecad
RUN python - <<'EOF'
import hashlib, os, urllib.request

arch = {"amd64": "x64", "arm64": "arm64"}[os.environ["TARGETARCH"]]
expected = os.environ["TAILWIND_SHA256_" + os.environ["TARGETARCH"].upper()]
url = (
    "https://github.com/tailwindlabs/tailwindcss/releases/download/"
    f"{os.environ['TAILWIND_VERSION']}/tailwindcss-linux-{arch}"
)
data = urllib.request.urlopen(url, timeout=120).read()
assert hashlib.sha256(data).hexdigest() == expected, "Tailwind checksum mismatch"
with open("/usr/local/bin/tailwindcss", "wb") as f:
    f.write(data)
os.chmod("/usr/local/bin/tailwindcss", 0o755)
EOF
WORKDIR /srv
# Tailwind reads the class names from the templates and scripts and emits only what is used.
COPY app/templates ./app/templates
COPY app/static/js ./app/static/js
COPY app/static/css/input.css ./app/static/css/input.css
RUN tailwindcss --input app/static/css/input.css --output app/static/css/app.css --minify

# ---- stage 2: the dependencies ---------------------------------------------------------------
FROM python:3.12.16-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
# Dependencies first: this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY --from=css /srv/app/static/css/app.css ./app/static/css/app.css

# ---- stage 3: the image that runs ------------------------------------------------------------
FROM python:3.12.16-slim-bookworm AS runtime
ENV PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /srv
COPY --from=builder --chown=app:app /srv /srv
USER app
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8002/health/live', timeout=2)"]
# Exec form: uvicorn is PID 1 and receives SIGTERM directly.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8002", \
     "--timeout-graceful-shutdown", "10", "--no-access-log"]
