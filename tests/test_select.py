"""Tests for the SOLEM "Run program" select entity."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
    SolemStation,
)
from custom_components.solem_irrigation.select import (
    OPTION_NONE,
    SolemRunProgramSelect,
    async_setup_entry,
)


@pytest.fixture
def module() -> SolemModule:
    """A controller with two stored programs."""
    return SolemModule(
        id="m1",
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[SolemStation(id="s1", name="Pelouse 1", index=1)],
        programs=[
            SolemProgram(id="p1", name="Matin", index=1),
            SolemProgram(id="p2", name="Soir", index=2),
        ],
    )


@pytest.fixture
def coordinator(module: SolemModule) -> MagicMock:
    """A mock coordinator exposing the module."""
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.modules = {module.id: module}
    coord.async_command_run_program = AsyncMock()
    return coord


def test_options_and_initial_option(coordinator, module):
    """Options are the placeholder followed by each program name."""
    entity = SolemRunProgramSelect(coordinator, module)
    assert entity.options == [OPTION_NONE, "Matin", "Soir"]
    assert entity.current_option == OPTION_NONE
    assert entity.unique_id == "m1_run_program"


async def test_select_runs_program_and_resets(hass, coordinator, module):
    """Selecting a program runs it and resets to the placeholder."""
    entity = SolemRunProgramSelect(coordinator, module)
    entity.hass = hass
    entity.entity_id = "select.run_program"
    entity.platform = MagicMock()

    await entity.async_select_option("Soir")

    coordinator.async_command_run_program.assert_awaited_once()
    ran_module, ran_program = coordinator.async_command_run_program.call_args[0]
    assert ran_module is module
    assert ran_program.id == "p2"
    # Reset so the same program can be picked again next time.
    assert entity.current_option == OPTION_NONE


async def test_select_placeholder_is_noop(coordinator, module):
    """Selecting the neutral placeholder does nothing."""
    entity = SolemRunProgramSelect(coordinator, module)
    await entity.async_select_option(OPTION_NONE)
    coordinator.async_command_run_program.assert_not_called()


async def test_select_unknown_program_raises(coordinator, module):
    """An unknown program name raises a validation error."""
    entity = SolemRunProgramSelect(coordinator, module)
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("Does not exist")
    coordinator.async_command_run_program.assert_not_called()


async def test_async_setup_entry_only_controllers_with_programs(coordinator, module):
    """Only watering controllers that have programs get a select."""
    pool = SolemModule(
        id="pool",
        name="Pool",
        serial="SER2",
        type="LR-PC",
        display_type="LR-PC",
        raw={},  # not typeIsWatering -> not a controller
        stations=[SolemStation(id="x", name="x", index=1)],
        programs=[SolemProgram(id="px", name="px", index=1)],
    )
    no_programs = SolemModule(
        id="m2",
        name="Controller 2",
        serial="SER3",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[SolemStation(id="s2", name="s2", index=1)],
        programs=[],
    )
    coordinator.modules = {
        module.id: module,
        pool.id: pool,
        no_programs.id: no_programs,
    }
    entry = MagicMock()
    entry.runtime_data = coordinator
    added: list = []

    await async_setup_entry(MagicMock(), entry, added.extend)

    assert [e.unique_id for e in added] == ["m1_run_program"]
