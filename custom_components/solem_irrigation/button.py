"""Button platform for SOLEM irrigation.

* One button per stored program (run it now).
* One stop button per controller (global stop).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
)
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SOLEM buttons."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    for module in coordinator.modules.values():
        if not module.is_controller:
            continue
        entities.append(SolemStopButton(coordinator, module))
        entities.extend(
            SolemProgramButton(coordinator, module, program)
            for program in module.programs
        )
    async_add_entities(entities)


class SolemStopButton(SolemModuleEntity, ButtonEntity):
    """Stop any running watering (global)."""

    _attr_translation_key = "stop"
    _attr_icon = "mdi:stop"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the stop button."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_stop"

    async def async_press(self) -> None:
        """Send a global stop."""
        await self.coordinator.client.async_stop(self._serial)
        self.coordinator.apply_optimistic_running_station(self._module_id, 0)
        self.coordinator.async_schedule_refresh()


class SolemProgramButton(SolemModuleEntity, ButtonEntity):
    """Run a stored program now."""

    _attr_icon = "mdi:play"

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        program: SolemProgram,
    ) -> None:
        """Initialise the program button."""
        super().__init__(coordinator, module)
        self._program = program
        self._attr_unique_id = f"{module.id}_program_{program.id}"
        self._attr_name = f"Run {program.name}"

    async def async_press(self) -> None:
        """Start the program."""
        await self.coordinator.client.async_run_program(
            self._serial, self._program.id
        )
        self.coordinator.async_schedule_refresh()
