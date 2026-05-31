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
    _extract_js_object,
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
            obj = await client.async_get_module("m1")
        assert obj["name"] == "Ctrl"
        assert obj["outputs"][0]["id"] == "o1"


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
            obj = await client.async_get_module("m1")
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
