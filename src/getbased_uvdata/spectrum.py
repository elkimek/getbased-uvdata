"""Bird-Riordan spectral reconstruction — Python port of js/sun-spectrum.js.

When the browser asks `/spectrum`, this server runs the same radiative-
transfer math the app would otherwise run client-side, but fed by REAL
CAMS-derived ozone DU + AOD instead of a 300 DU constant + Open-Meteo's
mostly-empty AOD field. Output is a wavelength-resolved surface UV
spectrum (W/m²/nm) the browser can integrate directly through its
existing channel-action-spectrum machinery.

Why server-side: the client model uncertainty band sits at ±20-45%
because Bird-Riordan is fed approximations. With CAMS values the same
math collapses to ±10-15% in the UV sweet-spot. Same engine, better
inputs.

The two implementations (here + js/sun-spectrum.js) MUST stay in
lockstep on action-spectrum constants, ozone cross-section table, and
diffuse-fraction table. Tests should fixture-pin both ends to the
same TUV/NIWA reference values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ── Wavelength grid (5 nm, 280-2500 nm) — matches js/sun-spectrum.js ───
WAVELENGTHS: list[float] = [float(nm) for nm in range(280, 2505, 5)]


# ── Action spectra ─────────────────────────────────────────────────────


def erythemal_at(nm: float) -> float:
    """CIE McKinlay-Diffey erythemal action spectrum (CIE S 007/E:1998).

    Used both for sunburn-dose math and as the actinic-UV proxy in the
    retinal-UV calc. Peaks at 297 nm, drops sharply through UVA."""
    if nm < 250:
        return 0.0
    if nm <= 298:
        return 1.0
    if nm <= 328:
        return 10 ** (0.094 * (298 - nm))
    if nm <= 400:
        return 10 ** (0.015 * (140 - nm))
    return 0.0


def vitamin_d_at(nm: float) -> float:
    """CIE 174:2006 vitamin-D action spectrum (smoothed approximation).

    Narrower window than erythemal (252-330 nm), peak at 297 nm."""
    if nm < 252 or nm > 330:
        return 0.0
    if nm <= 297:
        return 10 ** (-0.25 * (297 - nm))
    return 10 ** (-0.13 * (nm - 297))


# ── Ozone absorption (Bass-Paur table, log-space interp) ──────────────
# Cross-sections from JPL Publication 19-5 at 273 K. Vary across 6
# orders of magnitude through the Hartley + Huggins + Chappuis bands;
# log-space interpolation keeps the curve physical.
_O3_XSEC_TABLE: list[tuple[float, float]] = [
    (240.0, 9.45e-18),
    (250.0, 1.10e-17),  # Hartley peak
    (260.0, 4.50e-18),
    (270.0, 1.61e-18),
    (280.0, 3.85e-19),
    (285.0, 5.50e-19),  # Huggins shoulder
    (290.0, 1.40e-18),
    (295.0, 7.00e-19),
    (298.0, 4.50e-19),
    (300.0, 3.50e-19),
    (305.0, 1.50e-19),
    (310.0, 5.30e-20),
    (315.0, 1.90e-20),
    (320.0, 6.90e-21),
    (325.0, 2.00e-21),
    (330.0, 6.60e-22),
    (340.0, 1.50e-22),
    (350.0, 4.00e-23),
]
# 1 DU = 2.69e16 mol/cm² × 1000 normalisation so the call site can
# write `tau_O3 = ozone_absorption(nm) * (DU / 1000)` (matches the
# JS implementation byte-for-byte).
_O3_NORM = 2.69e19


def ozone_absorption(nm: float) -> float:
    """Returns ozone absorption coefficient such that
    τ_O3(λ, DU) = ozone_absorption(λ) × (DU / 1000) × airMass."""
    if nm < 600:
        if nm <= _O3_XSEC_TABLE[0][0]:
            return _O3_XSEC_TABLE[0][1] * _O3_NORM
        last = _O3_XSEC_TABLE[-1]
        if nm >= last[0]:
            return last[1] * _O3_NORM
        # Log-space linear interpolation across the table.
        for i in range(len(_O3_XSEC_TABLE) - 1):
            n1, s1 = _O3_XSEC_TABLE[i]
            n2, s2 = _O3_XSEC_TABLE[i + 1]
            if n1 <= nm < n2:
                t = (nm - n1) / (n2 - n1)
                log_sigma = math.log10(s1) + t * (math.log10(s2) - math.log10(s1))
                return (10**log_sigma) * _O3_NORM
    # Chappuis band (visible, weak)
    if nm < 700:
        return 0.4 * math.exp(-(((nm - 600) / 60) ** 2))
    return 0.01


# ── Extraterrestrial irradiance (ASTM E490 fit) ───────────────────────
_TOA_POINTS: list[tuple[float, float]] = [
    (280, 0.082),
    (300, 0.541),
    (320, 0.815),
    (340, 1.057),
    (360, 1.080),
    (380, 1.146),
    (400, 1.486),
    (420, 1.700),
    (450, 2.066),
    (500, 1.929),
    (550, 1.812),
    (600, 1.694),
    (650, 1.515),
    (700, 1.350),
    (800, 1.054),
    (900, 0.807),
    (1000, 0.620),
    (1200, 0.380),
    (1500, 0.205),
    (2000, 0.103),
    (2500, 0.038),
]


def extraterrestrial_irradiance(nm: float) -> float:
    """W/m²/nm at top of atmosphere (linear interp through ASTM E490 points)."""
    if nm <= _TOA_POINTS[0][0]:
        return _TOA_POINTS[0][1]
    if nm >= _TOA_POINTS[-1][0]:
        return _TOA_POINTS[-1][1]
    for i in range(len(_TOA_POINTS) - 1):
        n1, v1 = _TOA_POINTS[i]
        n2, v2 = _TOA_POINTS[i + 1]
        if n1 <= nm <= n2:
            t = (nm - n1) / (n2 - n1)
            return v1 + t * (v2 - v1)
    return 0.0


# ── Bird-Riordan reconstruction ───────────────────────────────────────


@dataclass
class Spectrum:
    wavelengths: list[float]
    irradiance: list[float]


def reconstruct_spectrum(
    *,
    zenith_deg: float,
    ozone_du: float = 300.0,
    altitude_m: float = 0.0,
    cloud_cover: float = 0.0,
    aod: float | None = None,
) -> Spectrum:
    """Return surface spectral irradiance over WAVELENGTHS at the given
    atmospheric state. Mirrors js/sun-spectrum.js reconstructSpectrum
    line-for-line — diffuse-fraction table, airMass scaling, etc."""
    if zenith_deg is None or zenith_deg >= 90:
        return Spectrum(wavelengths=WAVELENGTHS, irradiance=[0.0] * len(WAVELENGTHS))

    cos_z = math.cos(math.radians(zenith_deg))
    air_mass = 1.0 / max(cos_z, 0.001)
    alt_scale = math.exp(-altitude_m / 8000)
    cloud_t = 1.0 - 0.75 * cloud_cover
    beta = aod if (aod is not None and aod > 0) else 0.10

    irradiance: list[float] = []
    for nm in WAVELENGTHS:
        e0 = extraterrestrial_irradiance(nm)
        lambda_um = nm / 1000.0
        # Rayleigh
        tau_r = alt_scale / (lambda_um**4 * (115.6406 - 1.335 / (lambda_um**2)))
        tr = math.exp(-tau_r * air_mass)
        # Ozone (Bass-Paur)
        tau_o3 = ozone_absorption(nm) * (ozone_du / 1000.0)
        to = math.exp(-tau_o3 * air_mass)
        # Aerosol (Ångström α=1.14)
        tau_a = beta * (nm / 500.0) ** -1.14
        ta = math.exp(-tau_a * air_mass)
        direct_beam = e0 * tr * to * ta * cos_z * cloud_t
        # Diffuse fraction by band — matches JS table
        if lambda_um < 0.32:
            diffuse_frac = 0.55
        elif lambda_um < 0.40:
            diffuse_frac = 0.40
        elif lambda_um < 0.50:
            diffuse_frac = 0.25
        elif lambda_um < 0.70:
            diffuse_frac = 0.15
        else:
            diffuse_frac = 0.08
        am_scale = min(math.sqrt(air_mass), 3.0)
        surface = direct_beam * (1.0 + diffuse_frac * am_scale)
        irradiance.append(max(0.0, surface))

    return Spectrum(wavelengths=WAVELENGTHS, irradiance=irradiance)


# ── Derived quantities ─────────────────────────────────────────────────


def erythemal_irradiance_w_per_m2(spectrum: Spectrum) -> float:
    """Integrate spectrum × erythemal action spectrum × dλ. Result is
    erythemally-weighted surface irradiance in W/m²."""
    dl = 5.0
    s = 0.0
    for nm, ir in zip(spectrum.wavelengths, spectrum.irradiance, strict=False):
        if nm > 400:
            break
        w = erythemal_at(nm)
        if w <= 0:
            continue
        s += ir * w * dl
    return s


def uvi_from_spectrum(spectrum: Spectrum) -> float:
    """WHO UV Index = 40 × erythemal irradiance (W/m²). 1 UVI ≈
    25 mW/m² erythemally weighted."""
    return 40.0 * erythemal_irradiance_w_per_m2(spectrum)


def solar_zenith_angle(when_epoch: float, lat: float, lon: float) -> float:
    """Solar zenith angle in degrees at (lat, lon, time). Mirrors
    `js/sun-uvdata.js solarZenithAngle` byte-for-byte (same NOAA
    simplified algorithm, same fractional-year noon-centred basis,
    same coefficient table). Lockstep is enforced by tests on both
    sides."""
    import datetime as _dt

    d = _dt.datetime.fromtimestamp(when_epoch, tz=_dt.UTC)
    # Day of year (1-365) — JS uses Math.floor((date - new Date(Date.UTC(year, 0, 0))) / 86400000)
    # which is `tm_yday` (Jan 1 → 1) since Date.UTC(year, 0, 0) is Dec 31 prev.
    day_of_year = d.timetuple().tm_yday
    # Fractional year — noon-centred fractional hours (matches JS exactly).
    # JS: (dayOfYear - 1 + (utcHours - 12) / 24) — uses ONLY the integer
    # hour, not minute/second sub-hour. Mimic that to stay in lockstep.
    fractional_year = (2 * math.pi / 365) * (day_of_year - 1 + (d.hour - 12) / 24.0)
    # Solar declination
    decl = (
        0.006918
        - 0.399912 * math.cos(fractional_year)
        + 0.070257 * math.sin(fractional_year)
        - 0.006758 * math.cos(2 * fractional_year)
        + 0.000907 * math.sin(2 * fractional_year)
        - 0.002697 * math.cos(3 * fractional_year)
        + 0.001480 * math.sin(3 * fractional_year)
    )
    # Equation of time (minutes)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(fractional_year)
        - 0.032077 * math.sin(fractional_year)
        - 0.014615 * math.cos(2 * fractional_year)
        - 0.040849 * math.sin(2 * fractional_year)
    )
    # True solar time (minutes)
    utc_minutes = d.hour * 60 + d.minute + d.second / 60.0
    tst = utc_minutes + eqtime + 4 * lon
    ha = math.radians(tst / 4.0 - 180.0)
    lat_rad = math.radians(lat)
    cos_zenith = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(
        ha
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return math.degrees(math.acos(cos_zenith))
