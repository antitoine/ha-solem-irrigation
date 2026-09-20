"""Tests for the shared SOLEM base entity (device info + availability)."""

from unittest.mock import MagicMock

import pytest

from custom_components.solem_irrigation.const import DOMAIN, MANUFACTURER
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
)
from custom_components.solem_irrigation.entity import SolemModuleEntity


def _module(module_id: str = "m1", serial: str = "SER1") -> SolemModule:
    return SolemModule(
        id=module_id,
        name="Controller",
        serial=serial,
        type="LR-IS",
        display_type="LR-IS Pro",
        raw={"typeIsWatering": True},
        stations=[],
        programs=[],
    )


@pytest.fixture
def coordinator() -> MagicMock:
    coord = MagicMock(spec=SolemDataUpdateCoordinator)
    coord.last_update_success = True
    return coord


def test_device_info_basic(coordinator):
    module = _module()
    coordinator.modules = {"m1": module}
    entity = SolemModuleEntity(coordinator, module)

    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, "m1")}
    assert info["manufacturer"] == MANUFACTURER
    assert info["model"] == "LR-IS Pro"
    assert info["serial_number"] == "SER1"
    # The gateway link lives in `async_setup_entry`, not here: `via_device_id`
    # needs a registry id, and a bad one is a hard error in the device registry.
    assert "via_device" not in info
    assert "via_device_id" not in info


def test_device_info_blank_serial_is_none(coordinator):
    """An empty serial must not overwrite the `None` written at registration."""
    module = _module(serial="")
    coordinator.modules = {"m1": module}
    entity = SolemModuleEntity(coordinator, module)

    assert entity.device_info["serial_number"] is None


def test_available_tracks_module_presence(coordinator):
    module = _module()
    coordinator.modules = {"m1": module}
    entity = SolemModuleEntity(coordinator, module)
    assert entity.available is True

    # Module disappeared from the account on a later poll.
    coordinator.modules = {}
    assert entity.available is False
