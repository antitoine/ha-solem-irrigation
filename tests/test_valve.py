"""Tests for the SOLEM station valves."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemStation,
)
from custom_components.solem_irrigation.valve import (
    SolemStationValve,
    async_setup_entry,
)

STATION_1 = SolemStation(id="s1", name="Pelouse 1", index=1)
STATION_2 = SolemStation(id="s2", name="Potager", index=2)


@pytest.fixture
def module() -> SolemModule:
    return SolemModule(
        id="m1",
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[STATION_1, STATION_2],
        programs=[],
    )


@pytest.fixture
def coordinator(module) -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    return coord


def test_is_closed_tracks_running_station(coordinator, module):
    """Only the running station's valve is open; all others read closed."""
    coordinator.running_station_index.return_value = 1
    valve1 = SolemStationValve(coordinator, module, STATION_1)
    valve2 = SolemStationValve(coordinator, module, STATION_2)
    assert valve1.is_closed is False
    assert valve2.is_closed is True
    assert valve1.unique_id == "m1_valve_s1"


async def test_open_uses_remembered_duration(coordinator, module):
    coordinator.get_run_minutes.return_value = 8
    coordinator.async_command_run_station = AsyncMock()
    valve = SolemStationValve(coordinator, module, STATION_1)

    await valve.async_open_valve()

    coordinator.get_run_minutes.assert_called_once_with("s1")
    coordinator.async_command_run_station.assert_awaited_once_with(module, STATION_1, 8)


async def test_close_running_station_stops(coordinator, module):
    coordinator.running_station_index.return_value = 1
    coordinator.async_command_stop = AsyncMock()
    valve = SolemStationValve(coordinator, module, STATION_1)

    await valve.async_close_valve()

    coordinator.async_command_stop.assert_awaited_once_with(module)


async def test_close_other_station_is_noop(coordinator, module):
    """Closing a non-running zone must not stop the zone that *is* running."""
    coordinator.running_station_index.return_value = 2  # zone 2 is watering
    coordinator.async_command_stop = AsyncMock()
    valve = SolemStationValve(coordinator, module, STATION_1)

    await valve.async_close_valve()

    coordinator.async_command_stop.assert_not_called()


async def test_async_setup_entry_one_valve_per_station(coordinator, module):
    pool = SolemModule(
        id="pool",
        name="Pool",
        serial="SER2",
        type="LR-PC",
        display_type="LR-PC",
        raw={},
        stations=[SolemStation(id="x", name="x", index=1)],
        programs=[],
    )
    coordinator.modules = {module.id: module, pool.id: pool}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert sorted(v.unique_id for v in added) == ["m1_valve_s1", "m1_valve_s2"]
