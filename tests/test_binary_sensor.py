"""Tests for the SOLEM binary sensors."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.solem_irrigation.binary_sensor import (
    SolemRainSensorEntity,
    SolemRainThresholdSensor,
    async_setup_entry,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemRainGauge,
    SolemRainSensor,
    SolemRainSensorReading,
)

SNAPSHOT = {
    "id": "r1",
    "isLastMeasureBeyondThresholds": False,
    "highThreshold": 2,
    "actionWhenHighDailyThresholdExceeded": 6,
}
GAUGE = SolemRainGauge(
    id="r1", name="Pluvio Meter", index=1, expression="x*0.2794", raw=SNAPSHOT
)


@pytest.fixture
def module() -> SolemModule:
    return SolemModule(
        id="ms",
        name="Rain Sensor",
        serial="SER",
        type="lr-ms",
        display_type="lr-ms",
        raw={"typeIsSensor": True},
        rain_gauges=[GAUGE],
    )


@pytest.fixture
def coordinator(module) -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    coord.input_record.return_value = None
    coord.rain_sensor_reading.return_value = None
    return coord


def test_threshold_reads_the_live_flag_over_the_setup_snapshot(coordinator, module):
    sensor = SolemRainThresholdSensor(coordinator, module, GAUGE)
    assert sensor.unique_id == "ms_rain_threshold_r1"
    assert sensor.translation_placeholders == {"gauge": "Pluvio Meter"}
    # Before the first poll, the module page's copy stands in.
    assert sensor.is_on is False

    coordinator.input_record.return_value = {
        **SNAPSHOT,
        "isLastMeasureBeyondThresholds": True,
    }
    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {
        "daily_threshold": 2,
        # 6 is "OFF 1 day" in MySOLEM, as the #8 reporter read off its screen.
        "threshold_action": "off_1_day",
        "last_threshold_alert": None,
    }


def test_threshold_attributes_fall_back_to_the_setup_snapshot(coordinator, module):
    """The polled record may lack a setting the module page carried."""
    coordinator.input_record.return_value = {
        "id": "r1",
        "isLastMeasureBeyondThresholds": True,
        "lastHighThresholdAlertSent": "2026-10-07T04:46:06.186Z",
    }
    attributes = SolemRainThresholdSensor(
        coordinator, module, GAUGE
    ).extra_state_attributes
    assert attributes["daily_threshold"] == 2
    assert attributes["threshold_action"] == "off_1_day"
    assert attributes["last_threshold_alert"] == datetime(
        2026, 10, 7, 4, 46, 6, 186000, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, "none"),
        ("6", "off_1_day"),
        (7, "off_2_days"),
        ("8", "off_3_days"),
        (2, 2),
        ("x", "x"),
        (None, None),
    ],
)
def test_threshold_action_names_only_confirmed_codes(
    coordinator, module, code, expected
):
    coordinator.input_record.return_value = {
        **SNAPSHOT,
        "actionWhenHighDailyThresholdExceeded": code,
    }
    sensor = SolemRainThresholdSensor(coordinator, module, GAUGE)
    assert sensor.extra_state_attributes["threshold_action"] == expected


def test_threshold_unknown_when_solem_reports_no_flag(coordinator, module):
    gauge = SolemRainGauge(
        id="r1", name="Pluvio Meter", index=1, expression="x*0.2794", raw={"id": "r1"}
    )
    module.rain_gauges = [gauge]
    coordinator.input_record.return_value = {"id": "r1"}
    assert SolemRainThresholdSensor(coordinator, module, gauge).is_on is None


async def test_async_setup_entry_only_for_relevant_modules(coordinator, module):
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    coordinator.relevant_module_ids.return_value = set()
    await async_setup_entry(MagicMock(), entry, added.extend)
    assert added == []

    coordinator.relevant_module_ids.return_value = {"ms"}
    await async_setup_entry(MagicMock(), entry, added.extend)
    assert [s.unique_id for s in added] == ["ms_rain_threshold_r1"]


RAIN_SENSOR = SolemRainSensor(id="c1", name="Capteur de pluie", index=2, raw={})


def test_rain_sensor_is_wet_while_its_last_tick_is_1(coordinator, module):
    sensor = SolemRainSensorEntity(coordinator, module, RAIN_SENSOR)
    assert sensor.unique_id == "ms_rain_sensor_c1"
    assert sensor.device_class is BinarySensorDeviceClass.MOISTURE
    # Named after the input alone, so "Capteur de pluie" is not said twice.
    assert sensor.translation_placeholders == {"sensor": "Capteur de pluie"}
    # Unknown, not dry, until the sensor has reported.
    assert sensor.is_on is None
    assert sensor.extra_state_attributes is None

    measured = datetime(2026, 10, 7, 18, 55, tzinfo=UTC)
    coordinator.rain_sensor_reading.return_value = SolemRainSensorReading(
        wet=True, timestamp=measured
    )
    assert sensor.is_on is True
    assert sensor.extra_state_attributes == {
        "last_measurement": "2026-10-07T18:55:00+00:00"
    }

    coordinator.rain_sensor_reading.return_value = SolemRainSensorReading(
        wet=False, timestamp=measured
    )
    assert sensor.is_on is False


async def test_async_setup_entry_adds_rain_sensors(coordinator, module):
    module.rain_sensors = [RAIN_SENSOR]
    entry = MagicMock()
    entry.runtime_data = coordinator
    coordinator.relevant_module_ids.return_value = {"ms"}
    added: list = []
    await async_setup_entry(MagicMock(), entry, added.extend)
    assert [s.unique_id for s in added] == [
        "ms_rain_threshold_r1",
        "ms_rain_sensor_c1",
    ]
