"""Tests for the MySOLEM API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.solem_irrigation.api import (
    SolemApiClient,
    SolemAuthError,
    SolemConnectionError,
    _extract_input_labels,
    _extract_js_array,
    _extract_js_object,
    apply_expression,
)

USER_ID = "a1b2c3d4e5f6a1b2c3d4e5f6"  # 24 hex chars, as the regex requires
BASE = "https://mysolem.com"


def _client(session: aiohttp.ClientSession) -> SolemApiClient:
    return SolemApiClient(session, email="e@x.com", password="pw", region="Europe")


# -- _extract_js_object -------------------------------------------------------


def test_extract_js_object_simple():
    """A plain embedded object is parsed."""
    html = 'foo let module = {"name": "Pelouse", "index": 1} bar'
    assert _extract_js_object(html, "let module = ") == {
        "name": "Pelouse",
        "index": 1,
    }


def test_extract_js_object_nested_and_braces_in_strings():
    """Brace counting ignores braces that live inside string literals."""
    html = 'x let module = {"name": "a{b}c", "child": {"k": "}"}} y'
    assert _extract_js_object(html, "let module = ") == {
        "name": "a{b}c",
        "child": {"k": "}"},
    }


def test_extract_js_object_handles_escaped_quotes():
    """Escaped quotes inside strings do not end the string early."""
    html = r'let module = {"name": "a\"b", "n": 2}'
    assert _extract_js_object(html, "let module = ") == {"name": 'a"b', "n": 2}


def test_extract_js_object_marker_missing():
    """A missing marker returns None."""
    assert _extract_js_object("nothing here", "let module = ") is None


def test_extract_js_object_no_opening_brace():
    """A marker with no following object returns None."""
    assert _extract_js_object("let module = nope", "let module = ") is None


def test_extract_js_object_malformed_json_returns_none():
    """A balanced-but-invalid blob returns None rather than raising."""
    html = "let module = {not valid json}"
    assert _extract_js_object(html, "let module = ") is None


def test_extract_js_object_ignores_an_array():
    """Asking for an object where an array sits returns None."""
    assert _extract_js_object("var inputs = [1, 2]", "var inputs = ") is None


# -- _extract_js_array --------------------------------------------------------


def test_extract_js_array_simple():
    """A plain embedded array is parsed."""
    html = 'x var inputs = [{"id": "i1", "type": 1}] y'
    assert _extract_js_array(html, "var inputs = ") == [{"id": "i1", "type": 1}]


def test_extract_js_array_nested_objects_and_brackets_in_strings():
    """The inputs array nests objects and lists, and quotes stray brackets."""
    html = (
        'var inputs = [{"id": "i1", "settings": [{"nominalDebit": 1}], '
        '"name": "a]b[c"}, {"id": "i2"}] tail'
    )
    assert _extract_js_array(html, "var inputs = ") == [
        {"id": "i1", "settings": [{"nominalDebit": 1}], "name": "a]b[c"},
        {"id": "i2"},
    ]


def test_extract_js_array_marker_missing():
    """A missing marker returns None."""
    assert _extract_js_array("nothing here", "var inputs = ") is None


def test_extract_js_array_malformed_returns_none():
    """A balanced-but-invalid array returns None rather than raising."""
    assert _extract_js_array("var inputs = [nope]", "var inputs = ") is None


# -- apply_expression ---------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (None, 397.0),  # no expression is the identity
        ("", 397.0),
        ("x", 397.0),
        ("x/0.1", 3970.0),  # the observed 10 L/pulse meter
        ("x/100.0", 3.97),  # centi-units, e.g. a temperature probe
        ("x*0.1", 39.7),
        (" x / 0.1 ", 3970.0),  # tolerates whitespace
    ],
)
def test_apply_expression_supported_shapes(expression, expected):
    """The scaling formulas SOLEM is known to emit are applied."""
    assert apply_expression(expression, 397) == expected


@pytest.mark.parametrize(
    "expression",
    ["x/0", "y*2", "x+1", "x*", "__import__('os').system('ls')", "2*x"],
)
def test_apply_expression_rejects_anything_else(expression):
    """Unsupported formulas return None instead of being evaluated."""
    assert apply_expression(expression, 397) is None


def test_apply_expression_rounds_away_float_artefacts():
    """Results are rounded so artefacts never reach the recorder."""
    assert apply_expression("x/3", 1) == 0.333


# -- _extract_input_labels ----------------------------------------------------


def test_extract_input_labels():
    """The user's own sensor label is scraped from the settings form."""
    html = (
        '<input class="form-control" name="sensor-name" value="Flowmeter" '
        f'data-input-id="{"a" * 24}" />'
        '<input name="sensor-name" data-input-id="ffffffffffffffffffffffff" '
        'value="  " />'  # blank label -> ignored
        '<input name="other" value="x" data-input-id="bbbbbbbbbbbbbbbbbbbbbbbb" />'
    )
    assert _extract_input_labels(html) == {"a" * 24: "Flowmeter"}


def test_extract_input_labels_tolerates_attribute_order():
    """Attributes may appear in any order around name="sensor-name"."""
    html = f'<input data-input-id="{"c" * 24}" value="Compteur" name="sensor-name">'
    assert _extract_input_labels(html) == {"c" * 24: "Compteur"}


# -- user id parsing ----------------------------------------------------------


def test_parse_user_id():
    """The embedded ``userId`` literal is extracted."""
    assert SolemApiClient._parse_user_id(f'const userId = "{USER_ID}";') == USER_ID
    assert SolemApiClient._parse_user_id("no id at all") is None


def test_region_falls_back_to_europe():
    """An unknown region uses the European base URL."""
    client = SolemApiClient(MagicMock(), email="e", password="p", region="Atlantis")
    assert client._base == BASE


# -- login --------------------------------------------------------------------


async def test_login_success_from_login_response():
    """The userId embedded in the /login response is used directly."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        with aioresponses() as m:
            m.post(
                f"{BASE}/login", body=f'<script>const userId = "{USER_ID}";</script>'
            )
            assert await client.async_login() == USER_ID
        assert client.user_id == USER_ID


async def test_login_falls_back_to_modules_page():
    """When /login carries no userId, /modules is fetched to find it."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        with aioresponses() as m:
            m.post(f"{BASE}/login", body="<html>no id here</html>")
            m.get(f"{BASE}/modules", body=f'const userId = "{USER_ID}"')
            assert await client.async_login() == USER_ID


async def test_login_invalid_credentials_raises_auth_error():
    """No userId anywhere means the credentials were rejected."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        with aioresponses() as m:
            m.post(f"{BASE}/login", body="login page again")
            m.get(f"{BASE}/modules", body="still the login page")
            with pytest.raises(SolemAuthError):
                await client.async_login()


async def test_login_network_error_raises_connection_error():
    """A transport error on /login surfaces as a connection error."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        with aioresponses() as m:
            m.post(f"{BASE}/login", exception=aiohttp.ClientError("boom"))
            with pytest.raises(SolemConnectionError):
                await client.async_login()


async def test_login_modules_fetch_network_error():
    """A transport error while fetching /modules surfaces as a connection error."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        with aioresponses() as m:
            m.post(f"{BASE}/login", body="no id")
            m.get(f"{BASE}/modules", exception=aiohttp.ClientError("boom"))
            with pytest.raises(SolemConnectionError):
                await client.async_login()


# -- reads --------------------------------------------------------------------


async def test_get_module_ids():
    """Only entries carrying an ``id`` are returned."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.post(
                f"{BASE}/users/{USER_ID}/modules",
                body=json.dumps({"modules": [{"id": "m1"}, {"id": "m2"}, {"noid": 1}]}),
            )
            assert await client.async_get_module_ids() == ["m1", "m2"]


async def test_get_module_parses_embedded_object():
    """The module record is read from the embedded ``let module`` object."""
    html = (
        'x let module = {"name": "Ctrl", "serialNumber": "S", '
        '"outputs": [{"id": "o1", "name": "Z1", "index": 1}]} y'
    )
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/module/m1", body=html)
            obj, inputs = await client.async_get_module_page("m1")
        assert obj["name"] == "Ctrl"
        assert obj["outputs"][0]["id"] == "o1"
        assert inputs == []


async def test_get_module_page_parses_inputs_alongside_the_module():
    """One fetch yields both the module record and its sensor inputs."""
    input_id = "684d9b733e4339e5e6aa7b3a"
    html = (
        'let module = {"name": "Ctrl", "numberOfInputs": 1} '
        f'var inputs = [{{"id": "{input_id}", "name": "", "type": 1, '
        '"unit": 1, "expression": "x/0.1", "getName": "D\\u00e9bitm\\u00e8tre"}, '
        '{"noid": true}] '
        f'<input name="sensor-name" value="Flowmeter" data-input-id="{input_id}" />'
    )
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/module/m1", body=html)
            module, inputs = await client.async_get_module_page("m1")
    assert module["name"] == "Ctrl"
    # The record without an id is dropped, and the user's label wins over the
    # empty ``name`` from ``var inputs``.
    assert len(inputs) == 1
    assert inputs[0]["id"] == input_id
    assert inputs[0]["name"] == "Flowmeter"
    assert inputs[0]["expression"] == "x/0.1"


async def test_get_module_page_without_inputs():
    """A module page carrying no inputs array yields an empty list."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/module/m1", body='let module = {"name": "Gateway"}')
            module, inputs = await client.async_get_module_page("m1")
    assert module["name"] == "Gateway"
    assert inputs == []


async def test_get_module_page_keeps_solem_name_when_user_gave_none():
    """Without a user label the record keeps its own (empty) name."""
    input_id = "b" * 24
    html = f'let module = {{}} var inputs = [{{"id": "{input_id}", "name": ""}}]'
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/module/m1", body=html)
            _, inputs = await client.async_get_module_page("m1")
    assert inputs[0]["name"] == ""


async def test_get_last_input_tick():
    """The newest tick is unwrapped from its envelope."""
    body = json.dumps(
        {
            "tick": {
                "input": "i1",
                "value": 397,
                "timestamp": "2026-07-30T17:48:00.000Z",
            }
        }
    )
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/input/getLastTickFromInput?inputId=i1", body=body)
            tick = await client.async_get_last_input_tick("i1")
    assert tick["value"] == 397
    assert tick["timestamp"] == "2026-07-30T17:48:00.000Z"


@pytest.mark.parametrize("body", ['{"tick": null}', "{}", "[]"])
async def test_get_last_input_tick_without_a_reading(body):
    """A sensor that never reported yields an empty dict, not an error."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/input/getLastTickFromInput?inputId=i1", body=body)
            assert await client.async_get_last_input_tick("i1") == {}


async def test_get_module_sensor_data():
    """The windowed series is returned as a list of input records."""
    body = json.dumps(
        [
            {
                "id": "i1",
                "type": 1,
                "computedSensorData": [
                    {"tickTimestamp": "2026-07-30T17:47:00.000Z", "value": 3960},
                    {"tickTimestamp": "2026-07-30T17:48:00.000Z", "value": 3970},
                ],
            },
            "not a record",
        ]
    )
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(
                f"{BASE}/remote/module/getComputedSensorsData?moduleId=m1"
                "&startDate=A&endDate=B&forceRawOrComputed=computed",
                body=body,
            )
            records = await client.async_get_module_sensor_data("m1", "A", "B")
    assert len(records) == 1
    assert records[0]["computedSensorData"][-1]["value"] == 3970


async def test_get_module_sensor_data_non_list():
    """An unexpected payload degrades to an empty list."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(
                f"{BASE}/remote/module/getComputedSensorsData?moduleId=m1"
                "&startDate=A&endDate=B&forceRawOrComputed=computed",
                body="{}",
            )
            assert await client.async_get_module_sensor_data("m1", "A", "B") == []


async def test_get_module_state():
    """The live state JSON is returned as a dict."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(
                f"{BASE}/remote/module/state?moduleId=m1",
                body=json.dumps({"status": {"watering": {"runningStation": 2}}}),
            )
            state = await client.async_get_module_state("m1")
        assert state["status"]["watering"]["runningStation"] == 2


async def test_request_json_non_json_raises():
    """A non-JSON body where JSON is expected raises a connection error."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/remote/module/state?moduleId=m1", body="not json")
            with pytest.raises(SolemConnectionError):
                await client.async_get_module_state("m1")


async def test_request_retries_once_on_401():
    """A 401 triggers a single re-login and a retry of the original request."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        with aioresponses() as m:
            m.get(f"{BASE}/module/m1", status=401)
            m.post(f"{BASE}/login", body=f'const userId = "{USER_ID}"')
            m.get(f"{BASE}/module/m1", body='let module = {"name": "Ctrl"}')
            obj, _ = await client.async_get_module_page("m1")
        assert obj == {"name": "Ctrl"}


# -- writes (manual command payloads) -----------------------------------------


async def test_run_station_splits_hours_and_minutes():
    """A duration in minutes is split into hours + minutes for the controller."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_run_station("SER", "out1", 75)
        serial, fields = client._send_command.call_args[0]
        assert serial == "SER"
        assert fields == {
            "commandType": "station",
            "command": "out1",
            "commandParams[hours]": 1,
            "commandParams[minutes]": 15,
        }


async def test_run_station_clamps_to_at_least_one_minute():
    """A zero/negative duration is clamped to one minute."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_run_station("SER", "out1", 0)
        _, fields = client._send_command.call_args[0]
        assert fields["commandParams[hours]"] == 0
        assert fields["commandParams[minutes]"] == 1


async def test_run_program_payload():
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_run_program("SER", "prog1")
        serial, fields = client._send_command.call_args[0]
        assert serial == "SER"
        assert fields == {"commandType": "program", "command": "prog1"}


async def test_stop_payload():
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_stop("SER")
        _, fields = client._send_command.call_args[0]
        assert fields == {"commandType": "manualStop"}


async def test_set_status_enabled_ignores_days():
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_set_status("SER", enabled=True, days=5)
        _, fields = client._send_command.call_args[0]
        assert fields == {"command": "on", "commandType": "status", "commandParams": 0}


async def test_set_status_disabled_with_days():
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._send_command = AsyncMock()
        await client.async_set_status("SER", enabled=False, days=3)
        _, fields = client._send_command.call_args[0]
        assert fields == {"command": "off", "commandType": "status", "commandParams": 3}


async def test_send_command_builds_payload_and_headers():
    """``_send_command`` injects the serial and the XHR header."""
    async with aiohttp.ClientSession() as session:
        client = _client(session)
        client._user_id = USER_ID
        client._request_text = AsyncMock(return_value="ok")
        await client._send_command("SER", {"commandType": "manualStop"})
        args, kwargs = client._request_text.call_args
        assert args == ("POST", "/module/sendManualModuleCommand")
        assert kwargs["data"]["moduleSerialNumber"] == "SER"
        assert kwargs["data"]["commandType"] == "manualStop"
        assert kwargs["headers"]["X-Requested-With"] == "XMLHttpRequest"


# -- session-expiry detection -------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/login", True), ("/login/", True), ("/module/123", False), ("/modules", False)],
)
def test_looks_logged_out(path, expected):
    """A final URL on the /login path is the expired-session signal."""
    resp = MagicMock()
    resp.url.path = path
    assert SolemApiClient._looks_logged_out(resp, "") is expected
