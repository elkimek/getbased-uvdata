# Multi-stage build:
#   builder — installs the package + its dev headers (libnetcdf-dev / libhdf5-dev)
#   runtime — slim image with only the runtime libs + the installed package
# Saves ~200 MB vs a single stage that ships the dev headers.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libnetcdf-dev libhdf5-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# ───────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/install/lib/python3.12/site-packages

# Only the runtime libs — no dev headers. curl is for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnetcdf19 libhdf5-103-1t64 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. /data is the default cache dir and must be writable
# by this uid — chown it before chowning the install tree so a bind
# mount (docker-compose volume) inherits the right ownership.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin uvdata \
    && mkdir -p /data \
    && chown uvdata:uvdata /data

COPY --from=builder /install /install

USER uvdata
WORKDIR /home/uvdata

# Healthcheck: liveness only — first pull is queued by CDS, so this
# tolerates `ok: false` for a window. Failing assertion only when the
# server itself is unresponsive (port closed, process exited).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8324/healthz || exit 1

EXPOSE 8324
CMD ["getbased-uvdata"]
