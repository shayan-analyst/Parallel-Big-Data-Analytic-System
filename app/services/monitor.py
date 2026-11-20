from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

try:
    import pynvml
except Exception:
    pynvml = None

_PROCESS = psutil.Process(os.getpid())
# Prime both samplers so the first browser update is not a misleading all-zero
# sample. Subsequent values are OS counters sampled once per monitor poll.
psutil.cpu_percent(interval=None, percpu=True)
_PROCESS.cpu_percent(interval=None)
_LAST = {"time": time.monotonic(), "disk": None, "net": None, "process_io": None}
_NVML_READY = False


def _bytes(value: int | float | None) -> int:
    return int(value or 0)


def _gpu_info() -> list[dict[str, Any]]:
    global _NVML_READY
    if pynvml is None:
        return []
    try:
        if not _NVML_READY:
            pynvml.nvmlInit()
            _NVML_READY = True
        results = []
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            item: dict[str, Any] = {
                "index": index, "name": str(name),
                "gpu_util_percent": int(util.gpu),
                "memory_util_percent": int(util.memory),
                "memory_total_bytes": _bytes(memory.total),
                "memory_used_bytes": _bytes(memory.used),
                "memory_free_bytes": _bytes(memory.free),
            }
            try: item["temperature_c"] = int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
            except Exception: item["temperature_c"] = None
            try: item["power_watts"] = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 2)
            except Exception: item["power_watts"] = None
            results.append(item)
        return results
    except Exception:
        return []



def _cpu_model() -> str:
    """Return a hardware-reported CPU model name where the OS exposes one."""
    candidates = [platform.processor(), platform.uname().processor]
    system = platform.system()
    try:
        if system == "Windows":
            import winreg
            key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                candidates.append(winreg.QueryValueEx(key, "ProcessorNameString")[0])
        elif system == "Linux":
            cpuinfo = Path("/proc/cpuinfo")
            if cpuinfo.exists():
                for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.lower().startswith(("model name", "hardware")) and ":" in line:
                        candidates.append(line.split(":", 1)[1].strip())
                        break
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                check=False, capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                candidates.append(result.stdout.strip())
    except Exception:
        pass
    for value in candidates:
        if value and value.strip() and value.strip().lower() not in {"amd64", "x86_64", "arm64", "aarch64"}:
            return value.strip()
    return platform.machine() or "Unknown CPU"


def hardware_summary() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "cpu_name": _cpu_model(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_processors": psutil.cpu_count(logical=True),
        "cpu_frequency_mhz": round(freq.current, 1) if freq else None,
        "ram_total_bytes": _bytes(vm.total),
        "ram_available_bytes": _bytes(vm.available),
        "gpu_devices": _gpu_info(),
    }


def _process_snapshot(limit: int = 12) -> list[dict[str, Any]]:
    values = []
    for proc in psutil.process_iter(attrs=["pid", "name", "memory_info", "status"]):
        try:
            info = proc.info
            if info.get("pid") == os.getpid():
                continue  # This application is shown in the dedicated app metrics cards.
            mem = info.get("memory_info")
            values.append({
                "pid": info.get("pid"), "name": info.get("name") or "Unknown",
                "memory_bytes": _bytes(mem.rss if mem else 0),
                "status": info.get("status"),
                "cpu_percent": proc.cpu_percent(interval=None),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(values, key=lambda x: (x["cpu_percent"], x["memory_bytes"]), reverse=True)[:limit]


def _storage_snapshot() -> list[dict[str, Any]]:
    drives = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint
        if mount in seen:
            continue
        seen.add(mount)
        try:
            usage = psutil.disk_usage(mount)
            drives.append({
                "device": part.device, "mountpoint": mount, "filesystem": part.fstype,
                "total_bytes": _bytes(usage.total), "used_bytes": _bytes(usage.used),
                "free_bytes": _bytes(usage.free), "percent": float(usage.percent),
            })
        except (PermissionError, OSError):
            continue
    return drives


def get_live_metrics(*, light: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    elapsed = max(0.001, now - _LAST["time"])
    _LAST["time"] = now

    vm = psutil.virtual_memory()
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu_total = sum(cpu_per_core) / len(cpu_per_core) if cpu_per_core else psutil.cpu_percent(interval=None)
    proc_cpu = _PROCESS.cpu_percent(interval=None)
    try:
        mem = _PROCESS.memory_info()
        process_mem = _bytes(mem.rss)
        process_vms = _bytes(mem.vms)
        process_threads = _PROCESS.num_threads()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process_mem, process_vms, process_threads = 0, 0, 0

    app_io_rate = {"read_bytes_per_sec": 0.0, "write_bytes_per_sec": 0.0}
    app_io_total = {"read_bytes_total": 0, "write_bytes_total": 0}
    try:
        proc_io = _PROCESS.io_counters()
        current_io = (_bytes(proc_io.read_bytes), _bytes(proc_io.write_bytes))
        previous_io = _LAST["process_io"]
        if previous_io:
            app_io_rate = {
                "read_bytes_per_sec": max(0.0, (current_io[0] - previous_io[0]) / elapsed),
                "write_bytes_per_sec": max(0.0, (current_io[1] - previous_io[1]) / elapsed),
            }
        app_io_total = {"read_bytes_total": current_io[0], "write_bytes_total": current_io[1]}
        _LAST["process_io"] = current_io
    except (AttributeError, NotImplementedError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass

    disk = psutil.disk_io_counters()
    net = psutil.net_io_counters()
    disk_rate = {"read_bytes_per_sec": 0.0, "write_bytes_per_sec": 0.0}
    net_rate = {"bytes_recv_per_sec": 0.0, "bytes_sent_per_sec": 0.0}
    if disk:
        old = _LAST["disk"]
        if old:
            disk_rate = {
                "read_bytes_per_sec": max(0.0, (disk.read_bytes - old[0]) / elapsed),
                "write_bytes_per_sec": max(0.0, (disk.write_bytes - old[1]) / elapsed),
            }
        _LAST["disk"] = (disk.read_bytes, disk.write_bytes)
    if net:
        old = _LAST["net"]
        if old:
            net_rate = {
                "bytes_recv_per_sec": max(0.0, (net.bytes_recv - old[0]) / elapsed),
                "bytes_sent_per_sec": max(0.0, (net.bytes_sent - old[1]) / elapsed),
            }
        _LAST["net"] = (net.bytes_recv, net.bytes_sent)

    freq = psutil.cpu_freq()
    try:
        load = os.getloadavg() if hasattr(os, "getloadavg") else None
    except OSError:
        load = None
    sampled_at = time.time()
    return {
        "timestamp": sampled_at,
        "sampled_at": sampled_at,
        "sample_interval_seconds": round(elapsed, 3),
        "source": "local operating system via psutil",
        "cpu": {
            "total_percent": round(float(cpu_total), 1),
            "per_core_percent": [round(float(x), 1) for x in cpu_per_core],
            "process_percent": round(float(proc_cpu), 1),
            "frequency_mhz": round(freq.current, 1) if freq else None,
            "load_average": list(load) if load else None,
        },
        "memory": {
            "total_bytes": _bytes(vm.total), "available_bytes": _bytes(vm.available),
            "used_bytes": _bytes(vm.used), "percent": float(vm.percent),
            "process_rss_bytes": process_mem, "process_vms_bytes": process_vms,
            "process_threads": process_threads,
        },
        "gpu_devices": [] if light else _gpu_info(),
        "storage": [] if light else _storage_snapshot(),
        "disk_io": disk_rate,
        "application_io": {**app_io_rate, **app_io_total,
                            "label": "Application process I/O counters; may include backend/runtime files"},
        "network": {
            "bytes_sent_total": _bytes(net.bytes_sent if net else 0),
            "bytes_recv_total": _bytes(net.bytes_recv if net else 0),
            **net_rate,
            "label": "OS network traffic; not a maximum ISP speed test",
        },
        "processes": [] if light else _process_snapshot(),
        "monitor_process": {"pid": os.getpid(), "name": "Parallel Big Data Analytics"},
        "sampling_mode": "light" if light else "full",
    }
