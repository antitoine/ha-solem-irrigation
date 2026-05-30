"""Data update coordinator for SOLEM irrigation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

import aiohttp

from .api import (
    SolemApiClient,
    SolemAuthError,
    SolemConnectionError,
    SolemError,
)
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_RUN_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    IRRIGATION_TYPE_PREFIXES,
    REGION_EUROPE,
)

import logging

_LOGGER = logging.getLogger(__name__)

type SolemConfigEntry = ConfigEntry[SolemDataUpdateCoordinator]


@dataclass(slots=True)
class SolemStation:
    """A single watering station (the API calls these ``outputs``)."""

    id: str
    name: str
    index: int


@dataclass(slots=True)
class SolemProgram:
    """A stored watering program."""

    id: str
    name: str
    index: int


@dataclass(slots=True)
class SolemModule:
    """Static description of a module (controller, gateway, sensor…)."""

    id: str
    name: str
    serial: str
    type: str
    display_type: str
    raw: dict[str, Any]
    stations: list[SolemStation] = field(default_factory=list)
    programs: list[SolemProgram] = field(default_factory=list)

    @property
    def is_controller(self) -> bool:
        """An irrigation controller exposes stations and is an irrigation type.

        This excludes pool modules (lr-pc, lr-ps) that may also report stations
        but are managed elsewhere, and the gateway (which has no stations).
        """
        return bool(self.stations) and self.type.startswith(
            IRRIGATION_TYPE_PREFIXES
        )


class SolemDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinate one MySOLEM account: discover modules, poll live state."""

    config_entry: SolemConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SolemConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
        self.client = SolemApiClient(
            session,
            email=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
            region=entry.data.get(CONF_REGION, REGION_EUROPE),
        )
        self.modules: dict[str, SolemModule] = {}
        # Run duration (minutes) applied when a station switch is turned on.
        # Set by the "Run duration" number entity, read by the station switches.
        self.run_minutes: dict[str, int] = {}
        self._refresh_unsub: Callable[[], None] | None = None
        entry.async_on_unload(self._cancel_scheduled_refresh)

    @callback
    def _cancel_scheduled_refresh(self) -> None:
        if self._refresh_unsub is not None:
            self._refresh_unsub()
            self._refresh_unsub = None

    @callback
    def async_schedule_refresh(self, delay: float = 60.0) -> None:
        """Refresh once after ``delay`` seconds.

        Commands reach the controller over a slow LoRa downlink, so the cloud
        state does not reflect them immediately. Rather than polling right away
        (which would overwrite the optimistic value with a stale "idle"), we
        reconcile a little later, once the device has had time to ack.
        """
        self._cancel_scheduled_refresh()

        async def _do_refresh(_now) -> None:
            self._refresh_unsub = None
            await self.async_request_refresh()

        self._refresh_unsub = async_call_later(self.hass, delay, _do_refresh)

    def get_run_minutes(self, module_id: str) -> int:
        """Return the configured manual run duration for a module (minutes)."""
        return self.run_minutes.get(module_id, DEFAULT_RUN_MINUTES)

    async def async_setup(self) -> None:
        """Log in and discover modules and their stations/programs.

        Called once before the first refresh.
        """
        try:
            await self.client.async_login()
            raw_modules = await self.client.async_get_modules()
            modules: dict[str, SolemModule] = {}
            for raw in raw_modules:
                module_id = raw.get("id")
                if not module_id:
                    continue
                # A non-controller (gateway, sensor) or a transient hiccup must
                # not abort discovery of the rest of the fleet.
                try:
                    config = await self.client.async_get_module_config(module_id)
                except SolemError as err:
                    _LOGGER.warning(
                        "Could not read config for module %s: %s", module_id, err
                    )
                    config = {"outputs": [], "programs": []}
                modules[module_id] = SolemModule(
                    id=module_id,
                    name=raw.get("name", module_id),
                    serial=raw.get("serialNumber", ""),
                    type=raw.get("type", ""),
                    display_type=raw.get("displayType", raw.get("type", "")),
                    raw=raw,
                    stations=[
                        SolemStation(
                            id=o["id"],
                            name=o.get("name", f"Station {o.get('index', '?')}"),
                            index=int(o.get("index", 0)),
                        )
                        for o in config["outputs"]
                        if o.get("id")
                    ],
                    programs=[
                        SolemProgram(
                            id=p["id"],
                            name=p.get("name", f"Program {p.get('index', '?')}"),
                            index=int(p.get("index", 0)),
                        )
                        for p in config["programs"]
                        if p.get("id")
                    ],
                )
            self.modules = modules
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemConnectionError as err:
            raise ConfigEntryNotReady(str(err)) from err

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Poll the live watering state of every known module."""
        states: dict[str, dict[str, Any]] = {}
        last_error: SolemConnectionError | None = None
        for module_id in self.modules:
            try:
                states[module_id] = await self.client.async_get_module_state(
                    module_id
                )
            except SolemAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SolemConnectionError as err:
                _LOGGER.debug("State poll failed for %s: %s", module_id, err)
                last_error = err
                # Keep the previous reading for this module, if any.
                states[module_id] = self.module_state(module_id)
        # Only treat the cycle as failed if nothing at all came back.
        if last_error is not None and not any(states.values()):
            raise UpdateFailed(str(last_error))
        return states

    # -- helpers used by entities ------------------------------------------

    def relevant_module_ids(self) -> set[str]:
        """Modules this integration owns: irrigation controllers + their gateway.

        Keeps pool modules on the same account (handled by other integrations)
        out of Home Assistant.
        """
        ids = {m.id for m in self.modules.values() if m.is_controller}
        for module_id in list(ids):
            relay = self.module_state(module_id).get("relay")
            if relay and relay in self.modules:
                ids.add(relay)
        return ids

    def module_state(self, module_id: str) -> dict[str, Any]:
        """Return the cached live state for a module (possibly empty)."""
        return (self.data or {}).get(module_id, {})

    def running_station_index(self, module_id: str) -> int:
        """Return the index of the station currently watering (0 = none)."""
        watering = self.module_state(module_id).get("status", {}).get("watering", {})
        try:
            return int(watering.get("runningStation", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def apply_optimistic_running_station(self, module_id: str, index: int) -> None:
        """Optimistically reflect a manual command before the next poll.

        LoRa downlinks are slow, so update the cache immediately and notify
        listeners; the next poll reconciles with the controller's real state.
        """
        data = dict(self.data or {})
        state = dict(data.get(module_id, {}))
        status = dict(state.get("status", {}))
        watering = dict(status.get("watering", {}))
        watering["runningStation"] = index
        status["watering"] = watering
        state["status"] = status
        data[module_id] = state
        self.async_set_updated_data(data)
