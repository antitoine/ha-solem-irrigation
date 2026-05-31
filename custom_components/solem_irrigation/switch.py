"""Switch platform for SOLEM irrigation.

A single master switch per controller enables / disables it. Turning it off
disables permanently; the ``solem_irrigation.set_enabled`` service additionally
accepts ``days`` to disable for a period (the rain-delay path).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    ATTR_DAYS,
    ATTR_ENABLED,
    MAX_RAIN_DELAY_DAYS,
    SERVICE_SET_ENABLED,
)
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
)
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SOLEM enable switch and the ``set_enabled`` service."""
    coordinator = entry.runtime_data
    async_add_entities(
        SolemEnableSwitch(coordinator, module)
        for module in coordinator.modules.values()
        if module.is_controller
    )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_ENABLED,
        {
            vol.Required(ATTR_ENABLED): cv.boolean,
            vol.Optional(ATTR_DAYS): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_RAIN_DELAY_DAYS)
            ),
        },
        "async_set_enabled",
    )


class SolemEnableSwitch(SolemModuleEntity, SwitchEntity, RestoreEntity):
    """Enable / disable a controller.

    The cloud state has no reliable "enabled" flag, so this is an assumed-state
    switch whose value is restored across restarts.
    """

    _attr_assumed_state = True
    _attr_icon = "mdi:power"
    _attr_translation_key = "enabled"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the master enable switch."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_enabled"
        self._is_on = True

    async def async_added_to_hass(self) -> None:
        """Restore the last enabled state."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._is_on = last.state == "on"

    @property
    def is_on(self) -> bool:
        """Return the assumed enabled state."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the controller."""
        await self.async_set_enabled(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the controller permanently."""
        await self.async_set_enabled(enabled=False, days=0)

    async def async_set_enabled(self, enabled: bool, days: int = 0) -> None:
        """Apply the global on/off command.

        ``enabled=True`` turns the controller on. ``enabled=False`` disables it:
        ``days=0`` is permanent, ``days>0`` disables it for that many days (the
        rain-delay). Exposed both via the switch toggle and the
        ``solem_irrigation.set_enabled`` service.
        """
        await self.coordinator.client.async_set_status(
            self._serial, enabled=enabled, days=days
        )
        self._is_on = enabled
        self.async_write_ha_state()
