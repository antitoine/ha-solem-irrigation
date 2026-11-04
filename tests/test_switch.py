"""Tests for the SOLEM enable switch and the run / set_enabled services."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.entity import EntityCategory

import custom_components.solem_irrigation.switch as switch_module
from custom_components.solem_irrigation.api import (
    SolemConnectionError,
    SolemValidationError,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
    SolemStation,
)
from custom_components.solem_irrigation.schedule import ScheduleError
from custom_components.solem_irrigation.switch import (
    SolemEnableSwitch,
    SolemProgramEnabledSwitch,
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
    coord.async_command_set_status = AsyncMock()
    # No status in the live state: the switch falls back to its assumed value.
    coord.watering_enabled.return_value = None
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
    assert switch.assumed_state is True
    assert switch.unique_id == "m1_enabled"


def test_follows_the_reported_status(coordinator, module):
    """An OFF set from MySOLEM (or by a rain gauge) must show up in HA (#8)."""
    switch = SolemEnableSwitch(coordinator, module)
    coordinator.watering_enabled.return_value = False
    assert switch.is_on is False
    assert switch.assumed_state is False
    coordinator.watering_enabled.return_value = True
    switch._is_on = False  # a stale assumed value never wins over the cloud
    assert switch.is_on is True


async def test_turn_off_disables_permanently(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    await switch.async_turn_off()
    coordinator.async_command_set_status.assert_awaited_once_with(
        module, enabled=False, days=0
    )
    assert switch.is_on is False


async def test_turn_on_enables(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    switch._is_on = False
    await switch.async_turn_on()
    coordinator.async_command_set_status.assert_awaited_once_with(
        module, enabled=True, days=0
    )
    assert switch.is_on is True


async def test_set_enabled_service_with_rain_delay(hass, coordinator, module):
    switch = _switch(coordinator, module, hass)
    await switch.async_set_enabled(enabled=False, days=4)
    coordinator.async_command_set_status.assert_awaited_once_with(
        module, enabled=False, days=4
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


def _runnable_program(**raw) -> SolemProgram:
    return SolemProgram(
        id="p1",
        name="Matin",
        index=1,
        raw={
            "id": "p1",
            "name": "Matin",
            "cycle": 0,
            "weekDays": 127,
            "startTimes": [360] + [-1] * 7,
            "stationsDuration": [600] + [0] * 11,
            **raw,
        },
    )


def _module_with(module_id="m1", programs=None, **kwargs) -> SolemModule:
    base = dict(
        id=module_id,
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[STATION_1],
        programs=[_runnable_program()] if programs is None else programs,
    )
    base.update(kwargs)
    return SolemModule(**base)


async def test_async_setup_entry_adds_controller_and_program_switches(coordinator):
    module = _module_with()
    bluetooth = _module_with(
        "b1",
        type="bl-ip",
        raw={"typeIsWatering": True, "isBluetoothOnly": True},
        programs=[SolemProgram(id="bp", name="Tennis", index=1)],
    )
    pool = _module_with(
        "pool",
        type="LR-PC",
        raw={},
        programs=[SolemProgram(id="x", name="Filtration", index=1)],
    )
    coordinator.modules = {m.id: m for m in (module, bluetooth, pool)}
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    with patch.object(switch_module, "entity_platform") as platform_helper:
        platform = platform_helper.async_get_current_platform.return_value
        await async_setup_entry(MagicMock(), entry, added.extend)

    # A Bluetooth-only controller has no enable switch but keeps its program
    # switches; a pool controller gets neither.
    assert [s.unique_id for s in added] == ["m1_enabled", "m1_prog_p1", "b1_prog_bp"]
    services = {
        call.args[0]: call.kwargs
        for call in platform.async_register_entity_service.call_args_list
    }
    assert set(services) == {
        "set_enabled",
        "run",
        "set_program_schedule",
        "push_programs",
        "create_backup",
    }
    # Aimed at a whole device, an action reaches only the controller's switch.
    assert all(kw["entity_device_classes"] == [None] for kw in services.values())


def test_program_switch_reads_the_program(coordinator):
    module = _module_with()
    coordinator.modules = {module.id: module}
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])

    assert switch.unique_id == "m1_prog_p1"
    assert switch.translation_key == "prog_enabled"
    assert switch.translation_placeholders == {"program": "Matin"}
    assert switch.entity_category is EntityCategory.CONFIG
    assert switch.device_class is SwitchDeviceClass.SWITCH
    assert switch.is_on is True
    assert switch.icon == "mdi:calendar-check"

    module.programs[0].raw["weekDays"] = 0  # parked
    assert switch.is_on is False
    assert switch.icon == "mdi:calendar-remove"


def test_program_switch_is_off_for_a_program_with_nothing_to_run(coordinator):
    module = _module_with(programs=[_runnable_program(startTimes=[-1] * 8)])
    coordinator.modules = {module.id: module}
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])
    assert switch.is_on is False


def test_program_switch_is_off_for_an_unreadable_program(coordinator):
    module = _module_with(programs=[_runnable_program(cycle="junk")])
    coordinator.modules = {module.id: module}
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])
    assert switch.is_on is False


def test_program_switch_unavailable_once_the_program_is_gone(coordinator):
    module = _module_with()
    coordinator.modules = {module.id: module}
    coordinator.last_update_success = True
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])
    assert switch.available is True

    module.programs = []
    assert switch.available is False
    assert switch.is_on is False


async def test_program_switch_toggles_through_the_coordinator(hass, coordinator):
    module = _module_with()
    coordinator.modules = {module.id: module}
    coordinator.async_command_set_program_enabled = AsyncMock()
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])
    switch.hass = hass
    switch.entity_id = "switch.controller_matin_schedule"
    switch.platform = MagicMock()

    await switch.async_turn_off()
    coordinator.async_command_set_program_enabled.assert_awaited_once_with(
        module, "p1", False
    )
    await switch.async_turn_on()
    coordinator.async_command_set_program_enabled.assert_awaited_with(
        module, "p1", True
    )


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (ScheduleError("nothing to run"), ServiceValidationError),
        (ValueError("Unknown program"), ServiceValidationError),
        (SolemValidationError(["empty_stations"]), ServiceValidationError),
        (SolemConnectionError("boom"), HomeAssistantError),
    ],
)
async def test_program_switch_errors(hass, coordinator, error, raised):
    module = _module_with()
    coordinator.modules = {module.id: module}
    coordinator.async_command_set_program_enabled = AsyncMock(side_effect=error)
    switch = SolemProgramEnabledSwitch(coordinator, module, module.programs[0])

    with pytest.raises(raised) as err:
        await switch.async_turn_on()
    assert str(error) in str(err.value)


# -- program-editing services ---------------------------------------------------


async def test_set_program_schedule_routes_the_changes(hass, coordinator, module):
    coordinator.async_command_update_program = AsyncMock()
    coordinator.async_command_set_program_enabled = AsyncMock()
    switch = _switch(coordinator, module, hass)

    await switch.async_set_program_schedule(
        program="Matin", water_budget=80, weekdays=["mon"], push=True
    )

    coordinator.async_command_update_program.assert_awaited_once_with(
        module, "Matin", {"water_budget": 80, "weekdays": ["mon"]}, push=True
    )
    coordinator.async_command_set_program_enabled.assert_not_awaited()


async def test_set_program_schedule_with_enabled_pushes_once(hass, coordinator, module):
    coordinator.async_command_update_program = AsyncMock()
    coordinator.async_command_set_program_enabled = AsyncMock()
    switch = _switch(coordinator, module, hass)

    await switch.async_set_program_schedule(
        program="Matin", water_budget=80, enabled=True, push=True
    )

    # The edit holds its push; the enable that follows arms the one push.
    coordinator.async_command_update_program.assert_awaited_once_with(
        module, "Matin", {"water_budget": 80}, push=False
    )
    coordinator.async_command_set_program_enabled.assert_awaited_once_with(
        module, "Matin", True, push=True
    )


async def test_set_program_schedule_enabled_alone(hass, coordinator, module):
    coordinator.async_command_update_program = AsyncMock()
    coordinator.async_command_set_program_enabled = AsyncMock()
    switch = _switch(coordinator, module, hass)

    await switch.async_set_program_schedule(program="Matin", enabled=False, push=False)

    coordinator.async_command_update_program.assert_not_awaited()
    coordinator.async_command_set_program_enabled.assert_awaited_once_with(
        module, "Matin", False, push=False
    )


async def test_set_program_schedule_needs_something_to_change(
    hass, coordinator, module
):
    switch = _switch(coordinator, module, hass)
    with pytest.raises(ServiceValidationError):
        await switch.async_set_program_schedule(program="Matin", push=True)


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (
            ScheduleError("'windows' is for agricultural controllers"),
            ServiceValidationError,
        ),
        (SolemValidationError(["empty_stations"]), ServiceValidationError),
        (SolemConnectionError("boom"), HomeAssistantError),
        # The coordinator has started the reauth flow; the message gets through.
        (ConfigEntryAuthFailed("Session expired"), ConfigEntryAuthFailed),
    ],
)
async def test_set_program_schedule_errors(hass, coordinator, module, error, raised):
    coordinator.async_command_update_program = AsyncMock(side_effect=error)
    switch = _switch(coordinator, module, hass)
    with pytest.raises(raised) as err:
        await switch.async_set_program_schedule(program="Matin", water_budget=80)
    assert str(error) in str(err.value)


async def test_push_and_backup_services(hass, coordinator, module):
    coordinator.async_command_push_programs = AsyncMock()
    coordinator.async_command_create_backup = AsyncMock()
    switch = _switch(coordinator, module, hass)

    await switch.async_push_programs()
    coordinator.async_command_push_programs.assert_awaited_once_with(module)

    await switch.async_create_backup(name="Before winter")
    coordinator.async_command_create_backup.assert_awaited_once_with(
        module, "Before winter"
    )

    coordinator.async_command_push_programs = AsyncMock(
        side_effect=SolemConnectionError("unreachable")
    )
    with pytest.raises(HomeAssistantError):
        await switch.async_push_programs()


@pytest.mark.parametrize(
    "data",
    [
        {"program": "Matin", "weekdays": "mon"},
        {"program": "Matin", "start_times": "06:00"},
        {"program": "Matin", "durations": {1: "12.5"}},
        {"program": "Matin", "windows": {"start": "06:00", "end": "07:00"}},
    ],
)
def test_set_program_schedule_schema_coerces(data):
    validated = vol.Schema(switch_module._SET_PROGRAM_SCHEDULE_SCHEMA)(data)
    assert validated["push"] is True
    if "weekdays" in data:
        assert validated["weekdays"] == ["mon"]
    if "start_times" in data:
        assert validated["start_times"] == ["06:00"]
    if "durations" in data:
        assert validated["durations"] == {"1": 12.5}
    if "windows" in data:
        assert validated["windows"] == [{"start": "06:00", "end": "07:00"}]


@pytest.mark.parametrize(
    "data",
    [
        {"weekdays": ["mon"]},  # program is required
        {"program": "Matin", "weekdays": ["funday"]},
        {"program": "Matin", "schedule_type": "fortnightly"},
        {"program": "Matin", "every_days": 0},
        {"program": "Matin", "water_budget": 301},
        {"program": "Matin", "windows": [{"start": "06:00"}]},
        {"program": "Matin", "windows": [{"start": "6", "end": "7", "x": 1}]},
        # Non-finite or out-of-range numbers never reach the schedule logic.
        {"program": "Matin", "durations": {"1": "inf"}},
        {"program": "Matin", "durations": {"1": "nan"}},
        {"program": "Matin", "durations": {"1": 721}},
        {"program": "Matin", "durations": {"1": -1}},
        {
            "program": "Matin",
            "windows": [{"start": "6", "end": "7", "soak_on_minutes": "inf"}],
        },
        {
            "program": "Matin",
            "windows": [{"start": "6", "end": "7", "soak_off_minutes": 1441}],
        },
    ],
)
def test_set_program_schedule_schema_rejects(data):
    with pytest.raises(vol.Invalid):
        vol.Schema(switch_module._SET_PROGRAM_SCHEDULE_SCHEMA)(data)
