"""Tests for the SOLEM data update coordinator."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solem_irrigation.api import (
    SolemAuthError,
    SolemConnectionError,
    SolemError,
)
from custom_components.solem_irrigation.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_RUN_MINUTES,
    DOMAIN,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemModule,
    SolemProgram,
    SolemStation,
    _find_by_token,
)

DATA = {CONF_EMAIL: "e@x.com", CONF_PASSWORD: "pw", CONF_REGION: "Europe"}


def _module(**kwargs) -> SolemModule:
    base = dict(
        id="m1",
        name="Controller",
        serial="SER1",
        type="LR-IS",
        display_type="LR-IS",
        raw={"typeIsWatering": True},
        stations=[SolemStation(id="s1", name="Pelouse 1", index=1)],
        programs=[SolemProgram(id="p1", name="Matin", index=1)],
    )
    base.update(kwargs)
    return SolemModule(**base)


@pytest.fixture
async def coordinator(hass) -> SolemDataUpdateCoordinator:
    """A coordinator backed by a mock config entry (client/store patched per test).

    Async because building it creates an aiohttp client session, which needs a
    running event loop.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, entry_id="abc")
    entry.add_to_hass(hass)
    coord = SolemDataUpdateCoordinator(hass, entry)
    coord._store.async_load = AsyncMock(return_value=None)
    coord._store.async_save = AsyncMock()
    return coord


# -- pure helpers -------------------------------------------------------------


def test_find_by_token_resolves_index_and_name():
    items = [
        SolemStation(id="s1", name="Pelouse 1", index=1),
        SolemStation(id="s2", name="Potager", index=2),
    ]
    assert _find_by_token(items, 2).id == "s2"
    assert _find_by_token(items, "2").id == "s2"
    assert _find_by_token(items, "Potager").id == "s2"
    assert _find_by_token(items, "POTAGER").id == "s2"  # case-insensitive
    assert _find_by_token(items, "ghost") is None
    assert _find_by_token(items, 99) is None


def test_is_controller():
    assert _module().is_controller is True
    # Pool controller: has outputs but not the watering flag.
    assert _module(raw={"typeIsWatering": False}).is_controller is False
    # Gateway: watering flag but no stations.
    assert _module(raw={"typeIsWatering": True}, stations=[]).is_controller is False


def test_find_station_and_program():
    module = _module()
    assert module.find_station("Pelouse 1").id == "s1"
    assert module.find_station(1).id == "s1"
    assert module.find_program("Matin").id == "p1"
    assert module.find_program("nope") is None


# -- state helpers ------------------------------------------------------------


async def test_running_station_index(coordinator):
    coordinator.data = {"m1": {"status": {"watering": {"runningStation": 3}}}}
    assert coordinator.running_station_index("m1") == 3
    # Garbage / missing both read as idle.
    coordinator.data = {"m1": {"status": {"watering": {"runningStation": "x"}}}}
    assert coordinator.running_station_index("m1") == 0
    assert coordinator.running_station_index("unknown") == 0


async def test_apply_optimistic_running_station(coordinator):
    coordinator.data = {}
    coordinator.apply_optimistic_running_station("m1", 2)
    assert coordinator.running_station_index("m1") == 2


async def test_relevant_module_ids_includes_controllers_and_gateway(coordinator):
    controller = _module(id="m1")
    gateway = _module(id="g1", raw={}, stations=[])
    pool = _module(id="pool", raw={"typeIsWatering": False})
    coordinator.modules = {"m1": controller, "g1": gateway, "pool": pool}
    coordinator.data = {"m1": {"relay": "g1"}}
    assert coordinator.relevant_module_ids() == {"m1", "g1"}


async def test_module_state_defaults_to_empty(coordinator):
    coordinator.data = None
    assert coordinator.module_state("m1") == {}


# -- run-minutes persistence --------------------------------------------------


async def test_run_minutes_default_and_set(coordinator):
    assert coordinator.get_run_minutes("s1") == DEFAULT_RUN_MINUTES
    await coordinator.async_set_run_minutes("s1", 12)
    assert coordinator.get_run_minutes("s1") == 12
    coordinator._store.async_save.assert_awaited_once_with({"s1": 12})


async def test_setup_loads_stored_run_minutes(coordinator):
    coordinator._store.async_load = AsyncMock(return_value={"s1": 9})
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=[])
    await coordinator.async_setup()
    assert coordinator.get_run_minutes("s1") == 9


# -- discovery ----------------------------------------------------------------


async def test_setup_discovers_and_classifies_modules(coordinator):
    objs = {
        "m1": {
            "name": "Ctrl",
            "serialNumber": "S1",
            "type": "LR-IS",
            "typeIsWatering": True,
            "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
            "programs": [{"id": "pr1", "name": "Prog", "index": 1}],
        },
        "g1": {"name": "Gateway", "type": "LR-MB"},
        "pool": {
            "name": "Pool",
            "type": "LR-PC",
            "typeIsWatering": False,
            "outputs": [{"id": "x", "name": "x", "index": 1}],
        },
        "bad": {},  # unparseable page -> skipped
    }
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(
        return_value=["m1", "g1", "pool", "bad"]
    )
    coordinator.client.async_get_module = AsyncMock(side_effect=lambda mid: objs[mid])

    await coordinator.async_setup()

    assert set(coordinator.modules) == {"m1", "g1", "pool"}
    assert coordinator.modules["m1"].is_controller is True
    assert coordinator.modules["m1"].stations[0].name == "Z1"
    assert coordinator.modules["pool"].is_controller is False
    assert coordinator.modules["g1"].is_controller is False


async def test_setup_skips_module_that_errors(coordinator):
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1", "boom"])

    async def get_module(mid):
        if mid == "boom":
            raise SolemError("nope")
        return {
            "name": "Ctrl",
            "typeIsWatering": True,
            "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
        }

    coordinator.client.async_get_module = AsyncMock(side_effect=get_module)
    await coordinator.async_setup()
    assert set(coordinator.modules) == {"m1"}


async def test_setup_auth_error_maps_to_config_entry_auth_failed(coordinator):
    coordinator.client.async_login = AsyncMock(side_effect=SolemAuthError("bad"))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_setup()


async def test_setup_connection_error_maps_to_not_ready(coordinator):
    coordinator.client.async_login = AsyncMock(side_effect=SolemConnectionError("x"))
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_setup()


# -- polling ------------------------------------------------------------------


async def test_update_data_success(coordinator):
    coordinator.modules = {"m1": _module(id="m1"), "g1": _module(id="g1")}
    coordinator.client.async_get_module_state = AsyncMock(return_value={"ok": 1})
    result = await coordinator._async_update_data()
    assert result == {"m1": {"ok": 1}, "g1": {"ok": 1}}


async def test_update_data_auth_error(coordinator):
    coordinator.modules = {"m1": _module()}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemAuthError("expired")
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_update_data_partial_failure_is_tolerated(coordinator):
    coordinator.modules = {"m1": _module(id="m1"), "g1": _module(id="g1")}

    async def state(mid):
        if mid == "g1":
            raise SolemConnectionError("flaky")
        return {"ok": 1}

    coordinator.client.async_get_module_state = AsyncMock(side_effect=state)
    result = await coordinator._async_update_data()
    assert result["m1"] == {"ok": 1}
    assert result["g1"] == {}  # no prior data kept


async def test_update_data_total_failure_raises(coordinator):
    coordinator.modules = {"m1": _module(id="m1")}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("down")
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# -- command helpers ----------------------------------------------------------


async def test_command_stop(coordinator):
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = {}
    coordinator.client.async_stop = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_stop(module)
    coordinator.client.async_stop.assert_awaited_once_with("SER1")
    assert coordinator.running_station_index("m1") == 0
    coordinator.async_schedule_refresh.assert_called_once()


async def test_command_run_station(coordinator):
    module = _module()
    station = module.stations[0]
    coordinator.modules = {"m1": module}
    coordinator.data = {}
    coordinator.client.async_run_station = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_run_station(module, station, 7)
    coordinator.client.async_run_station.assert_awaited_once_with("SER1", "s1", 7)
    assert coordinator.running_station_index("m1") == station.index
    coordinator.async_schedule_refresh.assert_called_once()


async def test_command_run_program(coordinator):
    module = _module()
    program = module.programs[0]
    coordinator.client.async_run_program = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_run_program(module, program)
    coordinator.client.async_run_program.assert_awaited_once_with("SER1", "p1")
    coordinator.async_schedule_refresh.assert_called_once()


# -- scheduled refresh --------------------------------------------------------


async def test_schedule_and_cancel_refresh(coordinator):
    coordinator.async_schedule_refresh(delay=30)
    assert coordinator._refresh_unsub is not None
    coordinator._cancel_scheduled_refresh()
    assert coordinator._refresh_unsub is None


async def test_update_interval_is_five_minutes(coordinator):
    assert coordinator.update_interval == timedelta(minutes=5)
