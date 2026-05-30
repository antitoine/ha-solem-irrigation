"""Number platform for SOLEM irrigation.

* Run duration (minutes): how long a station runs when its switch is turned on.
* Rain delay (days): 0 = enabled, N>0 = disabled for N days.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    DEFAULT_RUN_MINUTES,
    MAX_RAIN_DELAY_DAYS,
    MAX_RUN_MINUTES,
    MIN_RUN_MINUTES,
)
from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator, SolemModule
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SOLEM number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for module in coordinator.modules.values():
        if not module.is_controller:
            continue
        entities.append(SolemRunDurationNumber(coordinator, module))
        entities.append(SolemRainDelayNumber(coordinator, module))
    async_add_entities(entities)


class SolemRunDurationNumber(SolemModuleEntity, RestoreNumber):
    """Duration (minutes) used when a station switch is turned on."""

    _attr_translation_key = "run_duration"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = MIN_RUN_MINUTES
    _attr_native_max_value = MAX_RUN_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the run-duration number."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_run_duration"
        self._value = float(DEFAULT_RUN_MINUTES)

    async def async_added_to_hass(self) -> None:
        """Restore the last duration and publish it to the coordinator."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_number_data()) is not None and (
            data.native_value is not None
        ):
            self._value = data.native_value
        self.coordinator.run_minutes[self._module_id] = int(self._value)

    @property
    def native_value(self) -> float:
        """Return the configured run duration."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Store a new run duration."""
        self._value = value
        self.coordinator.run_minutes[self._module_id] = int(value)
        self.async_write_ha_state()


class SolemRainDelayNumber(SolemModuleEntity, RestoreNumber):
    """Rain delay in days (0 = enabled, N>0 = disabled for N days)."""

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
        if days > 0:
            await self.coordinator.client.async_set_status(
                self._serial, enabled=False, days=days
            )
        else:
            await self.coordinator.client.async_set_status(
                self._serial, enabled=True
            )
        self._value = float(days)
        self.async_write_ha_state()
