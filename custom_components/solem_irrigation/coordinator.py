"""Data update coordinator for SOLEM irrigation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
    REGION_EUROPE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

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


def _find_by_token(
    items: list[SolemStation] | list[SolemProgram], token: str | int
) -> Any:
    """Resolve a station/program by name (case-insensitive) or numeric index.

    Used by the ``solem_irrigation.run`` service so a user can write either the
    human name ("Pelouse 1") or the index the controller uses for it.
    """
    text = str(token).strip()
    if text.isdigit():
        index = int(text)
        for item in items:
            if item.index == index:
                return item
    folded = text.casefold()
    for item in items:
        if item.name.casefold() == folded:
            return item
    return None


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
        """True for irrigation controllers (a watering type with stations).

        Uses SOLEM's own ``typeIsWatering`` flag, which is the authoritative
        signal: it is True for irrigation controllers (LR-IS/LR-IP/WF-IS…) and
        False for pool controllers (which can *also* expose ``outputs``),
        sensors, and the gateway. Filtering on ``type`` prefixes or merely
        "has stations" would wrongly pick up the pool controller.
        """
        return bool(self.stations) and bool(self.raw.get("typeIsWatering"))

    def find_station(self, token: str | int) -> SolemStation | None:
        """Return the station matching ``token`` (name or index), if any."""
        return _find_by_token(self.stations, token)

    def find_program(self, token: str | int) -> SolemProgram | None:
        """Return the program matching ``token`` (name or index), if any."""
        return _find_by_token(self.programs, token)


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
        # Manual-run duration (minutes), keyed by **station id**. Drives how long
        # opening a station's valve runs it; set by each station's "Run duration"
        # number (and by an explicit ``run`` service ``duration``). Persisted via
        # ``_store`` and seeded with ``DEFAULT_RUN_MINUTES`` per station.
        self.run_minutes: dict[str, int] = {}
        self._store: Store[dict[str, int]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
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

    def get_run_minutes(self, station_id: str) -> int:
        """Return the remembered manual run duration for a station (minutes)."""
        return self.run_minutes.get(station_id, DEFAULT_RUN_MINUTES)

    async def async_set_run_minutes(self, station_id: str, minutes: int) -> None:
        """Remember a station's manual-run duration, persist, and notify entities.

        ``async_update_listeners`` keeps the station's "Run duration" number in
        sync when the value is changed elsewhere (e.g. via the ``run`` service).
        """
        self.run_minutes[station_id] = int(minutes)
        await self._store.async_save(self.run_minutes)
        self.async_update_listeners()

    async def async_setup(self) -> None:
        """Log in and discover modules and their stations/programs.

        Called once before the first refresh.
        """
        stored = await self._store.async_load()
        if stored:
            self.run_minutes.update(
                {k: int(v) for k, v in stored.items() if v is not None}
            )
        try:
            await self.client.async_login()
            module_ids = await self.client.async_get_module_ids()
            modules: dict[str, SolemModule] = {}
            for module_id in module_ids:
                # A non-controller (gateway, sensor) or a transient hiccup must
                # not abort discovery of the rest of the fleet.
                try:
                    obj = await self.client.async_get_module(module_id)
                except SolemError as err:
                    _LOGGER.warning("Could not read module %s: %s", module_id, err)
                    continue
                if not obj:
                    _LOGGER.warning(
                        "Module %s returned no parseable data; skipping", module_id
                    )
                    continue
                module = SolemModule(
                    id=module_id,
                    name=obj.get("name", module_id),
                    serial=obj.get("serialNumber", ""),
                    type=obj.get("type", ""),
                    display_type=obj.get("displayType", obj.get("type", "")),
                    raw=obj,
                    stations=[
                        SolemStation(
                            id=o["id"],
                            name=o.get("name", f"Station {o.get('index', '?')}"),
                            index=int(o.get("index", 0)),
                        )
                        for o in (obj.get("outputs") or [])
                        if o.get("id")
                    ],
                    programs=[
                        SolemProgram(
                            id=p["id"],
                            name=p.get("name", f"Program {p.get('index', '?')}"),
                            index=int(p.get("index", 0)),
                        )
                        for p in (obj.get("programs") or [])
                        if p.get("id")
                    ],
                )
                modules[module_id] = module
                _LOGGER.debug(
                    "Discovered module %s: name=%s type=%s outputs=%d "
                    "programs=%d controller=%s",
                    module_id,
                    module.name,
                    module.type,
                    len(module.stations),
                    len(module.programs),
                    module.is_controller,
                )
            self.modules = modules

            controllers = [m for m in modules.values() if m.is_controller]
            if not controllers:
                _LOGGER.warning(
                    "SOLEM: found %d module(s) but no irrigation controllers, "
                    "so no entities will be created. Modules seen: %s",
                    len(modules),
                    ", ".join(f"{m.name} ({m.type})" for m in modules.values())
                    or "none",
                )
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
                states[module_id] = await self.client.async_get_module_state(module_id)
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

    # -- commands (client call + optimistic update + reconcile) ------------
    # Shared by the station valves and the ``run`` service so the optimistic
    # update and the delayed reconcile are applied consistently on every path.

    async def async_command_stop(self, module: SolemModule) -> None:
        """Stop any running watering on a controller (global stop)."""
        await self.client.async_stop(module.serial)
        self.apply_optimistic_running_station(module.id, 0)
        self.async_schedule_refresh()

    async def async_command_run_station(
        self, module: SolemModule, station: SolemStation, minutes: int
    ) -> None:
        """Run a single station for ``minutes`` (replaces any running station)."""
        await self.client.async_run_station(module.serial, station.id, minutes)
        self.apply_optimistic_running_station(module.id, station.index)
        self.async_schedule_refresh()

    async def async_command_run_program(
        self, module: SolemModule, program: SolemProgram
    ) -> None:
        """Start a stored program (its first station appears on the next poll)."""
        await self.client.async_run_program(module.serial, program.id)
        self.async_schedule_refresh()
