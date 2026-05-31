"""Number platform for SOLEM irrigation.

* Run duration (minutes), one per station: how long opening that station's valve
  runs it. Stored per station in the coordinator (persisted) and also used by the
  ``solem_irrigation.run`` service.
* Rain delay (days): 0 = enabled, N>0 = disabled for N days. The visible,
  dashboard-friendly form of ``solem_irrigation.set_enabled``'s ``days``
  argument, mirroring SOLEM's own "Report de pluie" field.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MAX_RAIN_DELAY_DAYS, MAX_RUN_MINUTES, MIN_RUN_MINUTES
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
    """Set up the rain-delay number and a run-duration number per station."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for module in coordinator.modules.values():
        if not module.is_controller:
            continue
        entities.append(SolemRainDelayNumber(coordinator, module))
        entities.extend(
            SolemRunDurationNumber(coordinator, module, station)
            for station in module.stations
        )
    async_add_entities(entities)


class SolemRunDurationNumber(SolemModuleEntity, NumberEntity):
    """How long opening a station's valve runs it (minutes).

    Backed by the coordinator's per-station remembered duration (persisted), so
    it stays in sync with the ``solem_irrigation.run`` service's ``duration``.
    Shown under *Configuration* rather than the main controls.
    """

    _attr_translation_key = "run_duration"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = MIN_RUN_MINUTES
    _attr_native_max_value = MAX_RUN_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        station: SolemStation,
    ) -> None:
        """Initialise the per-station run-duration number."""
        super().__init__(coordinator, module)
        self._station = station
        # Not "_station_"/"_run_duration" (legacy patterns): trailing station id.
        self._attr_unique_id = f"{module.id}_run_duration_{station.id}"
        self._attr_translation_placeholders = {"station": station.name}

    @property
    def native_value(self) -> float:
        """Return this station's remembered run duration."""
        return float(self.coordinator.get_run_minutes(self._station.id))

    async def async_set_native_value(self, value: float) -> None:
        """Store a new run duration for this station."""
        await self.coordinator.async_set_run_minutes(self._station.id, int(value))


class SolemRainDelayNumber(SolemModuleEntity, RestoreNumber):
    """Rain delay in days (0 = enabled, N>0 = disabled for N days).

    The cloud has no reliable read-back, so this is an assumed-state value
    restored across restarts.
    """

    _attr_translation_key = "rain_delay"
    _attr_icon = "mdi:weather-rainy"
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_RAIN_DELAY_DAYS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS
    _attr_mode = NumberMode.BOX
    _attr_assumed_state = True

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the rain-delay number."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_rain_delay"
        self._value = 0.0

    async def async_added_to_hass(self) -> None:
        """Restore the last rain-delay value."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_number_data()) is not None and (
            data.native_value is not None
        ):
            self._value = data.native_value

    @property
    def native_value(self) -> float:
        """Return the current rain-delay (days)."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Apply a rain delay (or clear it when set to 0)."""
        days = int(value)
        await self.coordinator.client.async_set_status(
            self._serial, enabled=days == 0, days=days
        )
        self._value = float(days)
        self.async_write_ha_state()
