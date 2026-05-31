"""Tests for the SOLEM sensors."""

from unittest.mock import MagicMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemStation,
)
from custom_components.solem_irrigation.sensor import (
    SolemBatterySensor,
    SolemLastCommunicationSensor,
    SolemRunningStationSensor,
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


# -- running station ----------------------------------------------------------


def test_running_station_name(coordinator, module):
    coordinator.running_station_index.return_value = 2
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value == "Potager"
    assert sensor.unique_id == "m1_running_station"


def test_running_station_idle(coordinator, module):
    coordinator.running_station_index.return_value = 0
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value is None


def test_running_station_unknown_index(coordinator, module):
    coordinator.running_station_index.return_value = 9
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value == "Station 9"


def test_running_station_time_remaining(coordinator, module):
    coordinator.running_station_index.return_value = 1
    coordinator.module_state.return_value = {"status": {"watering": {"time": "00:12"}}}
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.extra_state_attributes == {"time_remaining": "00:12"}


def test_running_station_no_time_remaining_when_zero(coordinator, module):
    coordinator.module_state.return_value = {"status": {"watering": {"time": "00:00"}}}
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.extra_state_attributes is None


# -- last communication -------------------------------------------------------


def test_last_communication_from_state(coordinator, module):
    coordinator.module_state.return_value = {
        "lastRadioCommunication": "2026-05-31T10:00:00+00:00"
    }
    sensor = SolemLastCommunicationSensor(coordinator, module)
    assert sensor.native_value == dt_util.parse_datetime("2026-05-31T10:00:00+00:00")


def test_last_communication_falls_back_to_static(coordinator, module):
    coordinator.module_state.return_value = {}
    module.raw["lastRadioCommunication"] = "2026-05-30T08:30:00+00:00"
    sensor = SolemLastCommunicationSensor(coordinator, module)
    assert sensor.native_value == dt_util.parse_datetime("2026-05-30T08:30:00+00:00")


def test_last_communication_none(coordinator, module):
    coordinator.module_state.return_value = {}
    sensor = SolemLastCommunicationSensor(coordinator, module)
    assert sensor.native_value is None


# -- battery ------------------------------------------------------------------


def test_battery_value(coordinator, module):
    module.raw["battery"] = 5
    sensor = SolemBatterySensor(coordinator, module)
    assert sensor.native_value == 5


def test_battery_none(coordinator, module):
    sensor = SolemBatterySensor(coordinator, module)
    assert sensor.native_value is None


# -- setup --------------------------------------------------------------------


async def test_async_setup_entry(coordinator, module):
    """Controllers get running-station + last-comm; gateways only last-comm."""
    gateway = SolemModule(
        id="g1",
        name="Gateway",
        serial="SERG",
        type="LR-MB",
        display_type="LR-MB",
        raw={"battery": 4},
        stations=[],
        programs=[],
    )
    coordinator.modules = {module.id: module, gateway.id: gateway}
    coordinator.relevant_module_ids.return_value = {"m1", "g1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    unique_ids = sorted(s.unique_id for s in added)
    assert unique_ids == [
        "g1_battery",
        "g1_last_communication",
        "m1_last_communication",
        "m1_running_station",
    ]
