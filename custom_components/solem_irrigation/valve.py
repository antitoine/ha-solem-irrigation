"""Valve platform for SOLEM irrigation.

Each watering station is a ``valve`` entity (the idiomatic Home Assistant model
for an irrigation zone, as used by e.g. the Hunter Hydrawise integration):

* **Open** the valve to start watering that station for the remembered run
  duration; the controller stops it automatically when the time elapses.
* **Close** the valve to stop it.

Crucially, every valve's open/closed state is derived from the coordinator's
single ``running_station_index``. The SOLEM controller waters one station at a
time, so opening one station optimistically marks it running, which makes every
other valve read *closed* — only one valve is ever open, faithfully modelling
the hardware. Use the ``solem_irrigation.run`` service to run a station for a
specific (non-default) duration.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
    """Set up one valve per station of each controller."""
    coordinator = entry.runtime_data
    entities: list[ValveEntity] = []
    for module in coordinator.modules.values():
        if not module.is_controller:
            continue
        entities.extend(
            SolemStationValve(coordinator, module, station)
            for station in module.stations
        )
    async_add_entities(entities)


class SolemStationValve(SolemModuleEntity, ValveEntity):
    """A single watering station, modelled as a water valve."""

    _attr_device_class = ValveDeviceClass.WATER
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        station: SolemStation,
    ) -> None:
        """Initialise the station valve."""
        super().__init__(coordinator, module)
        self._station = station
        # Not "_station_": that substring is pruned as a legacy switch on upgrade.
        self._attr_unique_id = f"{module.id}_valve_{station.id}"
        self._attr_name = station.name

    @property
    def is_closed(self) -> bool:
        """Open only while this station is the one currently watering."""
        return (
            self.coordinator.running_station_index(self._module_id)
            != self._station.index
        )

    async def async_open_valve(self, **kwargs: Any) -> None:
        """Start watering this station for its remembered duration."""
        minutes = self.coordinator.get_run_minutes(self._station.id)
        await self.coordinator.async_command_run_station(
            self._module, self._station, minutes
        )

    async def async_close_valve(self, **kwargs: Any) -> None:
        """Stop this station if it is the one running.

        Guarded so that closing an already-closed valve is a no-op: without it,
        closing zone A while zone B is running would send a global stop and halt
        B — surprising when you have just switched zones.
        """
        if (
            self.coordinator.running_station_index(self._module_id)
            == self._station.index
        ):
            await self.coordinator.async_command_stop(self._module)
