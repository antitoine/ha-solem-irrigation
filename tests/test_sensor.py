"""Tests for the SOLEM sensors."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfVolume, UnitOfVolumeFlowRate
from homeassistant.util import dt as dt_util

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemFlowReading,
    SolemModule,
    SolemStation,
)
from custom_components.solem_irrigation.sensor import (
    SolemBatterySensor,
    SolemLastCommunicationSensor,
    SolemRunningStationSensor,
    SolemWaterFlowRateSensor,
    SolemWaterUsedSensor,
    async_setup_entry,
)

STATION_1 = SolemStation(id="s1", name="Pelouse 1", index=1)
STATION_2 = SolemStation(id="s2", name="Potager", index=2)

FLOW_RECORD = {
    "id": "i1",
    "interval": 1,
    "expression": "x/0.1",
    "hasFaultyProbe": False,
    "isLastMeasureBeyondThresholds": False,
    "lowThreshold": 1500,
    "highThreshold": 2500,
    "settings": [
        {"nominalDebit": 1, "leakAlertVolume": 300},
        {"nominalDebit": 2, "leakAlertVolume": 300},
    ],
}


def _meter(unit: int = 1) -> SolemFlowMeter:
    return SolemFlowMeter(
        id="i1",
        name="Flowmeter",
        index=1,
        unit=unit,
        expression="x/0.1",
        interval=1,
        raw=dict(FLOW_RECORD),
    )


def _reading(volume: float = 3970.0, rate: float | None = 10.0) -> SolemFlowReading:
    return SolemFlowReading(
        volume=volume,
        raw_volume=volume / 10,
        timestamp=dt_util.parse_datetime("2026-07-30T17:48:00+00:00"),
        rate=rate,
        record=dict(FLOW_RECORD),
    )


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


def test_battery_zero_is_still_reported(coordinator, module):
    """On a battery module a level of 0 is a reading, not an absence."""
    module.raw["battery"] = 0
    sensor = SolemBatterySensor(coordinator, module)
    assert sensor.native_value == 0


async def test_no_battery_sensor_for_a_mains_powered_module(coordinator, module):
    """SOLEM reports battery 0 on AC-powered modules; that is not a reading.

    Observed on an LR-IS: ``battery: 0``, ``isBattery: False``,
    ``getPowerSourceTypeName: "AC 24V"``.
    """
    module.raw.update({"battery": 0, "isBattery": False})
    coordinator.modules = {module.id: module}
    coordinator.relevant_module_ids.return_value = {"m1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert not any(s.unique_id.endswith("_battery") for s in added)


async def test_battery_sensor_for_a_battery_powered_module(coordinator, module):
    """A module SOLEM flags as battery powered gets the sensor, even at 0."""
    module.raw.update({"battery": 0, "isBattery": True})
    coordinator.modules = {module.id: module}
    coordinator.relevant_module_ids.return_value = {"m1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert any(s.unique_id == "m1_battery" for s in added)


# -- flow meter ---------------------------------------------------------------


def test_water_used_value_and_metadata(coordinator, module):
    coordinator.flow_reading.return_value = _reading()
    sensor = SolemWaterUsedSensor(coordinator, module, _meter())
    assert sensor.native_value == 3970.0
    assert sensor.unique_id == "m1_water_used_i1"
    assert sensor.device_class is SensorDeviceClass.WATER
    assert sensor.state_class is SensorStateClass.TOTAL_INCREASING
    assert sensor.native_unit_of_measurement == UnitOfVolume.LITERS
    assert sensor.translation_placeholders == {"meter": "Flowmeter"}


def test_water_used_zero_is_a_reading(coordinator, module):
    """A freshly installed meter legitimately reads 0."""
    coordinator.flow_reading.return_value = _reading(volume=0.0)
    sensor = SolemWaterUsedSensor(coordinator, module, _meter())
    assert sensor.native_value == 0.0


def test_water_used_unknown_before_the_first_tick(coordinator, module):
    coordinator.flow_reading.return_value = None
    sensor = SolemWaterUsedSensor(coordinator, module, _meter())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_water_used_in_gallons(coordinator, module):
    sensor = SolemWaterUsedSensor(coordinator, module, _meter(unit=3))
    assert sensor.native_unit_of_measurement == UnitOfVolume.GALLONS


def test_water_used_attributes(coordinator, module):
    coordinator.flow_reading.return_value = _reading()
    sensor = SolemWaterUsedSensor(coordinator, module, _meter())
    attributes = sensor.extra_state_attributes
    assert attributes["meter_id"] == "i1"
    assert attributes["raw_value"] == 397.0
    assert attributes["last_measurement"] == "2026-07-30T17:48:00+00:00"
    assert attributes["expression"] == "x/0.1"
    assert attributes["faulty_probe"] is False
    assert attributes["beyond_thresholds"] is False
    assert attributes["low_threshold"] == 1500
    assert attributes["leak_alert_volume"] == 300
    # ``settings`` is positional, one entry per station in index order.
    assert attributes["nominal_flow"] == {"Pelouse 1": 1, "Potager": 2}


def test_water_used_attributes_without_station_settings(coordinator, module):
    coordinator.flow_reading.return_value = SolemFlowReading(
        volume=1.0,
        raw_volume=0.1,
        timestamp=dt_util.parse_datetime("2026-07-30T17:48:00+00:00"),
        rate=None,
        record={"id": "i1"},
    )
    sensor = SolemWaterUsedSensor(coordinator, module, _meter())
    attributes = sensor.extra_state_attributes
    assert "nominal_flow" not in attributes
    assert attributes["low_threshold"] is None


def test_water_flow_rate_value_and_metadata(coordinator, module):
    coordinator.flow_reading.return_value = _reading()
    sensor = SolemWaterFlowRateSensor(coordinator, module, _meter())
    assert sensor.native_value == 10.0
    assert sensor.unique_id == "m1_water_flow_rate_i1"
    assert sensor.device_class is SensorDeviceClass.VOLUME_FLOW_RATE
    assert sensor.state_class is SensorStateClass.MEASUREMENT
    assert sensor.native_unit_of_measurement == UnitOfVolumeFlowRate.LITERS_PER_MINUTE


def test_water_flow_rate_idle(coordinator, module):
    coordinator.flow_reading.return_value = _reading(rate=0.0)
    sensor = SolemWaterFlowRateSensor(coordinator, module, _meter())
    assert sensor.native_value == 0.0


def test_water_flow_rate_undeterminable(coordinator, module):
    """An indeterminable rate stays unknown rather than claiming zero."""
    coordinator.flow_reading.return_value = _reading(rate=None)
    sensor = SolemWaterFlowRateSensor(coordinator, module, _meter())
    assert sensor.native_value is None


def test_water_flow_rate_in_gallons(coordinator, module):
    sensor = SolemWaterFlowRateSensor(coordinator, module, _meter(unit=3))
    assert sensor.native_unit_of_measurement == UnitOfVolumeFlowRate.GALLONS_PER_MINUTE


def test_flow_sensor_unique_ids_survive_legacy_pruning(coordinator, module):
    """__init__ prunes ids containing _station_/_program_ or ending _run_duration."""
    ids = [
        SolemWaterUsedSensor(coordinator, module, _meter()).unique_id,
        SolemWaterFlowRateSensor(coordinator, module, _meter()).unique_id,
    ]
    for unique_id in ids:
        assert "_station_" not in unique_id
        assert "_program_" not in unique_id
        assert not unique_id.endswith(("_run_duration", "_manual_run"))


# -- setup --------------------------------------------------------------------


async def test_async_setup_entry(coordinator, module):
    """Controllers get running-station + last-comm; gateways only last-comm."""
    gateway = SolemModule(
        id="g1",
        name="Gateway",
        serial="SERG",
        type="LR-MB",
        display_type="LR-MB",
        raw={"battery": 4, "isBattery": True},
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


async def test_async_setup_entry_adds_flow_meter_sensors(coordinator, module):
    """A module carrying a meter gains a total and a rate sensor."""
    module.flow_meters = [_meter()]
    coordinator.modules = {module.id: module}
    coordinator.relevant_module_ids.return_value = {"m1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert sorted(s.unique_id for s in added) == [
        "m1_last_communication",
        "m1_running_station",
        "m1_water_flow_rate_i1",
        "m1_water_used_i1",
    ]
