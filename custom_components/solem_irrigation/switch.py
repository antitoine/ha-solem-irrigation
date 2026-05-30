"""Switch platform for SOLEM irrigation.

* One switch per station: turning it on runs that station for the configured
  duration; turning it off sends a global stop. The controller waters one
  station at a time, so at most one is "on".
* One master switch per controller: enable / disable the controller.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemStation,
)
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SOLEM switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    for module in coordinator.modules.values():
        if not module.is_controller:
            continue
        entities.append(SolemEnableSwitch(coordinator, module))
        entities.extend(
            SolemStationSwitch(coordinator, module, station)
            for station in module.stations
        )
    async_add_entities(entities)


class SolemStationSwitch(SolemModuleEntity, SwitchEntity):
    """A single watering station."""

    _attr_icon = "mdi:sprinkler"

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        station: SolemStation,
    ) -> None:
        """Initialise the station switch."""
        super().__init__(coordinator, module)
        self._station = station
        self._attr_unique_id = f"{module.id}_station_{station.id}"
        self._attr_name = station.name

    @property
    def is_on(self) -> bool:
        """True when this station is the one currently watering."""
        return (
            self.coordinator.running_station_index(self._module_id)
            == self._station.index
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Run this station for the configured duration."""
        minutes = self.coordinator.get_run_minutes(self._module_id)
        await self.coordinator.client.async_run_station(
            self._serial, self._station.id, minutes
        )
        self.coordinator.apply_optimistic_running_station(
            self._module_id, self._station.index
        )
        self.coordinator.async_schedule_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop watering (global stop)."""
        await self.coordinator.client.async_stop(self._serial)
        self.coordinator.apply_optimistic_running_station(self._module_id, 0)
        self.coordinator.async_schedule_refresh()


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
        await self.coordinator.client.async_set_status(self._serial, enabled=True)
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the controller permanently."""
        await self.coordinator.client.async_set_status(
            self._serial, enabled=False, days=0
        )
        self._is_on = False
        self.async_write_ha_state()
