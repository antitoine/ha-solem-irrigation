"""Tests for the SOLEM binary sensors."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.solem_irrigation.binary_sensor import (
    SolemRainThresholdSensor,
    async_setup_entry,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemRainGauge,
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
    [(0, "none"), ("6", "off_1_day"), (2, 2), ("x", "x"), (None, None)],
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
