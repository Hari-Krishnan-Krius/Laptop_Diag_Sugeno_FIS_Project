"""
utils/system_monitor.py
─────────────────────────────────────────────────────────────────────────────
Live hardware metrics reader for the Laptop Diagnostics System.

Windows sensor strategy (tried in order):
  1. LibreHardwareMonitorLib.dll via pythonnet — permanent, no admin needed
  2. MSAcpi_ThermalZoneTemperature — native Windows WMI, temperature only
  3. psutil.sensors_temperatures() — last resort

Sensor values that cannot be read are returned as None (not fake defaults).
The UI displays "N/A" for None values instead of misleading numbers.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import platform
import subprocess
import logging
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

_PLATFORM = platform.system()

# Sentinel: None means "not available on this hardware"
# The UI must handle None and show "N/A" rather than a fake number
_UNAVAILABLE = None

# Path to LHM DLL — accept both filenames
_BASE = Path(__file__).parent.parent
_LHM_DLL = (
    _BASE / "LibreHardwareMonitorLib.dll"
    if (_BASE / "LibreHardwareMonitorLib.dll").exists()
    else _BASE / "LibreHardwareMonitor.dll"
)

_lhm_computer = None   # cached Computer() object
_lhm_failed   = False  # True if DLL init failed permanently


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_system_metrics() -> dict:
    """
    Return a dict of current hardware readings.
    Values that cannot be measured are None — the caller/UI must handle this.
    Never raises.
    """
    try:
        base = _read_base_metrics()
        hw   = _read_hardware_metrics()
        base.update(hw)
        base["platform"] = _PLATFORM
        return base
    except Exception as exc:
        log.warning("get_system_metrics fallback: %s", exc)
        return _fallback_metrics()


# ─────────────────────────────────────────────────────────────────────────────
# Base metrics — psutil, always reliable
# ─────────────────────────────────────────────────────────────────────────────

def _read_base_metrics() -> dict:
    # interval=0.5 — blocks 500 ms, always returns a real non-zero reading
    cpu_usage = psutil.cpu_percent(interval=0.5)
    vm        = psutil.virtual_memory()
    disk_path = "C:\\" if _PLATFORM == "Windows" else "/"
    try:
        disk_pct = round(psutil.disk_usage(disk_path).percent, 2)
    except Exception:
        disk_pct = 0.0

    return {
        "cpu_usage":    round(cpu_usage, 2),
        "ram_percent":  round(vm.percent, 2),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "ram_used_gb":  round(vm.used  / (1024 ** 3), 2),
        "disk_percent": disk_pct,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Hardware metrics dispatch
# ─────────────────────────────────────────────────────────────────────────────

def _read_hardware_metrics() -> dict:
    if   _PLATFORM == "Windows": return _read_windows()
    elif _PLATFORM == "Linux":   return _read_linux()
    elif _PLATFORM == "Darwin":  return _read_macos()
    return _hw_none()


# ─────────────────────────────────────────────────────────────────────────────
# Windows — DLL → ACPI → psutil chain
# ─────────────────────────────────────────────────────────────────────────────

def _read_windows() -> dict:
    # Start with all sensors as None (not fake defaults)
    result = _hw_none()

    # Layer 1: Embedded LHM DLL (best source — no admin, no separate app)
    _try_embedded_lhm(result)

    # Layer 2: ACPI temperature (WMI → PowerShell → psutil fallback chain)
    if result["cpu_temp"] is None:
        _try_acpi_temp(result)
    if result["cpu_temp"] is None:
        _try_powershell_temp(result)
    if result["cpu_temp"] is None:
        _try_psutil_temp(result)

    # Log once what we actually got
    if not getattr(_read_windows, "_logged_status", False):
        sources = []
        if result["cpu_voltage"] is not None: sources.append(f"vcpu={result['cpu_voltage']}V")
        if result["cpu_temp"]    is not None: sources.append(f"temp={result['cpu_temp']}°C")
        if result["fan_rpm"]     is not None: sources.append(f"fan={result['fan_rpm']}RPM")
        if sources:
            log.info("Sensor readings: %s", "  ".join(sources))
        else:
            log.warning(
                "No hardware sensors available. "
                "Place LibreHardwareMonitorLib.dll next to run.py "
                "and run: pip install pythonnet"
            )
        _read_windows._logged_status = True

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Embedded LHM DLL
# ─────────────────────────────────────────────────────────────────────────────

def _try_embedded_lhm(result: dict) -> None:
    global _lhm_computer, _lhm_failed

    if _lhm_failed:
        return

    if not _LHM_DLL.exists():
        if not getattr(_try_embedded_lhm, "_warned_no_dll", False):
            log.info(
                "LibreHardwareMonitorLib.dll not found at %s — "
                "for full sensor support: pip install pythonnet, "
                "then copy LibreHardwareMonitorLib.dll there.", _BASE
            )
            _try_embedded_lhm._warned_no_dll = True
        return

    try:
        import clr
    except ImportError:
        if not getattr(_try_embedded_lhm, "_warned_no_clr", False):
            log.info("pythonnet not installed — run: pip install pythonnet")
            _try_embedded_lhm._warned_no_clr = True
        return

    try:
        if _lhm_computer is None:
            import sys, time as _time
            dll_dir = str(_LHM_DLL.parent)

            # Add to Python path for assembly resolution
            if dll_dir not in sys.path:
                sys.path.insert(0, dll_dir)

            # Add to Windows DLL search path so dependency DLLs are found
            # (Python 3.8+ only, Windows only)
            try:
                os.add_dll_directory(dll_dir)
            except (AttributeError, OSError):
                pass

            # Load using resolved absolute path — avoids sandboxing issues
            try:
                clr.AddReference(str(_LHM_DLL.resolve()))
            except Exception:
                try:
                    import System.Reflection as _SR
                    _SR.Assembly.LoadFrom(str(_LHM_DLL.resolve()))
                except Exception:
                    clr.AddReference(_LHM_DLL.stem)

            from LibreHardwareMonitor.Hardware import Computer
            computer = Computer()
            computer.IsCpuEnabled         = True
            computer.IsGpuEnabled         = True
            computer.IsMemoryEnabled      = True
            computer.IsMotherboardEnabled = True
            computer.IsBatteryEnabled     = False
            computer.IsStorageEnabled     = False
            computer.Open()

            # AMD Ryzen requires two Update() passes — first returns 0.0
            for hw in computer.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
            _time.sleep(0.3)
            for hw in computer.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()

            _lhm_computer = computer
            log.info("✅ LibreHardwareMonitor.dll loaded — embedded sensor reading active")

        # Regular update
        for hw in _lhm_computer.Hardware:
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()

        temp_candidates = []
        fan_candidates  = []

        for hw in _lhm_computer.Hardware:
            for sub in hw.SubHardware:
                _extract_lhm_sensors(sub.Sensors, result, temp_candidates, fan_candidates)
            _extract_lhm_sensors(hw.Sensors, result, temp_candidates, fan_candidates)

        # Best temperature: highest value at best priority
        if temp_candidates:
            temp_candidates.sort(key=lambda x: x[0])
            best_p    = temp_candidates[0][0]
            best_vals = [v for p, v in temp_candidates if p == best_p]
            chosen    = round(max(best_vals), 1)
            if 20.0 <= chosen <= 120.0:
                result["cpu_temp"] = chosen

        # Best fan
        if fan_candidates:
            fan_candidates.sort(key=lambda x: x[0])
            result["fan_rpm"] = round(fan_candidates[0][1], 0)

    except Exception as exc:
        log.warning("Embedded LHM error: %s", exc)
        _lhm_failed   = True
        _lhm_computer = None


def _lhm_hardware_names() -> dict:
    """
    Read CPU/GPU model names directly off the already-open LHM Computer
    object's hw.Name property — no extra WMI call needed, since the LHM
    Computer() instance is already loaded and enumerating hardware for
    sensor reading. Returns {} if LHM is unavailable or not yet loaded.
    """
    names = {}
    if _lhm_computer is None:
        return names
    try:
        from LibreHardwareMonitor.Hardware import HardwareType
        for hw in _lhm_computer.Hardware:
            ht = str(hw.HardwareType)
            if ht == "Cpu" and "cpu_model" not in names:
                names["cpu_model"] = str(hw.Name)
            elif ht in ("GpuNvidia", "GpuAmd", "GpuIntel") and "gpu_model" not in names:
                names["gpu_model"] = str(hw.Name)
                names["gpu_vendor"] = ht.replace("Gpu", "")
    except Exception as exc:
        log.debug("Could not read hardware names from LHM: %s", exc)
    return names


def _wmi_ram_type() -> str:
    """RAM generation (DDR4/DDR5/etc.) via WMI SMBIOSMemoryType. Windows only."""
    try:
        import wmi
        c = wmi.WMI()
        # SMBIOSMemoryType codes per DMTF spec; 26=DDR4, 34=DDR5, 24=DDR3
        type_map = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5"}
        for mem in c.Win32_PhysicalMemory():
            code = getattr(mem, "SMBIOSMemoryType", None)
            if code in type_map:
                return type_map[code]
    except Exception as exc:
        log.debug("WMI RAM type lookup unavailable: %s", exc)
    return ""


def _linux_ram_type() -> str:
    """RAM generation via dmidecode (Linux). Requires root; returns "" if unavailable."""
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
    except Exception as exc:
        log.debug("dmidecode RAM type lookup unavailable: %s", exc)
    return ""


def _linux_cpu_model() -> str:
    try:
        text = Path("/proc/cpuinfo").read_text()
        for line in text.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception as exc:
        log.debug("cpuinfo CPU model lookup unavailable: %s", exc)
    return ""


def get_hardware_identity() -> dict:
    """
    Identify CPU model, RAM type, and GPU model for calibration purposes
    (see utils/hardware_specs.py). Independent of get_system_metrics() —
    called once at registration / re-resolution time, not every poll,
    since this data does not change between polling cycles.

    Returns a dict with keys cpu_model, ram_type, gpu_model — any of which
    may be "" if that source was unavailable on this platform/machine.
    """
    identity = {"cpu_model": "", "ram_type": "", "gpu_model": ""}

    if _PLATFORM == "Windows":
        lhm_names = _lhm_hardware_names()
        identity["cpu_model"] = lhm_names.get("cpu_model", "")
        identity["gpu_model"] = lhm_names.get("gpu_model", "")
        identity["ram_type"]  = _wmi_ram_type()
        if not identity["cpu_model"]:
            try:
                import wmi
                c = wmi.WMI()
                for cpu in c.Win32_Processor():
                    identity["cpu_model"] = str(cpu.Name).strip()
                    break
            except Exception as exc:
                log.debug("WMI CPU model fallback unavailable: %s", exc)
    elif _PLATFORM == "Linux":
        identity["cpu_model"] = _linux_cpu_model()
        identity["ram_type"]  = _linux_ram_type()
        # GPU model on Linux is not currently resolved — no clean
        # dependency-free source is wired up; falls back to category
        # default for gpu_v_nom, same as before this feature existed.

    return identity


def _extract_lhm_sensors(sensors, result: dict,
                          temp_candidates: list, fan_candidates: list) -> None:
    """
    Extract from LHM ISensor[].

    AMD Ryzen notes:
      - Temperatures return 0.0 on first poll → skip values ≤ 0.5°C
      - CPU voltage = "Core (SVI2 TFN)" or "Core #N VID" (exclude "SoC")
      - GPU voltage idle = 0.001V → skip values < 0.1V
      - HP 86FD has NO fan sensor → fan_rpm stays None (shown as N/A in UI)
    """
    from LibreHardwareMonitor.Hardware import SensorType as ST
    for sensor in sensors:
        try:
            name  = str(sensor.Name).lower().strip()
            stype = sensor.SensorType
            raw   = sensor.Value
            if raw is None:
                continue
            val = float(raw)

            # ── Temperature ────────────────────────────────────────────────
            if stype == ST.Temperature:
                if val <= 0.5:
                    continue   # AMD SMU not ready yet
                if any(k in name for k in ("tdie","tctl","package","cpu die")):
                    temp_candidates.append((1, val))
                elif name.startswith("cpu") or name == "cpu":
                    temp_candidates.append((2, val))
                elif "core" in name:
                    temp_candidates.append((3, val))

            # ── Fan ────────────────────────────────────────────────────────
            elif stype == ST.Fan and val > 0:
                fan_candidates.append((1 if "cpu" in name else 2, val))

            # ── Voltage ────────────────────────────────────────────────────
            elif stype == ST.Voltage and val > 0:
                # CPU core voltage (AMD SVI2, Intel VCore)
                if any(k in name for k in ("svi2 tfn","svi2","vcore","cpu core",
                                            "core #0 vid","core #1 vid",
                                            "core #2 vid","core vid")) \
                   and "soc" not in name:
                    v = round(val, 3)
                    if 0.5 <= v <= 2.0 and result["cpu_voltage"] is None:
                        result["cpu_voltage"] = v

                # GPU voltage — skip idle near-zero
                elif any(k in name for k in ("gfx","gpu core","vgpu")):
                    if val >= 0.1:
                        result["gpu_voltage"] = round(val, 3)

                # RAM voltage
                elif any(k in name for k in ("dimm","ram","dram","memory","vddio")):
                    if 0.8 <= val <= 2.5:
                        result["ram_voltage"] = round(val, 3)

                # +3.3V rail
                elif any(k in name for k in ("3.3","3v3")):
                    result["rail_3v3"] = round(val, 3)

                # +5V rail (0–1000 proxy)
                elif any(k in name for k in ("5v","+5")):
                    result["rail_5v_mw"] = min(round(val * 100.0, 1), 1000.0)

        except Exception:
            continue


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: Native ACPI temperature
# ─────────────────────────────────────────────────────────────────────────────

def _try_acpi_temp(result: dict) -> None:
    """
    Read CPU temperature from native Windows without any third-party tool.

    Method A: WMI MSAcpi_ThermalZoneTemperature (root\\WMI namespace)
    Method B: PowerShell Get-WmiObject fallback — works even if the wmi
              Python package has issues, as PowerShell is always available.
    """
    # Method A: Python wmi package
    try:
        import wmi
        zones = wmi.WMI(namespace=r"root\WMI").MSAcpi_ThermalZoneTemperature()
        if zones:
            temps = [float(z.CurrentTemperature) for z in zones
                     if z.CurrentTemperature is not None]
            if temps:
                max_dk  = max(temps)
                celsius = round((max_dk / 10.0) - 273.15, 1)
                if 20.0 <= celsius <= 120.0:
                    result["cpu_temp"] = celsius
                    return
    except Exception:
        pass

    # Method B: PowerShell subprocess — no Python packages needed
    if result["cpu_temp"] is None:
        try:
            ps_cmd = (
                "Get-WmiObject -Namespace root/WMI "
                "-Class MSAcpi_ThermalZoneTemperature | "
                "Measure-Object -Property CurrentTemperature -Maximum | "
                "Select-Object -ExpandProperty Maximum"
            )
            out = subprocess.check_output(
                ["powershell", "-NonInteractive", "-Command", ps_cmd],
                timeout=5, stderr=subprocess.DEVNULL, text=True
            ).strip()
            if out:
                raw_val = float(out.split()[-1])   # last token = number
                celsius = round((raw_val / 10.0) - 273.15, 1)
                if 20.0 <= celsius <= 120.0:
                    result["cpu_temp"] = celsius
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2b: PowerShell temperature fallback (no wmi package needed)
# ─────────────────────────────────────────────────────────────────────────────

def _try_powershell_temp(result: dict) -> None:
    """PowerShell ACPI fallback — works without the wmi Python package."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: psutil temperatures
# ─────────────────────────────────────────────────────────────────────────────

def _try_psutil_temp(result: dict) -> None:
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key, entries in temps.items():
                for e in entries:
                    lbl = (e.label or key).lower()
                    if any(k in lbl for k in ("package","core","cpu","tdie")):
                        if e.current and e.current > 0:
                            result["cpu_temp"] = round(float(e.current), 1)
                            return
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Linux
# ─────────────────────────────────────────────────────────────────────────────

def _read_linux() -> dict:
    result = _hw_none()
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key, entries in temps.items():
                for e in entries:
                    lbl = (e.label or key).lower()
                    if any(t in lbl for t in ("core","cpu","package","tdie")):
                        if e.current and e.current > 0:
                            result["cpu_temp"] = float(e.current)
                            break
    except Exception:
        pass
    try:
        fans = psutil.sensors_fans()
        if fans:
            for key, entries in fans.items():
                if entries and entries[0].current > 0:
                    result["fan_rpm"] = float(entries[0].current)
                    break
    except Exception:
        pass
    try:
        raw  = subprocess.check_output(
            ["sensors","-j"], timeout=3, stderr=subprocess.DEVNULL
        )
        vcore = _parse_lmsensors_vcore(json.loads(raw))
        if vcore:
            result["cpu_voltage"] = vcore
    except Exception:
        pass
    return result


def _parse_lmsensors_vcore(data: dict):
    for chip, features in data.items():
        if not isinstance(features, dict): continue
        for feat, sub in features.items():
            if not isinstance(sub, dict): continue
            if any(k in feat.lower() for k in ("vcore","in0")):
                for sk, v in sub.items():
                    if "input" in sk and isinstance(v,(int,float)) and 0.5 <= v <= 2.0:
                        return round(float(v), 3)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# macOS
# ─────────────────────────────────────────────────────────────────────────────

def _read_macos() -> dict:
    result = _hw_none()
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for key, entries in temps.items():
                for e in entries:
                    lbl = (e.label or key).lower()
                    if any(t in lbl for t in ("cpu","core","tc0p")):
                        if e.current and e.current > 0:
                            result["cpu_temp"] = float(e.current)
                            return result
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["osx-cpu-temp"], timeout=3, stderr=subprocess.DEVNULL
        ).decode().strip()
        result["cpu_temp"] = float(out.replace("°C","").strip())
    except Exception:
        pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hw_none() -> dict:
    """Return all hardware sensor slots as None = not yet read."""
    return {
        "cpu_temp":    None,
        "fan_rpm":     None,
        "cpu_voltage": None,
        "ram_voltage": None,
        "gpu_voltage": None,
        "rail_3v3":    None,
        "rail_5v_mw":  None,
    }


def _fallback_metrics() -> dict:
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        vm  = psutil.virtual_memory()
        dp  = "C:\\" if _PLATFORM == "Windows" else "/"
        dk  = psutil.disk_usage(dp)
    except Exception:
        cpu, vm, dk = 50.0, None, None
    return {
        "cpu_usage":    round(cpu, 2),
        "ram_percent":  getattr(vm,"percent",50.0) if vm else 50.0,
        "ram_total_gb": round(getattr(vm,"total",0)/(1024**3),2) if vm else 8.0,
        "ram_used_gb":  round(getattr(vm,"used", 0)/(1024**3),2) if vm else 4.0,
        "disk_percent": getattr(dk,"percent",50.0) if dk else 50.0,
        "platform":     _PLATFORM,
        **_hw_none(),
    }
