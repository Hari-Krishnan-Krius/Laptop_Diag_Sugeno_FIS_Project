#!/usr/bin/env python3
"""
debug_sensors.py
Run this to diagnose sensor issues and test the embedded DLL approach.
Usage:  python debug_sensors.py
"""
import sys, platform
from pathlib import Path

print("=" * 65)
print("  Laptop Diagnostics — Sensor Debug Tool")
print("=" * 65)
print(f"  Python  : {sys.version.split()[0]}")
print(f"  OS      : {platform.system()} {platform.release()}")
BASE = Path(__file__).parent
print(f"  Folder  : {BASE}")
print()

# ── Step 1: WMI namespace check ───────────────────────────────────────────────
print("STEP 1: WMI namespace check")
try:
    import wmi
    print("  wmi package : ✅ installed")
    for ns in (r"root\LibreHardwareMonitor", r"root\OpenHardwareMonitor"):
        try:
            sensors = wmi.WMI(namespace=ns).Sensor()
            count = len(sensors) if sensors else 0
            if count:
                print(f"  {ns} : ✅ {count} sensors")
            else:
                print(f"  {ns} : ⚠️  0 sensors (LHM not running as Admin)")
        except Exception as e:
            print(f"  {ns} : ❌ {e}")
except ImportError:
    print("  wmi package : ❌ not installed  →  pip install wmi pywin32")

# ── Step 2: DLL check ─────────────────────────────────────────────────────────
print()
print("STEP 2: LibreHardwareMonitorLib.dll check")
dll_names = ["LibreHardwareMonitorLib.dll", "LibreHardwareMonitor.dll"]
dll_path = None
for name in dll_names:
    p = BASE / name
    if p.exists():
        dll_path = p
        print(f"  ✅ Found: {p}")
        break
if not dll_path:
    print(f"  ❌ Not found in {BASE}")
    print(f"     → Download LHM zip from:")
    print(f"       https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases")
    print(f"     → Extract zip")
    print(f"     → Copy LibreHardwareMonitorLib.dll to: {BASE}")

# ── Step 3: pythonnet check ───────────────────────────────────────────────────
print()
print("STEP 3: pythonnet (clr) check")
try:
    import clr as _clr
    print(f"  ✅ pythonnet installed")
except ImportError:
    print(f"  ❌ not installed  →  pip install pythonnet")
    _clr = None

# ── Step 4: Try loading DLL and reading sensors ───────────────────────────────
print()
print("STEP 4: Loading DLL and reading sensors...")
if dll_path and _clr:
    try:
        import clr
        sys.path.insert(0, str(dll_path.parent))
        try:
            clr.AddReference(str(dll_path))
        except Exception:
            clr.AddReference(dll_path.stem)

        from LibreHardwareMonitor.Hardware import Computer, SensorType

        computer = Computer()
        computer.IsCpuEnabled         = True
        computer.IsGpuEnabled         = True
        computer.IsMemoryEnabled      = True
        computer.IsMotherboardEnabled = True
        computer.IsBatteryEnabled     = False
        computer.IsStorageEnabled     = False
        computer.Open()

        print()
        print(f"  {'HW/Sensor':<45} {'Type':<15} {'Value'}")
        print("  " + "-" * 70)

        total = 0
        for hw in computer.Hardware:
            hw.Update()
            print(f"  [{hw.HardwareType}] {hw.Name}")
            for sub in hw.SubHardware:
                sub.Update()
                print(f"    [{sub.HardwareType}] {sub.Name}")
                for s in sub.Sensors:
                    total += 1
                    print(f"      {str(s.Name):<43} {str(s.SensorType):<15} {s.Value}")
            for s in hw.Sensors:
                total += 1
                print(f"    {str(s.Name):<45} {str(s.SensorType):<15} {s.Value}")

        computer.Close()
        print()
        print(f"  ✅ Total sensors read: {total}")
        print()
        print("  → DLL method works! Restart the server — sensors will appear in the dashboard.")

    except Exception as e:
        print(f"  ❌ Failed: {e}")
        import traceback
        traceback.print_exc()
elif not dll_path:
    print("  ⏭  Skipped — DLL not found (see Step 2)")
elif not _clr:
    print("  ⏭  Skipped — pythonnet not installed (see Step 3)")

print()
print("=" * 65)
