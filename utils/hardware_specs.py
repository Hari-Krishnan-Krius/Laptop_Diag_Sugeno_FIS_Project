"""
utils/hardware_specs.py
─────────────────────────────────────────────────────────────────────────────
Resolves a per-laptop calibration profile from identified hardware
(CPU model, RAM type, GPU model) instead of relying solely on the generic
5-tier CATEGORY_PROFILES bucket in fuzzy_engine.py.

Design principle (see capstone report, Ch. 5/8/11 discussion):
  Every source used here is INDEPENDENT of the laptop's own live sensor
  readings — a JEDEC standard, a manufacturer datasheet, or a community
  hardware database cannot be corrupted by a laptop that happens to already
  be faulty when it is registered. This avoids the self-learned-baseline
  contamination problem discussed for the rejected "Option B" design.

Each of the four calibratable fields (cpu_v_nom, ram_v_nom, gpu_v_nom,
fan_rpm_nom) is resolved independently through its own fallback chain.
A field that cannot be resolved from a better source simply falls back to
the existing CATEGORY_PROFILES value — nothing is invented.

This module is intentionally static/curated rather than a live web-scraper:
vendor spec pages change layout and can rate-limit automated requests, so a
periodically-updated static table is the safer engineering choice for a
project at this scale. Extend the tables below as more hardware is profiled.
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import re

log = logging.getLogger(__name__)

# ── RAM voltage: JEDEC standard by generation — deterministic, no ambiguity ──
# Matched against the free-text RAM type string reported by the agent
# (e.g. "DDR5", "DDR4", "LPDDR5", "LPDDR4X").
JEDEC_RAM_VOLTAGE = {
    "LPDDR5X": 1.01,
    "LPDDR5":  1.05,
    "LPDDR4X": 1.05,
    "LPDDR4":  1.10,
    "DDR5":    1.10,
    "DDR4":    1.20,
    "DDR3L":   1.35,
    "DDR3":    1.50,
}

# ── CPU voltage: curated VID range by model-name substring ──────────────────
# Source: Intel ARK / AMD product datasheets, captured as a nominal centre
# and a half-width tolerance (used as the gbell membership width, not a
# hard threshold) rather than a single fixed number, because real CPUs
# dynamically adjust voltage with load (Vcore is not a single fixed value).
# This table is intentionally small and should be extended as specific
# CPU families are profiled — an unmatched CPU falls back to the category
# default, it is never left unresolved.
CPU_VID_TABLE = [
    # (substring to match in the reported CPU model string, nominal V, half-width V)
    (r"Core.*i[3579]-1[0-4]\d{2}[A-Z]*",  1.10, 0.35),   # Intel 10th-14th gen mobile
    (r"Core.*i[3579]-[6-9]\d{3}[A-Z]*",   1.20, 0.30),   # Intel 6th-9th gen mobile
    (r"Ryzen.*[3579] [5-8]\d{3}[A-Z]*",   1.20, 0.30),   # AMD Ryzen 5000-8000 mobile
    (r"Ryzen.*[3579] [2-4]\d{3}[A-Z]*",   1.25, 0.30),   # AMD Ryzen 2000-4000 mobile
    (r"Apple M[1-4]",                     1.05, 0.20),   # Apple Silicon (approximate)
]

# ── Fan RPM: community-profiled ranges by laptop model substring ────────────
# Source pattern: NoteBookFanControl (NBFC)-style per-model configs.
# This table is a small seed set; production use should sync against the
# NBFC config repository rather than hand-maintaining entries here.
FAN_RPM_TABLE = {
    # substring in the reported system model string -> (idle_rpm, max_rpm)
    "ThinkPad": (2200, 4200),
    "Latitude": (2000, 4000),
    "ProBook":  (2100, 4100),
    "ROG":      (2800, 5500),
    "Predator": (2800, 5500),
    "Legion":   (2600, 5200),
}


def _match_cpu_voltage(cpu_model: str):
    if not cpu_model:
        return None
    for pattern, nominal, half_width in CPU_VID_TABLE:
        if re.search(pattern, cpu_model, re.IGNORECASE):
            return {"nominal": nominal, "half_width": half_width, "source": "cpu_vid_table"}
    return None


def _match_ram_voltage(ram_type: str):
    if not ram_type:
        return None
    key = ram_type.strip().upper()
    if key in JEDEC_RAM_VOLTAGE:
        return {"nominal": JEDEC_RAM_VOLTAGE[key], "source": "jedec"}
    return None


def _match_fan_profile(system_model: str):
    if not system_model:
        return None
    for substring, (idle_rpm, max_rpm) in FAN_RPM_TABLE.items():
        if substring.lower() in system_model.lower():
            # nominal centre used by the fuzzy engine is the idle/normal RPM,
            # not the max — max is retained for future use by the onboarding
            # calibration test (forced-load RPM ceiling).
            return {"nominal": idle_rpm, "max": max_rpm, "source": "nbfc_table"}
    return None


def resolve_calibration_profile(category_defaults: dict, cpu_model: str = "",
                                 ram_type: str = "", gpu_model: str = "",
                                 system_model: str = "") -> dict:
    """
    Build a per-laptop calibration profile, overriding category_defaults
    field-by-field wherever a better, hardware-specific source is available.

    Parameters
    ----------
    category_defaults : dict — the CATEGORY_PROFILES[category] entry, used
                         as the fallback of last resort for every field.
    cpu_model, ram_type, gpu_model, system_model : str — identified from
                         WMI (server / is_local) or the agent's own LHM /
                         dmidecode lookups. Any of these may be empty if
                         not yet identified (older agent, or lookup failed).

    Returns
    -------
    dict with the same shape as a CATEGORY_PROFILES entry
    (cpu_v_nom, ram_v_nom, gpu_v_nom, fan_rpm_nom), plus a parallel
    "_sources" dict recording where each field's value actually came from,
    so the UI/report can show "resolved from JEDEC" vs "category default"
    per field rather than presenting the whole profile as equally certain.
    """
    profile = dict(category_defaults)
    sources = {
        "cpu_v_nom":   "category_default",
        "ram_v_nom":   "category_default",
        "gpu_v_nom":   "category_default",
        "fan_rpm_nom": "category_default",
    }

    ram_match = _match_ram_voltage(ram_type)
    if ram_match:
        profile["ram_v_nom"] = ram_match["nominal"]
        sources["ram_v_nom"] = ram_match["source"]

    cpu_match = _match_cpu_voltage(cpu_model)
    if cpu_match:
        profile["cpu_v_nom"] = cpu_match["nominal"]
        sources["cpu_v_nom"] = cpu_match["source"]

    fan_match = _match_fan_profile(system_model)
    if fan_match:
        profile["fan_rpm_nom"] = fan_match["nominal"]
        sources["fan_rpm_nom"] = fan_match["source"]

    # GPU voltage: no reliable independent source implemented yet for
    # integrated graphics (see report Ch. 11 discussion). Discrete-GPU
    # vendor-API reading (NVAPI/ADL) is a live-value source, not a baseline
    # source, and is handled separately in system_monitor.py, not here.
    # gpu_v_nom therefore always stays on the category default in this
    # module — this is intentional, not an oversight.

    profile["_sources"] = sources
    unresolved = [k for k, v in sources.items() if v == "category_default"]
    if unresolved:
        log.info(
            "Calibration profile: %d/%d fields resolved from hardware-specific "
            "sources; falling back to category default for: %s",
            4 - len(unresolved), 4, ", ".join(unresolved),
        )
    return profile
