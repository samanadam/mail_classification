# syntax=docker/dockerfile:1.7
# Two images from one file:
#   --target api     FastAPI + built dashboard (small, always on)
#   --target worker  scheduler + daily run: Laya (CPU), ClamAV, gpg, pg_dump

FROM node:22-alpine AS dashboard
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MAILCAT_DATA_DIR=/data
RUN groupadd --system --gid 10001 mailcat \
 && useradd --system --uid 10001 --gid mailcat --home-dir /home/mailcat --create-home --shell /usr/sbin/nologin mailcat
WORKDIR /app
COPY backend/pyproject.toml backend/README.md* ./
COPY backend/mailcat ./mailcat
RUN pip install .

FROM base AS api
COPY --from=dashboard /src/dist /app/static
ENV MAILCAT_STATIC_DIR=/app/static
USER mailcat
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
CMD ["mailcat", "api", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS worker
# pg_dump must match the server's major version (postgres:16), so the client comes from PGDG.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends clamav clamav-daemon clamav-freshclam postgresql-client-16 \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/clamav \
 && chown mailcat:mailcat /run/clamav
# CPU-only torch keeps the image ~2 GB smaller than the default CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install "laya[onnx]>=0.3.24"
COPY deploy/clamd.conf /etc/clamav/clamd.conf
COPY deploy/freshclam.conf /etc/clamav/freshclam.conf
ENV HF_HOME=/data/models \
    XDG_CACHE_HOME=/data/cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=2
USER mailcat
CMD ["mailcat", "scheduler"]
