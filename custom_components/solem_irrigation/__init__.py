"""The SOLEM irrigation integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, MANUFACTURER
from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.VALVE,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.SENSOR,
]

# Suffixes/substrings of unique_ids created by earlier versions that the current
# control surface no longer provides. They are removed on setup so they do not
# linger as "unavailable" entities after an upgrade:
#   <= 0.1.x : station switches (_station_), program buttons (_program_),
#              single Run-duration number (_run_duration).
#   0.2.x    : the "Manual run" select (_manual_run), replaced by station valves.
# Intentionally NOT listed (current entities): ``_rain_delay`` (Rain-delay
# number), ``_run_program`` (Run-program select), ``_stop`` (Stop button, back
# in 0.4.1), and per-station ``_run_duration_<id>`` (does not end in
# ``_run_duration``).
_LEGACY_UNIQUE_ID_SUFFIXES = (
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
    # platforms add entities, then link each controller to its gateway.
    # `via_device_id` takes a device-registry id, which only exists once the
    # parent has been created -- and an id that is unknown, or that points at
    # the device itself, is a hard registry error, so it must never reach an
    # entity's `DeviceInfo`. Hence both passes live here rather than in
    # `entity.py`; omitting the key in `DeviceInfo` never clears the link.
    device_registry = dr.async_get(hass)
    device_ids: dict[str, str] = {}
    relevant = coordinator.relevant_module_ids()
    for module in coordinator.modules.values():
        if module.id not in relevant:
            continue
        device_ids[module.id] = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, module.id)},
            manufacturer=MANUFACTURER,
            name=module.name,
            model=module.display_type or module.type,
            serial_number=module.serial or None,
        ).id

    # Link a controller to the gateway (relay) it talks through, when known. A
    # relay that is not a module of ours, or that is the module itself, is left
    # unlinked rather than passed on: the registry rejects both.
    for module_id, device_id in device_ids.items():
        relay = coordinator.module_state(module_id).get("relay")
        if relay and relay != module_id and (via_id := device_ids.get(relay)):
            device_registry.async_update_device(device_id, via_device_id=via_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_cleanup_legacy_entities(
    hass: HomeAssistant, entry: SolemConfigEntry
) -> None:
    """Remove per-station/program/stop/number entities from older versions."""
    entity_registry = er.async_get(hass)
    # 0.6.0b1 briefly created a Battery sensor for every module, including
    # mains-powered ones where SOLEM reports a level of 0 meaning "no battery".
    stale_battery = {
        f"{module.id}_battery"
        for module in entry.runtime_data.modules.values()
        if not module.raw.get("isBattery")
    }
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        unique_id = entity.unique_id
        if (
            unique_id.endswith(_LEGACY_UNIQUE_ID_SUFFIXES)
            or any(token in unique_id for token in _LEGACY_UNIQUE_ID_SUBSTRINGS)
            or unique_id in stale_battery
        ):
            entity_registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolemConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
