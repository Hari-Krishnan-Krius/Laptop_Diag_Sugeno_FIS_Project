#!/usr/bin/env python3
"""
laptop_agent.py  —  Laptop Diagnostics Self-Registering Agent
═══════════════════════════════════════════════════════════════════════════════
REQUIREMENTS (on the remote machine):
    pip install psutil requests pythonnet

SETUP — keep ALL of the following in the SAME folder as this script:
    laptop_agent.py                  ← this file
    LibreHardwareMonitorLib.dll      ← main sensor DLL
    Aga.Controls.dll
    BlackSharp.Core.dll
    DiskInfoToolkit.dll
    HidSharp.dll
    Microsoft.Bcl.AsyncInterfaces.dll
    Microsoft.Bcl.HashCode.dll
    Microsoft.Win32.TaskScheduler.dll
    OxyPlot.dll
    OxyPlot.WindowsForms.dll
    RAMSPDToolkit-NDD.dll
    System.Buffers.dll
    System.CodeDom.dll
    System.Collections.Immutable.dll
    System.Formats.Nrbf.dll
    System.IO.Pipelines.dll
    System.Memory.dll
    System.Numerics.Vectors.dll
    System.Reflection.Metadata.dll
    System.Resources.Extensions.dll
    System.Runtime.CompilerServices.Unsafe.dll
    System.Security.AccessControl.dll
    System.Security.Principal.Windows.dll
    System.Text.Encodings.Web.dll
    System.Text.Json.dll
    System.Threading.AccessControl.dll
    System.Threading.Tasks.Extensions.dll

NO LibreHardwareMonitor application needed.
NO admin rights needed (DLL loads in-process as a normal user).
NO WMI dependency for sensors.

CONFIGURATION — edit the two lines below (SERVER_URL and API_KEY):

USAGE:
    python laptop_agent.py              # run forever, report every 10 min
    python laptop_agent.py --test       # print sensor values, do not send
    python laptop_agent.py --once       # register + one report, then exit
    python laptop_agent.py --reset      # clear saved ID, force re-register
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# ── CONFIGURATION — edit these two lines ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
SERVER_URL     = os.environ.get("DIAG_SERVER_URL",  "https://devoutly-numerous-delicacy.ngrok-free.dev").rstrip("/")
API_KEY        = os.environ.get("DIAG_API_KEY",     "11fb98a09e5d8a3617e454854221e505e96ddfd0e684e0e00a0f412834753329")
INTERVAL_SECS  = int(os.environ.get("DIAG_INTERVAL", "600"))   # 600 = 10 minutes
CATEGORY       = os.environ.get("DIAG_CATEGORY",    "midrange")
ALERT_EMAIL    = os.environ.get("DIAG_EMAIL",        "hari.ai09.reva@gmail.com")
DISPLAY_NAME   = os.environ.get("DIAG_NAME",         "")
TIMEOUT_SECS   = int(os.environ.get("DIAG_TIMEOUT",  "15"))
RETRY_COUNT    = 3
RETRY_DELAY    = 10

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / ".agent_state.json"
LOG_FILE   = SCRIPT_DIR / "laptop_agent.log"
_PLATFORM  = platform.system()

# ── All DLL files that must live next to this script ─────────────────────────
# LibreHardwareMonitorLib.dll depends on all of these at runtime.
# They must all be in SCRIPT_DIR — pythonnet resolves them from sys.path.
REQUIRED_DLLS = [
    "LibreHardwareMonitorLib.dll",          # ← the sensor engine
    "Aga.Controls.dll",
    "BlackSharp.Core.dll",
    "DiskInfoToolkit.dll",
    "HidSharp.dll",
    "Microsoft.Bcl.AsyncInterfaces.dll",
    "Microsoft.Bcl.HashCode.dll",
    "Microsoft.Win32.TaskScheduler.dll",
    "OxyPlot.dll",
    "OxyPlot.WindowsForms.dll",
    "RAMSPDToolkit-NDD.dll",
    "System.Buffers.dll",
    "System.CodeDom.dll",
    "System.Collections.Immutable.dll",
    "System.Formats.Nrbf.dll",
    "System.IO.Pipelines.dll",
    "System.Memory.dll",
    "System.Numerics.Vectors.dll",
    "System.Reflection.Metadata.dll",
    "System.Resources.Extensions.dll",
    "System.Runtime.CompilerServices.Unsafe.dll",
    "System.Security.AccessControl.dll",
    "System.Security.Principal.Windows.dll",
    "System.Text.Encodings.Web.dll",
    "System.Text.Json.dll",
    "System.Threading.AccessControl.dll",
    "System.Threading.Tasks.Extensions.dll",
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger("agent")

# ── LHM state (module-level cache) ────────────────────────────────────────────
_lhm_computer = None
_lhm_failed   = False


# ═══════════════════════════════════════════════════════════════════════════════
# ── DLL setup ──────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _check_dlls() -> tuple:
    """
    Returns (lhm_path, missing_list).
    lhm_path is the Path to LibreHardwareMonitorLib.dll if found, else None.
    missing_list contains names of any DLLs not present in SCRIPT_DIR.
    """
    lhm_path = None
    missing  = []
    for name in REQUIRED_DLLS:
        p = SCRIPT_DIR / name
        if p.exists():
            if name == "LibreHardwareMonitorLib.dll":
                lhm_path = p
        else:
            missing.append(name)
    return lhm_path, missing




def _setup_dll_path() -> bool:
    """
    Prepare the environment so pythonnet can load LibreHardwareMonitorLib.dll:
      1. Insert SCRIPT_DIR into sys.path for assembly resolution
      2. Add SCRIPT_DIR to Windows DLL search path (Python 3.8+)

    Returns True if LibreHardwareMonitorLib.dll is present.
    """
    script_str = str(SCRIPT_DIR)

    # 1. Add to Python path so clr.AddReference finds the assembly
    if script_str not in sys.path:
        sys.path.insert(0, script_str)

    # 2. Add to Windows DLL search path (Python 3.8+, Windows only)
    if _PLATFORM == "Windows":
        try:
            os.add_dll_directory(script_str)
        except (AttributeError, OSError):
            pass  # Python < 3.8 or non-Windows — sys.path is enough

    lhm, missing = _check_dlls()
    if missing:
        if "LibreHardwareMonitorLib.dll" in missing:
            log.warning("LibreHardwareMonitorLib.dll not found in %s", SCRIPT_DIR)
            log.warning("Copy all DLL files next to laptop_agent.py for full sensor support")
        else:
            log.debug("Optional DLLs missing (non-critical): %s", ", ".join(missing))
    return lhm is not None


# ═══════════════════════════════════════════════════════════════════════════════
# ── Machine identity ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _get_mac() -> str:
    try:
        mac = uuid.getnode()
        if (mac >> 40) % 2 == 0:
            return ":".join(f"{(mac >> (8*i)) & 0xff:02x}" for i in range(5, -1, -1))
    except Exception:
        pass
    return hashlib.sha1(platform.node().encode()).hexdigest()[:17]


def _machine_id() -> str:
    raw = f"{_get_mac()}:{platform.node()}".encode()
    return hashlib.sha1(raw).hexdigest()


def _display_name() -> str:
    if DISPLAY_NAME:
        return DISPLAY_NAME
    name = platform.node() or socket.gethostname() or "Unknown-Laptop"
    return name.strip() or "Unknown-Laptop"


def _run_powershell_cim(class_name: str, property_name: str) -> str:
    """
    Shared helper: query a single CIM property via PowerShell's
    Get-CimInstance. Used instead of wmic.exe, which has been removed
    entirely on recent Windows 11 builds (confirmed directly during
    development — wmic returned "not recognized" on a real test machine).
    Get-CimInstance ships with PowerShell on every supported Windows
    version, so this needs no extra dependency and no fallback binary.
    """
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance -ClassName {class_name} | "
             f"Select-Object -First 1 -ExpandProperty {property_name})"],
            timeout=8, stderr=subprocess.DEVNULL
        ).decode(errors="ignore").strip()
        return out
    except Exception:
        return ""


def _laptop_model() -> str:
    if _PLATFORM == "Windows":
        # Try the legacy wmic.exe path first (still present on older
        # Windows 10 installs); fall through to PowerShell if unavailable —
        # wmic.exe was removed entirely starting with recent Windows 11
        # builds, so this cannot be the only method.
        try:
            out = subprocess.check_output(
                ["wmic", "computersystem", "get", "model", "/value"],
                timeout=5, stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            for line in out.splitlines():
                if line.lower().startswith("model="):
                    val = line.split("=", 1)[1].strip()
                    if val and "System Product" not in val:
                        return val
        except Exception:
            pass
        val = _run_powershell_cim("Win32_ComputerSystem", "Model")
        if val and "System Product" not in val:
            return val
        log.warning("Laptop model lookup failed via both wmic and Get-CimInstance "
                    "— using a generic placeholder name instead")
    elif _PLATFORM == "Linux":
        for p in ("/sys/class/dmi/id/product_name", "/sys/class/dmi/id/board_name"):
            try:
                val = Path(p).read_text().strip()
                if val and val not in ("None", "To be filled by O.E.M."):
                    return val
            except Exception:
                pass
    return f"{_PLATFORM} Machine"


def _ram_type() -> str:
    """
    RAM generation (DDR4/DDR5/etc.) for per-laptop calibration
    (see utils/hardware_specs.py). Returns "" if not resolvable —
    server falls back to the category default in that case, same as
    if this field never existed.
    """
    type_map = {"20": "DDR", "21": "DDR2", "24": "DDR3", "26": "DDR4", "34": "DDR5"}
    if _PLATFORM == "Windows":
        try:
            out = subprocess.check_output(
                ["wmic", "memorychip", "get", "SMBIOSMemoryType", "/value"],
                timeout=5, stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            for line in out.splitlines():
                if line.lower().startswith("smbiosmemorytype="):
                    code = line.split("=", 1)[1].strip()
                    if code in type_map:
                        return type_map[code]
        except Exception:
            pass
        code = _run_powershell_cim("Win32_PhysicalMemory", "SMBIOSMemoryType")
        if code in type_map:
            return type_map[code]
        log.warning("RAM type lookup failed via both wmic and Get-CimInstance "
                    "(got code '%s') — calibration will use the category default "
                    "for ram_v_nom instead of the real JEDEC value", code)
    elif _PLATFORM == "Linux":
        try:
            out = subprocess.check_output(
                ["dmidecode", "--type", "17"], timeout=5, stderr=subprocess.DEVNULL
            ).decode(errors="ignore")
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Type:") and "Unknown" not in line:
                    val = line.split(":", 1)[1].strip()
                    if val and val != "DRAM":
                        return val
        except Exception:
            # dmidecode usually requires root — silently unavailable is expected
            pass
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# ── Sensor collection ─────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def collect_metrics() -> dict:
    try:
        import psutil
    except ImportError:
        log.error("psutil not installed — run:  pip install psutil requests pythonnet")
        sys.exit(1)

    # Single blocking call — most accurate reading.
    # interval=1.0 measures actual usage over 1 second.
    # The old two-call pattern (prime + interval=None) returns 0.0 on the
    # second call because the first call already consumed the counter delta.
    cpu_usage = round(psutil.cpu_percent(interval=1.0), 2)
    vm = psutil.virtual_memory()

    disk_path = "C:\\" if _PLATFORM == "Windows" else "/"
    try:
        disk_pct = round(psutil.disk_usage(disk_path).percent, 2)
    except Exception:
        disk_pct = 0.0

    base = {
        "cpu_usage":    cpu_usage,
        "ram_percent":  round(vm.percent, 2),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "ram_used_gb":  round(vm.used  / (1024 ** 3), 2),
        "disk_percent": disk_pct,
        "platform":     _PLATFORM,
        "hostname":     platform.node(),
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }

    # Hardware sensors — None means "not available on this hardware"
    hw = {
        "cpu_temp":    None,
        "fan_rpm":     None,
        "cpu_voltage": None,
        "ram_voltage": None,
        "gpu_voltage": None,
        "rail_3v3":    None,
        "rail_5v_mw":  None,
        # Hardware identity for per-laptop calibration (see
        # utils/hardware_specs.py on the server). "" means not identified —
        # server falls back to the category default for that field, same
        # as before this feature existed. Populated by _try_lhm_dll() on
        # Windows (cpu_model/gpu_model) and _ram_type() below (all platforms).
        "cpu_model":   "",
        "ram_type":    "",
        "gpu_model":   "",
    }

    if _PLATFORM == "Windows":
        _collect_windows(hw, psutil)
    elif _PLATFORM == "Linux":
        _collect_linux(hw, psutil)
    elif _PLATFORM == "Darwin":
        _collect_macos(hw, psutil)

    # RAM type: independent of the LHM/ACPI sensor chain above, resolved
    # the same way regardless of which sensor method succeeded.
    if not hw.get("ram_type"):
        hw["ram_type"] = _ram_type()

    # CPU model on Linux: LHM isn't available there, so read /proc/cpuinfo
    # directly. On Windows, cpu_model is already populated by _try_lhm_dll()
    # above when the DLL loads successfully.
    if _PLATFORM == "Linux" and not hw.get("cpu_model"):
        try:
            text = Path("/proc/cpuinfo").read_text()
            for line in text.splitlines():
                if line.lower().startswith("model name"):
                    hw["cpu_model"] = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

    base.update(hw)
    return base



# ── Windows ──────────────────────────────────────────────────────────────────

def _collect_windows(result: dict, psutil) -> None:
    """
    Three-method fallback chain for Windows sensors:
      1. LibreHardwareMonitorLib.dll loaded directly via pythonnet (best)
      2. Native ACPI WMI for temperature only (no extra packages beyond wmi)
      3. PowerShell subprocess for temperature (no Python packages at all)
    """
    # Ensure DLL folder is in the search path before attempting to load
    _setup_dll_path()

    if _try_lhm_dll(result):
        return   # DLL succeeded — got everything available on this hardware

    # Temperature fallback chain
    if result["cpu_temp"] is None:
        _try_acpi_wmi(result)
    if result["cpu_temp"] is None:
        _try_powershell_temp(result)
    if result["cpu_temp"] is None:
        _try_psutil_temp(result, psutil)


def _try_lhm_dll(result: dict) -> bool:
    """
    Load LibreHardwareMonitorLib.dll directly using pythonnet.

    Key details:
    - SCRIPT_DIR must already be in sys.path (done by _setup_dll_path)
    - All dependency DLLs must be in SCRIPT_DIR
    - No LibreHardwareMonitor application needed
    - No admin rights needed for most sensors
    - AMD Ryzen requires two Update() passes (first pass returns 0.0)
    - Computer object is cached in _lhm_computer for efficiency

    Returns True if at least one sensor value was obtained.
    """
    global _lhm_computer, _lhm_failed

    if _lhm_failed:
        return False

    lhm_path, _ = _check_dlls()
    if lhm_path is None:
        return False

    try:
        import clr  # pythonnet
    except ImportError:
        log.warning(
            "pythonnet not installed — DLL sensor reading unavailable.\n"
            "  Fix: pip install pythonnet"
        )
        _lhm_failed = True
        return False

    try:
        if _lhm_computer is None:
            # Load the assembly using the full absolute path.
            # Using the resolved absolute path avoids the network-location
            # sandboxing error (HRESULT 0x80131515) on some Windows setups.
            try:
                import clr as _clr_inner
                # Method 1: full path string (most reliable)
                _clr_inner.AddReference(str(lhm_path.resolve()))
            except Exception:
                try:
                    # Method 2: load via System.Reflection directly
                    import System.Reflection as SR
                    SR.Assembly.LoadFrom(str(lhm_path.resolve()))
                except Exception:
                    # Method 3: stem name only
                    clr.AddReference("LibreHardwareMonitorLib")

            from LibreHardwareMonitor.Hardware import Computer, SensorType

            computer = Computer()
            computer.IsCpuEnabled         = True
            computer.IsGpuEnabled         = True
            computer.IsMemoryEnabled      = True
            computer.IsMotherboardEnabled = True
            computer.IsBatteryEnabled     = False   # not needed for diagnostics
            computer.IsStorageEnabled     = False   # not needed for diagnostics
            computer.Open()

            # AMD Ryzen SMU sensors return 0.0 on first Update().
            # Two passes with a short sleep fixes this.
            for hw in computer.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
            time.sleep(0.35)
            for hw in computer.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()

            _lhm_computer = computer
            log.info("LibreHardwareMonitorLib.dll loaded — embedded sensor reading active")

        else:
            # Subsequent calls: just update values
            for hw in _lhm_computer.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()

        # Parse all sensor values
        temp_candidates = []
        fan_candidates  = []

        for hw in _lhm_computer.Hardware:
            _parse_sensors(hw.Sensors, result, temp_candidates, fan_candidates)
            for sub in hw.SubHardware:
                _parse_sensors(sub.Sensors, result, temp_candidates, fan_candidates)

        # Capture CPU/GPU model names off the same already-open Hardware
        # enumeration — no extra WMI call needed. Used server-side for
        # per-laptop calibration (see utils/hardware_specs.py); harmless
        # extra fields for an older server that doesn't read them yet.
        for hw in _lhm_computer.Hardware:
            ht = str(hw.HardwareType)
            if ht == "Cpu" and not result.get("cpu_model"):
                result["cpu_model"] = str(hw.Name)
            elif ht in ("GpuNvidia", "GpuAmd", "GpuIntel") and not result.get("gpu_model"):
                result["gpu_model"] = str(hw.Name)

        # Select best temperature reading
        # Priority 1 = Tdie/Tctl/Package (most accurate)
        # Priority 2 = CPU-level
        # Priority 3 = individual Core readings
        if temp_candidates:
            temp_candidates.sort(key=lambda x: x[0])
            best_priority = temp_candidates[0][0]
            candidates_at_best = [v for p, v in temp_candidates if p == best_priority]
            chosen = round(max(candidates_at_best), 1)
            if 20.0 <= chosen <= 120.0:
                result["cpu_temp"] = chosen

        # Select best fan reading (prefer CPU fan over chassis fans)
        if fan_candidates and result["fan_rpm"] is None:
            fan_candidates.sort(key=lambda x: x[0])
            result["fan_rpm"] = round(fan_candidates[0][1], 0)

        got_something = (
            result["cpu_temp"]    is not None or
            result["cpu_voltage"] is not None
        )
        return got_something

    except Exception as exc:
        log.warning("DLL sensor error: %s — falling back to ACPI", exc)
        _lhm_failed = True
        _lhm_computer = None
        return False


def _parse_sensors(sensors, result: dict, temps: list, fans: list) -> None:
    """
    Parse a LibreHardwareMonitor ISensor[] array.
    Handles both AMD (SVI2 TFN, Tdie, Tctl) and Intel (VCore, Package) naming.
    Updates result dict in-place; appends to temps and fans lists.
    """
    try:
        from LibreHardwareMonitor.Hardware import SensorType as ST
    except Exception:
        return

    for sensor in sensors:
        try:
            name  = str(sensor.Name).lower().strip()
            stype = sensor.SensorType
            raw   = sensor.Value
            if raw is None:
                continue
            val = float(raw)

            # ── Temperature ─────────────────────────────────────────────────
            if stype == ST.Temperature:
                if val <= 0.5:
                    continue   # AMD SMU not ready or genuinely zero — skip
                if any(k in name for k in ("tdie", "tctl", "package", "cpu die")):
                    temps.append((1, val))   # highest priority
                elif name.startswith("cpu") or name == "cpu":
                    temps.append((2, val))
                elif "core" in name:
                    temps.append((3, val))   # lowest priority — use if nothing better

            # ── Fan ─────────────────────────────────────────────────────────
            elif stype == ST.Fan and val > 0:
                priority = 1 if "cpu" in name else 2
                fans.append((priority, val))

            # ── Voltage ─────────────────────────────────────────────────────
            elif stype == ST.Voltage and val > 0:

                # CPU core voltage
                # AMD: "SVI2 TFN", "Core #N VID"
                # Intel: "CPU Core", "VCore"
                if (any(k in name for k in (
                        "svi2 tfn", "svi2", "vcore", "cpu core",
                        "core #0 vid", "core #1 vid", "core #2 vid",
                        "core #3 vid", "core vid"))
                        and "soc" not in name):
                    v = round(val, 3)
                    if 0.5 <= v <= 2.0 and result["cpu_voltage"] is None:
                        result["cpu_voltage"] = v

                # GPU core voltage
                elif any(k in name for k in ("gfx", "gpu core", "vgpu")):
                    if val >= 0.1:
                        result["gpu_voltage"] = round(val, 3)

                # RAM / DIMM voltage
                elif any(k in name for k in ("dimm", "ram", "dram", "memory", "vddio")):
                    if 0.8 <= val <= 2.5:
                        result["ram_voltage"] = round(val, 3)

                # +3.3V rail
                elif any(k in name for k in ("3.3", "3v3")):
                    result["rail_3v3"] = round(val, 3)

                # +5V rail (stored as 0–1000 proxy metric: actual_volts × 100)
                elif any(k in name for k in ("5v", "+5")):
                    result["rail_5v_mw"] = min(round(val * 100.0, 1), 1000.0)

        except Exception:
            continue   # never crash on a single bad sensor


def _try_acpi_wmi(result: dict) -> None:
    """
    Read CPU temperature from native Windows ACPI via WMI.
    Requires the 'wmi' Python package (pip install wmi).
    Falls through silently if not available.
    """
    try:
        import wmi
        zones = wmi.WMI(namespace=r"root\WMI").MSAcpi_ThermalZoneTemperature()
        if zones:
            readings = [
                float(z.CurrentTemperature) for z in zones
                if z.CurrentTemperature is not None
            ]
            if readings:
                celsius = round((max(readings) / 10.0) - 273.15, 1)
                if 20.0 <= celsius <= 120.0:
                    result["cpu_temp"] = celsius
    except Exception:
        pass


def _try_powershell_temp(result: dict) -> None:
    """
    Read CPU temperature via PowerShell — no Python packages needed.
    PowerShell is built into every Windows machine since Vista.
    """
    try:
        ps_cmd = (
            "Get-WmiObject -Namespace root/WMI "
            "-Class MSAcpi_ThermalZoneTemperature | "
            "Measure-Object -Property CurrentTemperature -Maximum | "
            "Select-Object -ExpandProperty Maximum"
        )
        out = subprocess.check_output(
            ["powershell", "-NonInteractive", "-Command", ps_cmd],
            timeout=6, stderr=subprocess.DEVNULL, text=True
        ).strip()
        if out:
            raw_val = float(out.split()[-1])
            celsius = round((raw_val / 10.0) - 273.15, 1)
            if 20.0 <= celsius <= 120.0:
                result["cpu_temp"] = celsius
    except Exception:
        pass


def _try_psutil_temp(result: dict, psutil) -> None:
    """
    Last-resort temperature via psutil.
    Usually requires admin on Windows but included as final fallback.
    """
    try:
        temps = psutil.sensors_temperatures() or {}
        for key, entries in temps.items():
            for e in entries:
                lbl = (e.label or key).lower()
                if any(k in lbl for k in ("package", "core", "cpu", "tdie")):
                    if e.current and e.current > 0:
                        result["cpu_temp"] = round(float(e.current), 1)
                        return
    except Exception:
        pass


# ── Linux ────────────────────────────────────────────────────────────────────

def _collect_linux(result: dict, psutil) -> None:
    # Temperature
    try:
        temps = psutil.sensors_temperatures() or {}
        for key, entries in temps.items():
            for e in entries:
                if any(t in (e.label or key).lower() for t in ("core", "cpu", "package", "tdie")):
                    if e.current and e.current > 0:
                        result["cpu_temp"] = float(e.current)
                        break
    except Exception:
        pass

    # Fan
    try:
        fans = psutil.sensors_fans() or {}
        for key, entries in fans.items():
            if entries and entries[0].current > 0:
                result["fan_rpm"] = float(entries[0].current)
                break
    except Exception:
        pass

    # CPU voltage via lm-sensors (sensors -j)
    try:
        raw  = subprocess.check_output(
            ["sensors", "-j"], timeout=3, stderr=subprocess.DEVNULL
        )
        data = json.loads(raw)
        for chip, features in data.items():
            if not isinstance(features, dict):
                continue
            for feat, sub in features.items():
                if not isinstance(sub, dict):
                    continue
                if any(k in feat.lower() for k in ("vcore", "in0")):
                    for sk, v in sub.items():
                        if "input" in sk and isinstance(v, (int, float)) and 0.5 <= v <= 2.0:
                            result["cpu_voltage"] = round(float(v), 3)
    except Exception:
        pass


# ── macOS ────────────────────────────────────────────────────────────────────

def _collect_macos(result: dict, psutil) -> None:
    try:
        temps = psutil.sensors_temperatures() or {}
        for key, entries in temps.items():
            for e in entries:
                if any(t in (e.label or key).lower() for t in ("cpu", "core", "tc0p")):
                    if e.current and e.current > 0:
                        result["cpu_temp"] = float(e.current)
                        return
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["osx-cpu-temp"], timeout=3, stderr=subprocess.DEVNULL
        ).decode()
        result["cpu_temp"] = float(out.replace("°C", "").strip())
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# ── Local state (saved registration) ─────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("Could not save state: %s", exc)


def _clear_state() -> None:
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# ── HTTP helpers ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def _post(endpoint: str, body: dict):
    """POST JSON to the server with retry logic."""
    try:
        import requests
    except ImportError:
        log.error("requests not installed — run:  pip install psutil requests pythonnet")
        sys.exit(1)

    url     = SERVER_URL + endpoint
    headers = {"Content-Type": "application/json", "X-Agent-Key": API_KEY, "ngrok-skip-browser-warning": "true"}

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.post(
                url, json=body, headers=headers, timeout=TIMEOUT_SECS
            )
            if resp.status_code in (200, 201):
                return resp.json()
            elif resp.status_code == 401:
                log.error("API key rejected (401) — check DIAG_API_KEY in this file")
                return None
            elif resp.status_code == 404:
                log.warning("404 from server — stale ID, will re-register")
                _clear_state()
                return None
            else:
                log.warning("Attempt %d/%d: HTTP %d", attempt, RETRY_COUNT, resp.status_code)
        except Exception as exc:
            log.warning("Attempt %d/%d failed: %s", attempt, RETRY_COUNT, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ── Registration ──────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def register_or_load() -> str:
    """
    Load saved laptop_id if server URL matches.
    If not saved, or URL changed, or state cleared by 404 — register fresh.
    """
    state = _load_state()

    if state.get("laptop_id") and state.get("server_url") == SERVER_URL:
        log.info("Using saved ID: %s  (%s)", state["laptop_id"], state.get("name", "?"))
        return state["laptop_id"]

    # Register fresh
    name = _display_name()
    log.info("Registering '%s' with %s ...", name, SERVER_URL)

    payload = {
        "machine_id":       _machine_id(),
        "name":             name,
        "model":            _laptop_model(),
        "cpu_model":        "",   # best-effort only; full identification happens on
        "ram_type":         _ram_type(),   # the first report once LHM/sensors are warmed up
        "gpu_model":        "",
        "category":         CATEGORY,
        "email":            ALERT_EMAIL,
        "platform":         _PLATFORM,
        "hostname":         platform.node(),
        "polling_interval": INTERVAL_SECS,
    }

    result = _post("/api/agent/register", payload)
    if not result:
        log.error(
            "Registration failed.\n"
            "  Check 1: SERVER_URL is reachable: %s\n"
            "  Check 2: API_KEY matches AGENT_API_KEY in server .env",
            SERVER_URL
        )
        sys.exit(1)

    lid    = result["laptop_id"]
    rname  = result.get("name", name)
    action = "Re-connected to" if result.get("existing") else "Registered as"
    log.info("%s '%s'  (ID: %s)", action, rname, lid)

    _save_state({
        "laptop_id":  lid,
        "name":       rname,
        "server_url": SERVER_URL,
        "registered": datetime.now(timezone.utc).isoformat(),
    })
    return lid


# ═══════════════════════════════════════════════════════════════════════════════
# ── Report ────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def check_pending(laptop_id: str) -> bool:
    """
    Ask the server if the dashboard has requested an immediate report.
    Called every 10 seconds while the agent is idle between intervals.
    Returns True if the server wants a fresh report right now.
    """
    try:
        import requests as _req
        url     = SERVER_URL + "/api/agent/check-pending"
        headers = {"Content-Type": "application/json", "X-Agent-Key": API_KEY, "ngrok-skip-browser-warning": "true"}
        resp = _req.post(url, json={"laptop_id": laptop_id},
                         headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("report_now", False)
    except Exception:
        pass  # network hiccup — not critical, just skip this check
    return False


def send_report(laptop_id: str, metrics: dict) -> bool:
    result = _post("/api/agent/report", {"laptop_id": laptop_id, "metrics": metrics})
    if result:
        log.info(
            "Diagnosis: %-22s | Severity: %-8s | Confidence: %.1f%%",
            result.get("diagnosis", "?"),
            result.get("severity",  "?"),
            result.get("confidence", 0) * 100,
        )
        if result.get("notified"):
            log.info("Alert email sent")
        return True
    log.warning("Report failed — will retry next interval")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# ── Entry point ───────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Laptop Diagnostics Agent — reads sensors via DLL, reports to server"
    )
    parser.add_argument("--test",  action="store_true",
                        help="Print all sensor values without sending to server")
    parser.add_argument("--once",  action="store_true",
                        help="Register + one report, then exit")
    parser.add_argument("--reset", action="store_true",
                        help="Delete saved ID and force re-registration")
    args = parser.parse_args()

    # ── --reset ──────────────────────────────────────────────────────────────
    if args.reset:
        _clear_state()
        print("Saved state cleared. Agent will re-register on next run.")
        return

    # ── --test ───────────────────────────────────────────────────────────────
    if args.test:
        _setup_dll_path()
        print("\n" + "=" * 60)
        print("  Sensor Test Mode (no data sent to server)")
        print("=" * 60)

        lhm_path, missing = _check_dlls()
        if lhm_path:
            print(f"\n  DLL:  FOUND  {lhm_path}")
        else:
            print(f"\n  DLL:  NOT FOUND in {SCRIPT_DIR}")
            print("  Copy LibreHardwareMonitorLib.dll and all dependency DLLs here.")

        if missing:
            crit = [m for m in missing if m == "LibreHardwareMonitorLib.dll"]
            deps = [m for m in missing if m != "LibreHardwareMonitorLib.dll"]
            if crit:
                print(f"\n  MISSING (critical): {', '.join(crit)}")
            if deps:
                print(f"  MISSING (dependency DLLs): {len(deps)} files")
                for d in deps:
                    print(f"    - {d}")

        print("\n  Collecting metrics...")
        metrics = collect_metrics()

        print("\n  System:")
        system_keys = ["cpu_usage", "ram_percent", "ram_total_gb", "ram_used_gb", "disk_percent", "hostname", "platform"]
        for k in system_keys:
            print(f"    {k:<22}: {metrics.get(k)}")

        print("\n  Hardware Sensors:")
        sensor_keys = ["cpu_temp", "fan_rpm", "cpu_voltage", "ram_voltage", "gpu_voltage", "rail_3v3", "rail_5v_mw"]
        for k in sensor_keys:
            v = metrics.get(k)
            status = "  <-- not available on this hardware" if v is None else ""
            print(f"    {k:<22}: {v}{status}")

        print("\n  Server: " + SERVER_URL)
        print("=" * 60 + "\n")
        return

    # ── Normal / --once modes ─────────────────────────────────────────────────
    if not SERVER_URL.startswith("http"):
        log.error(
            "SERVER_URL not set correctly.\n"
            "  Edit laptop_agent.py — set SERVER_URL at the top of the file\n"
            "  e.g. SERVER_URL = 'http://192.168.1.9:5000'"
        )
        sys.exit(1)

    if not API_KEY:
        log.error(
            "API_KEY not set.\n"
            "  Edit laptop_agent.py — set API_KEY to match AGENT_API_KEY in server .env"
        )
        sys.exit(1)

    # Startup banner
    _setup_dll_path()
    lhm_path, missing = _check_dlls()
    log.info("=" * 55)
    log.info("  Laptop Diagnostics Agent")
    log.info("  Server   : %s", SERVER_URL)
    log.info("  Hostname : %s", platform.node())
    log.info("  Model    : %s", _laptop_model())
    log.info("  Interval : %d seconds (%d min)", INTERVAL_SECS, INTERVAL_SECS // 60)
    if lhm_path:
        log.info("  DLL      : FOUND — full sensor reading enabled")
        if missing:
            log.info("  DLL deps : %d dependency DLLs missing (may affect sensors)", len(missing))
    else:
        log.info("  DLL      : NOT FOUND — temperature via ACPI only")
        log.info("             Copy all DLL files next to laptop_agent.py")
    log.info("=" * 55)

    laptop_id = register_or_load()

    if args.once:
        metrics = collect_metrics()
        send_report(laptop_id, metrics)
        return

    log.info("Running. Reports every %d seconds. Press Ctrl+C to stop.", INTERVAL_SECS)
    while True:
        try:
            # Re-run register_or_load each cycle — handles 404 self-heal
            laptop_id = register_or_load()
            metrics   = collect_metrics()
            ok        = send_report(laptop_id, metrics)

            if not ok and not STATE_FILE.exists():
                # 404 happened and state was cleared — re-register immediately
                log.info("Re-registering now (no delay)...")
                time.sleep(3)
                continue

        except KeyboardInterrupt:
            log.info("Agent stopped by user.")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc)

        # Sleep in 10-second chunks, checking for pending report requests each time.
        # This allows the dashboard "Diagnose Now" button to trigger a fresh
        # report within ~10 seconds instead of waiting the full interval.
        elapsed = 0
        while elapsed < INTERVAL_SECS:
            time.sleep(10)
            elapsed += 10
            if elapsed < INTERVAL_SECS:   # don't check right before normal report
                try:
                    if check_pending(laptop_id):
                        log.info("Dashboard requested fresh report — sending now")
                        break   # exit sleep loop → send report immediately
                except Exception:
                    pass


if __name__ == "__main__":
    main()
