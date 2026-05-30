"""Base entity for SOLEM irrigation."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SolemDataUpdateCoordinator, SolemModule


class SolemModuleEntity(CoordinatorEntity[SolemDataUpdateCoordinator]):
    """Base entity bound to a single SOLEM module."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SolemDataUpdateCoordinator, module: SolemModule
    ) -> None:
        """Initialise the entity for ``module``."""
        super().__init__(coordinator)
        self._module_id = module.id
        self._serial = module.serial

    @property
    def _module(self) -> SolemModule:
        return self.coordinator.modules[self._module_id]

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info, linking controllers to their LoRa gateway."""
        module = self._module
        info = DeviceInfo(
            identifiers={(DOMAIN, module.id)},
            name=module.name,
            manufacturer=MANUFACTURER,
            model=module.display_type or module.type,
            serial_number=module.serial,
        )
        # Link a controller to the gateway (relay) it talks through, when known.
        relay = self.coordinator.module_state(module.id).get("relay")
        if relay and relay != module.id and relay in self.coordinator.modules:
            info["via_device"] = (DOMAIN, relay)
        return info

    @property
    def available(self) -> bool:
        """Available while the coordinator has data for this module."""
        return super().available and self._module_id in self.coordinator.modules
