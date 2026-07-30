"""Tests for the SOLEM data update coordinator."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
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
    SolemFlowMeter,
    SolemModule,
    SolemProgram,
    SolemStation,
    _build_flow_meters,
    _compute_flow_rate,
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
    coordinator.client.async_get_module_page = AsyncMock(
        side_effect=lambda mid: (objs[mid], [])
    )

    await coordinator.async_setup()

    assert set(coordinator.modules) == {"m1", "g1", "pool"}
    assert coordinator.modules["m1"].is_controller is True
    assert coordinator.modules["m1"].stations[0].name == "Z1"
    assert coordinator.modules["pool"].is_controller is False
    assert coordinator.modules["g1"].is_controller is False


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
