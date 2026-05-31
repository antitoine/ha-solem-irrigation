"""The SOLEM irrigation integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MANUFACTURER
from .coordinator import SolemConfigEntry, SolemDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: SolemConfigEntry) -> bool:
    """Set up SOLEM irrigation from a config entry."""
    coordinator = SolemDataUpdateCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

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


async def async_unload_entry(hass: HomeAssistant, entry: SolemConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
