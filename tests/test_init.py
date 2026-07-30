"""Tests for SOLEM irrigation setup, unload and legacy cleanup."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solem_irrigation import (
    PLATFORMS,
    _async_cleanup_legacy_entities,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.solem_irrigation.api import SolemAuthError
from custom_components.solem_irrigation.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DOMAIN,
)
from custom_components.solem_irrigation.coordinator import SolemDataUpdateCoordinator

DATA = {CONF_EMAIL: "e@x.com", CONF_PASSWORD: "pw", CONF_REGION: "Europe"}

_LOGIN = "custom_components.solem_irrigation.coordinator.SolemApiClient.async_login"
_IDS = (
    "custom_components.solem_irrigation.coordinator.SolemApiClient.async_get_module_ids"
)
_GET = (
    "custom_components.solem_irrigation.coordinator."
    "SolemApiClient.async_get_module_page"
)
_STATE = (
    "custom_components.solem_irrigation.coordinator."
    "SolemApiClient.async_get_module_state"
)

CONTROLLER = {
    "name": "Controller",
    "serialNumber": "SER1",
    "type": "LR-IS",
    "typeIsWatering": True,
    "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
    "programs": [{"id": "p1", "name": "Matin", "index": 1}],
}


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, entry_id="e1")
    entry.add_to_hass(hass)
    return entry


async def test_setup_and_unload_entry(hass: HomeAssistant, entry) -> None:
    """A controller is discovered, its device registered, then it unloads."""
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    with (
        patch(_LOGIN, AsyncMock(return_value="uid")),
        patch(_IDS, AsyncMock(return_value=["m1"])),
        patch(_GET, AsyncMock(return_value=(CONTROLLER, []))),
        patch(_STATE, AsyncMock(return_value={})),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry) is True
        forward.assert_called_once_with(entry, PLATFORMS)

    assert isinstance(entry.runtime_data, SolemDataUpdateCoordinator)
    # The controller's device was pre-registered.
    device_registry = dr.async_get(hass)
    assert device_registry.async_get_device(identifiers={(DOMAIN, "m1")}) is not None

    with patch(
        "homeassistant.config_entries.ConfigEntries.async_unload_platforms",
        new_callable=AsyncMock,
        return_value=True,
    ) as unload:
        assert await async_unload_entry(hass, entry) is True
        unload.assert_called_once_with(entry, PLATFORMS)


async def test_setup_entry_auth_failure(hass: HomeAssistant, entry) -> None:
    """A rejected login during setup surfaces as ConfigEntryAuthFailed."""
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    with (
        patch(_LOGIN, AsyncMock(side_effect=SolemAuthError("bad"))),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


async def test_legacy_entity_cleanup(hass: HomeAssistant, entry) -> None:
    """Only entities from earlier control surfaces are pruned on setup."""
    registry = er.async_get(hass)
    legacy = [
        "m1_run_duration",  # old single run-duration number
        "m1_manual_run",  # old "Manual run" select
        "m1_station_s1",  # old per-station switch
        "m1_program_p1",  # old per-program button
    ]
    kept = [
        "m1_rain_delay",
        "m1_run_program",
        "m1_stop",
        "m1_run_duration_s1",  # current per-station number (trailing id)
        "m1_enabled",
    ]
    for unique_id in legacy + kept:
        registry.async_get_or_create("switch", DOMAIN, unique_id, config_entry=entry)

    _async_cleanup_legacy_entities(hass, entry)

    remaining = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert remaining == set(kept)
