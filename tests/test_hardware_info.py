"""Hardware inventory is UI-only: parsing is pure, and nothing may run during a workload."""
from app.services import hardware_info as hw


WINDOWS_FIXTURE = {
    "cpu": {"Name": "11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz", "Manufacturer": "GenuineIntel",
            "MaxClockSpeed": 2419, "L2CacheSize": 5120, "L3CacheSize": 8192, "AddressWidth": 64,
            "SocketDesignation": "U3E1", "VirtualizationFirmwareEnabled": True},
    "mem": [
        {"Capacity": "8589934592", "Speed": 3200, "ConfiguredClockSpeed": 3200, "SMBIOSMemoryType": 26,
         "MemoryType": 0, "FormFactor": 12, "Manufacturer": "Samsung ", "PartNumber": "M471A1K43DB1-CWE ", "DeviceLocator": "ChannelA-DIMM0"},
    ],
    "memarray": {"MemoryDevices": 2},
    "disks": {"FriendlyName": "KBG40ZNS256G NVMe KIOXIA 256GB", "MediaType": "4", "BusType": "17",
              "Size": "256060514304", "SpindleSpeed": 0, "HealthStatus": "0"},
    "gpus": [
        {"Name": "Intel(R) Iris(R) Xe Graphics", "AdapterRAM": 1073741824, "DriverVersion": "31.0.101.4502",
         "AdapterCompatibility": "Intel Corporation", "CurrentHorizontalResolution": 1920,
         "CurrentVerticalResolution": 1080, "CurrentRefreshRate": 60},
        {"Name": "Microsoft Basic Display Adapter", "AdapterRAM": 0},
    ],
    "os": {"Caption": "Microsoft Windows 11 Pro", "Version": "10.0.22631", "BuildNumber": "22631", "OSArchitecture": "64-bit"},
    "sys": {"Manufacturer": "Dell Inc.", "Model": "Latitude 5420", "SystemType": "x64-based PC"},
    "bios": {"Manufacturer": "Dell Inc.", "SMBIOSBIOSVersion": "1.20.0"},
}


def test_windows_inventory_parses_ram_storage_gpu_os():
    dx = [{"name": "Intel(R) Iris(R) Xe Graphics", "luid": 0xE5B9, "dedicated": 128 * 1024 * 1024, "shared": 4 * 1024 ** 3}]
    out = hw.parse_windows_inventory(WINDOWS_FIXTURE, dx, "23H2")
    assert out["memory"]["type"] == "DDR4" and out["memory"]["speed_mts"] == 3200
    assert out["memory"]["modules"][0]["form_factor"] == "SODIMM"
    assert out["memory"]["slots_used"] == 1 and out["memory"]["slots_total"] == 2
    disk = out["storage_devices"][0]
    assert disk["type"] == "SSD" and "NVMe" in disk["description"] and disk["size_bytes"] == 256060514304
    assert [g["name"] for g in out["gpus"]] == ["Intel(R) Iris(R) Xe Graphics"]   # virtual adapter dropped
    gpu = out["gpus"][0]
    assert gpu["vendor"] == "Intel" and gpu["dedicated_bytes"] == 128 * 1024 * 1024 and gpu["luid"] == 0xE5B9
    assert out["os"]["name"] == "Windows 11 Pro" and out["os"]["feature_version"] == "23H2"


def test_cpu_generation_and_codename():
    info = hw.cpu_generation("11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz", 6, 140)
    assert info == {"generation": "11th Gen Intel Core", "codename": "Tiger Lake"}
    assert hw.cpu_generation("Intel(R) Core(TM) i7-8550U CPU @ 1.80GHz")["generation"] == "8th Gen Intel Core"
    assert hw.cpu_generation("Intel(R) Core(TM) i7-12700H")["generation"] == "12th Gen Intel Core"
    assert hw.cpu_generation("AMD Ryzen 5 5600H with Radeon Graphics")["generation"] == "AMD Ryzen 5000 series"
    assert hw.parse_processor_string("Intel64 Family 6 Model 140 Stepping 1, GenuineIntel") == {
        "family": 6, "model": 140, "stepping": 1, "vendor_id": "GenuineIntel"}


def test_memory_and_disk_classification():
    assert hw.memory_type_label(34) == "DDR5" and hw.memory_type_label(0, 24) == "DDR3" and hw.memory_type_label(0, 0) is None
    assert hw.classify_disk("3", "11", 7200)["type"] == "HDD"
    assert hw.classify_disk("SSD", "SATA")["description"] == "SATA SSD"
    assert hw.classify_disk("0", "17")["type"] == "SSD"
    assert hw.classify_disk("0", "3", 0, "Mystery")["type"] == "Unknown"


def test_gpu_counter_aggregation_matches_task_manager_logic():
    engine = [
        ("pid_10_luid_0x00000000_0x0000E5B9_phys_0_eng_0_engtype_3D", 30.0, 0),
        ("pid_11_luid_0x00000000_0x0000E5B9_phys_0_eng_0_engtype_3D", 15.0, 0),
        ("pid_11_luid_0x00000000_0x0000E5B9_phys_0_eng_1_engtype_Copy", 5.0, 0),
        ("pid_12_luid_0x00000000_0x0000E5B9_phys_0_eng_0_engtype_3D", 99.0, 4),   # invalid sample ignored
    ]
    dedicated = [("luid_0x00000000_0x0000E5B9_phys_0", 1048576.0, 0)]
    shared = [("luid_0x00000000_0x0000E5B9_phys_0", 2097152.0, 0)]
    result = hw.aggregate_gpu_counters(engine, dedicated, shared)
    assert result[0xE5B9] == {"util_percent": 45.0, "dedicated_used_bytes": 1048576, "shared_used_bytes": 2097152}


def test_details_never_collects_while_workload_is_active(monkeypatch):
    monkeypatch.setattr(hw, "_CACHE", None)
    called = []
    monkeypatch.setattr(hw, "_collect", lambda: called.append(1) or {})
    deferred = hw.get_details(collect_allowed=False)
    assert deferred["status"] == "deferred" and not called
    assert deferred["cpu"]["logical_processors"]


def test_details_collect_once_then_cache(monkeypatch):
    monkeypatch.setattr(hw, "_CACHE", None)
    calls = []
    monkeypatch.setattr(hw, "_collect", lambda: calls.append(1) or {"gpus": [], "storage_devices": []})
    first = hw.get_details()
    second = hw.get_details()
    assert first["status"] == "ready" and second is first and len(calls) == 1


def test_api_pauses_extra_telemetry_during_benchmark(monkeypatch):
    from app import main
    monkeypatch.setattr(main.benchmark, "has_active_jobs", lambda: True)
    monkeypatch.setattr(main.generation, "has_active_jobs", lambda: False)
    monkeypatch.setattr(main.hardware_info, "live_extra", lambda: (_ for _ in ()).throw(AssertionError("must not sample")))
    assert main.hardware_live_extra()["paused"] is True
    monkeypatch.setattr(hw, "_CACHE", None)
    monkeypatch.setattr(hw, "_collect", lambda: (_ for _ in ()).throw(AssertionError("must not collect")))
    assert main.hardware_details()["status"] == "deferred"
