"""Binary sensor platform for SOLEM irrigation."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import THRESHOLD_ACTIONS
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemRainGauge,
    SolemRainSensor,
)
from .entity import SolemModuleEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolemConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up SOLEM binary sensors."""
    coordinator = entry.runtime_data
    relevant = coordinator.relevant_module_ids()
    entities: list[BinarySensorEntity] = []
    for module in coordinator.modules.values():
        if module.id not in relevant:
            continue
        entities.extend(
            SolemRainThresholdSensor(coordinator, module, gauge)
            for gauge in module.rain_gauges
        )
        entities.extend(
            SolemRainSensorEntity(coordinator, module, sensor)
            for sensor in module.rain_sensors
        )
    async_add_entities(entities)


class SolemRainThresholdSensor(SolemModuleEntity, BinarySensorEntity):
    """SOLEM's own ``isLastMeasureBeyondThresholds`` flag for a rain gauge.

    Surfaced as-is, never re-derived. It sets when the day's rainfall crosses
    the threshold, but it latches: on the #8 reporter's LR-MS it stayed set for
    days after the OFF it triggered had expired, through a day that stayed well
    under the threshold. What the controllers do about it is a separate MySOLEM
    setting, so this says the threshold was crossed, not that watering is
    suspended -- each controller's own ON/OFF status says that.
    """

    _attr_translation_key = "rain_threshold"
    _attr_icon = "mdi:weather-pouring"

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        gauge: SolemRainGauge,
    ) -> None:
        """Initialise the threshold sensor for ``gauge``."""
        super().__init__(coordinator, module)
        self._gauge_id = gauge.id
        self._attr_unique_id = f"{module.id}_rain_threshold_{gauge.id}"
        self._attr_translation_placeholders = {"gauge": gauge.name}

    @property
    def _record(self) -> dict[str, Any]:
        # The polled record wins, but the module page's setup-time copy fills in
        # what it lacks: the two do not always carry the same keys (an action
        # never configured can be absent), and until the first poll the setup
        # copy is all there is.
        setup = next(
            (g.raw for g in self._module.rain_gauges if g.id == self._gauge_id), {}
        )
        return {**setup, **(self.coordinator.input_record(self._gauge_id) or {})}

    @property
    def is_on(self) -> bool | None:
        """Return SOLEM's threshold flag, or None if it does not report one."""
        value = self._record.get("isLastMeasureBeyondThresholds")
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose the configured threshold, its action, and when it last fired."""
        record = self._record
        last_alert = record.get("lastHighThresholdAlertSent")
        return {
            "daily_threshold": record.get("highThreshold"),
            "threshold_action": _threshold_action(
                record.get("actionWhenHighDailyThresholdExceeded")
            ),
            "last_threshold_alert": (
                dt_util.parse_datetime(last_alert) if last_alert else None
            ),
        }


def _threshold_action(code: Any) -> Any:
    """Name a known action code; pass any other through raw.

    MySOLEM's own form posts the code as a string, so both spellings occur.
    """
    try:
        return THRESHOLD_ACTIONS.get(int(code), code)
    except TypeError, ValueError:
        return code


class SolemRainSensorEntity(SolemModuleEntity, BinarySensorEntity):
    """An on/off rain sensor wired to a module's input: on while it is wet.

    Read from the input's newest tick, the same 0/1 MySOLEM plots for it. The
    controller's live state also carries a ``watering.sensor`` field on such a
    module, but SOLEM's web app never reads it, so what it means is unverified.
    """

    _attr_translation_key = "rain_sensor"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(
        self,
        coordinator: SolemDataUpdateCoordinator,
        module: SolemModule,
        sensor: SolemRainSensor,
    ) -> None:
        """Initialise the entity for ``sensor``."""
        super().__init__(coordinator, module)
        self._sensor_id = sensor.id
        self._attr_unique_id = f"{module.id}_rain_sensor_{sensor.id}"
        # Named after the input alone: MySOLEM already calls it "Capteur de
        # pluie" by default, so a "{sensor} rain sensor" name would say it twice.
        self._attr_translation_placeholders = {"sensor": sensor.name}

    @property
    def is_on(self) -> bool | None:
        """Return whether the sensor is wet, or None until it first reports."""
        reading = self.coordinator.rain_sensor_reading(self._sensor_id)
        return reading.wet if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose when the sensor last reported."""
        reading = self.coordinator.rain_sensor_reading(self._sensor_id)
        if reading is None:
            return None
        return {"last_measurement": reading.timestamp.isoformat()}
