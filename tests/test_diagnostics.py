"""Tests for the diagnostics dump.

The dump is meant to be pasted into a public GitHub issue, so the two things
worth guarding are opposites: nothing sensitive may survive, and everything
*un*modelled must.
"""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.util import dt as dt_util

from custom_components.solem_irrigation.coordinator import (
    SolemDataUpdateCoordinator,
    SolemFlowMeter,
    SolemFlowReading,
    SolemModule,
    SolemProgram,
    SolemStation,
)
from custom_components.solem_irrigation.diagnostics import (
    async_get_config_entry_diagnostics,
)

# An input type the integration does not model: this is what issue #8 is about.
RAIN_INPUT = {
    "id": "i9",
    "type": 42,
    "index": 2,
    "unit": 7,
    "expression": "x/10.0",
    "getName": "Pluviomètre",
    "typeIsCumulativeData": True,
    "typeIsMoistureSensor": False,
}

FLOW_INPUT = {"id": "i1", "type": 1, "index": 1, "unit": 1, "expression": "x/0.1"}


@pytest.fixture
def module() -> SolemModule:
    return SolemModule(
        id="m1",
        name="Controller",
        serial="1000000000000001",
        type="lr-is",
        display_type="lr-is",
        raw={
            "name": "Controller",
            "typeIsWatering": True,
            "serialNumber": "1000000000000001",
            "moduleSerialNumber": "1000000000000001",
            "addressWeather": {"cityName": "Jacou", "departementName": "Hérault"},
            "weatherForecast": {"0": {"Day": {"Rain": {"Value": 0}}}},
            "sensorState": False,
            "seenAt": "2026-09-21T06:00:15.405Z",
        },
        stations=[SolemStation(id="s1", name="Pelouse", index=1)],
        programs=[SolemProgram(id="p1", name="Matin", index=1)],
        flow_meters=[
            SolemFlowMeter(
                id="i1",
                name="Flowmeter",
                index=1,
                unit=1,
                expression="x/0.1",
                interval=1,
                raw=dict(FLOW_INPUT),
            )
        ],
        raw_inputs=[dict(FLOW_INPUT), dict(RAIN_INPUT)],
    )


@pytest.fixture
def entry(module) -> MagicMock:
    coordinator = MagicMock(spec=SolemDataUpdateCoordinator)
    coordinator.modules = {module.id: module}
    coordinator.relevant_module_ids.return_value = {"m1"}
    coordinator.module_state.return_value = {
        "lastRadioCommunication": "2026-09-21T05:59:39.000Z",
        "relay": "g1",
    }
    coordinator.last_update_success = True
    coordinator.run_minutes = {"s1": 10}
    coordinator.flow = {
        "i1": SolemFlowReading(
            volume=3970.0,
            raw_volume=397.0,
            timestamp=dt_util.parse_datetime("2026-09-21T05:44:25+00:00"),
            rate=10.0,
            record=dict(FLOW_INPUT),
        )
    }
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.data = {"email": "e@x.com", "password": "pw", "region": "Europe"}
    return entry


async def test_diagnostics_redacts_credentials_and_identity(hass, entry):
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["email"] == REDACTED
    assert result["entry"]["data"]["password"] == REDACTED
    # ``async_redact_data`` matches keys exactly, so every spelling of the
    # serial needs listing -- this is the one that is easy to miss.
    raw = result["modules"]["m1"]["raw"]
    assert raw["serialNumber"] == REDACTED
    assert raw["moduleSerialNumber"] == REDACTED
    # The user's town has no business in a file pasted into a public issue.
    assert raw["addressWeather"] == REDACTED
    assert "1000000000000001" not in str(result)
    assert "Jacou" not in str(result)


async def test_diagnostics_keeps_region_and_names(hass, entry):
    """Redaction must not eat what makes the dump discussable."""
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["region"] == "Europe"
    assert result["modules"]["m1"]["raw"]["name"] == "Controller"
    assert result["modules"]["m1"]["type"] == "lr-is"


async def test_diagnostics_exposes_unmodelled_inputs(hass, entry):
    """The whole point: a sensor the integration ignores still shows up."""
    result = await async_get_config_entry_diagnostics(hass, entry)
    module = result["modules"]["m1"]

    # Only the flow meter is modelled...
    assert [m["name"] for m in module["flow_meters"]] == ["Flowmeter"]
    # ...but the rain gauge survives verbatim, flags and all.
    rain = next(i for i in module["raw_inputs"] if i["id"] == "i9")
    assert rain["type"] == 42
    assert rain["getName"] == "Pluviomètre"
    assert rain["typeIsCumulativeData"] is True


async def test_diagnostics_drops_noisy_blobs(hass, entry):
    """The embedded AccuWeather forecast would bury everything else."""
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert "weatherForecast" not in result["modules"]["m1"]["raw"]
    # ...while an unknown-but-small flag is kept, in case it is the answer.
    assert result["modules"]["m1"]["raw"]["sensorState"] is False


async def test_diagnostics_survives_a_coordinator_with_no_data(hass, entry, module):
    """A dump must work on exactly the install that needs one.

    ``relevant_module_ids`` is a real method that walks every module's state,
    and ``data`` is None until a refresh succeeds -- which is precisely the
    situation someone downloads diagnostics in.
    """
    coordinator = entry.runtime_data
    coordinator.data = None
    coordinator.relevant_module_ids = lambda: (
        SolemDataUpdateCoordinator.relevant_module_ids(coordinator)
    )
    coordinator.module_state = lambda mid: SolemDataUpdateCoordinator.module_state(
        coordinator, mid
    )

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["modules"]["m1"]["state"] == {}
    assert result["modules"]["m1"]["is_relevant"] is True  # a controller
    assert result["coordinator"]["relevant_module_ids"] == ["m1"]


async def test_diagnostics_includes_state_and_flow(hass, entry):
    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["modules"]["m1"]["state"]["relay"] == "g1"
    assert result["modules"]["m1"]["is_controller"] is True
    assert result["modules"]["m1"]["is_relevant"] is True
    assert result["flow_readings"]["i1"]["volume"] == 3970.0
    assert result["coordinator"]["last_update_success"] is True
