"""Button platform for SOLEM irrigation.

A single *Stop watering* button per controller sends a global stop. It stops
**anything** running — a manual station run or a program — and is always
available, unlike closing a station valve (which only helps once a specific
station is actively watering, and not at all right after a program starts).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator, SolemModule
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a global Stop button per controller."""
    coordinator = entry.runtime_data
    async_add_entities(
        SolemStopButton(coordinator, module)
        for module in coordinator.modules.values()
        if module.is_controller
    )


class SolemStopButton(SolemModuleEntity, ButtonEntity):
    """Stop any running watering — manual station or program (global)."""

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
        await self.coordinator.async_command_stop(self._module)
