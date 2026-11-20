from app.services import monitor


def test_live_metrics_are_sampled_from_local_os():
    metrics = monitor.get_live_metrics()
    assert metrics["source"] == "local operating system via psutil"
    assert metrics["sample_interval_seconds"] >= 0
    assert isinstance(metrics["cpu"]["per_core_percent"], list)
    assert 0 <= metrics["memory"]["percent"] <= 100
    assert metrics["memory"]["total_bytes"] > 0
    assert metrics["timestamp"] > 0


def test_light_live_metrics_skip_expensive_inventory():
    metrics = monitor.get_live_metrics(light=True)
    assert metrics["sampling_mode"] == "light"
    assert metrics["processes"] == []
    assert metrics["storage"] == []
    assert metrics["gpu_devices"] == []
    assert metrics["memory"]["process_rss_bytes"] > 0
