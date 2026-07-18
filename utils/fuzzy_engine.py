"""
utils/fuzzy_engine.py
─────────────────────────────────────────────────────────────────────────────
Sugeno Fuzzy Inference System — Laptop Motherboard Diagnostics
Laptop-Motherboard-Diagnostics © 2026

8 Input Variables:
  X1  cpu_usage      (%)        0–100
  X2  fan_rpm        (RPM)      0–6000
  X3  cpu_temp       (°C)       0–120
  X4  cpu_voltage    (V)        0–2
  X5  ram_voltage    (V)        0–2
  X6  gpu_voltage    (V)        0–2
  X7  rail_3v3       (V)        0–5
  X8  rail_5v_mw     (metric)   0–1000

8 Fault Classes:
  VRM Failure | GPU Failure | RAM Fault | Short Circuit |
  BIOS Corrupt | Cooling Failure | PSU Issue | Healthy System

Algorithm: Generalised Bell (gbell) membership functions,
           weighted average defuzzification (Sugeno-style),
           geometric mean for Healthy baseline.

Note (Issue #10): The Healthy System rule uses a geometric mean of all
  nominal memberships. This can dominate when all inputs are near nominal.
  Tested behaviour is expected — validate against known-fault datasets and
  tune _HEALTHY_WEIGHT_SCALE if the system is biased toward healthy.
─────────────────────────────────────────────────────────────────────────────
"""

import numpy as np

# ── Constants ────────────────────────────────────────────────────────────────

CLASSES = [
    "VRM Failure",
    "GPU Failure",
    "RAM Fault",
    "Short Circuit",
    "BIOS Corrupt",
    "Cooling Failure",
    "PSU Issue",
    "Healthy System",
]

# One-hot class output vectors (Sugeno crisp outputs)
CLASS_VECTORS = {c: np.eye(len(CLASSES))[i] for i, c in enumerate(CLASSES)}

# Severity map for each fault class
SEVERITY_MAP = {
    "VRM Failure":     "CRITICAL",
    "GPU Failure":     "HIGH",
    "RAM Fault":       "MEDIUM",
    "Short Circuit":   "CRITICAL",
    "BIOS Corrupt":    "HIGH",
    "Cooling Failure": "HIGH",
    "PSU Issue":       "MEDIUM",
    "Healthy System":  "OK",
}

# Category-specific voltage tolerance multipliers
# These adjust the membership function centres per laptop class
CATEGORY_PROFILES = {
    "basic":       {"cpu_v_nom": 1.10, "ram_v_nom": 1.20, "gpu_v_nom": 0.90, "fan_rpm_nom": 2000},
    "midrange":    {"cpu_v_nom": 1.20, "ram_v_nom": 1.25, "gpu_v_nom": 1.00, "fan_rpm_nom": 2500},
    "highend":     {"cpu_v_nom": 1.25, "ram_v_nom": 1.35, "gpu_v_nom": 1.05, "fan_rpm_nom": 3000},
    "gaming":      {"cpu_v_nom": 1.30, "ram_v_nom": 1.35, "gpu_v_nom": 1.20, "fan_rpm_nom": 3500},
    "workstation": {"cpu_v_nom": 1.30, "ram_v_nom": 1.40, "gpu_v_nom": 1.15, "fan_rpm_nom": 3200},
}

# Scale factor applied to the Healthy System rule weight.
# Lower values reduce healthy-bias; 1.0 = original behaviour.
# Tune against a labelled fault dataset if healthy dominates too often.
# Reduced from 1.0 to 0.85 to prevent the Healthy rule from dominating
# when unavailable sensors fall back to nominal defaults.
# All 5 N/A sensors default to nominal → geometric mean stays high.
# 0.85 scale ensures a real fault signal can override the healthy baseline.
_HEALTHY_WEIGHT_SCALE = 0.85


# ── Membership Function ───────────────────────────────────────────────────────

def gbell(x, width, center, slope=2):
    """
    Generalised Bell membership function.
      μ(x) = 1 / (1 + |(x − center) / width|^(2·slope))
    Returns a value in [0, 1].
    """
    return 1.0 / (1.0 + abs((x - center) / (width + 1e-10)) ** (2 * slope))


# ── Confidence Categorisation ─────────────────────────────────────────────────

def categorize_confidence(conf: float) -> str:
    if conf < 0.60:
        return "Low"
    elif conf < 0.80:
        return "Medium"
    return "High"


# ── Core Sugeno Inference ─────────────────────────────────────────────────────

def get_diagnostics_sugeno(inputs: list, category: str = "midrange", debug: bool = False):
    """
    Run the Sugeno FIS over 8 hardware inputs.

    Parameters
    ----------
    inputs   : list of 8 floats  [X1…X8]
               Expected to already be validated/clamped by the caller.
    category : laptop category key (basic | midrange | highend | gaming | workstation)
    debug    : if True, prints rule contributions to stdout

    Returns
    -------
    primary_class   : str    — top fault classification
    secondary_class : str    — second-ranked fault
    confidence      : float  — top1 / (top1+top2)
    rule_weights    : list   — 8 raw firing strengths (floats)
    """
    if len(inputs) != 8:
        raise ValueError(f"Expected 8 inputs, got {len(inputs)}")

    X1, X2, X3, X4, X5, X6, X7, X8 = [float(v) for v in inputs]

    # Retrieve category profile (fall back to midrange if unknown)
    prof = CATEGORY_PROFILES.get(category, CATEGORY_PROFILES["midrange"])
    cpu_v_nom   = prof["cpu_v_nom"]
    ram_v_nom   = prof["ram_v_nom"]
    gpu_v_nom   = prof["gpu_v_nom"]
    fan_rpm_nom = prof["fan_rpm_nom"]

    # ── FUZZIFICATION ──────────────────────────────────────────────────────────

    # X1: CPU Usage (%)
    m1_h = gbell(X1, 20, 80)     # high
    m1_n = gbell(X1, 20, 50)     # normal

    # X2: Fan RPM  — adapted per category
    m2_l = gbell(X2, 1000, 1000)              # low
    m2_n = gbell(X2, 1000, fan_rpm_nom)       # normal

    # X3: CPU Temperature (°C)
    m3_h = gbell(X3, 30, 105)    # high
    m3_l = gbell(X3, 30, 15)     # low (cold / stalled)
    m3_n = gbell(X3, 30, 60)     # normal

    # X4: CPU Voltage (V)  — adapted per category
    m4_l = gbell(X4, 0.4, cpu_v_nom - 0.6)   # low
    m4_n = gbell(X4, 0.4, cpu_v_nom)          # normal

    # X5: RAM Voltage (V)  — adapted per category
    m5_l = gbell(X5, 0.4, ram_v_nom - 0.6)   # low
    m5_n = gbell(X5, 0.4, ram_v_nom)          # normal

    # X6: GPU Voltage (V)  — adapted per category
    m6_l = gbell(X6, 0.3, gpu_v_nom - 0.45)  # low
    m6_n = gbell(X6, 0.3, gpu_v_nom)          # normal

    # X7: +3.3V Rail (V)
    m7_l = gbell(X7, 0.4, 2.7)   # low (drooping rail)
    m7_n = gbell(X7, 0.4, 3.3)   # nominal

    # X8: +5V Rail Metric (mW proxy)
    m8_l = gbell(X8, 200, 200)   # low
    m8_n = gbell(X8, 200, 500)   # normal

    # ── RULE EVALUATION ────────────────────────────────────────────────────────

    w = np.zeros(8, dtype=float)

    # Rule 0: VRM Failure  — high CPU load + low CPU voltage + high temp
    w[0] = m1_h * m4_l * m3_h

    # Rule 1: GPU Failure  — low GPU voltage + high temperature
    w[1] = m6_l * m3_h

    # Rule 2: RAM Fault    — low RAM voltage + normal temperature
    w[2] = m5_l * m3_n

    # Rule 3: Short Circuit — low 5V rail + low 3.3V rail + cold board
    # Product (AND) ensures ALL three conditions must be present together.
    # The old weighted sum fired too easily from any single drooping rail.
    w[3] = m8_l * m7_l * m3_l

    # Rule 4: BIOS Corrupt  — nominal 3.3V but very cold (stalled at POST)
    w[4] = m7_n * m3_l

    # Rule 5: Cooling Failure — dual-path evidence:
    #   Path A: low fan RPM + high CPU load  (direct — needs fan sensor)
    #   Path B: critically high temp + high CPU load x 0.7
    #           (fallback when fan sensor not exposed — HP/Dell/most laptops)
    w[5] = max(m2_l * m1_h,
               m3_h * m1_h * 0.7)

    # Rule 6: PSU Issue    — low 3.3V rail + normal temperature
    w[6] = m7_l * m3_n

    # Rule 7: Healthy System — geometric mean of all nominal memberships
    # _HEALTHY_WEIGHT_SCALE can be reduced (e.g. 0.8) if this rule
    # dominates too often when tested against known fault datasets.
    healthy_memberships = np.array(
        [m1_n, m2_n, m3_n, m4_n, m5_n, m6_n, m7_n, m8_n], dtype=float
    )
    w[7] = float(
        np.prod(healthy_memberships) ** (1.0 / len(healthy_memberships))
    ) * _HEALTHY_WEIGHT_SCALE

    if debug:
        print("\n--- Rule Firing Strengths ---")
        for i, val in enumerate(w):
            print(f"  {CLASSES[i]:<18}: {val:.4f}")

    # ── AGGREGATION & DEFUZZIFICATION ──────────────────────────────────────────

    sum_w = float(np.sum(w))
    if sum_w == 0.0:
        return "Healthy System", "None", 0.0, w.tolist()

    z = np.array(list(CLASS_VECTORS.values()), dtype=float)       # (8, 8)
    final_vector = np.dot(w, z) / sum_w                           # (8,)

    # ── DECISION ───────────────────────────────────────────────────────────────

    top2_idx = np.argsort(final_vector)[-2:][::-1]
    primary_class   = CLASSES[top2_idx[0]]
    secondary_class = CLASSES[top2_idx[1]]

    top1 = float(final_vector[top2_idx[0]])
    top2 = float(final_vector[top2_idx[1]])

    confidence = top1 / (top1 + top2 + 1e-10)

    return primary_class, secondary_class, confidence, w.tolist()
