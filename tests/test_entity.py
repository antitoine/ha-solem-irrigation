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
    coordinator.module_state.return_value = {}
    entity = SolemModuleEntity(coordinator, module)

    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, "m1")}
    assert info["manufacturer"] == MANUFACTURER
    assert info["model"] == "LR-IS Pro"
    assert info["serial_number"] == "SER1"
    assert "via_device" not in info


def test_device_info_links_to_gateway(coordinator):
    """A controller is linked to the gateway (relay) it talks through."""
    controller = _module("m1")
    gateway = _module("g1", serial="SERG")
    coordinator.modules = {"m1": controller, "g1": gateway}
    coordinator.module_state.return_value = {"relay": "g1"}
    entity = SolemModuleEntity(coordinator, controller)

    assert entity.device_info["via_device"] == (DOMAIN, "g1")


def test_device_info_ignores_unknown_relay(coordinator):
    """A relay that isn't a known module must not create a dangling via_device."""
    controller = _module("m1")
    coordinator.modules = {"m1": controller}
    coordinator.module_state.return_value = {"relay": "g1"}
    entity = SolemModuleEntity(coordinator, controller)

    assert "via_device" not in entity.device_info


def test_available_tracks_module_presence(coordinator):
    module = _module()
    coordinator.modules = {"m1": module}
    entity = SolemModuleEntity(coordinator, module)
    assert entity.available is True

    # Module disappeared from the account on a later poll.
    coordinator.modules = {}
    assert entity.available is False
