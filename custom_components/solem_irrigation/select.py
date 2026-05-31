"""Select platform for SOLEM irrigation.

A "Run program" select per controller: its options are the stored programs, and
picking one starts that program now. It is a momentary trigger, not a state —
``current_option`` is always reset to a neutral placeholder, so picking the same
program twice in a row fires both times (a select never re-fires the option
already shown). Stopping / running a station is done with the station valves;
running a program for a controller is also available via the
``solem_irrigation.run`` service.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator, SolemModule
from .entity import SolemModuleEntity

# Neutral placeholder shown when no program is being launched. Kept as a fixed,
# language-neutral value (program names are dynamic and can't be translated).
OPTION_NONE = "—"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a "Run program" select per controller that has programs."""
    coordinator = entry.runtime_data
    async_add_entities(
        SolemRunProgramSelect(coordinator, module)
        for module in coordinator.modules.values()
        if module.is_controller and module.programs
    )


class SolemRunProgramSelect(SolemModuleEntity, SelectEntity):
    """Pick a stored program to run now."""

    _attr_translation_key = "run_program"
    _attr_icon = "mdi:play-box-multiple"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the run-program select."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_run_program"
        self._attr_current_option = OPTION_NONE

    @property
    def options(self) -> list[str]:
        """The placeholder followed by each stored program's name."""
        return [OPTION_NONE, *(program.name for program in self._module.programs)]

    async def async_select_option(self, option: str) -> None:
        """Start the chosen program, then reset to the placeholder."""
        if option == OPTION_NONE:
            return
        program = self._module.find_program(option)
        if program is None:
            raise ServiceValidationError(f"Unknown program: {option}")
        await self.coordinator.async_command_run_program(self._module, program)
        # Reset so the same program can be picked again next time.
        self._attr_current_option = OPTION_NONE
        self.async_write_ha_state()
