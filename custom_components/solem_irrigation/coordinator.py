"""Data update coordinator for SOLEM irrigation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SolemApiClient,
    SolemAuthError,
    SolemConnectionError,
    SolemError,
    apply_expression,
)
from .const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_INPUT_INTERVAL,
    DEFAULT_RUN_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FLOW_IDLE_AFTER,
    FLOW_WINDOW,
    INPUT_TYPE_FLOW_METER,
    REGION_EUROPE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

type SolemConfigEntry = ConfigEntry[SolemDataUpdateCoordinator]

# A rate needs two ticks, and they must be adjacent enough to belong to the same
# stretch of flow rather than sit either side of a pause.
MIN_TICKS_FOR_RATE = 2
MAX_TICK_GAP_INTERVALS = 2


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
class SolemFlowMeter:
    """A flow meter wired to a module (the API calls these ``inputs``)."""

    id: str
    name: str
    index: int
    unit: int
    expression: str
    interval: int
    raw: dict[str, Any]


@dataclass(slots=True)
class SolemFlowReading:
    """The latest reading of a flow meter.

    ``volume`` is SOLEM's lifetime counter in the meter's own unit. ``rate`` is
    derived from consecutive ticks and is None while it cannot be determined
    (which is not the same as zero).
    """

    volume: float
    raw_volume: float
    timestamp: datetime
    rate: float | None
    record: dict[str, Any]


def _build_flow_meters(inputs: list[dict[str, Any]]) -> list[SolemFlowMeter]:
    """Pick the flow meters out of a module's inputs, in index order."""
    meters: list[SolemFlowMeter] = []
    for record in sorted(inputs, key=lambda r: int(r.get("index", 0) or 0)):
        if record.get("type") != INPUT_TYPE_FLOW_METER:
            continue
        expression = record.get("expression") or ""
        if apply_expression(expression, 1.0) is None:
            # Publishing a mis-scaled cumulative value would poison long-term
            # statistics, which a user cannot easily purge. Skip it instead.
            _LOGGER.warning(
                "Ignoring SOLEM flow meter %s: unsupported scaling expression %r",
                record["id"],
                expression,
            )
            continue
        meters.append(
            SolemFlowMeter(
                id=record["id"],
                name=record.get("name")
                or record.get("getName")
                or f"Flow meter {record.get('index', '?')}",
                index=int(record.get("index", 0) or 0),
                unit=int(record.get("unit", 0) or 0),
                expression=expression,
                interval=int(record.get("interval", 0) or 0) or DEFAULT_INPUT_INTERVAL,
                raw=record,
            )
        )
    return meters


def _usable_ticks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ticks of ``record`` that a rate may be derived from.

    Interpolated ticks are synthesised by the cloud, and a tick flagged
    ``lastTickBeforeStartDate`` sits outside the window for continuity; a delta
    across either would be fabricated flow.
    """
    return [
        tick
        for tick in record.get("computedSensorData") or []
        if isinstance(tick, dict)
        and not tick.get("interpolated")
        and tick.get("flag") != "lastTickBeforeStartDate"
        and tick.get("tickTimestamp")
        and tick.get("value") is not None
    ]


def _compute_flow_rate(
    record: dict[str, Any], now: datetime, interval: int
) -> float | None:
    """Derive the current flow rate (unit per minute) from a window of ticks.

    Ticks are only recorded while water actually flows, so an empty window means
    the meter has been idle throughout it. Deriving the rate from the window
    (rather than from a delta between polls) is what keeps it honest: a delta
    spanning a pause would divide real flow by hours of idleness and report a
    confident fraction of the true rate.
    """
    ticks = _usable_ticks(record)
    if not ticks:
        return 0.0
    timestamps = [dt_util.parse_datetime(t["tickTimestamp"]) for t in ticks]
    if timestamps[-1] is None:
        return None
    if now - timestamps[-1] > FLOW_IDLE_AFTER:
        return 0.0
    if len(ticks) < MIN_TICKS_FOR_RATE or timestamps[-2] is None:
        return None
    elapsed = (timestamps[-1] - timestamps[-2]).total_seconds() / 60
    # A duplicate or out-of-order tick would otherwise divide by zero, and ticks
    # further apart than a couple of intervals straddle a pause in the flow.
    if not 0 < elapsed <= MAX_TICK_GAP_INTERVALS * interval:
        return None
    # max() absorbs a counter rollover so the rate never reads negative.
    delta = max(0.0, float(ticks[-1]["value"]) - float(ticks[-2]["value"]))
    return round(delta / elapsed, 2)


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
    flow_meters: list[SolemFlowMeter] = field(default_factory=list)

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
        # Latest flow-meter reading, keyed by **flow meter (input) id**. Held
        # here rather than in ``data`` because it comes from its own endpoints,
        # not from the module state poll -- same reasoning as ``run_minutes``.
        self.flow: dict[str, SolemFlowReading] = {}
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
                    obj, inputs = await self.client.async_get_module_page(module_id)
                except SolemError as err:
                    _LOGGER.warning("Could not read module %s: %s", module_id, err)
                    continue
                if not obj:
                    _LOGGER.warning(
                        "Module %s returned no parseable data; skipping", module_id
                    )
                    continue
                if not inputs and int(obj.get("numberOfInputs", 0) or 0) > 0:
                    # The inputs are scraped from an embedded ``var inputs``
                    # array. If SOLEM ever renames it, parsing yields nothing and
                    # the sensors silently disappear -- so say so out loud.
                    _LOGGER.warning(
                        "Module %s declares %s input(s) but none could be parsed; "
                        "flow-meter sensors will be missing",
                        module_id,
                        obj.get("numberOfInputs"),
                    )
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
                    flow_meters=_build_flow_meters(inputs),
                )
                modules[module_id] = module
                _LOGGER.debug(
                    "Discovered module %s: name=%s type=%s outputs=%d "
                    "programs=%d flow_meters=%d controller=%s",
                    module_id,
                    module.name,
                    module.type,
                    len(module.stations),
                    len(module.programs),
                    len(module.flow_meters),
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

        # Flow meters live on their own endpoints. A failure here never fails
        # the cycle: the watering state above stays the sole authority.
        for module_id in self.relevant_module_ids():
            if self.modules[module_id].flow_meters:
                await self._async_poll_flow(module_id)
        return states

    async def _async_poll_flow(self, module_id: str) -> None:
        """Refresh every flow-meter reading on a module.

        The cumulative total comes from each meter's newest tick (which the cloud
        always answers with, however old it is) while the rate is derived from a
        short window of ticks. On failure the previous reading is kept: blanking
        a cumulative water sensor punches a hole in long-term statistics.
        """
        module = self.modules[module_id]
        now = dt_util.utcnow()
        try:
            records = await self.client.async_get_module_sensor_data(
                module_id,
                (now - FLOW_WINDOW).isoformat().replace("+00:00", "Z"),
                now.isoformat().replace("+00:00", "Z"),
            )
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemConnectionError as err:
            _LOGGER.debug("Flow window fetch failed for %s: %s", module_id, err)
            records = []
        by_id = {r["id"]: r for r in records if r.get("id")}

        for meter in module.flow_meters:
            try:
                tick = await self.client.async_get_last_input_tick(meter.id)
            except SolemAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SolemConnectionError as err:
                _LOGGER.debug("Tick fetch failed for meter %s: %s", meter.id, err)
                continue

            raw_value = tick.get("value")
            timestamp = dt_util.parse_datetime(tick.get("timestamp") or "")
            if raw_value is None or timestamp is None:
                # A meter that has never reported: leave it unknown rather than
                # inventing a zero that would land in long-term statistics.
                continue
            volume = apply_expression(meter.expression, float(raw_value))
            if volume is None:
                continue

            record = by_id.get(meter.id, {})
            self.flow[meter.id] = SolemFlowReading(
                volume=volume,
                raw_volume=float(raw_value),
                timestamp=dt_util.as_utc(timestamp),
                rate=_compute_flow_rate(record, now, meter.interval),
                record=record or meter.raw,
            )

    def flow_reading(self, meter_id: str) -> SolemFlowReading | None:
        """Return the latest reading for a flow meter, if one has arrived."""
        return self.flow.get(meter_id)

    # -- helpers used by entities ------------------------------------------

    def relevant_module_ids(self) -> set[str]:
        """Modules this integration owns: irrigation controllers + their gateway.

        Keeps pool modules on the same account (handled by other integrations)
        out of Home Assistant.

        A watering module carrying a flow meter is already a controller. SOLEM
        also sells the meter as a module of its own (lr-fl and friends), which is
        not a watering type, so those are admitted on the strength of their
        meters -- untested, as I only have the controller-attached topology.
        """
        ids = {
            m.id
            for m in self.modules.values()
            if m.is_controller or (m.flow_meters and not m.raw.get("typeIsPoolProduct"))
        }
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
