"""getbased-uvdata — CAMS-fed UV/atmosphere relay for getbased.health.

Pulls CAMS atmospheric composition forecasts on a schedule, indexes the
grid in memory, and serves per-(lat, lon, time) lookups in the same JSON
shape the browser app already accepts from Open-Meteo. Optionally merges
Open-Meteo's hourly clouds/temp/UVI into the response so a single
upstream call from the browser delivers everything the Light & Sun
module needs.

Two deploy targets share this code:
  • Hosted: maintainer-run instance behind /api/proxy?meteo=cams.
  • Self-host: user-run docker image, pointed at via the existing
    `selfhost` Sun Data Source mode.
"""

__version__ = "0.1.1"
