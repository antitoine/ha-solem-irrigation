"""The SOLEM irrigation integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, MANUFACTURER
from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.VALVE,
    Platform.SENSOR,
]

# Suffixes/substrings of unique_ids created by earlier versions that the current
# control surface no longer provides. They are removed on setup so they do not
# linger as "unavailable" entities after an upgrade:
#   <= 0.1.x : station switches (_station_), program buttons (_program_),
#              stop button (_stop), Run-duration / Rain-delay numbers.
#   0.2.x    : the "Manual run" select (_manual_run), replaced by station valves.
_LEGACY_UNIQUE_ID_SUFFIXES = (
    "_stop",
    "_rain_delay",
    "_run_duration",
    "_manual_run",
)
_LEGACY_UNIQUE_ID_SUBSTRINGS = ("_station_", "_program_")


async def async_setup_entry(hass: HomeAssistant, entry: SolemConfigEntry) -> bool:
    """Set up SOLEM irrigation from a config entry."""
    coordinator = SolemDataUpdateCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    _async_cleanup_legacy_entities(hass, entry)

    # Pre-register the devices (controllers and their gateway) before the
    # platforms add entities, so child `via_device` links always resolve
    # regardless of platform setup order.
    device_registry = dr.async_get(hass)
    for module in coordinator.modules.values():
        if module.id not in coordinator.relevant_module_ids():
            continue
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, module.id)},
            manufacturer=MANUFACTURER,
            name=module.name,
            model=module.display_type or module.type,
            serial_number=module.serial or None,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_cleanup_legacy_entities(
    hass: HomeAssistant, entry: SolemConfigEntry
) -> None:
    """Remove per-station/program/stop/number entities from older versions."""
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        unique_id = entity.unique_id
        if unique_id.endswith(_LEGACY_UNIQUE_ID_SUFFIXES) or any(
            token in unique_id for token in _LEGACY_UNIQUE_ID_SUBSTRINGS
        ):
            entity_registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolemConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
