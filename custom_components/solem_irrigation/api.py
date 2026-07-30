"""Client for the MySOLEM web API.

This talks to the same cloud backend as the MySOLEM web app
(https://mysolem.com), which is a Sails.js application authenticated with a
session cookie. It is *not* the mobile OAuth2 API. All endpoints and payloads
below were reverse-engineered from the web app:

* ``POST /login``                              -> session cookie
* ``GET  /modules``                            -> page embeds ``const userId``
* ``POST /users/{userId}/modules``             -> module list (JSON)
* ``GET  /module/{id}``                        -> page embeds ``let module = {…}``
                                                  (stations = ``outputs``, ``programs``)
                                                  and ``var inputs = […]`` (sensors)
* ``GET  /remote/module/state?moduleId={id}``  -> live watering state (JSON)
* ``GET  /input/getLastTickFromInput``         -> newest sensor tick (JSON)
* ``GET  /remote/module/getComputedSensorsData`` -> windowed sensor series (JSON)
* ``POST /module/sendManualModuleCommand``     -> manual commands (form-urlencoded)

Note that ``inputs`` (the module's sensors, e.g. a flow meter) are *not* part of
the ``let module`` object: they live in a sibling ``var inputs`` array on the
same page, so both are parsed from a single fetch.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

from .const import (
    BASE_URLS,
    CMD_PROGRAM,
    CMD_STATION,
    CMD_STATUS,
    CMD_STOP,
    STATUS_OFF,
    STATUS_ON,
)

_LOGGER = logging.getLogger(__name__)

# const userId = "abc123…"
_USERID_RE = re.compile(r'userId\s*=\s*["\']([a-f0-9]{24})["\']')
# let module = { … }  (a balanced object is extracted starting from the brace)
_MODULE_MARKER = "let module = "
# var inputs = [ … ]  (the module's sensors; a flow meter is type 1)
_INPUTS_MARKER = "var inputs = "

# The label the user typed for a sensor is not in ``var inputs`` (whose ``name``
# is empty); it is rendered into the settings form instead:
#   <input … name="sensor-name" value="Flowmeter" data-input-id="abc…" />
# Match the whole tag first, then pull the attributes out, so attribute order
# does not matter.
_SENSOR_NAME_TAG_RE = re.compile(r"<input[^>]*\bname=\"sensor-name\"[^>]*>")
_VALUE_ATTR_RE = re.compile(r"\bvalue=\"([^\"]*)\"")
_INPUT_ID_ATTR_RE = re.compile(r"\bdata-input-id=\"([a-f0-9]{24})\"")

# Sensor readings are stored raw and scaled by a tiny formula the cloud ships
# alongside them, e.g. "x/0.1" (a 10 L/pulse meter) or "x/100.0" (centi-degrees).
_EXPRESSION_RE = re.compile(r"^x(?:\s*([*/])\s*(\d+(?:\.\d+)?))?$")


def apply_expression(expression: str | None, value: float) -> float | None:
    """Scale a raw sensor ``value`` with SOLEM's ``expression``.

    The expression comes from the cloud, so it is matched against a strict
    pattern rather than evaluated: ``x``, ``x*<number>`` and ``x/<number>`` are
    the only shapes SOLEM is known to emit.

    Returns None when the expression is present but unsupported (or divides by
    zero), so callers can refuse to publish a value they cannot scale. A missing
    or empty expression is the identity, not a failure.
    """
    if not expression:
        return float(value)
    match = _EXPRESSION_RE.match(expression.strip())
    if match is None:
        return None
    operator, operand = match.groups()
    if operator is None:
        return float(value)
    number = float(operand)
    if operator == "*":
        scaled = value * number
    elif number == 0:
        return None
    else:
        scaled = value / number
    # Keep float artefacts (3970.0000000000005) out of the recorder.
    return round(scaled, 3)


class SolemError(Exception):
    """Base error."""


class SolemAuthError(SolemError):
    """Authentication failed (bad credentials / expired session)."""


class SolemConnectionError(SolemError):
    """The cloud could not be reached or returned an unexpected response."""


def _extract_js_literal(html: str, marker: str, opener: str) -> Any | None:
    """Extract the first balanced ``opener``-delimited literal after ``marker``.

    The MySOLEM pages embed plain JSON literals (double-quoted keys and
    strings), so once the balanced span is isolated it parses with ``json``.
    Only the chosen bracket pair is counted -- JSON nesting is balanced, so an
    inner ``[`` inside an object (or vice versa) can never close the outer span.
    Brackets that appear inside string literals are ignored.
    """
    closer = {"{": "}", "[": "]"}[opener]
    start_marker = html.find(marker)
    if start_marker == -1:
        return None
    start = html.find(opener, start_marker)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(html)):
        char = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                blob = html[start : i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    _LOGGER.debug("Failed to JSON-decode embedded literal")
                    return None
    return None


def _extract_js_object(html: str, marker: str) -> dict[str, Any] | None:
    """Extract the first balanced ``{…}`` object following ``marker``."""
    value = _extract_js_literal(html, marker, "{")
    return value if isinstance(value, dict) else None


def _extract_js_array(html: str, marker: str) -> list[Any] | None:
    """Extract the first balanced ``[…]`` array following ``marker``."""
    value = _extract_js_literal(html, marker, "[")
    return value if isinstance(value, list) else None


def _extract_input_labels(html: str) -> dict[str, str]:
    """Map input id -> the label the user gave that sensor in the web app."""
    labels: dict[str, str] = {}
    for tag in _SENSOR_NAME_TAG_RE.findall(html):
        input_id = _INPUT_ID_ATTR_RE.search(tag)
        value = _VALUE_ATTR_RE.search(tag)
        if input_id and value and value.group(1).strip():
            labels[input_id.group(1)] = value.group(1).strip()
    return labels


class SolemApiClient:
    """Thin async client around the MySOLEM web API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        region: str,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._email = email
        self._password = password
        self._region = region
        self._base = BASE_URLS.get(region, BASE_URLS["Europe"])
        self._user_id: str | None = None

    @property
    def user_id(self) -> str | None:
        """Return the discovered MySOLEM user id (after login)."""
        return self._user_id

    # -- auth ---------------------------------------------------------------

    async def async_login(self) -> str:
        """Log in and return the MySOLEM user id.

        Raises:
            SolemAuthError: credentials rejected.
            SolemConnectionError: network / unexpected response.
        """
        payload = {
            "email": self._email,
            "password": self._password,
            "rememberMe": "on",
            "country-select": self._region,
        }
        try:
            async with self._session.post(f"{self._base}/login", data=payload) as resp:
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise SolemConnectionError(f"Login request failed: {err}") from err

        user_id = self._parse_user_id(html)
        if user_id is None:
            # The login POST normally redirects to /modules (which carries the
            # userId). If that did not happen, fetch it explicitly so we can
            # tell "wrong password" apart from "odd redirect".
            user_id = await self._fetch_user_id()
        if user_id is None:
            raise SolemAuthError("Invalid MySOLEM credentials")

        self._user_id = user_id
        return user_id

    @staticmethod
    def _parse_user_id(html: str) -> str | None:
        match = _USERID_RE.search(html)
        return match.group(1) if match else None

    async def _fetch_user_id(self) -> str | None:
        try:
            async with self._session.get(f"{self._base}/modules") as resp:
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise SolemConnectionError(f"Could not load /modules: {err}") from err
        return self._parse_user_id(html)

    async def _ensure_login(self) -> None:
        if self._user_id is None:
            await self.async_login()

    # -- reads --------------------------------------------------------------

    async def async_get_module_ids(self) -> list[str]:
        """Return the IDs of every module on the account.

        Note: this endpoint only returns ``{"id": …}`` per module unless a
        field projection is sent in the request body, so we use it purely for
        discovery and read the full record per module via :meth:`async_get_module`.
        """
        await self._ensure_login()
        data = await self._request_json("POST", f"/users/{self._user_id}/modules")
        modules = data.get("modules", []) if isinstance(data, dict) else []
        return [m["id"] for m in modules if isinstance(m, dict) and m.get("id")]

    async def async_get_module_page(
        self, module_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return ``(module, inputs)`` parsed from a single module-page fetch.

        The module record is the embedded ``let module = {…}`` object and
        contains everything we need: ``name``, ``serialNumber``, ``type``, the
        ``typeIs*`` capability flags, ``outputs`` (stations) and ``programs``.
        The inputs are the module's sensors, from the sibling ``var inputs``
        array; a flow meter is ``type`` 1. The page weighs well over a megabyte,
        so both are taken from the same request.

        Each input's empty ``name`` is filled in with the label the user gave it
        in the web app, which is rendered elsewhere in the page. Returns
        ``({}, [])`` if the page could not be parsed.
        """
        await self._ensure_login()
        html = await self._request_text("GET", f"/module/{module_id}")
        module = _extract_js_object(html, _MODULE_MARKER) or {}
        labels = _extract_input_labels(html)
        inputs = [
            {**record, "name": record.get("name") or labels.get(record["id"], "")}
            for record in _extract_js_array(html, _INPUTS_MARKER) or []
            if isinstance(record, dict) and record.get("id")
        ]
        return module, inputs

    async def async_get_module_state(self, module_id: str) -> dict[str, Any]:
        """Return the live watering state for a module."""
        await self._ensure_login()
        data = await self._request_json(
            "GET", f"/remote/module/state?moduleId={module_id}"
        )
        return data if isinstance(data, dict) else {}

    async def async_get_last_input_tick(self, input_id: str) -> dict[str, Any]:
        """Return the newest tick recorded for a sensor input.

        Unlike the windowed series this always answers, however old the reading
        is, which makes it the reliable source for a cumulative counter. The
        ``value`` is **raw**: scale it with :func:`apply_expression` using the
        input's ``expression``. Returns an empty dict for a sensor that has
        never reported.
        """
        await self._ensure_login()
        data = await self._request_json(
            "GET", f"/input/getLastTickFromInput?inputId={input_id}"
        )
        tick = data.get("tick") if isinstance(data, dict) else None
        return tick if isinstance(tick, dict) else {}

    async def async_get_module_sensor_data(
        self, module_id: str, start: str, end: str
    ) -> list[dict[str, Any]]:
        """Return each of a module's inputs with its ticks between two dates.

        ``start``/``end`` are ISO-8601 strings (this module stays free of Home
        Assistant, so the caller formats them). Values in ``computedSensorData``
        already have the input's ``expression`` applied. Ticks only exist while
        the sensor is actually measuring, so a quiet window legitimately returns
        the input records with an empty series.
        """
        await self._ensure_login()
        data = await self._request_json(
            "GET",
            f"/remote/module/getComputedSensorsData?moduleId={module_id}"
            f"&startDate={start}&endDate={end}&forceRawOrComputed=computed",
        )
        return (
            [record for record in data if isinstance(record, dict)]
            if isinstance(data, list)
            else []
        )

    # -- writes (manual commands) ------------------------------------------

    async def async_run_station(
        self, serial: str, output_id: str, minutes: int
    ) -> None:
        """Run a single station (``output``) for ``minutes``."""
        hours, mins = divmod(max(1, int(minutes)), 60)
        await self._send_command(
            serial,
            {
                "commandType": CMD_STATION,
                "command": output_id,
                "commandParams[hours]": hours,
                "commandParams[minutes]": mins,
            },
        )

    async def async_run_program(self, serial: str, program_id: str) -> None:
        """Start a stored program."""
        await self._send_command(
            serial, {"commandType": CMD_PROGRAM, "command": program_id}
        )

    async def async_stop(self, serial: str) -> None:
        """Stop any running manual watering / program (global)."""
        await self._send_command(serial, {"commandType": CMD_STOP})

    async def async_set_status(self, serial: str, enabled: bool, days: int = 0) -> None:
        """Enable/disable the controller.

        ``enabled=True``  -> turn ON (``days`` ignored).
        ``enabled=False`` -> turn OFF; ``days=0`` is permanent, ``days>0`` is a
        rain-delay of that many days.
        """
        await self._send_command(
            serial,
            {
                "command": STATUS_ON if enabled else STATUS_OFF,
                "commandType": CMD_STATUS,
                "commandParams": 0 if enabled else int(days),
            },
        )

    async def _send_command(self, serial: str, fields: dict[str, Any]) -> None:
        await self._ensure_login()
        payload = {"moduleSerialNumber": serial, **fields}
        await self._request_text(
            "POST",
            "/module/sendManualModuleCommand",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    # -- request plumbing ---------------------------------------------------

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        _retry: bool = True,
    ) -> str:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method, url, data=data, headers=headers
            ) as resp:
                text = await resp.text()
                # An expired session bounces us to the login page.
                if self._looks_logged_out(resp, text):
                    if _retry:
                        _LOGGER.debug("Session expired; re-authenticating")
                        self._user_id = None
                        await self.async_login()
                        return await self._request_text(
                            method, path, data=data, headers=headers, _retry=False
                        )
                    raise SolemAuthError("Session expired and re-login failed")
                resp.raise_for_status()
                return text
        except aiohttp.ClientResponseError as err:
            if err.status in (401, 403) and _retry:
                self._user_id = None
                await self.async_login()
                return await self._request_text(
                    method, path, data=data, headers=headers, _retry=False
                )
            raise SolemConnectionError(f"{method} {path} failed: {err}") from err
        except aiohttp.ClientError as err:
            raise SolemConnectionError(f"{method} {path} failed: {err}") from err

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        text = await self._request_text(method, path, **kwargs)
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise SolemConnectionError(f"{method} {path} did not return JSON") from err

    @staticmethod
    def _looks_logged_out(resp: aiohttp.ClientResponse, text: str) -> bool:
        """Did an expired session bounce us to the login page?

        aiohttp follows redirects by default, so a 302 -> /login leaves the
        final URL on the login path. That redirect is the reliable signal; we
        deliberately avoid sniffing page text (the authenticated module page
        also contains login/region markup in its menus).
        """
        return resp.url.path.rstrip("/").endswith("/login")
