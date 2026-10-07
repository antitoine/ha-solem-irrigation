"""Tests for the SOLEM run-duration and rain-delay numbers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemStation,
)
from custom_components.solem_irrigation.number import (
    SolemRainDelayNumber,
    SolemRunDurationNumber,
    async_setup_entry,
)

STATION_1 = SolemStation(id="s1", name="Pelouse 1", index=1)


@pytest.fixture
def module() -> SolemModule:
    return SolemModule(
        id="m1",
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[STATION_1],
        programs=[],
    )


@pytest.fixture
def coordinator(module) -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    coord.client = AsyncMock()
    coord.async_command_set_status = AsyncMock()
    # No status in the live state: the number falls back to its assumed value.
    coord.rain_delay_days.return_value = None
    return coord


def test_run_duration_native_value(coordinator, module):
    coordinator.get_run_minutes.return_value = 7
    number = SolemRunDurationNumber(coordinator, module, STATION_1)
    assert number.native_value == 7.0
    assert number.unique_id == "m1_run_duration_s1"


async def test_run_duration_set_value(coordinator, module):
    coordinator.async_set_run_minutes = AsyncMock()
    number = SolemRunDurationNumber(coordinator, module, STATION_1)
    await number.async_set_native_value(15.0)
    coordinator.async_set_run_minutes.assert_awaited_once_with("s1", 15)


async def test_rain_delay_disables_for_days(hass, coordinator, module):
    number = SolemRainDelayNumber(coordinator, module)
    number.hass = hass
    number.entity_id = "number.rain_delay"
    number.platform = MagicMock()

    await number.async_set_native_value(3)

    coordinator.async_command_set_status.assert_awaited_once_with(
        module, enabled=False, days=3
    )
    assert number.native_value == 3.0
    assert number.assumed_state is True


async def test_rain_delay_zero_re_enables(hass, coordinator, module):
    number = SolemRainDelayNumber(coordinator, module)
    number.hass = hass
    number.entity_id = "number.rain_delay"
    number.platform = MagicMock()

    await number.async_set_native_value(0)

    coordinator.async_command_set_status.assert_awaited_once_with(
        module, enabled=True, days=0
    )
    assert number.native_value == 0.0


def test_rain_delay_follows_the_reported_days(coordinator, module):
    """The cloud's days left win, whoever set the delay."""
    number = SolemRainDelayNumber(coordinator, module)
    number._value = 5.0
    coordinator.rain_delay_days.return_value = 1
    assert number.native_value == 1.0
    assert number.assumed_state is False


async def test_async_setup_entry_rain_delay_plus_per_station(coordinator, module):
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    unique_ids = sorted(n.unique_id for n in added)
    assert unique_ids == ["m1_rain_delay", "m1_run_duration_s1"]
