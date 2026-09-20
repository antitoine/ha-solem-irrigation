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
        """Return device info for this module's device.

        The controller -> gateway link is deliberately absent: `via_device_id`
        needs a device-registry id, so it is applied in ``__init__.py`` when the
        devices are pre-registered. Omitting the key never clears the link.
        """
        module = self._module
        return DeviceInfo(
            identifiers={(DOMAIN, module.id)},
            name=module.name,
            manufacturer=MANUFACTURER,
            model=module.display_type or module.type,
            serial_number=module.serial or None,
        )

    @property
    def available(self) -> bool:
        """Available while the coordinator has data for this module."""
        return super().available and self._module_id in self.coordinator.modules
