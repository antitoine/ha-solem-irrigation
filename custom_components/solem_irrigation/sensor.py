"""Sensor platform for SOLEM irrigation."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator, SolemModule
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SOLEM sensors."""
    coordinator = entry.runtime_data
    relevant = coordinator.relevant_module_ids()
    entities: list[SensorEntity] = []
    for module in coordinator.modules.values():
        if module.id not in relevant:
            continue
        entities.append(SolemLastCommunicationSensor(coordinator, module))
        if module.is_controller:
            entities.append(SolemRunningStationSensor(coordinator, module))
        if module.raw.get("battery"):
            entities.append(SolemBatterySensor(coordinator, module))
    async_add_entities(entities)


class SolemRunningStationSensor(SolemModuleEntity, SensorEntity):
    """Name of the station currently watering (None when idle)."""

    _attr_translation_key = "running_station"
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the running-station sensor."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_running_station"

    @property
    def native_value(self) -> str | None:
        """Return the running station's name, or None if idle."""
        index = self.coordinator.running_station_index(self._module_id)
        if index <= 0:
            return None
        for station in self._module.stations:
            if station.index == index:
                return station.name
        return f"Station {index}"

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose the remaining run time reported by the controller."""
        watering = (
            self.coordinator.module_state(self._module_id)
            .get("status", {})
            .get("watering", {})
        )
        time_left = watering.get("time")
        if not time_left or time_left == "00:00":
            return None
        return {"time_remaining": time_left}


class SolemLastCommunicationSensor(SolemModuleEntity, SensorEntity):
    """Timestamp of the last radio communication with the module."""

    _attr_translation_key = "last_communication"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the last-communication sensor."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_last_communication"

    @property
    def native_value(self) -> datetime | None:
        """Return the last radio communication time."""
        raw = self.coordinator.module_state(self._module_id).get(
            "lastRadioCommunication"
        ) or self._module.raw.get("lastRadioCommunication")
        return dt_util.parse_datetime(raw) if raw else None


class SolemBatterySensor(SolemModuleEntity, SensorEntity):
    """Battery indicator as reported by SOLEM (typically a 0-5 bar level).

    Deliberately not a ``battery`` device-class in ``%``: SOLEM reports a small
    level (e.g. 5 with a battery voltage of 59), which would render as "5%".
    """

    _attr_translation_key = "battery"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the battery sensor."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_battery"

    @property
    def native_value(self) -> int | None:
        """Return the battery level reported by SOLEM."""
        value = self._module.raw.get("battery")
        return int(value) if value is not None else None
