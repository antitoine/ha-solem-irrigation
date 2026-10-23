"""Sensor platform for SOLEM irrigation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import INPUT_UNIT_GALLON, LAST_COMMUNICATION_KEYS
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemFlowReading,
    SolemModule,
    SolemRainGauge,
    SolemTurbine,
    SolemTurbineReading,
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
        # Not gated on `relevant`: a Bluetooth-only controller gets no other
        # entity, yet MySOLEM still holds its programs.
        if module.has_schedule:
            entities.append(SolemScheduleSensor(coordinator, module))
        if module.id not in relevant:
            continue
        entities.append(SolemLastCommunicationSensor(coordinator, module))
        if module.is_controller:
            entities.append(SolemRunningStationSensor(coordinator, module))
        # SOLEM reports ``battery: 0`` on mains-powered modules, where it means
        # "no battery" rather than "empty". Its own ``isBattery`` flag is the
        # authoritative signal, and unlike a truthiness check on the level it
        # still yields a sensor for a battery module that really has run flat.
        if module.raw.get("isBattery"):
            entities.append(SolemBatterySensor(coordinator, module))
        for meter in module.flow_meters:
            entities.append(SolemWaterUsedSensor(coordinator, module, meter))
            entities.append(SolemWaterFlowRateSensor(coordinator, module, meter))
        entities.extend(
            SolemRainfallSensor(coordinator, module, gauge)
            for gauge in module.rain_gauges
        )
        for turbine in module.turbines:
            entities.append(
                SolemTurbineWaterUsedTodaySensor(coordinator, module, turbine)
            )
            entities.append(SolemTurbineFlowRateSensor(coordinator, module, turbine))
    async_add_entities(entities)


class SolemRunningStationSensor(SolemModuleEntity, SensorEntity):
    """Name of the station currently watering (None when idle).

    An LR-AG can water several stations at once: their names are then joined,
    and so are their remaining times, in the same order.
    """

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
        if self._module.is_agricultural:
            names = {station.index: station.name for station in self._module.stations}
            running = self.coordinator.ag_running_outputs(self._module_id)
            return ", ".join(names.get(i, f"Station {i}") for i in running) or None
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
        if self._module.is_agricultural:
            # One entry per running output, in the order of the names, so the
            # two stay aligned: "--:--" stands in for a time not reported yet
            # (the classic path above returns None instead, having one station).
            times = [
                str(output["time"])
                if output.get("time") and output["time"] != "00:00"
                else "--:--"
                for output in self.coordinator.ag_running_outputs(
                    self._module_id
                ).values()
            ]
            if all(time == "--:--" for time in times):
                return None
            return {"time_remaining": ", ".join(times)}
        watering = (
            self.coordinator.module_state(self._module_id)
            .get("status", {})
            .get("watering", {})
        )
        time_left = watering.get("time")
        if not time_left or time_left == "00:00":
            return None
        return {"time_remaining": time_left}


class SolemScheduleSensor(SolemModuleEntity, SensorEntity):
    """When the module next waters, from the programs stored in MySOLEM.

    The state is the next programmed run, unknown while the controller is off or
    nothing is due. The attributes carry every stored program and the runs due
    today and tomorrow; those runs change with the clock, so they are kept out
    of the recorder rather than written to the database on every change.
    """

    _attr_translation_key = "schedule"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:calendar-clock"
    _unrecorded_attributes = frozenset({"occurrences"})

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the schedule sensor."""
        super().__init__(coordinator, module)
        self._attr_unique_id = f"{module.id}_schedule"
        self._overview: dict[str, Any] = coordinator.schedule_overview(module.id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Compute the overview once per update, not once per property."""
        self._overview = self.coordinator.schedule_overview(self._module_id)
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        """Return the start of the next programmed run, if any."""
        return self._overview.get("next_run")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the programs, the upcoming runs and the push status."""
        return {k: v for k, v in self._overview.items() if k != "next_run"}


class SolemLastCommunicationSensor(SolemModuleEntity, SensorEntity):
    """Timestamp of the module's last contact with the MySOLEM cloud.

    Which key carries it depends on how the module reaches the cloud, so the
    candidates in ``LAST_COMMUNICATION_KEYS`` are tried in order. Reading only
    ``lastRadioCommunication`` left every non-LoRa module -- the LR-MB gateway,
    and WiFi controllers such as the SMART-IS -- permanently ``unknown``.
    """

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
        """Return the module's last contact time, whichever key reports it.

        The live state wins over the module page for a given key: both carry
        ``lastRadioCommunication``, and the polled one is the fresher.

        ``raw`` starts as a setup-time snapshot of the (>1 MB) module page, but
        the coordinator refreshes ``seenAt`` on every poll through the cheap
        field projection -- see ``LIVE_MODULE_FIELDS`` -- so the gateway's
        timestamp advances like a LoRa controller's rather than sticking at
        whatever the last reload captured.
        """
        state = self.coordinator.module_state(self._module_id)
        raw = self._module.raw
        for key in LAST_COMMUNICATION_KEYS:
            if value := (state.get(key) or raw.get(key)):
                return dt_util.parse_datetime(value)
        return None


class SolemBatterySensor(SolemModuleEntity, SensorEntity):
    """Battery indicator as reported by SOLEM (typically a 0-5 bar level).

    Deliberately not a ``battery`` device-class in ``%``: SOLEM reports a small
    level (e.g. 4 alongside a battery voltage of 53 decivolts for 4x AAA), which
    would render as "4%". Only created for modules SOLEM flags as battery
    powered -- see ``async_setup_entry``.
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


class SolemTurbineEntity(SolemModuleEntity, SensorEntity):
    """Base for the sensors of a turbine flow meter (#12)."""

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        turbine: SolemTurbine,
    ) -> None:
        """Initialise the entity for ``turbine``."""
        super().__init__(coordinator, module)
        self._turbine_id = turbine.id
        self._gallons = turbine.unit == INPUT_UNIT_GALLON
        # Named like a flow meter's sensors, through the same placeholder.
        self._attr_translation_placeholders = {"meter": turbine.name}

    @property
    def _reading(self) -> SolemTurbineReading | None:
        return self.coordinator.turbine_reading(self._turbine_id)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the newest tick's time and SOLEM's own probe flag."""
        reading = self._reading
        if reading is None:
            return None
        record = self.coordinator.input_record(self._turbine_id) or {}
        return {
            "turbine_id": self._turbine_id,
            "last_measurement": (
                reading.timestamp.isoformat() if reading.timestamp else None
            ),
            "faulty_probe": record.get("hasFaultyProbe"),
        }


class SolemTurbineWaterUsedTodaySensor(SolemTurbineEntity):
    """Water through a turbine since local midnight.

    A turbine reports flow, not a running total, so there is no lifetime counter
    to read: the day's ticks are added up instead, starting again at midnight
    the way MySOLEM's daily chart does. ``last_reset`` marks each new day, so
    long-term statistics and the Energy dashboard take it as it is.
    """

    _attr_translation_key = "water_used_today"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        turbine: SolemTurbine,
    ) -> None:
        """Initialise the water-used-today sensor."""
        super().__init__(coordinator, module, turbine)
        self._attr_unique_id = f"{module.id}_water_used_today_{turbine.id}"
        self._attr_native_unit_of_measurement = (
            UnitOfVolume.GALLONS if self._gallons else UnitOfVolume.LITERS
        )

    @property
    def native_value(self) -> float | None:
        """Return today's volume, or None until the first window arrives."""
        reading = self._reading
        return reading.volume_today if reading else None

    @property
    def last_reset(self) -> datetime | None:
        """Return the local midnight that today's total counts from."""
        reading = self._reading
        return dt_util.start_of_local_day(reading.day) if reading else None


class SolemTurbineFlowRateSensor(SolemTurbineEntity):
    """Current flow through a turbine: its newest tick, 0 once the water stops."""

    _attr_translation_key = "water_flow_rate"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        turbine: SolemTurbine,
    ) -> None:
        """Initialise the flow-rate sensor."""
        super().__init__(coordinator, module, turbine)
        self._attr_unique_id = f"{module.id}_water_flow_rate_{turbine.id}"
        self._attr_native_unit_of_measurement = (
            UnitOfVolumeFlowRate.GALLONS_PER_MINUTE
            if self._gallons
            else UnitOfVolumeFlowRate.LITERS_PER_MINUTE
        )

    @property
    def native_value(self) -> float | None:
        """Return the current flow rate, or None until the first window arrives."""
        reading = self._reading
        return reading.rate if reading else None


class SolemRainfallSensor(SolemModuleEntity, SensorEntity):
    """SOLEM's lifetime rainfall total, as counted by a tipping-bucket gauge.

    A lifetime counter, like *water used*, so a restart never double-counts it;
    daily or weekly totals are a ``utility_meter`` away.
    """

    _attr_translation_key = "rainfall"
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    # SOLEM stores rainfall in millimetres and converts to inches for display.
    _attr_native_unit_of_measurement = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        gauge: SolemRainGauge,
    ) -> None:
        """Initialise the rainfall sensor for ``gauge``."""
        super().__init__(coordinator, module)
        self._gauge_id = gauge.id
        self._attr_unique_id = f"{module.id}_rainfall_{gauge.id}"
        # Named through a placeholder for the same reason as the flow meters.
        self._attr_translation_placeholders = {"gauge": gauge.name}

    @property
    def native_value(self) -> float | None:
        """Return the rainfall total, or None until the first tick arrives."""
        reading = self.coordinator.rain_reading(self._gauge_id)
        return reading.total if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the newest tick and SOLEM's own probe flag."""
        reading = self.coordinator.rain_reading(self._gauge_id)
        if reading is None:
            return None
        record = self.coordinator.input_record(self._gauge_id) or {}
        return {
            "gauge_id": self._gauge_id,
            "raw_value": reading.raw_total,
            "last_measurement": reading.timestamp.isoformat(),
            "faulty_probe": record.get("hasFaultyProbe"),
        }
