# Slim Python image with the netCDF/eccodes stack baked in. ~250 MB after
# strip — small enough to run on a $5 VPS with room for the in-memory
# CAMS grid (~150 MB at 0.4° global resolution).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# netCDF4 needs libnetcdf + libhdf5; cdsapi pulls these via wheels but
# the manylinux wheel for hdf5 still needs zlib at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnetcdf-dev libhdf5-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Healthcheck hits /healthz so docker / systemd / k8s can detect a
# wedged background-pull loop. Fails fast (3 s) so a missed CAMS pull
# doesn't keep the container "healthy" while serving 503s.
HEALTHCHECK --interval=60s --timeout=3s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8324/healthz | grep -q '"ok": *true' || exit 1

EXPOSE 8324
CMD ["getbased-uvdata"]
