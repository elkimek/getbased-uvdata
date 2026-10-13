# Multi-stage build:
#   builder — installs the package + its dev headers (libnetcdf-dev / libhdf5-dev)
#   runtime — slim image with only the runtime libs + the installed package
# Saves ~200 MB vs a single stage that ships the dev headers.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder

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
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/install/lib/python3.12/site-packages

# Only curl (for the healthcheck) + ca-certs (for HTTPS to CDS-API).
# The netCDF4 / xarray Python wheels ship their own bundled libnetcdf
# + libhdf5 in manylinux wheels — we don't need apt-installed runtime
# libs (the package names also drift per Debian release: libnetcdf19
# in bookworm, libnetcdf22 in trixie, etc — fragile to pin).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
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
