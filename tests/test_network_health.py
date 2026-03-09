from datetime import timedelta

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from services.network_health import NetworkHealthMonitor


def test_network_health_monitor_grace_period_before_first_success():
    monitor = NetworkHealthMonitor(partition_threshold_seconds=15, startup_grace_seconds=30)

    monitor.state.initialized_at = monitor.state.initialized_at - timedelta(seconds=20)
    monitor.state.last_successful_api_call = monitor.state.initialized_at

    assert monitor.check_partition() is False


def test_network_health_monitor_detects_partition_after_grace_without_success():
    monitor = NetworkHealthMonitor(partition_threshold_seconds=15, startup_grace_seconds=30)

    monitor.state.initialized_at = monitor.state.initialized_at - timedelta(seconds=31)
    monitor.state.last_successful_api_call = monitor.state.initialized_at

    assert monitor.check_partition() is True


def test_network_health_monitor_recovers_after_success():
    monitor = NetworkHealthMonitor(partition_threshold_seconds=15, startup_grace_seconds=30)

    monitor.state.initialized_at = monitor.state.initialized_at - timedelta(seconds=31)
    monitor.state.last_successful_api_call = monitor.state.initialized_at

    assert monitor.check_partition() is True

    monitor.record_success()

    assert monitor.check_partition() is False
    assert monitor.state.is_partitioned is False
    assert monitor.state.has_successful_api_call is True


def test_execution_service_v2_reuses_client_network_monitor():
    class StubLedger:
        pass

    client = PolymarketClientV2(paper_trading=True, retry_backoff_base=0)
    service = ExecutionServiceV2(client, StubLedger())

    assert service.network_monitor is client.network_monitor