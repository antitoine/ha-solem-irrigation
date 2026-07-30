"""Sensor platform for SOLEM irrigation."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import INPUT_UNIT_GALLON
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemFlowReading,
    SolemModule,
)
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
        if module.raw.get("battery") is not None:
            entities.append(SolemBatterySensor(coordinator, module))
        for meter in module.flow_meters:
            entities.append(SolemWaterUsedSensor(coordinator, module, meter))
            entities.append(SolemWaterFlowRateSensor(coordinator, module, meter))
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


class SolemFlowMeterEntity(SolemModuleEntity, SensorEntity):
    """Base for the sensors of a flow meter wired to a module."""

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        meter: SolemFlowMeter,
    ) -> None:
        """Initialise the entity for ``meter``."""
        super().__init__(coordinator, module)
        self._meter_id = meter.id
        self._unit = meter.unit
        # Always name via a placeholder, even with a single meter: switching to a
        # plain name would rename the entity the day a second meter is added.
        self._attr_translation_placeholders = {"meter": meter.name}

    @property
    def _reading(self) -> SolemFlowReading | None:
        return self.coordinator.flow_reading(self._meter_id)


class SolemWaterUsedSensor(SolemFlowMeterEntity):
    """SOLEM's lifetime water counter, as read by the flow meter."""

    _attr_translation_key = "water_used"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        meter: SolemFlowMeter,
    ) -> None:
        """Initialise the water-used sensor."""
        super().__init__(coordinator, module, meter)
        self._attr_unique_id = f"{module.id}_water_used_{meter.id}"
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.GALLONS
            if meter.unit == INPUT_UNIT_GALLON
            else UnitOfVolume.LITERS
        )

    @property
    def native_value(self) -> float | None:
        """Return the counter reading, or None until the first tick arrives."""
        reading = self._reading
        return reading.volume if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the meter's configuration and SOLEM's own probe flags."""
        reading = self._reading
        if reading is None:
            return None
        record = reading.record
        settings = record.get("settings") or []
        attributes: dict[str, object] = {
            "meter_id": self._meter_id,
            "raw_value": reading.raw_volume,
            "last_measurement": reading.timestamp.isoformat(),
            "measurement_interval": record.get("interval"),
            "expression": record.get("expression"),
            # SOLEM's own flags, refreshed on every poll. What these thresholds
            # are compared against is unverified (a reading of 397 sits below a
            # lowThreshold of 1500 yet reports False), so they are surfaced
            # as-is and never re-derived here.
            "faulty_probe": record.get("hasFaultyProbe"),
            "beyond_thresholds": record.get("isLastMeasureBeyondThresholds"),
            "low_threshold": record.get("lowThreshold"),
            "high_threshold": record.get("highThreshold"),
        }
        if settings:
            # ``settings`` is positional: one entry per station, in index order.
            attributes["leak_alert_volume"] = settings[0].get("leakAlertVolume")
            attributes["nominal_flow"] = {
                station.name: setting.get("nominalDebit")
                for station, setting in zip(
                    sorted(self._module.stations, key=lambda s: s.index),
                    settings,
                    strict=False,
                )
            }
        return attributes


class SolemWaterFlowRateSensor(SolemFlowMeterEntity):
    """Current flow rate, derived from the meter's most recent ticks."""

    _attr_translation_key = "water_flow_rate"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        meter: SolemFlowMeter,
    ) -> None:
        """Initialise the flow-rate sensor."""
        super().__init__(coordinator, module, meter)
        self._attr_unique_id = f"{module.id}_water_flow_rate_{meter.id}"
        self._attr_native_unit_of_measurement = (
            UnitOfVolumeFlowRate.GALLONS_PER_MINUTE
            if meter.unit == INPUT_UNIT_GALLON
            else UnitOfVolumeFlowRate.LITERS_PER_MINUTE
        )

    @property
    def native_value(self) -> float | None:
        """Return the current flow rate, or None when it cannot be determined."""
        reading = self._reading
        return reading.rate if reading else None
