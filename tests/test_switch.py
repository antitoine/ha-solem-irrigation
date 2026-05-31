"""Tests for the SOLEM enable switch and the run / set_enabled services."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError

import custom_components.solem_irrigation.switch as switch_module
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
    SolemStation,
)
from custom_components.solem_irrigation.switch import (
    SolemEnableSwitch,
    async_setup_entry,
)

STATION_1 = SolemStation(id="s1", name="Pelouse 1", index=1)
PROGRAM_1 = SolemProgram(id="p1", name="Matin", index=1)


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
        programs=[PROGRAM_1],
    )


@pytest.fixture
def coordinator(module) -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    coord.client = AsyncMock()
    return coord


def _switch(coordinator, module, hass):
    switch = SolemEnableSwitch(coordinator, module)
    switch.hass = hass
    switch.entity_id = "switch.irrigation_enabled"
    switch.platform = MagicMock()
    return switch


def test_defaults_on(coordinator, module):
    switch = SolemEnableSwitch(coordinator, module)
    assert switch.is_on is True
    assert switch.unique_id == "m1_enabled"


async def test_turn_off_disables_permanently(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    await switch.async_turn_off()
    coordinator.client.async_set_status.assert_awaited_once_with(
        "SER1", enabled=False, days=0
    )
    assert switch.is_on is False


async def test_turn_on_enables(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    switch._is_on = False
    await switch.async_turn_on()
    coordinator.client.async_set_status.assert_awaited_once_with(
        "SER1", enabled=True, days=0
    )
    assert switch.is_on is True


async def test_set_enabled_service_with_rain_delay(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    await switch.async_set_enabled(enabled=False, days=4)
    coordinator.client.async_set_status.assert_awaited_once_with(
        "SER1", enabled=False, days=4
    )
    assert switch.is_on is False


# -- run service --------------------------------------------------------------


async def test_run_stop(coordinator, module):
    coordinator.async_command_stop = AsyncMock()
    switch = SolemEnableSwitch(coordinator, module)
    await switch.async_handle_run(mode="stop")
    coordinator.async_command_stop.assert_awaited_once_with(module)


async def test_run_program(coordinator, module):
    coordinator.async_command_run_program = AsyncMock()
    switch = SolemEnableSwitch(coordinator, module)
    await switch.async_handle_run(mode="program", program="Matin")
    ran_module, ran_program = coordinator.async_command_run_program.call_args[0]
    assert ran_module is module
    assert ran_program.id == "p1"


async def test_run_program_requires_program(coordinator, module):
    switch = SolemEnableSwitch(coordinator, module)
    with pytest.raises(ServiceValidationError):
        await switch.async_handle_run(mode="program")


async def test_run_program_unknown(coordinator, module):
    switch = SolemEnableSwitch(coordinator, module)
    with pytest.raises(ServiceValidationError):
        await switch.async_handle_run(mode="program", program="ghost")


async def test_run_station_default_duration(coordinator, module):
    coordinator.get_run_minutes.return_value = 5
    coordinator.async_command_run_station = AsyncMock()
    switch = SolemEnableSwitch(coordinator, module)
    await switch.async_handle_run(mode="station", station="Pelouse 1")
    coordinator.async_command_run_station.assert_awaited_once_with(module, STATION_1, 5)


async def test_run_station_explicit_duration_is_remembered(coordinator, module):
    coordinator.async_set_run_minutes = AsyncMock()
    coordinator.async_command_run_station = AsyncMock()
    switch = SolemEnableSwitch(coordinator, module)
    await switch.async_handle_run(mode="station", station="Pelouse 1", duration=20)
    coordinator.async_set_run_minutes.assert_awaited_once_with("s1", 20)
    coordinator.async_command_run_station.assert_awaited_once_with(
        module, STATION_1, 20
    )


async def test_run_station_requires_station(coordinator, module):
    switch = SolemEnableSwitch(coordinator, module)
    with pytest.raises(ServiceValidationError):
        await switch.async_handle_run(mode="station")


async def test_run_station_unknown(coordinator, module):
    switch = SolemEnableSwitch(coordinator, module)
    with pytest.raises(ServiceValidationError):
        await switch.async_handle_run(mode="station", station="ghost")


async def test_async_setup_entry_one_switch_per_controller(coordinator, module):
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

    # The platform also registers the two entity services; give it a platform.
    with patch.object(switch_module, "entity_platform") as platform_helper:
        platform_helper.async_get_current_platform.return_value = MagicMock()
        await async_setup_entry(MagicMock(), entry, added.extend)

    assert [s.unique_id for s in added] == ["m1_enabled"]
