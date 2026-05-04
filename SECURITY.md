# Security policy

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security findings. Use one of:

1. GitHub's [private security advisories](https://github.com/elkimek/getbased-uvdata/security/advisories/new) — preferred.
2. Email the maintainer directly via the contact in the [getbased.health](https://getbased.health) site.

Initial response within ~3 business days. Coordinated disclosure preferred; the timeline depends on severity (critical: 24 h, high: 7 days, medium: 30 days from the time a reproducible report lands).

## Scope

This repository is the **CAMS-fed UV/atmosphere relay** that backs the Light & Sun module of [getbased.health](https://getbased.health). Inside scope:

- Authentication / bearer handling
- Input validation on `/uv`, `/spectrum`, `/healthz`, `/metrics`
- Path traversal / file-handling on `CAMS_CACHE_DIR`
- Credential leakage via logs / response bodies / response headers
- Snapshot deserialisation safety (npz)
- DoS surface (unbounded query params, missing upstream timeouts, compute amplification on `/spectrum`)
- CORS configuration
- Dependency CVEs (top-level deps in `pyproject.toml`)

Out of scope:

- Bugs in [Copernicus ADS](https://ads.atmosphere.copernicus.eu/) itself
- Bugs in [Open-Meteo](https://open-meteo.com)
- Bugs in `xarray` / `cdsapi` / `httpx` / `fastapi` (report upstream)
- DoS via simply burning the operator's CAMS quota — operators are expected to set `GETBASED_UVDATA_BEARER` and front the relay with their own rate-limiting reverse proxy

## Operational guidance for self-hosters

- **Always set `GETBASED_UVDATA_BEARER`** in production. Open mode lets any reachable client burn your CAMS quota.
- **Rotate bearers periodically.** They're long-lived shared secrets between the relay and the consumer (Vercel proxy or app's Settings panel); a leaked bearer can drain quota until rotated.
- **Front the relay with TLS + a rate-limiting reverse proxy.** This repo's auth is "do you know the password?", not "how often can you ask?" — production deploys should sit behind Caddy/nginx/Cloudflare with per-IP rate limits.
- **Don't log `/healthz` body to a public log aggregator.** It's intentionally minimal but still reveals snapshot freshness; pair it with authentication if your aggregator is shared.
- **Use a process supervisor that respects healthcheck failures** (Docker, systemd, k8s — all do by default). The retry-with-backoff loop keeps trying CDS, but a wedged process that the healthcheck flips unhealthy on is the catch-all for everything else.

## Threat model

We assume:

- The relay is reachable on the public internet under TLS.
- The bearer is shared between the relay and exactly one trusted consumer (Vercel proxy for the maintainer's hosted instance; the user's own app for self-host).
- The CDS-API key is a long-lived secret; leaking it lets an attacker drain the operator's CAMS quota.
- Open-Meteo is a trusted upstream (we hardcode the URL; do not make it env-configurable without a host allowlist).
- Snapshots on disk live in operator-controlled space — `CAMS_CACHE_DIR` is not attacker-controlled at runtime.

## Known posture

- All credentials are read from environment variables; no hardcoded values.
- Constant-time bearer compare via `hmac.compare_digest`.
- `np.load(allow_pickle=False)` — disk snapshot deserialisation can't execute code.
- CDS-API exception strings are scrubbed before they reach `/healthz` body, response headers, or stdout (live values of `CAMS_API_KEY` / `GETBASED_UVDATA_BEARER` substitution).
- `/metrics` is bearer-gated. `/healthz` is open but minimal-info.
- CORS is a deny-by-default allowlist; operator extras must validate as `scheme://host[:port]`.

## What's NOT a vulnerability we can act on

- Reports that don't include reproduction steps + expected/actual behaviour.
- Findings against the Lab Charts app itself — those go to its own [security policy](https://github.com/elkimek/get-based/security).
- Reports that depend on operator misconfiguration the README explicitly warns against (e.g. running open mode on the public internet).
