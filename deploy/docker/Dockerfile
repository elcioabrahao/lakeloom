# syntax=docker/dockerfile:1
# Lakeloom container image (NFR-DEP-01, NFR-DEP-04). Build from the repo root:
#   docker build -f deploy/docker/Dockerfile -t lakeloom:dev .

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12.14-slim-trixie AS builder
COPY --from=uv /uv /uvx /bin/
# Precompile .pyc at build time: the container runs with a read-only root filesystem.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cache layer), then the project itself (non-editable).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.14-slim-trixie AS runtime
LABEL org.opencontainers.image.title="Lakeloom" \
      org.opencontainers.image.source="https://github.com/elcioabrahao/lakeloom" \
      org.opencontainers.image.licenses="Apache-2.0"
# Upgrade base packages first so fixed Debian security updates land in the image even
# before the python tag is refreshed (NFR-SEC-17: Trivy CRITICAL/HIGH gate).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 lakeloom \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lakeloom \
    && mkdir /data && chown 10001:10001 /data
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    # NFR-SEC-04: stdio is off in containers unless explicitly enabled.
    LAKELOOM_STDIO_ENABLED=false
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["lakeloom"]
CMD ["serve"]
