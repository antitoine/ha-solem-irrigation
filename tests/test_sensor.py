"""Tests for the SOLEM sensors."""

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.util import dt as dt_util

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemFlowReading,
    SolemModule,
    SolemRainGauge,
    SolemRainReading,
    SolemStation,
    SolemTurbine,
    SolemTurbineReading,
)
from custom_components.solem_irrigation.sensor import (
    SolemBatterySensor,
    SolemLastCommunicationSensor,
    SolemRainfallSensor,
    SolemRunningStationSensor,
    SolemTurbineFlowRateSensor,
    SolemTurbineWaterUsedTodaySensor,
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


def _ag_module() -> SolemModule:
    return SolemModule(
        id="ag1",
        name="Field",
        serial="AGSER",
        type="lr-ag",
        display_type="lr-ag",
        raw={"typeIsWatering": False, "typeIsAgricultural": True},
        stations=[STATION_1, STATION_2],
        programs=[],
    )


def test_ag_running_stations_are_joined(coordinator):
    """An LR-AG can water several stations at once."""
    module = _ag_module()
    coordinator.modules = {module.id: module}
    coordinator.ag_running_outputs.return_value = {
        1: {"state": 1, "time": "00:10"},
        2: {"state": 2, "time": "01:05"},
        7: {"state": 2, "time": "00:00"},
    }
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value == "Pelouse 1, Potager, Station 7"
    # One entry per station, so the times stay aligned with the names.
    assert sensor.extra_state_attributes == {"time_remaining": "00:10, 01:05, --:--"}


def test_ag_running_stations_keep_their_times_aligned_before_the_first_poll(
    coordinator,
):
    """After the optimistic patch a station has no time yet: it must not shift."""
    module = _ag_module()
    coordinator.modules = {module.id: module}
    coordinator.ag_running_outputs.return_value = {
        1: {"state": 1, "time": "00:00"},
        2: {"state": 1, "time": "00:40"},
    }
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value == "Pelouse 1, Potager"
    assert sensor.extra_state_attributes == {"time_remaining": "--:--, 00:40"}


def test_ag_running_stations_without_any_time_report_none(coordinator):
    module = _ag_module()
    coordinator.modules = {module.id: module}
    coordinator.ag_running_outputs.return_value = {1: {"state": 1}}
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.extra_state_attributes is None


def test_ag_running_station_idle(coordinator):
    module = _ag_module()
    coordinator.modules = {module.id: module}
    coordinator.ag_running_outputs.return_value = {}
    sensor = SolemRunningStationSensor(coordinator, module)
    assert sensor.native_value is None
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


def test_last_communication_falls_back_to_seen_at(coordinator, module):
    """A module with no LoRa radio reports ``seenAt`` instead (issue #7).

    The LR-MB gateway and WiFi controllers (SMART-IS) talk to the cloud
    directly and never emit ``lastRadioCommunication``, which used to leave
    their sensor permanently unknown.
    """
    coordinator.module_state.return_value = {}
    module.raw["seenAt"] = "2026-09-21T06:00:15.405Z"
    sensor = SolemLastCommunicationSensor(coordinator, module)
    assert sensor.native_value == dt_util.parse_datetime("2026-09-21T06:00:15.405Z")


def test_last_communication_prefers_radio_over_seen_at(coordinator, module):
    """A LoRa module keeps the radio timestamp even if ``seenAt`` is present."""
    coordinator.module_state.return_value = {
        "lastRadioCommunication": "2026-05-31T10:00:00+00:00"
    }
    module.raw["seenAt"] = "2026-09-21T06:00:15.405Z"
    sensor = SolemLastCommunicationSensor(coordinator, module)
    assert sensor.native_value == dt_util.parse_datetime("2026-05-31T10:00:00+00:00")


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
        SolemTurbineWaterUsedTodaySensor(coordinator, module, TURBINE).unique_id,
        SolemTurbineFlowRateSensor(coordinator, module, TURBINE).unique_id,
    ]
    for unique_id in ids:
        assert "_station_" not in unique_id
        assert "_program_" not in unique_id
        assert not unique_id.endswith(("_run_duration", "_manual_run"))


# -- turbine ------------------------------------------------------------------

TURBINE = SolemTurbine(
    id="t1", name="Turbine", index=1, unit=1, interval=1, raw={"id": "t1"}
)
TURBINE_READING = SolemTurbineReading(
    day=date(2026, 10, 11),
    volume_today=57.75,
    rate=6.43,
    timestamp=dt_util.parse_datetime("2026-10-11T17:42:00+00:00"),
)


def test_turbine_water_used_today(coordinator, module):
    coordinator.turbine_reading.return_value = TURBINE_READING
    coordinator.input_record.return_value = {"hasFaultyProbe": False}
    sensor = SolemTurbineWaterUsedTodaySensor(coordinator, module, TURBINE)

    assert sensor.unique_id == "m1_water_used_today_t1"
    assert sensor.translation_key == "water_used_today"
    assert sensor.translation_placeholders == {"meter": "Turbine"}
    assert sensor.native_value == 57.75
    assert sensor.device_class == SensorDeviceClass.WATER
    # A daily total, not a lifetime counter: ``last_reset`` marks each day.
    assert sensor.state_class == SensorStateClass.TOTAL
    assert sensor.last_reset == datetime(
        2026, 10, 11, tzinfo=dt_util.get_default_time_zone()
    )
    assert sensor.native_unit_of_measurement == UnitOfVolume.LITERS
    assert sensor.extra_state_attributes == {
        "turbine_id": "t1",
        "last_measurement": "2026-10-11T17:42:00+00:00",
        "faulty_probe": False,
    }


def test_turbine_flow_rate(coordinator, module):
    coordinator.turbine_reading.return_value = TURBINE_READING
    coordinator.input_record.return_value = None
    sensor = SolemTurbineFlowRateSensor(coordinator, module, TURBINE)

    assert sensor.unique_id == "m1_water_flow_rate_t1"
    assert sensor.translation_key == "water_flow_rate"
    assert sensor.native_value == 6.43
    assert sensor.device_class == SensorDeviceClass.VOLUME_FLOW_RATE
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.native_unit_of_measurement == UnitOfVolumeFlowRate.LITERS_PER_MINUTE
    assert sensor.extra_state_attributes["faulty_probe"] is None


def test_turbine_sensors_unknown_before_the_first_window(coordinator, module):
    coordinator.turbine_reading.return_value = None
    used = SolemTurbineWaterUsedTodaySensor(coordinator, module, TURBINE)
    rate = SolemTurbineFlowRateSensor(coordinator, module, TURBINE)
    assert used.native_value is None
    assert used.last_reset is None
    assert used.extra_state_attributes is None
    assert rate.native_value is None


def test_turbine_without_a_tick_yet_has_no_measurement_time(coordinator, module):
    coordinator.turbine_reading.return_value = SolemTurbineReading(
        day=date(2026, 10, 11), volume_today=0.0, rate=0.0, timestamp=None
    )
    coordinator.input_record.return_value = None
    sensor = SolemTurbineFlowRateSensor(coordinator, module, TURBINE)
    assert sensor.native_value == 0.0
    assert sensor.extra_state_attributes["last_measurement"] is None


def test_turbine_sensors_in_gallons(coordinator, module):
    turbine = SolemTurbine(
        id="t1", name="Turbine", index=1, unit=3, interval=1, raw={"id": "t1"}
    )
    used = SolemTurbineWaterUsedTodaySensor(coordinator, module, turbine)
    rate = SolemTurbineFlowRateSensor(coordinator, module, turbine)
    assert used.native_unit_of_measurement == UnitOfVolume.GALLONS
    assert rate.native_unit_of_measurement == UnitOfVolumeFlowRate.GALLONS_PER_MINUTE


async def test_async_setup_entry_adds_turbine_sensors(coordinator, module):
    module.turbines = [TURBINE]
    coordinator.relevant_module_ids.return_value = {"m1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert {"m1_water_used_today_t1", "m1_water_flow_rate_t1"} <= {
        s.unique_id for s in added
    }


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


# -- rain gauge ---------------------------------------------------------------

GAUGE = SolemRainGauge(
    id="r1", name="Pluvio Meter", index=1, expression="x*0.2794", raw={"id": "r1"}
)


def test_rainfall_value_and_metadata(coordinator, module):
    coordinator.rain_reading.return_value = SolemRainReading(
        total=27.94,
        raw_total=100,
        timestamp=dt_util.parse_datetime("2026-09-30T14:00:00+00:00"),
    )
    coordinator.input_record.return_value = {"hasFaultyProbe": False}
    sensor = SolemRainfallSensor(coordinator, module, GAUGE)

    assert sensor.unique_id == "m1_rainfall_r1"
    assert sensor.native_value == 27.94
    assert sensor.device_class == SensorDeviceClass.PRECIPITATION
    assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
    assert sensor.native_unit_of_measurement == UnitOfPrecipitationDepth.MILLIMETERS
    assert sensor.translation_placeholders == {"gauge": "Pluvio Meter"}
    assert sensor.extra_state_attributes == {
        "gauge_id": "r1",
        "raw_value": 100,
        "last_measurement": "2026-09-30T14:00:00+00:00",
        "faulty_probe": False,
    }


def test_rainfall_unknown_before_the_first_tick(coordinator, module):
    coordinator.rain_reading.return_value = None
    sensor = SolemRainfallSensor(coordinator, module, GAUGE)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


async def test_async_setup_entry_adds_rainfall_sensor(coordinator, module):
    module.rain_gauges = [GAUGE]
    coordinator.relevant_module_ids.return_value = {"m1"}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert "m1_rainfall_r1" in {s.unique_id for s in added}
