"""Read-only hardware inventory for the Overview "Your computer" panel.

This module is deliberately separate from the benchmark engine and never imports it.

Load policy (so the benchmark's timing is never disturbed):
  * The static inventory (CPU generation, DDR type, SSD/HDD, GPU, Windows build ...) is collected
    ONCE per server run and cached. Collection on Windows spawns a single PowerShell process.
  * `get_details(collect_allowed=False)` never spawns anything; the API passes False while a
    benchmark or dataset generation is running.
  * `live_extra()` (GPU utilisation + volume usage) is only called by the browser while the
    Overview page is visible, and the API refuses it while a benchmark is running.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Any

import psutil

from app.services import monitor

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None
_LAST_FAILURE = 0.0
_RETRY_AFTER_SECONDS = 60.0

# SMBIOS "Memory Device Type" codes (DMTF DSP0134).
_SMBIOS_MEMORY_TYPES = {
    18: "DDR", 19: "DDR2", 20: "DDR2 FB-DIMM", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2",
    29: "LPDDR3", 30: "LPDDR4", 32: "HBM", 33: "HBM2", 34: "DDR5", 35: "LPDDR5",
}
_FORM_FACTORS = {8: "DIMM", 12: "SODIMM", 13: "SRIMM", 15: "FB-DIMM"}
_INTEL_CODENAMES = {
    126: "Ice Lake", 140: "Tiger Lake", 141: "Tiger Lake", 151: "Alder Lake", 154: "Alder Lake",
    165: "Comet Lake", 166: "Comet Lake", 167: "Rocket Lake", 170: "Meteor Lake",
    183: "Raptor Lake", 186: "Raptor Lake", 191: "Raptor Lake",
}
_PHYSICAL_MEDIA = {"3": "HDD", "4": "SSD", "5": "SCM", "HDD": "HDD", "SSD": "SSD", "SCM": "SCM"}
_PHYSICAL_BUS = {
    "1": "SCSI", "2": "ATAPI", "3": "ATA", "7": "USB", "8": "RAID", "9": "iSCSI", "10": "SAS",
    "11": "SATA", "12": "SD", "13": "MMC", "16": "Storage Spaces", "17": "NVMe",
}
_VIRTUAL_GPU_WORDS = ("basic display", "remote display", "virtual", "mirror", "indirect display", "parsec")


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return [value] if isinstance(value, dict) else []


def _num(value: Any) -> int | None:
    try:
        number = int(float(value))
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- pure helpers (unit-tested)
def cpu_generation(name: str, family: int | None = None, model: int | None = None) -> dict[str, str | None]:
    """Best-effort marketing generation and codename from the CPU brand string."""
    text = name or ""
    generation: str | None = None
    match = re.search(r"(\d{1,2})(?:st|nd|rd|th)\s+Gen", text, re.I)
    if match:
        generation = f"{_ordinal(int(match.group(1)))} Gen Intel Core"
    elif re.search(r"Core\(TM\)\s*Ultra", text, re.I):
        generation = "Intel Core Ultra"
    else:
        match = re.search(r"\bi[3579]-(\d{4,5})", text)
        if match:
            digits = match.group(1)
            gen = int(digits[:2]) if len(digits) == 5 else int(digits[0])
            generation = f"{_ordinal(gen)} Gen Intel Core"
        else:
            match = re.search(r"Ryzen\s+(?:AI\s+)?[3579]\s+(\d)(\d{3})", text)
            if match:
                generation = f"AMD Ryzen {match.group(1)}000 series"
    codename = _INTEL_CODENAMES.get(model or -1) if (family == 6 and "intel" in text.lower() + " intel") else None
    return {"generation": generation, "codename": codename}


def parse_processor_string(value: str) -> dict[str, Any]:
    """`Intel64 Family 6 Model 140 Stepping 1, GenuineIntel` -> parts."""
    out: dict[str, Any] = {"family": None, "model": None, "stepping": None, "vendor_id": None}
    match = re.search(r"Family\s+(\d+)\s+Model\s+(\d+)\s+Stepping\s+(\d+)", value or "", re.I)
    if match:
        out["family"], out["model"], out["stepping"] = (int(match.group(i)) for i in (1, 2, 3))
    tail = (value or "").rsplit(",", 1)[-1].strip()
    if tail in {"GenuineIntel", "AuthenticAMD", "CentaurHauls", "HygonGenuine"}:
        out["vendor_id"] = tail
    return out


def memory_type_label(smbios: Any, legacy: Any = None) -> str | None:
    for code in (smbios, legacy):
        try:
            label = _SMBIOS_MEMORY_TYPES.get(int(code))
        except (TypeError, ValueError):
            continue
        if label:
            return label
    return None


def classify_disk(media: Any, bus: Any, spindle: Any = None, name: str = "") -> dict[str, str]:
    media_label = _PHYSICAL_MEDIA.get(str(media or "").strip().upper(), "")
    bus_label = _PHYSICAL_BUS.get(str(bus or "").strip(), str(bus or "").strip())
    bus_upper = bus_label.upper()
    lowered = (name or "").lower()
    if bus_upper == "NVME" or "nvme" in lowered:
        kind, detail = "SSD", "NVMe SSD (M.2 / PCIe)"
    elif media_label == "SSD" or "ssd" in lowered:
        kind = "SSD"
        detail = "SATA SSD" if bus_upper in {"SATA", "ATA"} else f"{bus_label or 'Solid-state'} SSD"
    elif media_label == "HDD" or (_num(spindle) or 0) > 0:
        kind, detail = "HDD", "Hard disk drive (spinning platters)"
    elif bus_upper == "USB":
        kind, detail = "External", "USB storage"
    else:
        kind, detail = "Unknown", "Drive type not reported by Windows"
    return {"type": kind, "interface": bus_label or "—", "description": detail}


def is_virtual_gpu(name: str) -> bool:
    lowered = (name or "").lower()
    return any(word in lowered for word in _VIRTUAL_GPU_WORDS)


def gpu_vendor(name: str, compatibility: str = "") -> str:
    text = f"{name} {compatibility}".lower()
    for key, label in (("nvidia", "NVIDIA"), ("geforce", "NVIDIA"), ("amd", "AMD"), ("radeon", "AMD"),
                       ("intel", "Intel"), ("apple", "Apple")):
        if key in text:
            return label
    return "Unknown"


def parse_windows_inventory(raw: dict[str, Any], dx_adapters: list[dict[str, Any]] | None = None,
                            display_version: str | None = None) -> dict[str, Any]:
    """Turn the PowerShell/CIM JSON payload into the API shape (pure function)."""
    dx_adapters = dx_adapters or []
    cpu_row = (_as_list(raw.get("cpu")) or [{}])[0]
    mem_rows = _as_list(raw.get("mem"))
    mem_array = (_as_list(raw.get("memarray")) or [{}])[0]
    os_row = (_as_list(raw.get("os")) or [{}])[0]
    sys_row = (_as_list(raw.get("sys")) or [{}])[0]
    bios_row = (_as_list(raw.get("bios")) or [{}])[0]

    sticks = []
    for row in mem_rows:
        label = memory_type_label(row.get("SMBIOSMemoryType"), row.get("MemoryType"))
        speed = _num(row.get("ConfiguredClockSpeed")) or _num(row.get("Speed"))
        sticks.append({
            "slot": row.get("DeviceLocator") or "—",
            "capacity_bytes": _num(row.get("Capacity")),
            "type": label,
            "speed_mts": speed,
            "form_factor": _FORM_FACTORS.get(_num(row.get("FormFactor")) or -1),
            "manufacturer": (row.get("Manufacturer") or "").strip() or None,
            "part_number": (row.get("PartNumber") or "").strip() or None,
        })
    kinds = {s["type"] for s in sticks if s["type"]}
    speeds = {s["speed_mts"] for s in sticks if s["speed_mts"]}
    memory = {
        "type": "/".join(sorted(kinds)) if kinds else None,
        "speed_mts": max(speeds) if speeds else None,
        "modules": sticks,
        "slots_used": len(sticks) or None,
        "slots_total": _num(mem_array.get("MemoryDevices")),
    }
    if sticks and all(s["form_factor"] is None for s in sticks) and memory["type"] and memory["type"].startswith("LP"):
        memory["layout_note"] = "Soldered low-power memory (not upgradeable)"

    disks = []
    for row in _as_list(raw.get("disks")):
        info = classify_disk(row.get("MediaType"), row.get("BusType"), row.get("SpindleSpeed"), row.get("FriendlyName") or "")
        disks.append({"model": (row.get("FriendlyName") or "Disk").strip(), "size_bytes": _num(row.get("Size")),
                      "health": row.get("HealthStatus"), **info})
    if not disks:   # Storage module unavailable -> fall back to Win32_DiskDrive
        for row in _as_list(raw.get("diskdrive")):
            info = classify_disk(row.get("MediaType"), row.get("InterfaceType"), None, row.get("Model") or "")
            disks.append({"model": (row.get("Model") or "Disk").strip(), "size_bytes": _num(row.get("Size")),
                          "health": None, **info})

    gpus = []
    for row in _as_list(raw.get("gpus")):
        name = (row.get("Name") or "").strip()
        if not name or is_virtual_gpu(name):
            continue
        dx = next((d for d in dx_adapters if (d.get("name") or "").strip().lower() == name.lower()), None) \
            or next((d for d in dx_adapters if name.lower() in (d.get("name") or "").lower()), None)
        adapter_ram = _num(row.get("AdapterRAM"))
        if adapter_ram == 0xFFFFFFFF:
            adapter_ram = None
        width, height = _num(row.get("CurrentHorizontalResolution")), _num(row.get("CurrentVerticalResolution"))
        gpus.append({
            "name": name,
            "vendor": gpu_vendor(name, row.get("AdapterCompatibility") or ""),
            "dedicated_bytes": (dx or {}).get("dedicated") or adapter_ram,
            "shared_bytes": (dx or {}).get("shared"),
            "driver": row.get("DriverVersion"),
            "resolution": f"{width} × {height}" if width and height else None,
            "refresh_hz": _num(row.get("CurrentRefreshRate")),
            "luid": (dx or {}).get("luid"),
        })

    caption = (os_row.get("Caption") or "").replace("Microsoft ", "").strip() or f"Windows {platform.release()}"
    cpu_name = (cpu_row.get("Name") or "").strip()
    return {
        "cpu": {
            "name": cpu_name or None,
            "vendor": (cpu_row.get("Manufacturer") or "").strip() or None,
            "base_mhz": _num(cpu_row.get("MaxClockSpeed")),
            "l2_kb": _num(cpu_row.get("L2CacheSize")),
            "l3_kb": _num(cpu_row.get("L3CacheSize")),
            "address_width": _num(cpu_row.get("AddressWidth")),
            "socket": (cpu_row.get("SocketDesignation") or "").strip() or None,
            "virtualization": cpu_row.get("VirtualizationFirmwareEnabled"),
        },
        "memory": memory,
        "storage_devices": disks,
        "gpus": gpus,
        "os": {
            "name": caption,
            "version": os_row.get("Version"),
            "build": os_row.get("BuildNumber"),
            "feature_version": display_version,
            "architecture": os_row.get("OSArchitecture"),
        },
        "machine": {
            "manufacturer": (sys_row.get("Manufacturer") or "").strip() or None,
            "model": (sys_row.get("Model") or "").strip() or None,
            "system_type": sys_row.get("SystemType"),
            "bios": " ".join(x for x in ((bios_row.get("Manufacturer") or "").strip(), (bios_row.get("SMBIOSBIOSVersion") or "").strip()) if x) or None,
        },
    }


_ENGINE_RE = re.compile(r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+).*?_eng_(\d+)_")
_ADAPTER_RE = re.compile(r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)")


def aggregate_gpu_counters(engine_items: list[tuple[str, float, int]],
                           dedicated_items: list[tuple[str, float, int]],
                           shared_items: list[tuple[str, float, int]]) -> dict[int, dict[str, Any]]:
    """Group Windows "GPU Engine" / "GPU Adapter Memory" counters per adapter LUID.

    Utilisation follows Task Manager: sum every process' usage of one hardware engine, then
    report the busiest engine of the adapter (capped at 100%).
    """
    per_engine: dict[tuple[int, int], float] = {}
    for name, value, status in engine_items:
        if status not in (0, 1):
            continue
        match = _ENGINE_RE.search(name or "")
        if not match:
            continue
        luid = (int(match.group(1), 16) << 32) | int(match.group(2), 16)
        key = (luid, int(match.group(3)))
        per_engine[key] = per_engine.get(key, 0.0) + max(0.0, float(value))
    adapters: dict[int, dict[str, Any]] = {}
    for (luid, _engine), total in per_engine.items():
        slot = adapters.setdefault(luid, {"util_percent": 0.0, "dedicated_used_bytes": None, "shared_used_bytes": None})
        slot["util_percent"] = max(slot["util_percent"], min(100.0, total))
    for field, items in (("dedicated_used_bytes", dedicated_items), ("shared_used_bytes", shared_items)):
        for name, value, status in items:
            if status not in (0, 1):
                continue
            match = _ADAPTER_RE.search(name or "")
            if not match:
                continue
            luid = (int(match.group(1), 16) << 32) | int(match.group(2), 16)
            slot = adapters.setdefault(luid, {"util_percent": None, "dedicated_used_bytes": None, "shared_used_bytes": None})
            slot[field] = int(max(0.0, float(value)))
    for slot in adapters.values():
        if slot["util_percent"] is not None:
            slot["util_percent"] = round(slot["util_percent"], 1)
    return adapters


# ---------------------------------------------------------------- Windows collectors
_POWERSHELL = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$o=[ordered]@{}
$o.cpu=Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,MaxClockSpeed,L2CacheSize,L3CacheSize,AddressWidth,SocketDesignation,VirtualizationFirmwareEnabled
$o.mem=Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,SMBIOSMemoryType,MemoryType,FormFactor,Manufacturer,PartNumber,DeviceLocator
$o.memarray=Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MemoryDevices
$o.disks=Get-PhysicalDisk | Select-Object FriendlyName,@{n='MediaType';e={"$([int]$_.MediaType)"}},@{n='BusType';e={"$([int]$_.BusType)"}},Size,SpindleSpeed,@{n='HealthStatus';e={"$($_.HealthStatus)"}}
$o.diskdrive=Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,InterfaceType,MediaType
$o.gpus=Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion,AdapterCompatibility,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate
$o.os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
$o.sys=Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,SystemType
$o.bios=Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion
$o | ConvertTo-Json -Depth 4 -Compress
"""


def _windows_directx_adapters() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX") as root:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, sub) as key:
                        def value(name: str) -> Any:
                            try:
                                return winreg.QueryValueEx(key, name)[0]
                            except OSError:
                                return None
                        description, luid = value("Description"), value("AdapterLuid")
                        if description and luid is not None:
                            out.append({"name": str(description), "luid": int(luid),
                                        "dedicated": _num(value("DedicatedVideoMemory")),
                                        "shared": _num(value("SharedSystemMemory"))})
                except OSError:
                    continue
    except Exception:
        return []
    return out


def _windows_display_version() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            for name in ("DisplayVersion", "ReleaseId"):
                try:
                    return str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _collect_windows() -> dict[str, Any]:
    flags = 0x08000000 | 0x00004000          # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _POWERSHELL],
        capture_output=True, timeout=25, creationflags=flags,
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError((result.stderr or b"").decode("utf-8", errors="replace")[:200] or "PowerShell returned no data")
    start = text.find("{")
    raw = json.loads(text[start:])
    return parse_windows_inventory(raw, _windows_directx_adapters(), _windows_display_version())


# ---------------------------------------------------------------- Linux / macOS collectors (best effort)
def _run(command: list[str], timeout: float = 4.0) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return done.stdout if done.returncode == 0 else ""
    except Exception:
        return ""


def _collect_linux() -> dict[str, Any]:
    out: dict[str, Any] = {"cpu": {}, "memory": {"type": None, "speed_mts": None, "modules": [],
                                                  "note": "RAM type/speed needs administrator access on this OS."},
                           "storage_devices": [], "gpus": [], "os": {}, "machine": {}}
    if shutil.which("lsblk"):
        try:
            data = json.loads(_run(["lsblk", "-J", "-d", "-b", "-o", "NAME,MODEL,SIZE,ROTA,TRAN,TYPE"]) or "{}")
            for dev in data.get("blockdevices", []):
                if dev.get("type") != "disk" or str(dev.get("name", "")).startswith(("loop", "ram", "zram")):
                    continue
                tran = (dev.get("tran") or "").lower()
                rota = str(dev.get("rota")) in {"1", "True", "true"}
                info = classify_disk("HDD" if rota else "SSD", tran.upper() if tran else "", None, dev.get("model") or "")
                if tran == "virtio":
                    info = {"type": "Virtual", "interface": "VirtIO", "description": "Virtual disk (reported by the hypervisor)"}
                out["storage_devices"].append({"model": (dev.get("model") or dev.get("name") or "Disk").strip(),
                                               "size_bytes": _num(dev.get("size")), "health": None, **info})
        except Exception:
            pass
    if shutil.which("lspci"):
        for line in _run(["lspci"]).splitlines():
            if re.search(r"VGA compatible|3D controller|Display controller", line):
                name = line.split(":", 2)[-1].strip()
                out["gpus"].append({"name": name, "vendor": gpu_vendor(name), "dedicated_bytes": None,
                                    "shared_bytes": None, "driver": None, "resolution": None, "refresh_hz": None, "luid": None})
    try:
        release = {}
        for line in open("/etc/os-release", encoding="utf-8"):
            if "=" in line:
                key, _, value = line.partition("=")
                release[key] = value.strip().strip('"')
        out["os"] = {"name": release.get("PRETTY_NAME") or platform.system(), "version": platform.release(),
                     "build": None, "feature_version": None, "architecture": platform.machine()}
    except OSError:
        out["os"] = {"name": platform.system(), "version": platform.release(), "build": None,
                     "feature_version": None, "architecture": platform.machine()}
    return out


def _collect_macos() -> dict[str, Any]:
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    model = _run(["sysctl", "-n", "hw.model"]).strip()
    return {"cpu": {"name": chip or None}, "memory": {"type": None, "speed_mts": None, "modules": []},
            "storage_devices": [], "gpus": [],
            "os": {"name": f"macOS {platform.mac_ver()[0]}", "version": platform.mac_ver()[0], "build": None,
                   "feature_version": None, "architecture": platform.machine()},
            "machine": {"manufacturer": "Apple", "model": model or None, "system_type": None, "bios": None}}


# ---------------------------------------------------------------- public API
def _basic() -> dict[str, Any]:
    """Cheap, always-available facts (no subprocess)."""
    name = monitor._cpu_model()
    proc = parse_processor_string(platform.processor() or platform.uname().processor or "")
    freq = psutil.cpu_freq()
    vm = psutil.virtual_memory()
    gen = cpu_generation(name, proc["family"], proc["model"])
    bits = struct.calcsize("P") * 8
    return {
        "cpu": {
            "name": name, "vendor_id": proc["vendor_id"], "family": proc["family"], "model": proc["model"],
            "stepping": proc["stepping"], "generation": gen["generation"], "codename": gen["codename"],
            "physical_cores": psutil.cpu_count(logical=False), "logical_processors": psutil.cpu_count(logical=True),
            "architecture": platform.machine(), "bits": bits,
            "max_mhz": round(freq.max, 0) if freq and freq.max else None,
        },
        "memory": {"total_bytes": int(vm.total), "type": None, "speed_mts": None, "modules": []},
        "os": {"name": f"{platform.system()} {platform.release()}", "version": platform.version(),
               "architecture": platform.machine(), "bits": bits},
        "machine": {"hostname": platform.node()},
        "storage_devices": [], "gpus": [],
    }


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for section, values in extra.items():
        if isinstance(values, dict) and isinstance(base.get(section), dict):
            for key, value in values.items():
                if value not in (None, "", []):
                    base[section][key] = value
        else:
            base[section] = values
    if base["cpu"].get("name"):
        gen = cpu_generation(base["cpu"]["name"], base["cpu"].get("family"), base["cpu"].get("model"))
        base["cpu"]["generation"] = gen["generation"] or base["cpu"].get("generation")
        base["cpu"]["codename"] = gen["codename"] or base["cpu"].get("codename")
    return base


def _collect() -> dict[str, Any]:
    system = platform.system()
    if system == "Windows":
        return _collect_windows()
    if system == "Linux":
        return _collect_linux()
    if system == "Darwin":
        return _collect_macos()
    return {}


def get_details(collect_allowed: bool = True) -> dict[str, Any]:
    """Static inventory. Cached; never spawns a process when `collect_allowed` is False."""
    global _CACHE, _LAST_FAILURE
    if _CACHE is not None:
        return _CACHE
    if not collect_allowed:
        return {**_basic(), "status": "deferred",
                "message": "Full hardware inventory is read after the running benchmark or generation finishes, so it cannot affect timing."}
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        base = _basic()
        if time.monotonic() - _LAST_FAILURE < _RETRY_AFTER_SECONDS and _LAST_FAILURE:
            return {**base, "status": "partial", "message": "Detailed inventory is temporarily unavailable."}
        try:
            data = _merge(base, _collect())
            data["status"] = "ready"
            data["collected_at"] = time.time()
            _CACHE = data
            return data
        except Exception as exc:  # PowerShell blocked, timeout, odd locale ...
            _LAST_FAILURE = time.monotonic()
            return {**base, "status": "partial", "message": f"Detailed inventory could not be read ({type(exc).__name__}). Basic facts are shown."}


class _PdhGpuSampler:
    """Windows Performance Counters for GPU utilisation of ANY vendor (Intel / AMD / NVIDIA)."""

    PDH_FMT_DOUBLE = 0x200
    PDH_MORE_DATA = 0x800007D2

    def __init__(self) -> None:
        self.ok = False
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._primed = False
        try:
            class _Union(ctypes.Union):
                _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                            ("largeValue", ctypes.c_longlong)]

            class _Value(ctypes.Structure):
                _anonymous_ = ("u",)
                _fields_ = [("CStatus", ctypes.c_ulong), ("u", _Union)]

            class _Item(ctypes.Structure):
                _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Value)]

            self._Item = _Item
            self._pdh = ctypes.windll.pdh  # type: ignore[attr-defined]
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            for key, path in (("engine", r"\GPU Engine(*)\Utilization Percentage"),
                              ("dedicated", r"\GPU Adapter Memory(*)\Dedicated Usage"),
                              ("shared", r"\GPU Adapter Memory(*)\Shared Usage")):
                handle = ctypes.c_void_p()
                if self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)) == 0:
                    self._counters[key] = handle
            self.ok = "engine" in self._counters
            if self.ok:
                self._pdh.PdhCollectQueryData(self._query)   # first sample primes the rate counters
        except Exception:
            self.ok = False

    def _array(self, handle: ctypes.c_void_p) -> list[tuple[str, float, int]]:
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        code = self._pdh.PdhGetFormattedCounterArrayW(handle, self.PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if (code & 0xFFFFFFFF) != self.PDH_MORE_DATA or size.value == 0:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        code = self._pdh.PdhGetFormattedCounterArrayW(handle, self.PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer)
        if code != 0:
            return []
        items = ctypes.cast(buffer, ctypes.POINTER(self._Item))
        return [(items[i].szName or "", float(items[i].FmtValue.doubleValue), int(items[i].FmtValue.CStatus))
                for i in range(count.value)]

    def sample(self) -> dict[int, dict[str, Any]] | None:
        if not self.ok:
            return None
        try:
            if self._pdh.PdhCollectQueryData(self._query) != 0:
                return None
            data = aggregate_gpu_counters(self._array(self._counters["engine"]),
                                          self._array(self._counters["dedicated"]) if "dedicated" in self._counters else [],
                                          self._array(self._counters["shared"]) if "shared" in self._counters else [])
            return data
        except Exception:
            return None


_PDH: _PdhGpuSampler | None = None


def _gpu_live() -> dict[str, Any]:
    details = _CACHE or {}
    known = details.get("gpus") or []
    gpus: list[dict[str, Any]] = []
    for item in monitor._gpu_info():     # NVIDIA via NVML, when installed
        gpus.append({"name": item["name"], "util_percent": item["gpu_util_percent"],
                     "dedicated_used_bytes": item["memory_used_bytes"], "dedicated_total_bytes": item["memory_total_bytes"],
                     "shared_used_bytes": None, "temperature_c": item.get("temperature_c"),
                     "power_watts": item.get("power_watts"), "source": "NVML"})
    if platform.system() == "Windows":
        global _PDH
        if _PDH is None:
            _PDH = _PdhGpuSampler()
        sampled = _PDH.sample()
        if sampled:
            for luid, values in sampled.items():
                gpu = next((g for g in known if g.get("luid") == luid), None)
                name = gpu["name"] if gpu else f"GPU adapter {len(gpus) + 1}"
                if any(name.lower() == g["name"].lower() or name.lower() in g["name"].lower() for g in gpus):
                    continue   # already reported by NVML with richer data
                if values["util_percent"] is None and values["dedicated_used_bytes"] is None:
                    continue
                gpus.append({"name": name, "util_percent": values["util_percent"],
                             "dedicated_used_bytes": values["dedicated_used_bytes"],
                             "dedicated_total_bytes": (gpu or {}).get("dedicated_bytes"),
                             "shared_used_bytes": values["shared_used_bytes"],
                             "shared_total_bytes": (gpu or {}).get("shared_bytes"),
                             "temperature_c": None, "power_watts": None, "source": "Windows performance counters"})
    return {"available": bool(gpus), "gpus": gpus}


def live_extra() -> dict[str, Any]:
    """Slow-changing live values for the Overview hardware cards (GPU + volumes)."""
    return {"paused": False, "sampled_at": time.time(), "volumes": monitor._storage_snapshot(), **_gpu_live()}
