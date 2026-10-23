"""Tests for the SOLEM data update coordinator."""

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solem_irrigation import coordinator as coordinator_module
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
    ENABLE_ACK_GRACE,
)
from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemModule,
    SolemProgram,
    SolemRainGauge,
    SolemRainSensor,
    SolemStation,
    SolemTurbine,
    SolemTurbineReading,
    _build_flow_meters,
    _build_rain_gauges,
    _build_rain_sensors,
    _build_turbines,
    _compute_flow_rate,
    _compute_turbine_reading,
    _find_by_token,
)

FLOW_INPUT = {
    "id": "i1",
    "name": "Flowmeter",
    "type": 1,
    "unit": 1,
    "index": 1,
    "interval": 1,
    "expression": "x/0.1",
    "getName": "Débitmètre",
}


def _flow_meter(**kwargs) -> SolemFlowMeter:
    base = dict(
        id="i1",
        name="Flowmeter",
        index=1,
        unit=1,
        expression="x/0.1",
        interval=1,
        raw=dict(FLOW_INPUT),
    )
    base.update(kwargs)
    return SolemFlowMeter(**base)


def _ticks(*pairs) -> dict:
    """Build an input record from ``(minute, value)`` pairs."""
    return {
        "id": "i1",
        "computedSensorData": [
            {"tickTimestamp": f"2026-07-30T17:{minute:02d}:00+00:00", "value": value}
            for minute, value in pairs
        ],
    }


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
    # Every poll now also re-reads the cheap module-field projection; tests that
    # care about it override this.
    coord.client.async_get_module_fields = AsyncMock(return_value={})
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


def test_is_bluetooth_only():
    # SOLEM's own flag decides...
    assert _module(raw={"isBluetoothOnly": True}).is_bluetooth_only is True
    # ...with the web app's list of types as a fallback when the flag is absent.
    assert _module(type="bl-ip", raw={}).is_bluetooth_only is True
    # A LoRa controller is also Bluetooth-capable, but not *only* Bluetooth.
    assert (
        _module(type="lr-ip-eco", raw={"isBluetoothOnly": False}).is_bluetooth_only
        is False
    )
    assert _module().is_bluetooth_only is False


def test_a_bluetooth_only_controller_is_not_a_controller():
    """MySOLEM can neither read nor command it, so it gets no entities."""
    module = _module(
        type="bl-ip", raw={"typeIsWatering": True, "isBluetoothOnly": True}
    )
    assert module.is_controller is False


async def test_relevant_module_ids_leave_out_bluetooth_only_modules(coordinator):
    gauge = SolemRainGauge(id="r1", name="R", index=1, expression="x", raw={})
    coordinator.modules = {
        "b1": _module(id="b1", raw={"typeIsWatering": True, "isBluetoothOnly": True}),
        "b2": _module(
            id="b2", raw={"isBluetoothOnly": True}, stations=[], rain_gauges=[gauge]
        ),
    }
    coordinator.data = {}
    assert coordinator.relevant_module_ids() == set()


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


def _watering(**fields) -> dict:
    return {"m1": {"status": {"watering": fields}}}


@pytest.mark.parametrize(
    ("watering", "enabled", "days"),
    [
        # As SOLEM's web app reads it: "OFF 1 jour" (the #8 payload).
        ({"state": 0, "rainDelay": 1}, False, 1),
        ({"state": "OFF", "rainDelay": 3}, False, 3),
        # OFF with no day count, or the 255 the web app treats as permanent.
        ({"state": 0, "rainDelay": 0}, False, 0),
        ({"state": 0, "rainDelay": 255}, False, 0),
        ({"state": 0}, False, 0),
        ({"state": 0, "rainDelay": "x"}, False, 0),
        # ``state`` reads 1 both idle and while a station runs.
        ({"state": 1, "rainDelay": 0, "runningStation": 2}, True, 0),
        # A stray delay on an ON controller is not a delay.
        ({"state": 1, "rainDelay": 4}, True, 0),
    ],
)
async def test_reported_watering_status(coordinator, watering, enabled, days):
    coordinator.data = _watering(**watering)
    assert coordinator.watering_enabled("m1") is enabled
    assert coordinator.rain_delay_days("m1") == days


async def test_watering_status_unknown_without_a_reported_state(coordinator):
    coordinator.data = _watering(runningStation=0, rainDelay=2)
    assert coordinator.watering_enabled("m1") is None
    assert coordinator.rain_delay_days("m1") is None
    assert coordinator.watering_enabled("unknown") is None


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
    coordinator.client.async_get_module_page = AsyncMock(
        side_effect=lambda mid: (objs[mid], [])
    )

    await coordinator.async_setup()

    assert set(coordinator.modules) == {"m1", "g1", "pool"}
    assert coordinator.modules["m1"].is_controller is True
    assert coordinator.modules["m1"].stations[0].name == "Z1"
    assert coordinator.modules["pool"].is_controller is False
    assert coordinator.modules["g1"].is_controller is False


async def test_setup_keeps_every_input_not_just_flow_meters(coordinator):
    """``raw_inputs`` keeps the sensors the integration does not model yet.

    A rain gauge or a soil probe is an input of some other type, which
    ``_build_flow_meters`` drops -- and ``raw`` (the ``let module`` object) does
    not carry the inputs at all. Without this, such a sensor is invisible even
    in diagnostics, which is what issue #8 is blocked on.
    """
    moisture = {"id": "i2", "type": 4, "index": 2, "getName": "Sonde d'humidité"}
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        return_value=(
            {
                "name": "Ctrl",
                "type": "LR-IS",
                "typeIsWatering": True,
                "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
            },
            [dict(FLOW_INPUT), moisture],
        )
    )

    await coordinator.async_setup()

    module = coordinator.modules["m1"]
    assert [m.id for m in module.flow_meters] == ["i1"]
    assert module.raw_inputs == [dict(FLOW_INPUT), moisture]
    assert "i2" not in str(module.raw)


async def test_poll_refreshes_the_fields_the_module_page_would_freeze(coordinator):
    """The gateway's `seenAt` and every battery level must keep moving.

    `raw` comes from a >1 MB module page read once at setup, so before this
    those values only changed on a reload -- measured as 3 changes in 24 h on a
    real LR-MB-10 against 286 for a LoRa controller.
    """
    gateway = _module(id="g1", raw={"seenAt": "2026-09-21T06:00:00Z", "battery": 5})
    coordinator.modules = {"g1": gateway}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_fields = AsyncMock(
        return_value={
            "g1": {"id": "g1", "seenAt": "2026-09-21T18:00:00Z", "battery": 3}
        }
    )

    await coordinator._async_update_data()

    assert gateway.raw["seenAt"] == "2026-09-21T18:00:00Z"
    assert gateway.raw["battery"] == 3


async def test_field_refresh_merges_and_never_drops_capability_flags(coordinator):
    """The projection returns stored columns only, so it must not replace `raw`.

    `typeIsWatering` and friends are computed getters SOLEM will not project;
    replacing the record instead of merging would declassify every controller.
    """
    module = _module(id="m1", raw={"typeIsWatering": True, "battery": 5})
    coordinator.modules = {"m1": module}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_fields = AsyncMock(
        return_value={
            "m1": {"id": "m1", "battery": 4},
            "ghost": {"id": "ghost", "battery": 1},  # not a module we know
        }
    )

    await coordinator._async_update_data()

    assert module.raw["battery"] == 4
    assert module.raw["typeIsWatering"] is True
    assert module.is_controller is True
    assert "ghost" not in coordinator.modules


async def test_field_refresh_failure_keeps_the_previous_values(coordinator):
    """A blank battery or timestamp is worse than a slightly stale one."""
    module = _module(id="m1", raw={"typeIsWatering": True, "battery": 5})
    coordinator.modules = {"m1": module}
    coordinator.client.async_get_module_state = AsyncMock(return_value={"ok": 1})
    coordinator.client.async_get_module_fields = AsyncMock(
        side_effect=SolemConnectionError("boom")
    )

    result = await coordinator._async_update_data()

    assert result == {"m1": {"ok": 1}}  # the cycle still succeeds
    assert module.raw["battery"] == 5


async def test_field_refresh_auth_failure_is_reauth(coordinator):
    coordinator.modules = {"m1": _module(id="m1")}
    coordinator.client.async_get_module_state = AsyncMock(return_value={"ok": 1})
    coordinator.client.async_get_module_fields = AsyncMock(
        side_effect=SolemAuthError("nope")
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_setup_skips_module_that_errors(coordinator):
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1", "boom"])

    async def get_module_page(mid):
        if mid == "boom":
            raise SolemError("nope")
        return (
            {
                "name": "Ctrl",
                "typeIsWatering": True,
                "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
            },
            [],
        )

    coordinator.client.async_get_module_page = AsyncMock(side_effect=get_module_page)
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


async def test_update_data_never_polls_a_bluetooth_only_module(coordinator):
    """An account of Bluetooth-only modules must still set up (#11).

    Their state endpoint answers 503 by design, so polling them made every
    cycle fail and the first refresh never succeeded.
    """
    coordinator.modules = {
        "b1": _module(id="b1", type="bl-ip", raw={"isBluetoothOnly": True}),
        "b2": _module(id="b2", type="bl-ip", raw={"isBluetoothOnly": True}),
    }
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    assert await coordinator._async_update_data() == {}
    coordinator.client.async_get_module_state.assert_not_awaited()


async def test_update_data_still_fails_when_every_reachable_module_fails(coordinator):
    """Skipping Bluetooth modules must not hide a real outage."""
    coordinator.modules = {
        "m1": _module(id="m1"),
        "b1": _module(id="b1", type="bl-ip", raw={"isBluetoothOnly": True}),
    }
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("down")
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    coordinator.client.async_get_module_state.assert_awaited_once_with("m1")


async def test_update_data_total_failure_raises(coordinator):
    coordinator.modules = {"m1": _module(id="m1")}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("down")
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


def _stored(**watering) -> dict:
    """A controller record's stored ``status``, shaped like the live reply's."""
    return {
        "dialogTimeStamp": "2026-09-14T06:00:00.000Z",
        "relayIsLongPolling": False,
        "watering": {"origin": 0, "runningProgram": 0, "time": "00:00", **watering},
        "radio": [],
    }


async def test_update_data_falls_back_to_the_status_stored_on_the_record(
    coordinator,
):
    """Every gateway powered down for the winter: every poll answers 503.

    The first refresh has no previous reading to keep, so it used to fail and
    setup never completed. The cloud still holds each module's last status.
    """
    coordinator.modules = {
        "m1": _module(
            id="m1",
            raw={
                "typeIsWatering": True,
                "isOnline": False,
                "relay": "g1",
                "status": _stored(state=0, rainDelay=3, runningStation=1),
            },
        ),
        "g1": _module(id="g1", raw={"status": None}),
    }
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503 Service Unavailable")
    )
    coordinator.data = await coordinator._async_update_data()

    assert coordinator.watering_enabled("m1") is False
    assert coordinator.rain_delay_days("m1") == 3
    assert coordinator.running_station_index("m1") == 1
    assert coordinator.module_state("m1")["relay"] == "g1"
    assert coordinator.module_state("g1") == {}
    assert coordinator.relevant_module_ids() == {"m1", "g1"}


async def test_a_stored_status_never_acks_an_on_off_command(coordinator):
    """Only a status polled after the command counts, never the stored one."""
    module = _module(raw={"typeIsWatering": True, "status": _stored(state=1)})
    coordinator.modules = {"m1": module}
    coordinator.data = _watering(state=1, rainDelay=0, runningStation=0)
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=False)
    module.raw["status"] = _stored(state=0, rainDelay=0)
    # A poll fails with nothing cached for the module: the record's status,
    # though it agrees with the command, is no ack.
    coordinator.data = {}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("m1") is False
    assert "m1" in coordinator._status_pending


async def test_update_data_still_fails_without_any_stored_status(coordinator):
    coordinator.modules = {"m1": _module(id="m1", raw={"status": None})}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_live_reading_replaces_the_stored_status(coordinator):
    coordinator.modules = {
        "m1": _module(raw={"typeIsWatering": True, "status": _stored(state=0)})
    }
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("m1") is False

    # The gateway is back on: the live state wins...
    coordinator.client.async_get_module_state = AsyncMock(
        return_value={"status": {"watering": {"state": 1, "runningStation": 0}}}
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("m1") is True

    # ...and is the reading kept when it goes off again.
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("m1") is True


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


async def test_command_set_status_is_reflected_optimistically(coordinator):
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = _watering(state=1, rainDelay=0, runningStation=0)
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_set_status(module, enabled=False, days=2)
    coordinator.client.async_set_status.assert_awaited_once_with(
        "SER1", enabled=False, days=2
    )
    assert coordinator.watering_enabled("m1") is False
    assert coordinator.rain_delay_days("m1") == 2
    coordinator.async_schedule_refresh.assert_called_once()

    await coordinator.async_command_set_status(module, enabled=True)
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0


async def test_command_set_status_invents_no_status(coordinator):
    """A controller that never reports a status must not appear to."""
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = {}
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=False)
    assert coordinator.watering_enabled("m1") is None
    assert "m1" not in coordinator._status_pending
    coordinator.async_schedule_refresh.assert_called_once()


async def _set_status_then_poll(coordinator, enabled, days, polled):
    """Issue an ON/OFF command, then poll a cloud still reporting ``polled``."""
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = _watering(state=1, rainDelay=0, runningStation=0)
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=enabled, days=days)
    coordinator.client.async_get_module_state = AsyncMock(
        return_value={"status": {"watering": polled}}
    )
    coordinator.data = await coordinator._async_update_data()


async def test_command_set_status_holds_until_the_cloud_confirms_it(coordinator):
    """The reconcile poll lands before the LoRa ack: the old status must not win."""
    await _set_status_then_poll(
        coordinator, enabled=False, days=2, polled={"state": 1, "rainDelay": 0}
    )
    assert coordinator.watering_enabled("m1") is False
    assert coordinator.rain_delay_days("m1") == 2

    # The ack arrives: the cloud is authoritative again, from then on.
    coordinator.client.async_get_module_state = AsyncMock(
        return_value={"status": {"watering": {"state": 0, "rainDelay": 2}}}
    )
    coordinator.data = await coordinator._async_update_data()
    assert "m1" not in coordinator._status_pending
    coordinator.data = _watering(state=1, rainDelay=0)
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0


async def test_command_set_status_on_holds_too(coordinator):
    await _set_status_then_poll(
        coordinator, enabled=True, days=0, polled={"state": 0, "rainDelay": 3}
    )
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0


async def test_command_set_status_changing_rain_delay_while_off_holds_too(
    coordinator,
):
    """Changing the delay on an already-OFF module holds until the new delay lands."""
    await _set_status_then_poll(
        coordinator, enabled=False, days=5, polled={"state": 0, "rainDelay": 2}
    )
    assert coordinator.watering_enabled("m1") is False
    assert coordinator.rain_delay_days("m1") == 5
    assert "m1" in coordinator._status_pending

    coordinator.client.async_get_module_state = AsyncMock(
        return_value={"status": {"watering": {"state": 0, "rainDelay": 5}}}
    )
    coordinator.data = await coordinator._async_update_data()
    assert "m1" not in coordinator._status_pending
    assert coordinator.rain_delay_days("m1") == 5


async def test_command_set_status_gives_up_after_the_grace(coordinator):
    """A command the cloud never confirms stops masking what it reports."""
    await _set_status_then_poll(
        coordinator, enabled=False, days=0, polled={"state": 1, "rainDelay": 0}
    )
    enabled, days, issued_at = coordinator._status_pending["m1"]
    coordinator._status_pending["m1"] = (
        enabled,
        days,
        issued_at - ENABLE_ACK_GRACE,
    )
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0
    assert "m1" not in coordinator._status_pending


async def test_command_set_status_ignores_a_matching_cached_status(coordinator):
    """Agreement is judged on polled data only, never on the (patched) cache."""
    await _set_status_then_poll(
        coordinator, enabled=False, days=0, polled={"state": 1, "rainDelay": 0}
    )
    # Reading the status back does not count as the ack, however often.
    assert coordinator.watering_enabled("m1") is False
    assert coordinator.watering_enabled("m1") is False
    assert "m1" in coordinator._status_pending

    # Nor does a failed poll, which keeps the cached (patched) status.
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("flaky")
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("m1") is False
    assert "m1" in coordinator._status_pending


async def test_command_set_status_ignores_a_poll_started_before_it(coordinator):
    """ON, OFF, ON: a poll in flight since before the second ON reads ON too."""
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = _watering(state=1, rainDelay=0, runningStation=0)
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=False)

    async def state(_mid):
        # The command lands while this poll's request is out.
        await coordinator.async_command_set_status(module, enabled=True)
        return {"status": {"watering": {"state": 1, "rainDelay": 0}}}

    coordinator.client.async_get_module_state = AsyncMock(side_effect=state)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator._status_pending["m1"][:2] == (True, 0)


async def test_command_set_status_keeps_the_later_of_overlapping_commands(
    coordinator,
):
    module = _module()
    coordinator.modules = {"m1": module}
    coordinator.data = _watering(state=1, rainDelay=0, runningStation=0)
    coordinator.async_schedule_refresh = MagicMock()
    second_done = asyncio.Event()

    async def slow_off(*_args, **_kwargs):
        await second_done.wait()

    coordinator.client.async_set_status = AsyncMock(side_effect=slow_off)
    first = asyncio.ensure_future(
        coordinator.async_command_set_status(module, enabled=False, days=3)
    )
    await asyncio.sleep(0)
    coordinator.client.async_set_status = AsyncMock()
    await coordinator.async_command_set_status(module, enabled=True)
    second_done.set()
    await first
    assert coordinator._status_pending["m1"][:2] == (True, 0)
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0


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


# -- flow meters: discovery ---------------------------------------------------


def test_build_flow_meters_keeps_only_flow_meters():
    """Only inputs of the flow-meter type become meters, in index order."""
    meters = _build_flow_meters(
        [
            {"id": "temp", "type": 5, "index": 2},  # a temperature probe
            dict(FLOW_INPUT, id="second", index=3),
            FLOW_INPUT,
        ]
    )
    assert [m.id for m in meters] == ["i1", "second"]
    assert meters[0].name == "Flowmeter"
    assert meters[0].unit == 1
    assert meters[0].interval == 1


def test_build_flow_meters_falls_back_to_solem_name():
    """Without a user label the meter takes SOLEM's own name."""
    meters = _build_flow_meters([dict(FLOW_INPUT, name="")])
    assert meters[0].name == "Débitmètre"


def test_build_flow_meters_falls_back_to_a_generic_name():
    """With no name at all the index identifies the meter."""
    record = dict(FLOW_INPUT, name="", index=2)
    del record["getName"]
    assert _build_flow_meters([record])[0].name == "Flow meter 2"


def test_build_flow_meters_defaults_a_missing_interval():
    """A missing or zero interval falls back to one minute."""
    assert _build_flow_meters([dict(FLOW_INPUT, interval=0)])[0].interval == 1


def test_build_flow_meters_skips_unscalable_expression(caplog):
    """A meter we cannot scale is dropped rather than published mis-scaled."""
    meters = _build_flow_meters([dict(FLOW_INPUT, expression="2*x + 1")])
    assert meters == []
    assert "unsupported scaling expression" in caplog.text


# -- flow meters: rate derivation ---------------------------------------------

_NOW = dt_util.parse_datetime("2026-07-30T17:49:00+00:00")


def test_flow_rate_from_two_adjacent_ticks():
    """Consecutive ticks give the rate over the interval between them."""
    assert _compute_flow_rate(_ticks((47, 3960), (48, 3970)), _NOW, 1) == 10.0


def test_flow_rate_uses_only_the_last_two_ticks():
    """Earlier ticks in the window do not dilute the current rate."""
    record = _ticks((40, 3800), (47, 3960), (48, 3970))
    assert _compute_flow_rate(record, _NOW, 1) == 10.0


def test_flow_rate_is_zero_when_the_window_is_empty():
    """No ticks at all means no flow throughout the window."""
    assert _compute_flow_rate({"id": "i1", "computedSensorData": []}, _NOW, 1) == 0.0
    assert _compute_flow_rate({"id": "i1"}, _NOW, 1) == 0.0


def test_flow_rate_is_zero_once_the_newest_tick_goes_stale():
    """Watering that stopped reads zero, not the rate it stopped at."""
    stale = dt_util.parse_datetime("2026-07-30T18:30:00+00:00")
    assert _compute_flow_rate(_ticks((47, 3960), (48, 3970)), stale, 1) == 0.0


def test_flow_rate_is_unknown_with_a_single_fresh_tick():
    """One tick cannot yield a rate -- unknown beats a fabricated zero."""
    assert _compute_flow_rate(_ticks((48, 3970)), _NOW, 1) is None


def test_flow_rate_is_unknown_across_a_pause():
    """Ticks either side of a pause would average real flow over idle time."""
    record = _ticks((10, 3000), (48, 3970))
    assert _compute_flow_rate(record, _NOW, 1) is None


def test_flow_rate_is_unknown_for_a_duplicate_tick():
    """Two ticks at the same instant must not divide by zero."""
    assert _compute_flow_rate(_ticks((48, 3960), (48, 3970)), _NOW, 1) is None


def test_flow_rate_ignores_interpolated_and_continuity_ticks():
    """Synthesised ticks and the pre-window tick never feed a delta."""
    record = _ticks((47, 3960), (48, 3970))
    record["computedSensorData"].append(
        {
            "tickTimestamp": "2026-07-30T17:49:00+00:00",
            "value": 9999,
            "interpolated": True,
        }
    )
    record["computedSensorData"].insert(
        0,
        {
            "tickTimestamp": "2026-07-30T16:00:00+00:00",
            "value": 1,
            "flag": "lastTickBeforeStartDate",
        },
    )
    assert _compute_flow_rate(record, _NOW, 1) == 10.0


def test_flow_rate_never_goes_negative_on_a_counter_reset():
    """A meter replaced mid-window must not report negative flow."""
    assert _compute_flow_rate(_ticks((47, 3960), (48, 5)), _NOW, 1) == 0.0


def test_flow_rate_skips_ticks_missing_a_value_or_timestamp():
    """Malformed ticks are ignored rather than crashing the poll."""
    record = {
        "id": "i1",
        "computedSensorData": [
            {"tickTimestamp": None, "value": 1},
            {"value": None, "tickTimestamp": "2026-07-30T17:48:00+00:00"},
            "junk",
        ],
    }
    assert _compute_flow_rate(record, _NOW, 1) == 0.0


# -- flow meters: polling -----------------------------------------------------


def _flow_coordinator(coordinator, **overrides):
    """Wire a coordinator up with one controller carrying one flow meter.

    The polling path reads the real clock, so the ticks are anchored to "now" --
    a fixed timestamp would age past the idle window and read as no flow.
    """
    newest = dt_util.utcnow() - timedelta(seconds=30)
    module = _module(flow_meters=[_flow_meter()])
    coordinator.modules = {"m1": module}
    coordinator.data = {"m1": {}}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = AsyncMock(
        return_value=[
            {
                "id": "i1",
                "computedSensorData": [
                    {
                        "tickTimestamp": (newest - timedelta(minutes=1)).isoformat(),
                        "value": 3960,
                    },
                    {"tickTimestamp": newest.isoformat(), "value": 3970},
                ],
            }
        ]
    )
    coordinator.client.async_get_last_input_tick = AsyncMock(
        return_value={"value": 397, "timestamp": newest.isoformat()}
    )
    for name, value in overrides.items():
        setattr(coordinator.client, name, value)
    return module, newest


async def test_poll_flow_scales_the_counter_and_derives_the_rate(coordinator):
    """The raw tick is scaled by the expression; the rate comes from the window."""
    _, newest = _flow_coordinator(coordinator)
    await coordinator._async_update_data()
    reading = coordinator.flow_reading("i1")
    assert reading.volume == 3970.0  # 397 raw / 0.1
    assert reading.raw_volume == 397
    assert reading.rate == 10.0
    assert reading.timestamp == newest


async def test_poll_flow_keeps_the_previous_reading_when_the_tick_fails(coordinator):
    """A blanked cumulative sensor would punch a hole in statistics."""
    _flow_coordinator(coordinator)
    await coordinator._async_update_data()
    coordinator.client.async_get_last_input_tick = AsyncMock(
        side_effect=SolemConnectionError("offline")
    )
    await coordinator._async_update_data()
    assert coordinator.flow_reading("i1").volume == 3970.0


async def test_poll_flow_survives_a_failing_window_fetch(coordinator):
    """Losing the window costs the rate, not the total."""
    _flow_coordinator(
        coordinator,
        async_get_module_sensor_data=AsyncMock(side_effect=SolemConnectionError("x")),
    )
    await coordinator._async_update_data()
    reading = coordinator.flow_reading("i1")
    assert reading.volume == 3970.0
    assert reading.rate == 0.0  # no ticks known -> idle


async def test_poll_flow_ignores_a_meter_that_never_reported(coordinator):
    """An empty tick leaves the sensor unknown rather than reading zero."""
    _flow_coordinator(coordinator, async_get_last_input_tick=AsyncMock(return_value={}))
    await coordinator._async_update_data()
    assert coordinator.flow_reading("i1") is None


async def test_poll_flow_maps_auth_error_to_config_entry_auth_failed(coordinator):
    """An expired session during a flow poll re-triggers the auth flow."""
    _flow_coordinator(
        coordinator,
        async_get_module_sensor_data=AsyncMock(side_effect=SolemAuthError("bad")),
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_poll_flow_maps_tick_auth_error_to_config_entry_auth_failed(coordinator):
    """The same holds for the per-meter tick request."""
    _flow_coordinator(
        coordinator,
        async_get_last_input_tick=AsyncMock(side_effect=SolemAuthError("bad")),
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_poll_flow_skips_irrelevant_modules(coordinator):
    """A pool module's own meters are none of this integration's business."""
    pool = _module(
        id="pool",
        raw={"typeIsWatering": False, "typeIsPoolProduct": True},
        stations=[],
        flow_meters=[_flow_meter(id="pool_i")],
    )
    coordinator.modules = {"pool": pool}
    coordinator.data = {"pool": {}}
    coordinator.client.async_get_module_state = AsyncMock(return_value={"ok": 1})
    coordinator.client.async_get_module_sensor_data = AsyncMock(return_value=[])
    coordinator.client.async_get_last_input_tick = AsyncMock(return_value={})
    await coordinator._async_update_data()
    coordinator.client.async_get_last_input_tick.assert_not_awaited()


async def test_standalone_meter_module_is_relevant(coordinator):
    """SOLEM also sells the meter as its own non-watering module."""
    meter_module = _module(
        id="lrfl",
        raw={"typeIsWatering": False, "typeIsLrFl": True},
        stations=[],
        flow_meters=[_flow_meter()],
    )
    coordinator.modules = {"lrfl": meter_module}
    coordinator.data = {"lrfl": {}}
    assert coordinator.relevant_module_ids() == {"lrfl"}


async def test_setup_warns_about_bluetooth_only_modules(coordinator, caplog):
    """A user must be told why a Bluetooth controller shows nothing."""
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["b1", "m1"])
    objs = {
        "b1": {"name": "Tennis", "type": "bl-ip", "isBluetoothOnly": True},
        "m1": {"name": "Ctrl", "type": "lr-is", "isBluetoothOnly": False},
    }
    coordinator.client.async_get_module_page = AsyncMock(
        side_effect=lambda mid: (objs[mid], [])
    )
    await coordinator.async_setup()
    [warning] = [r.message for r in caplog.records if "over Bluetooth" in r.message]
    assert "Tennis (bl-ip)" in warning
    assert "Ctrl" not in warning


async def test_setup_warns_when_declared_inputs_cannot_be_parsed(coordinator, caplog):
    """A renamed ``var inputs`` must not fail silently."""
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        return_value=({"name": "Ctrl", "typeIsWatering": True, "numberOfInputs": 1}, [])
    )
    await coordinator.async_setup()
    assert "declares 1 input(s) but none could be parsed" in caplog.text


async def test_setup_builds_flow_meters(coordinator):
    """Discovery attaches the parsed meters to their module."""
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        return_value=(
            {"name": "Ctrl", "typeIsWatering": True, "numberOfInputs": 1},
            [FLOW_INPUT],
        )
    )
    await coordinator.async_setup()
    assert [m.id for m in coordinator.modules["m1"].flow_meters] == ["i1"]


async def test_flow_rate_is_unknown_when_the_newest_timestamp_is_unparseable():
    """A tick whose timestamp we cannot read yields no rate."""
    record = {
        "id": "i1",
        "computedSensorData": [{"tickTimestamp": "not-a-date", "value": 1}],
    }
    assert _compute_flow_rate(record, _NOW, 1) is None


async def test_poll_flow_skips_a_meter_whose_value_cannot_be_scaled(coordinator):
    """Belt-and-braces: a meter that slipped through with a bad expression."""
    _flow_coordinator(coordinator)
    coordinator.modules["m1"].flow_meters = [_flow_meter(expression="2*x")]
    await coordinator._async_update_data()
    assert coordinator.flow_reading("i1") is None


# -- rain gauges (#8) -----------------------------------------------------------

# Trimmed from the LR-MS "Rain Sensor" of issue #8.
RAIN_INPUT = {
    "id": "r1",
    "name": "",
    "getName": "Pluvio Meter",
    "type": 14,
    "unit": 13,
    "index": 1,
    "expression": "x*0.2794",
    "typeIsCumulativeData": True,
    "highThreshold": 2,
    "isLastMeasureBeyondThresholds": False,
}


def _rain_module(**kwargs) -> SolemModule:
    base = dict(
        id="ms",
        type="lr-ms",
        display_type="lr-ms",
        raw={"typeIsWatering": False, "typeIsSensor": True},
        stations=[],
        programs=[],
        rain_gauges=_build_rain_gauges([dict(RAIN_INPUT)]),
    )
    base.update(kwargs)
    return _module(**base)


def test_build_rain_gauges_keeps_only_type_14():
    """Type 2 is the on/off rain *sensor*, a different thing."""
    gauges = _build_rain_gauges(
        [dict(RAIN_INPUT), {**RAIN_INPUT, "id": "r2", "type": 2}, dict(FLOW_INPUT)]
    )
    assert gauges == [
        SolemRainGauge(
            id="r1", name="Pluvio Meter", index=1, expression="x*0.2794", raw=RAIN_INPUT
        )
    ]


def test_build_rain_gauges_skips_unscalable_expression(caplog):
    assert _build_rain_gauges([{**RAIN_INPUT, "expression": "Math.pow(x,2)"}]) == []
    assert "Ignoring SOLEM rain gauge r1" in caplog.text


async def test_rain_gauge_module_is_relevant(coordinator):
    """A standalone LR-MS is not a controller, yet it is ours."""
    coordinator.modules = {"ms": _rain_module()}
    coordinator.data = {"ms": {}}
    assert coordinator.relevant_module_ids() == {"ms"}


async def test_poll_scales_the_rain_total_and_keeps_the_live_flags(coordinator):
    newest = dt_util.utcnow() - timedelta(minutes=3)
    coordinator.modules = {"ms": _rain_module()}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = AsyncMock(
        return_value=[
            {
                **RAIN_INPUT,
                "isLastMeasureBeyondThresholds": True,
                "computedSensorData": [],
            }
        ]
    )
    coordinator.client.async_get_last_input_tick = AsyncMock(
        return_value={"value": 100, "timestamp": newest.isoformat()}
    )

    await coordinator._async_update_data()

    reading = coordinator.rain_reading("r1")
    assert reading.total == 27.94  # 100 tips x 0.2794 mm
    assert reading.raw_total == 100
    assert reading.timestamp == newest
    # The window carries the flag even with no ticks in it.
    assert coordinator.input_record("r1")["isLastMeasureBeyondThresholds"] is True


async def test_poll_keeps_the_previous_rain_total_when_the_tick_fails(coordinator):
    coordinator.modules = {"ms": _rain_module()}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = AsyncMock(return_value=[])
    coordinator.client.async_get_last_input_tick = AsyncMock(
        return_value={"value": 100, "timestamp": dt_util.utcnow().isoformat()}
    )
    await coordinator._async_update_data()
    coordinator.client.async_get_last_input_tick = AsyncMock(
        side_effect=SolemConnectionError("offline")
    )
    await coordinator._async_update_data()
    assert coordinator.rain_reading("r1").total == 27.94
    assert coordinator.input_record("r1") is None  # never returned by a window


# -- rain sensors (#8) ----------------------------------------------------------

# Trimmed from the dry-contact rain sensor on an LR-IP-ECO, issue #8.
RAIN_SENSOR_INPUT = {
    "id": "c1",
    "name": "Capteur de pluie",
    "getName": "Capteur de pluie",
    "type": 2,
    "unit": 0,
    "index": 2,
    "expression": "x",
    "lowThreshold": 0,
    "highThreshold": 1,
    "typeIsTOR": True,
    "isLastMeasureBeyondThresholds": True,
}
# The same controller's turbine (#12).
TURBINE_INPUT = {
    "id": "t1",
    "name": "Turbine",
    "type": 33,
    "unit": 1,
    "index": 1,
    "expression": "javascript:if(x<=0){0.0}else{x}",
}


def test_build_rain_sensors_keeps_only_type_2_in_index_order():
    second = {**RAIN_SENSOR_INPUT, "id": "c2", "name": "", "index": 3}
    sensors = _build_rain_sensors(
        [second, dict(TURBINE_INPUT), dict(RAIN_SENSOR_INPUT), dict(RAIN_INPUT)]
    )
    assert sensors == [
        SolemRainSensor(
            id="c1", name="Capteur de pluie", index=2, raw=RAIN_SENSOR_INPUT
        ),
        # A blank label falls back to SOLEM's own.
        SolemRainSensor(id="c2", name="Capteur de pluie", index=3, raw=second),
    ]


async def test_rain_sensor_module_is_relevant(coordinator):
    """A sensor module with only a rain sensor is ours, like a gauge's."""
    sensor = _build_rain_sensors([dict(RAIN_SENSOR_INPUT)])
    coordinator.modules = {
        "ms": _module(
            id="ms",
            raw={"typeIsWatering": False},
            stations=[],
            programs=[],
            rain_sensors=sensor,
        )
    }
    coordinator.data = {"ms": {}}
    assert coordinator.relevant_module_ids() == {"ms"}


@pytest.mark.parametrize(("value", "wet"), [(1, True), (0, False)])
async def test_poll_reads_the_rain_sensor_from_its_newest_tick(coordinator, value, wet):
    """A controller with no meter or gauge still gets its inputs polled."""
    newest = dt_util.utcnow() - timedelta(hours=12)
    coordinator.modules = {
        "m1": _module(rain_sensors=_build_rain_sensors([dict(RAIN_SENSOR_INPUT)]))
    }
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = AsyncMock(return_value=[])
    coordinator.client.async_get_last_input_tick = AsyncMock(
        return_value={"value": value, "timestamp": newest.isoformat()}
    )

    await coordinator._async_update_data()

    reading = coordinator.rain_sensor_reading("c1")
    assert reading.wet is wet
    # However old: the last tick stands until the next one.
    assert reading.timestamp == newest
    coordinator.client.async_get_last_input_tick.assert_awaited_once_with("c1")


async def test_poll_keeps_the_previous_rain_sensor_reading_on_failure(coordinator):
    coordinator.modules = {
        "m1": _module(rain_sensors=_build_rain_sensors([dict(RAIN_SENSOR_INPUT)]))
    }
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = AsyncMock(return_value=[])
    coordinator.client.async_get_last_input_tick = AsyncMock(
        return_value={"value": 1, "timestamp": dt_util.utcnow().isoformat()}
    )
    await coordinator._async_update_data()
    coordinator.client.async_get_last_input_tick = AsyncMock(
        side_effect=SolemConnectionError("offline")
    )
    await coordinator._async_update_data()
    assert coordinator.rain_sensor_reading("c1").wet is True
    assert coordinator.rain_sensor_reading("other") is None


# -- turbines (#12) -----------------------------------------------------------

# The #12 day on an LR-IP-ECO, as its window returned it (litres per minute):
# two one-minute bursts in the early hours, then a watering run ending on the 0
# tick a turbine sends when the water stops. MySOLEM charted 4.1, 7.2 and 46.5 L.
TURBINE_TICKS = [
    ("2026-10-11T01:01:00.000Z", 4.058388237899999),
    ("2026-10-11T01:02:00.000Z", 0),
    ("2026-10-11T05:01:00.000Z", 7.237591665819),
    ("2026-10-11T05:02:00.000Z", 0),
    ("2026-10-11T17:34:00.000Z", 2.185219681484),
    # No 17:35 tick: MySOLEM leaves the gap unfilled, and so does the sum.
    ("2026-10-11T17:36:00.000Z", 8.0357307584),
    ("2026-10-11T17:37:00.000Z", 2.427487961971),
    ("2026-10-11T17:38:00.000Z", 7.2019639999999985),
    ("2026-10-11T17:39:00.000Z", 3.495349442475),
    ("2026-10-11T17:40:00.000Z", 8.1746021056),
    ("2026-10-11T17:41:00.000Z", 8.500848801983999),
    ("2026-10-11T17:42:00.000Z", 6.431647349003999),
    ("2026-10-11T17:43:00.000Z", 0),
]

# Local midnight of 2026-10-11 in Paris (CEST, UTC+2), as the coordinator
# passes it: in local time, so its date is the day being counted.
PARIS_DAY_START = datetime(2026, 10, 11, tzinfo=ZoneInfo("Europe/Paris"))


def _turbine_record(ticks=TURBINE_TICKS, **extra) -> dict:
    return {
        "id": "t1",
        "type": 33,
        "computedSensorData": [
            {"tickTimestamp": timestamp, "value": value, **extra}
            for timestamp, value in ticks
        ],
    }


def _at(timestamp: str) -> datetime:
    return dt_util.parse_datetime(timestamp)


def test_build_turbines_keeps_only_type_33_in_index_order():
    second = {**TURBINE_INPUT, "id": "t2", "name": "", "index": 3}
    turbines = _build_turbines(
        [second, dict(RAIN_SENSOR_INPUT), dict(TURBINE_INPUT), dict(FLOW_INPUT)]
    )
    assert turbines == [
        # No ``interval`` on the record: one tick a minute, as on the real one.
        SolemTurbine(
            id="t1", name="Turbine", index=1, unit=1, interval=1, raw=TURBINE_INPUT
        ),
        # Neither a label nor SOLEM's own: a generic name.
        SolemTurbine(
            id="t2", name="Turbine 3", index=3, unit=1, interval=1, raw=second
        ),
    ]


def test_turbine_day_total_matches_mysolem():
    """4.1 + 7.2 + 46.5 L on MySOLEM's chart, to the rounding."""
    reading = _compute_turbine_reading(
        _turbine_record(), _at("2026-10-11T17:50:00Z"), PARIS_DAY_START, 1, None
    )
    assert reading == SolemTurbineReading(
        day=date(2026, 10, 11),
        volume_today=57.75,
        rate=0.0,
        timestamp=_at("2026-10-11T17:43:00Z"),
    )


def test_turbine_rate_is_the_newest_tick_while_water_flows():
    ticks = [t for t in TURBINE_TICKS if t[0] < "2026-10-11T17:43"]
    reading = _compute_turbine_reading(
        _turbine_record(ticks), _at("2026-10-11T17:45:00Z"), PARIS_DAY_START, 1, None
    )
    assert reading.rate == 6.43
    assert reading.timestamp == _at("2026-10-11T17:42:00Z")


def test_turbine_rate_falls_to_zero_when_the_stop_tick_never_arrives():
    ticks = [t for t in TURBINE_TICKS if t[0] < "2026-10-11T17:43"]
    reading = _compute_turbine_reading(
        _turbine_record(ticks), _at("2026-10-11T17:53:00Z"), PARIS_DAY_START, 1, None
    )
    assert reading.rate == 0.0
    # The volume already through is still counted.
    assert reading.volume_today == 57.75


def test_turbine_counts_only_todays_ticks_but_reads_the_rate_from_any():
    """Ticks before local midnight are yesterday's, yet still say water flows."""
    reading = _compute_turbine_reading(
        _turbine_record([("2026-10-10T21:59:00.000Z", 5.0)]),
        _at("2026-10-10T22:01:00Z"),
        PARIS_DAY_START,
        1,
        None,
    )
    assert reading.volume_today == 0.0
    assert reading.rate == 5.0


def test_turbine_skips_interpolated_and_continuity_ticks():
    for extra in ({"interpolated": True}, {"flag": "lastTickBeforeStartDate"}):
        reading = _compute_turbine_reading(
            _turbine_record(**extra),
            _at("2026-10-11T17:50:00Z"),
            PARIS_DAY_START,
            1,
            None,
        )
        assert reading.volume_today == 0.0
        assert reading.timestamp is None


def test_turbine_skips_ticks_with_an_unparseable_timestamp():
    reading = _compute_turbine_reading(
        _turbine_record([("not a date", 9.0), *TURBINE_TICKS]),
        _at("2026-10-11T17:50:00Z"),
        PARIS_DAY_START,
        1,
        None,
    )
    assert reading.volume_today == 57.75


def test_turbine_volume_scales_with_the_tick_interval():
    reading = _compute_turbine_reading(
        _turbine_record(), _at("2026-10-11T17:50:00Z"), PARIS_DAY_START, 2, None
    )
    assert reading.volume_today == 115.5


def test_turbine_day_total_never_drops_within_a_day():
    """A short window must not take back litres already reported."""
    previous = SolemTurbineReading(
        day=date(2026, 10, 11),
        volume_today=60.0,
        rate=0.0,
        timestamp=_at("2026-10-11T17:43:00Z"),
    )
    reading = _compute_turbine_reading(
        _turbine_record(), _at("2026-10-11T17:55:00Z"), PARIS_DAY_START, 1, previous
    )
    assert reading.volume_today == 60.0


def test_turbine_day_total_starts_again_on_a_new_day():
    previous = SolemTurbineReading(
        day=date(2026, 10, 10),
        volume_today=60.0,
        rate=0.0,
        timestamp=_at("2026-10-10T17:43:00Z"),
    )
    reading = _compute_turbine_reading(
        _turbine_record([]), _at("2026-10-10T22:05:00Z"), PARIS_DAY_START, 1, previous
    )
    assert reading.day == date(2026, 10, 11)
    assert reading.volume_today == 0.0
    assert reading.rate == 0.0
    # Its newest tick is still the one from yesterday.
    assert reading.timestamp == previous.timestamp


def _turbine_coordinator(coordinator, window):
    module = _module(turbines=_build_turbines([dict(TURBINE_INPUT)]))
    coordinator.modules = {"m1": module}
    coordinator.data = {"m1": {}}
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_sensor_data = window
    coordinator.client.async_get_last_input_tick = AsyncMock(return_value={})
    return module


async def test_poll_reaches_back_to_local_midnight_for_a_turbine(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-11T17:50:00+00:00")
    window = AsyncMock(return_value=[_turbine_record()])
    _turbine_coordinator(coordinator, window)

    await coordinator._async_update_data()

    window.assert_awaited_once_with(
        "m1", "2026-10-10T22:00:00Z", "2026-10-11T17:50:00Z"
    )
    assert coordinator.turbine_reading("t1").volume_today == 57.75
    # The turbine's raw newest tick is never asked for.
    coordinator.client.async_get_last_input_tick.assert_not_awaited()


async def test_poll_counts_the_25_hour_day_the_clocks_go_back(
    hass, coordinator, freezer
):
    """2026-10-25 in Paris starts at 22:00 UTC the day before and lasts 25 h."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-25T22:30:00+00:00")
    window = AsyncMock(
        return_value=[
            _turbine_record(
                [
                    ("2026-10-24T21:55:00.000Z", 1.0),  # 23:55 on the 24th
                    ("2026-10-24T22:05:00.000Z", 2.0),  # 00:05 CEST
                    ("2026-10-25T00:30:00.000Z", 4.0),  # 02:30 CEST
                    ("2026-10-25T01:30:00.000Z", 8.0),  # 02:30 CET
                    ("2026-10-25T22:20:00.000Z", 16.0),  # 23:20 CET
                ]
            )
        ]
    )
    _turbine_coordinator(coordinator, window)

    await coordinator._async_update_data()

    assert window.await_args.args[1] == "2026-10-24T22:00:00Z"
    reading = coordinator.turbine_reading("t1")
    assert reading.day == date(2026, 10, 25)
    assert reading.volume_today == 30.0


async def test_poll_keeps_the_turbine_reading_when_the_window_fails(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-11T17:50:00+00:00")
    _turbine_coordinator(coordinator, AsyncMock(return_value=[_turbine_record()]))
    await coordinator._async_update_data()

    coordinator.client.async_get_module_sensor_data = AsyncMock(
        side_effect=SolemConnectionError("offline")
    )
    await coordinator._async_update_data()

    assert coordinator.turbine_reading("t1").volume_today == 57.75
    assert coordinator.turbine_reading("other") is None


async def test_poll_window_stays_short_without_a_turbine(coordinator, freezer):
    """Only a turbine needs the whole day; everything else reads 15 minutes."""
    freezer.move_to("2026-10-11T17:50:00+00:00")
    _flow_coordinator(coordinator)
    await coordinator._async_update_data()
    coordinator.client.async_get_module_sensor_data.assert_awaited_once_with(
        "m1", "2026-10-11T17:35:00Z", "2026-10-11T17:50:00Z"
    )


async def test_turbine_module_is_relevant(coordinator):
    """A non-watering module carrying a turbine is ours, like a meter's."""
    coordinator.modules = {
        "t": _module(
            id="t",
            raw={"typeIsWatering": False},
            stations=[],
            programs=[],
            turbines=_build_turbines([dict(TURBINE_INPUT)]),
        )
    }
    coordinator.data = {"t": {}}
    assert coordinator.relevant_module_ids() == {"t"}


async def test_setup_builds_turbines(coordinator):
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        return_value=(
            {
                "name": "Ceyreste",
                "type": "lr-ip-eco",
                "typeIsWatering": True,
                "outputs": [{"id": "o1", "name": "Potager", "index": 1}],
            },
            [dict(TURBINE_INPUT), dict(RAIN_SENSOR_INPUT)],
        )
    )

    await coordinator.async_setup()

    assert [t.id for t in coordinator.modules["m1"].turbines] == ["t1"]


# -- agricultural (LR-AG) modules ---------------------------------------------

# The flags an LR-AG's module record carries (#5): not a watering type.
AG_FLAGS = {
    "typeIsWatering": False,
    "typeIsAgricultural": True,
    "typeIsLrag": True,
    "isBluetoothOnly": False,
}


def _ag_module(**kwargs) -> SolemModule:
    base = dict(
        id="ag1",
        name="Field",
        serial="AGSER",
        type="lr-ag",
        display_type="lr-ag",
        raw=dict(AG_FLAGS),
        stations=[
            SolemStation(id="o1", name="North", index=1),
            SolemStation(id="o2", name="South", index=2),
            SolemStation(id="o3", name="Station 3", index=3),
        ],
        # One program per station, counted from 0.
        programs=[
            SolemProgram(id="pr1", name="North", index=0),
            SolemProgram(id="pr2", name="South", index=1),
            SolemProgram(id="pr3", name="Station 3", index=2),
        ],
    )
    base.update(kwargs)
    return SolemModule(**base)


def _ag_output(index: int, state: int = 0, rain_delay: int = 0, time="00:00"):
    """One entry of ``status.ag.outputs``, whose ``index`` counts from 0."""
    return {"index": index, "state": state, "time": time, "rainDelay": rain_delay}


def _ag_state(*outputs) -> dict:
    """An LR-AG live state, shaped like a recorded one."""
    return {
        "status": {
            "dialogTimeStamp": "2026-07-01T06:00:00.000Z",
            "relayIsLongPolling": True,
            "ag": {"outputs": list(outputs)},
            "radio": [],
        },
        "lastRadioCommunication": "2026-07-01T06:00:00.000Z",
        "batteryVoltage": 90,
        "relay": "g1",
        "waitingForStatusUpdate": False,
    }


def test_an_agricultural_module_is_a_controller():
    assert _ag_module().is_agricultural is True
    assert _ag_module().is_controller is True
    # Either of SOLEM's two agricultural flags admits it.
    assert _ag_module(raw={"typeIsAgricultural": True}).is_controller is True
    assert _ag_module(raw={"typeIsLrag": True}).is_controller is True
    assert _module().is_agricultural is False
    # Still needs stations, like any controller.
    assert _ag_module(stations=[]).is_controller is False


def test_a_bluetooth_only_agricultural_module_is_not_a_controller():
    """A BL-AG is out for the same reason as any Bluetooth-only controller."""
    module = _ag_module(type="bl-ag", raw={"typeIsAgricultural": True})
    assert module.is_bluetooth_only is True
    assert module.is_controller is False


def test_paired_program_counts_programs_from_zero():
    module = _ag_module()
    assert module.paired_program(module.stations[1]).id == "pr2"
    assert module.paired_program(SolemStation(id="o9", name="x", index=9)) is None


async def test_setup_names_ag_stations_after_their_programs(coordinator):
    """An LR-AG keeps factory output names; the app's names are on its programs."""
    objs = {
        "ag1": {
            "name": "Field",
            "type": "lr-ag",
            **AG_FLAGS,
            "outputs": [
                {"id": "o1", "name": "Station 1", "index": 1},
                {"id": "o2", "name": "Station 2", "index": 2},
                {"id": "o3", "name": "Station 3", "index": 3},
            ],
            "programs": [
                {"id": "pr1", "name": "North", "index": 0},
                {"id": "pr2", "name": "South", "index": 1},
                {"id": "pr3", "name": "", "index": 2},  # never renamed
            ],
        },
        "m1": {
            "name": "Ctrl",
            "type": "lr-is",
            "typeIsWatering": True,
            "outputs": [{"id": "s1", "name": "Z1", "index": 1}],
            "programs": [{"id": "p1", "name": "Matin", "index": 0}],
        },
    }
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["ag1", "m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        side_effect=lambda mid: (objs[mid], [])
    )

    await coordinator.async_setup()

    module = coordinator.modules["ag1"]
    assert module.is_controller is True
    assert [s.name for s in module.stations] == ["North", "South", "Station 3"]
    assert module.find_station("South").id == "o2"
    # A watering controller keeps its own station names.
    assert coordinator.modules["m1"].stations[0].name == "Z1"


async def test_ag_running_outputs_reads_every_output(coordinator):
    """Several stations can water at once, and a program run reports ``state`` 2."""
    coordinator.modules = {"ag1": _ag_module()}
    coordinator.data = {
        "ag1": _ag_state(
            _ag_output(2, state=2, time="00:40"),  # a program run
            _ag_output(0, state=1, time="00:05"),  # a manual run
            _ag_output(1, state=0),
            {"index": "x", "state": 1},
            {"state": 1},
            "garbage",
            {"index": 5, "state": "on"},
        )
    }
    assert list(coordinator.ag_running_outputs("ag1")) == [1, 3]
    assert coordinator.ag_running_outputs("ag1")[3]["time"] == "00:40"
    assert coordinator.ag_running_outputs("unknown") == {}


@pytest.mark.parametrize(
    ("delays", "enabled", "days"),
    [
        ((0, 0, 0), True, 0),
        # 255 is a permanent OFF (the recorded LR-AG state reads exactly this).
        ((255, 255, 255), False, 0),
        ((3, 3, 3), False, 3),
        # On while any output still is.
        ((0, 255, 3), True, 0),
        # Every output off: back on with the first one that comes back.
        ((255, 3, 5), False, 3),
    ],
)
async def test_reported_ag_status(coordinator, delays, enabled, days):
    coordinator.modules = {"ag1": _ag_module()}
    coordinator.data = {
        "ag1": _ag_state(
            *(_ag_output(i, rain_delay=d) for i, d in enumerate(delays)),
            {"index": 3, "rainDelay": "x"},  # unreadable: ignored
        )
    }
    assert coordinator.watering_enabled("ag1") is enabled
    assert coordinator.rain_delay_days("ag1") == days


async def test_ag_status_unknown_without_any_rain_delay(coordinator):
    coordinator.data = {"ag1": _ag_state({"index": 0, "state": 1})}
    assert coordinator.watering_enabled("ag1") is None
    assert coordinator.rain_delay_days("ag1") is None


async def test_ag_poll_reads_status_and_admits_its_gateway(coordinator):
    module = _ag_module()
    gateway = _module(id="g1", raw={"typeIsRelay": True}, stations=[])
    coordinator.modules = {"ag1": module, "g1": gateway}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=lambda mid: (
            _ag_state(*(_ag_output(i, rain_delay=255) for i in range(3)))
            if mid == "ag1"
            else {}
        )
    )

    coordinator.data = await coordinator._async_update_data()

    assert coordinator.watering_enabled("ag1") is False
    assert coordinator.rain_delay_days("ag1") == 0
    assert coordinator.ag_running_outputs("ag1") == {}
    assert coordinator.relevant_module_ids() == {"ag1", "g1"}


async def test_ag_command_run_station_names_its_program(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {}
    coordinator.client.async_run_ag_station = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_run_station(module, module.stations[1], 30)

    coordinator.client.async_run_ag_station.assert_awaited_once_with(
        "AGSER", "ag1", "pr2", 1, 30
    )
    # Shown at once, even before the first poll filled the cache.
    assert list(coordinator.ag_running_outputs("ag1")) == [2]
    coordinator.async_schedule_refresh.assert_called_once()


async def test_ag_command_run_station_keeps_other_stations_running(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {"ag1": _ag_state(_ag_output(0, state=2), _ag_output(1))}
    coordinator.client.async_run_ag_station = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_run_station(module, module.stations[1], 5)

    assert list(coordinator.ag_running_outputs("ag1")) == [1, 2]
    # The rest of the live state is left alone.
    assert coordinator.module_state("ag1")["relay"] == "g1"


async def test_ag_command_run_station_without_a_program(coordinator):
    module = _ag_module(programs=[])
    coordinator.modules = {"ag1": module}
    coordinator.data = {}
    coordinator.client.async_run_ag_station = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_run_station(module, module.stations[0], 5)

    coordinator.client.async_run_ag_station.assert_awaited_once_with(
        "AGSER", "ag1", "o1", 0, 5
    )


async def test_ag_command_stop_stops_every_station(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {
        "ag1": _ag_state(_ag_output(0, state=1), _ag_output(2, state=2))
    }
    coordinator.client.async_stop_ag_stations = AsyncMock()
    coordinator.client.async_stop = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_stop(module)

    coordinator.client.async_stop_ag_stations.assert_awaited_once_with(
        "AGSER", [0, 1, 2]
    )
    coordinator.client.async_stop.assert_not_awaited()
    assert coordinator.ag_running_outputs("ag1") == {}
    coordinator.async_schedule_refresh.assert_called_once()


async def test_ag_command_stop_station_stops_only_that_one(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {
        "ag1": _ag_state(_ag_output(0, state=1), _ag_output(2, state=2))
    }
    coordinator.client.async_stop_ag_station = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_stop_ag_station(module, module.stations[2])

    coordinator.client.async_stop_ag_station.assert_awaited_once_with(
        "AGSER", "ag1", "o3", 2
    )
    assert list(coordinator.ag_running_outputs("ag1")) == [1]
    coordinator.async_schedule_refresh.assert_called_once()


async def test_ag_command_run_program_runs_its_station(coordinator):
    """An LR-AG has no program-start command: its program's station is run."""
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {}
    coordinator.run_minutes = {"o2": 25}
    coordinator.client.async_run_ag_station = AsyncMock()
    coordinator.client.async_run_program = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_run_program(module, module.programs[1])

    coordinator.client.async_run_ag_station.assert_awaited_once_with(
        "AGSER", "ag1", "pr2", 1, 25
    )
    coordinator.client.async_run_program.assert_not_awaited()
    assert list(coordinator.ag_running_outputs("ag1")) == [2]


async def test_ag_command_run_program_without_a_station(coordinator):
    module = _ag_module()
    orphan = SolemProgram(id="pr9", name="Spare", index=8)
    module.programs.append(orphan)
    coordinator.client.async_run_ag_station = AsyncMock()

    with pytest.raises(ServiceValidationError):
        await coordinator.async_command_run_program(module, orphan)
    coordinator.client.async_run_ag_station.assert_not_awaited()


async def test_ag_command_set_status_uses_its_own_endpoint(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.data = {"ag1": _ag_state(*(_ag_output(i) for i in range(3)))}
    coordinator.client.async_set_ag_status = AsyncMock()
    coordinator.client.async_set_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()

    await coordinator.async_command_set_status(module, enabled=False, days=4)

    coordinator.client.async_set_ag_status.assert_awaited_once_with(
        "ag1", enabled=False, days=4
    )
    coordinator.client.async_set_status.assert_not_awaited()
    assert coordinator._status_pending["ag1"][:2] == (False, 4)
    # Patched the way the LR-AG reports it: per output.
    outputs = coordinator.module_state("ag1")["status"]["ag"]["outputs"]
    assert [o["rainDelay"] for o in outputs] == [4, 4, 4]
    assert coordinator.rain_delay_days("ag1") == 4

    # A permanent OFF is reported as 255, not 0.
    await coordinator.async_command_set_status(module, enabled=False)
    outputs = coordinator.module_state("ag1")["status"]["ag"]["outputs"]
    assert [o["rainDelay"] for o in outputs] == [255, 255, 255]


async def test_ag_command_set_status_holds_until_the_cloud_confirms_it(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    on = _ag_state(*(_ag_output(i) for i in range(3)))
    coordinator.data = {"ag1": on}
    coordinator.client.async_set_ag_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=False, days=2)

    # The reconcile poll still reads every output ON.
    coordinator.client.async_get_module_state = AsyncMock(return_value=on)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("ag1") is False
    assert coordinator.rain_delay_days("ag1") == 2

    # Then the ack: every output reports the delay.
    coordinator.client.async_get_module_state = AsyncMock(
        return_value=_ag_state(*(_ag_output(i, rain_delay=2) for i in range(3)))
    )
    coordinator.data = await coordinator._async_update_data()
    assert "ag1" not in coordinator._status_pending
    assert coordinator.rain_delay_days("ag1") == 2


async def test_a_classic_module_ignores_a_null_ag_block(coordinator):
    coordinator.modules = {"m1": _module()}
    coordinator.data = {
        "m1": {"status": {"ag": None, "watering": {"state": 1, "rainDelay": 0}}}
    }
    assert coordinator.watering_enabled("m1") is True
    assert coordinator.rain_delay_days("m1") == 0


async def test_a_classic_module_reads_watering_even_if_it_carries_ag_outputs(
    coordinator,
):
    coordinator.modules = {"m1": _module()}
    coordinator.data = {
        "m1": {
            "status": {
                "ag": {"outputs": [_ag_output(0, rain_delay=0)]},
                "watering": {"state": 0, "rainDelay": 0},
            }
        }
    }
    assert coordinator.watering_enabled("m1") is False


async def test_ag_status_ignores_outputs_that_are_not_stations(coordinator):
    """An orphan output the all-stations command never touches cannot block OFF."""
    coordinator.modules = {"ag1": _ag_module()}
    coordinator.data = {
        "ag1": _ag_state(
            *(_ag_output(i, rain_delay=255) for i in range(3)),
            _ag_output(7, rain_delay=0),
        )
    }
    assert coordinator.watering_enabled("ag1") is False


async def test_ag_command_set_status_rejects_a_rain_delay_over_the_cap(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    coordinator.client.async_set_ag_status = AsyncMock()
    with pytest.raises(ServiceValidationError, match="15"):
        await coordinator.async_command_set_status(module, enabled=False, days=16)
    coordinator.client.async_set_ag_status.assert_not_awaited()


async def test_ag_command_set_status_on_is_acked_by_a_later_poll(coordinator):
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    off = _ag_state(*(_ag_output(i, rain_delay=255) for i in range(3)))
    coordinator.data = {"ag1": off}
    coordinator.client.async_set_ag_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=True)
    assert coordinator._status_pending["ag1"][:2] == (True, 0)

    # Still OFF at the cloud: held.
    coordinator.client.async_get_module_state = AsyncMock(return_value=off)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator._status_pending["ag1"][:2] == (True, 0)
    assert coordinator.watering_enabled("ag1") is True

    # Every station output back at rainDelay 0: acked.
    coordinator.client.async_get_module_state = AsyncMock(
        return_value=_ag_state(*(_ag_output(i) for i in range(3)))
    )
    coordinator.data = await coordinator._async_update_data()
    assert "ag1" not in coordinator._status_pending
    assert coordinator.watering_enabled("ag1") is True


async def test_ag_command_set_status_ignores_a_poll_started_before_it(coordinator):
    """OFF, ON: a poll in flight since before the ON reads every output ON too."""
    module = _ag_module()
    coordinator.modules = {"ag1": module}
    on = _ag_state(*(_ag_output(i) for i in range(3)))
    coordinator.data = {"ag1": on}
    coordinator.client.async_set_ag_status = AsyncMock()
    coordinator.async_schedule_refresh = MagicMock()
    await coordinator.async_command_set_status(module, enabled=False)

    async def state(_mid):
        # The command lands while this poll's request is out.
        await coordinator.async_command_set_status(module, enabled=True)
        return on

    coordinator.client.async_get_module_state = AsyncMock(side_effect=state)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator._status_pending["ag1"][:2] == (True, 0)


async def test_update_data_falls_back_to_an_ag_record_status(coordinator):
    module = _ag_module(
        raw={
            **AG_FLAGS,
            "status": _ag_state(_ag_output(0, rain_delay=255))["status"],
        }
    )
    coordinator.modules = {"ag1": module}
    coordinator.client.async_get_module_state = AsyncMock(
        side_effect=SolemConnectionError("503")
    )
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.watering_enabled("ag1") is False
    assert coordinator.rain_delay_days("ag1") == 0


# -- program schedules --------------------------------------------------------

T0 = "2026-10-01T10:00:00.000Z"
T1 = "2026-10-02T08:30:00.000Z"


def _program_raw(**kwargs) -> dict:
    """A classic program record: every day at 06:00, 10 minutes on station 1."""
    base = {
        "id": "p1",
        "name": "Matin",
        "index": 1,
        "cycle": 0,
        "weekDays": 127,
        "startTimes": [360, -1, -1, -1, -1, -1, -1, -1],
        "stationsDuration": [600] + [0] * 11,
        "waterBudget": 100,
        "timeBetweenStations": 0,
    }
    base.update(kwargs)
    return base


def _scheduled(raw: dict | None = None, **kwargs) -> SolemModule:
    raw = raw or _program_raw()
    return _module(
        programs=[
            SolemProgram(id=raw["id"], name=raw["name"], index=raw["index"], raw=raw)
        ],
        **kwargs,
    )


def _ag_window(start: int, end: int, on: int = 600, off: int = 0) -> dict:
    return {"start": start, "end": end, "on": on, "off": off}


def _ag_program_raw(index: int, **kwargs) -> dict:
    """An LR-AG program record: one window, every day."""
    base = {
        "id": f"pr{index + 1}",
        "name": f"Rang {index + 1}",
        "index": index,
        "cycle": 0,
        "weekDays": 127,
        "windows": [_ag_window(1320, 120)] + [_ag_window(-1, -1, 0, 0)] * 3,
        "waterBudget": 100,
    }
    base.update(kwargs)
    return base


def _ag_scheduled(*raws: dict) -> SolemModule:
    return _ag_module(
        stations=[
            SolemStation(id=f"o{i + 1}", name=f"Station {i + 1}", index=i + 1)
            for i in range(len(raws))
        ],
        programs=[
            SolemProgram(id=r["id"], name=r["name"], index=r["index"], raw=r)
            for r in raws
        ],
    )


def _reread_setup(coordinator, modules, fields, page) -> None:
    coordinator.modules = modules
    coordinator.client.async_get_module_state = AsyncMock(return_value={})
    coordinator.client.async_get_module_fields = AsyncMock(return_value=fields)
    coordinator.client.async_get_module_page = page


def _page(name: str = "Renamed", updated: str = T1) -> AsyncMock:
    return AsyncMock(
        return_value=(
            {
                "programs": [{"id": "p1", "name": name, "index": 1}],
                "programmingUpdatedAt": updated,
            },
            [],
        )
    )


async def test_setup_keeps_each_programs_raw_record(coordinator):
    record = _program_raw(id="pr1", name="Prog")
    coordinator.client.async_login = AsyncMock(return_value="uid")
    coordinator.client.async_get_module_ids = AsyncMock(return_value=["m1"])
    coordinator.client.async_get_module_page = AsyncMock(
        return_value=(
            {
                "name": "Ctrl",
                "type": "LR-IS",
                "typeIsWatering": True,
                "outputs": [{"id": "o1", "name": "Z1", "index": 1}],
                "programs": [record, {"name": "no id, dropped"}],
            },
            [],
        )
    )

    await coordinator.async_setup()

    programs = coordinator.modules["m1"].programs
    assert [p.id for p in programs] == ["pr1"]
    assert programs[0].raw == record


async def test_programming_change_rereads_only_that_module(coordinator):
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    m2 = _module(id="m2", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    page = _page()
    _reread_setup(
        coordinator,
        {"m1": m1, "m2": m2},
        {
            "m1": {"id": "m1", "programmingUpdatedAt": T1},
            "m2": {"id": "m2", "programmingUpdatedAt": T0},
        },
        page,
    )

    await coordinator._async_update_data()

    page.assert_awaited_once_with("m1")
    assert [p.name for p in m1.programs] == ["Renamed"]
    assert [p.name for p in m2.programs] == ["Matin"]
    assert m1.raw["programmingUpdatedAt"] == T1
    assert m1.raw["typeIsWatering"] is True


async def test_same_programming_time_spelled_differently_skips_reread(coordinator):
    gateway = _module(id="g1", raw={"seenAt": T0})
    m1 = _module(
        id="m1",
        raw={
            "typeIsWatering": True,
            "programmingUpdatedAt": "2026-10-01T10:00:41.855Z",
        },
    )
    page = _page()
    _reread_setup(
        coordinator,
        {"m1": m1, "g1": gateway},
        {
            "m1": {
                "id": "m1",
                "programmingUpdatedAt": "2026-10-01T10:00:41.855+00:00",
            },
            # A gateway has no programs and reports null.
            "g1": {"id": "g1", "programmingUpdatedAt": None},
        },
        page,
    )

    await coordinator._async_update_data()

    page.assert_not_awaited()
    assert [p.name for p in m1.programs] == ["Matin"]


async def test_reread_failure_keeps_programs_and_retries_next_poll(coordinator):
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    page = AsyncMock(side_effect=SolemConnectionError("boom"))
    _reread_setup(
        coordinator,
        {"m1": m1},
        {"m1": {"id": "m1", "programmingUpdatedAt": T1}},
        page,
    )

    result = await coordinator._async_update_data()

    assert result == {"m1": {}}  # the poll itself still succeeds
    assert [p.name for p in m1.programs] == ["Matin"]
    assert m1.raw["programmingUpdatedAt"] == T0

    coordinator.client.async_get_module_page = _page()
    await coordinator._async_update_data()

    assert [p.name for p in m1.programs] == ["Renamed"]
    assert m1.raw["programmingUpdatedAt"] == T1


async def test_reread_unparseable_page_keeps_programs(coordinator):
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    _reread_setup(
        coordinator,
        {"m1": m1},
        {"m1": {"id": "m1", "programmingUpdatedAt": T1}},
        AsyncMock(return_value=({}, [])),
    )

    await coordinator._async_update_data()

    assert [p.name for p in m1.programs] == ["Matin"]
    assert m1.raw["programmingUpdatedAt"] == T0


async def test_reread_auth_error_is_reauth(coordinator):
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    _reread_setup(
        coordinator,
        {"m1": m1},
        {"m1": {"id": "m1", "programmingUpdatedAt": T1}},
        AsyncMock(side_effect=SolemAuthError("nope")),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_bluetooth_only_module_programs_are_reread(coordinator):
    b1 = _module(
        id="b1",
        type="bl-ip",
        raw={
            "typeIsWatering": True,
            "isBluetoothOnly": True,
            "programmingUpdatedAt": T0,
        },
    )
    page = _page()
    _reread_setup(
        coordinator,
        {"b1": b1},
        {"b1": {"id": "b1", "programmingUpdatedAt": T1}},
        page,
    )

    await coordinator._async_update_data()

    page.assert_awaited_once_with("b1")
    assert [p.name for p in b1.programs] == ["Renamed"]
    coordinator.client.async_get_module_state.assert_not_awaited()


async def test_pool_module_programming_change_is_not_reread(coordinator):
    pool = _module(id="pool", raw={"typeIsWatering": False, "programmingUpdatedAt": T0})
    page = _page()
    _reread_setup(
        coordinator,
        {"pool": pool},
        {"pool": {"id": "pool", "programmingUpdatedAt": T1}},
        page,
    )

    await coordinator._async_update_data()

    page.assert_not_awaited()
    assert pool.has_schedule is False
    assert pool.raw["programmingUpdatedAt"] == T1  # still tracked, just not acted on


async def test_reread_does_not_touch_the_live_state(coordinator):
    """The page's stored `status` must never count as an ON/OFF ack."""
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    page = AsyncMock(
        return_value=(
            {
                "programs": [{"id": "p1", "name": "Renamed", "index": 1}],
                "status": _stored(state=1),
            },
            [],
        )
    )
    _reread_setup(
        coordinator,
        {"m1": m1},
        {"m1": {"id": "m1", "programmingUpdatedAt": T1}},
        page,
    )
    coordinator._status_pending["m1"] = (False, 0, dt_util.utcnow())

    await coordinator._async_update_data()

    assert coordinator._status_pending["m1"][:2] == (False, 0)


async def test_reread_renames_ag_stations_after_new_program_names(coordinator):
    ag = _ag_scheduled(_ag_program_raw(0))
    ag.raw["programmingUpdatedAt"] = T0
    page = AsyncMock(
        return_value=(
            {"programs": [_ag_program_raw(0, name="Vigne")]},
            [],
        )
    )
    _reread_setup(
        coordinator,
        {"ag1": ag},
        {"ag1": {"id": "ag1", "programmingUpdatedAt": T1}},
        page,
    )

    await coordinator._async_update_data()

    assert [s.name for s in ag.stations] == ["Vigne"]


async def test_schedule_overview_unknown_module_is_empty(coordinator):
    assert coordinator.schedule_overview("ghost") == {}


async def test_schedule_overview_weekly_next_run_across_midnight(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-18T21:30:00+00:00")  # Sunday 23:30 in Paris
    coordinator.modules = {"m1": _scheduled(_program_raw(weekDays=1))}

    overview = coordinator.schedule_overview("m1")

    assert overview["next_run"] == datetime(
        2026, 10, 19, 6, 0, tzinfo=ZoneInfo("Europe/Paris")
    )
    [program] = overview["programs"]
    assert program["days"] == ["mon"]
    assert program["start_times"] == ["06:00"]
    assert program["durations"] == {"1": 10.0}
    assert program["stations"] == ["Pelouse 1"]
    assert [o["start"] for o in overview["occurrences"]] == [
        "2026-10-19T06:00:00+02:00"
    ]
    assert overview["occurrences"][0]["end"] == "2026-10-19T06:10:00+02:00"
    assert overview["pending_push"] is False


async def test_schedule_overview_decodes_weekdays(coordinator):
    coordinator.modules = {"m1": _scheduled(_program_raw(weekDays=69))}

    [program] = coordinator.schedule_overview("m1")["programs"]

    assert program["days"] == ["mon", "wed", "sun"]


async def test_schedule_overview_parked_program_is_listed_but_never_runs(coordinator):
    coordinator.modules = {"m1": _scheduled(_program_raw(cycle=0, weekDays=0))}

    overview = coordinator.schedule_overview("m1")

    assert [p["enabled"] for p in overview["programs"]] == [False]
    assert overview["occurrences"] == []
    assert overview["next_run"] is None


async def test_schedule_overview_ag_windows(hass, coordinator, freezer):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-18T21:30:00+00:00")  # Sunday 23:30 in Paris
    coordinator.modules = {"ag1": _ag_scheduled(_ag_program_raw(0, name="Vigne"))}

    overview = coordinator.schedule_overview("ag1")

    [program] = overview["programs"]
    assert program["windows"] == [
        {"start": "22:00", "end": "02:00", "on_min": 10.0, "off_min": 0.0}
    ]
    assert program["stations"] == ["Station 1"]
    assert "2026-10-19T02:00:00+02:00" in [o["end"] for o in overview["occurrences"]]
    assert {o["station"] for o in overview["occurrences"]} == {"Station 1"}
    assert overview["next_run"] == datetime(
        2026, 10, 19, 22, 0, tzinfo=ZoneInfo("Europe/Paris")
    )


async def test_schedule_overview_ag_skips_rain_delayed_station(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-18T21:30:00+00:00")
    coordinator.modules = {"ag1": _ag_scheduled(_ag_program_raw(0), _ag_program_raw(1))}
    coordinator.data = {
        "ag1": _ag_state(_ag_output(0, rain_delay=3), _ag_output(1)),
    }

    overview = coordinator.schedule_overview("ag1")

    assert len(overview["programs"]) == 2  # still listed
    assert overview["occurrences"]
    assert {o["station_index"] for o in overview["occurrences"]} == {2}


async def test_schedule_overview_off_controller_has_no_next_run(coordinator):
    coordinator.modules = {"m1": _scheduled()}
    coordinator.data = _watering(state=0)

    overview = coordinator.schedule_overview("m1")

    assert overview["next_run"] is None
    assert overview["occurrences"] == []
    assert len(overview["programs"]) == 1


async def test_schedule_overview_unknown_status_still_schedules(coordinator):
    coordinator.modules = {"m1": _scheduled()}
    coordinator.data = {"m1": {}}  # no `status.watering.state` reported

    overview = coordinator.schedule_overview("m1")

    assert overview["next_run"] is not None
    assert overview["occurrences"]


async def test_schedule_overview_caps_occurrences(
    hass, coordinator, freezer, monkeypatch
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-18T21:30:00+00:00")
    # 8 start times x 7 stations x 2 days = 112 runs.
    raw = _program_raw(
        startTimes=[0, 180, 360, 540, 720, 900, 1080, 1260],
        stationsDuration=[60] * 7 + [0] * 5,
    )
    coordinator.modules = {
        "m1": _scheduled(
            raw,
            stations=[
                SolemStation(id=f"s{i}", name=f"Zone {i}", index=i) for i in range(1, 8)
            ],
        )
    }

    monkeypatch.setattr(coordinator_module, "MAX_SCHEDULE_OCCURRENCES", 50)

    occurrences = coordinator.schedule_overview("m1")["occurrences"]

    assert len(occurrences) == 50
    assert [o["start"] for o in occurrences] == sorted(o["start"] for o in occurrences)


async def test_schedule_overview_pending_push(coordinator):
    coordinator.modules = {"m1": _scheduled()}
    module = coordinator.modules["m1"]
    module.raw.update(
        programmingUpdatedAt="2026-10-02T10:00:00.000Z",
        programmingSentAt="2026-10-02T09:00:00.000Z",
    )
    assert coordinator.schedule_overview("m1")["pending_push"] is True

    module.raw["programmingSentAt"] = "2026-10-02T10:00:00.000Z"
    assert coordinator.schedule_overview("m1")["pending_push"] is False

    module.raw["programmingSentAt"] = "2026-10-02T11:00:00.000Z"
    assert coordinator.schedule_overview("m1")["pending_push"] is False


async def test_reread_with_undecodable_program_keeps_everything(coordinator):
    m1 = _module(id="m1", raw={"typeIsWatering": True, "programmingUpdatedAt": T0})
    page = AsyncMock(
        return_value=(
            {
                "programs": [{"id": "p1", "name": "Bad", "index": None}],
                "programmingUpdatedAt": T1,
            },
            [],
        )
    )
    _reread_setup(
        coordinator,
        {"m1": m1},
        {"m1": {"id": "m1", "programmingUpdatedAt": T1}},
        page,
    )

    result = await coordinator._async_update_data()

    assert result == {"m1": {}}  # the poll itself still succeeds
    assert [p.name for p in m1.programs] == ["Matin"]
    assert m1.raw["programmingUpdatedAt"] == T0


async def test_failed_reread_does_not_adopt_a_fresh_sent_at(coordinator):
    m1 = _module(
        id="m1",
        raw={
            "typeIsWatering": True,
            "programmingUpdatedAt": T0,
            "programmingSentAt": T0,
        },
    )
    _reread_setup(
        coordinator,
        {"m1": m1},
        {
            "m1": {
                "id": "m1",
                "programmingUpdatedAt": T1,
                "programmingSentAt": T1,
            }
        },
        AsyncMock(side_effect=SolemConnectionError("boom")),
    )

    await coordinator._async_update_data()

    assert m1.raw["programmingUpdatedAt"] == T0
    assert m1.raw["programmingSentAt"] == T0


def test_programming_out_of_sync_tolerates_naive_and_aware_timestamps():
    naive = "2026-10-02T10:00:00"
    aware = "2026-10-02T09:00:00+00:00"
    assert (
        coordinator_module._programming_out_of_sync(
            {"programmingUpdatedAt": naive, "programmingSentAt": aware}
        )
        is True
    )
    assert (
        coordinator_module._programming_out_of_sync(
            {"programmingUpdatedAt": aware, "programmingSentAt": naive}
        )
        is False
    )


async def test_schedule_overview_window_is_two_local_midnights(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to("2026-10-24T10:00:00+00:00")  # 12:00 local, day before DST end
    # Tomorrow (25 Oct) lasts 25 h: 23:30 is beyond a 48 h window.
    raw = _program_raw(
        startTimes=[1410, -1, -1, -1, -1, -1, -1, -1], stationsDuration=[600] + [0] * 11
    )
    coordinator.modules = {"m1": _scheduled(raw)}

    starts = [o["start"] for o in coordinator.schedule_overview("m1")["occurrences"]]

    assert "2026-10-25T23:30:00+01:00" in starts


async def test_schedule_overview_pending_on_ignores_stale_rain_delays(
    hass, coordinator, freezer
):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-10-18T21:30:00+00:00")
    coordinator.modules = {"ag1": _ag_scheduled(_ag_program_raw(0), _ag_program_raw(1))}
    coordinator.data = {
        "ag1": _ag_state(_ag_output(0, rain_delay=3), _ag_output(1)),
    }
    coordinator._status_pending["ag1"] = (True, 0, dt_util.utcnow())

    overview = coordinator.schedule_overview("ag1")

    assert {o["station_index"] for o in overview["occurrences"]} == {1, 2}


async def test_schedule_overview_skips_an_undecodable_program(coordinator):
    good = _program_raw()
    bad = _program_raw(id="p2", name="Broken", index=2, cycle="x")
    module = _module(
        programs=[
            SolemProgram(id=r["id"], name=r["name"], index=r["index"], raw=r)
            for r in (bad, good)
        ]
    )
    coordinator.modules = {"m1": module}

    overview = coordinator.schedule_overview("m1")

    assert [p["name"] for p in overview["programs"]] == ["Matin"]
    assert overview["occurrences"]
