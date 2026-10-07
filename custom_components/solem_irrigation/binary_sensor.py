"""Binary sensor platform for SOLEM irrigation."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import THRESHOLD_ACTIONS
from .coordinator import (
    SolemConfigEntry,
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemRainGauge,
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
    async_add_entities(
        SolemRainThresholdSensor(coordinator, module, gauge)
        for module in coordinator.modules.values()
        if module.id in relevant
        for gauge in module.rain_gauges
    )


class SolemRainThresholdSensor(SolemModuleEntity, BinarySensorEntity):
    """SOLEM's own ``isLastMeasureBeyondThresholds`` flag for a rain gauge.

    Surfaced as-is, never re-derived: the field names point at a *daily*
    rainfall threshold, but when SOLEM clears the flag is unverified (on a flow
    meter it has been seen to stay set long after the flow stopped). What the
    controllers then do about it is a separate MySOLEM setting, so this says
    the threshold was crossed, not that watering is suspended.
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
