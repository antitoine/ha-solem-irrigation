"""Data update coordinator for SOLEM irrigation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import schedule
from .api import (
    SolemApiClient,
    SolemAuthError,
    SolemConnectionError,
    SolemError,
    apply_expression,
)
from .const import (
    BLUETOOTH_ONLY_TYPES,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_REGION,
    DEFAULT_INPUT_INTERVAL,
    DEFAULT_RUN_MINUTES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENABLE_ACK_GRACE,
    FLOW_IDLE_AFTER,
    FLOW_WINDOW,
    INPUT_TYPE_FLOW_METER,
    INPUT_TYPE_RAIN_GAUGE,
    INPUT_TYPE_RAIN_SENSOR,
    INPUT_TYPE_TURBINE,
    LIVE_MODULE_FIELDS,
    MAX_RAIN_DELAY_DAYS_AG,
    MAX_SCHEDULE_OCCURRENCES,
    RAIN_DELAY_PERMANENT,
    REGION_EUROPE,
    STORAGE_KEY,
    STORAGE_VERSION,
    TURBINE_IDLE_AFTER,
    WATERING_STATES_OFF,
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
    # The program as the cloud returned it, which the schedule is decoded from.
    raw: dict[str, Any] = field(default_factory=dict)


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


@dataclass(slots=True)
class SolemRainGauge:
    """A tipping-bucket rain gauge (an input, like a flow meter)."""

    id: str
    name: str
    index: int
    expression: str
    raw: dict[str, Any]


@dataclass(slots=True)
class SolemRainReading:
    """The latest reading of a rain gauge: SOLEM's lifetime total, in mm."""

    total: float
    raw_total: float
    timestamp: datetime


@dataclass(slots=True)
class SolemRainSensor:
    """An on/off rain sensor wired to a module's sensor input."""

    id: str
    name: str
    index: int
    raw: dict[str, Any]


@dataclass(slots=True)
class SolemRainSensorReading:
    """The latest reading of a rain sensor: wet or dry, as last ticked."""

    wet: bool
    timestamp: datetime


@dataclass(slots=True)
class SolemTurbine:
    """A turbine flow meter: an input that reports flow, not a running total."""

    id: str
    name: str
    index: int
    unit: int
    interval: int
    raw: dict[str, Any]


@dataclass(slots=True)
class SolemTurbineReading:
    """What a turbine has measured today, and the flow through it right now.

    ``volume_today`` adds up the ticks since local midnight of ``day``, in the
    turbine's own unit. ``rate`` is its newest tick (unit per minute), and 0 once
    the water has stopped. ``timestamp`` is that newest tick's time.
    """

    day: date
    volume_today: float
    rate: float
    timestamp: datetime | None


def _ag_outputs(state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """An LR-AG's live outputs (``status.ag.outputs``), keyed by station index.

    The wire counts these outputs from 0, while the module record -- and so
    ``SolemStation.index`` -- counts them from 1. Empty for any other module.
    """
    outputs: dict[int, dict[str, Any]] = {}
    for output in ((state.get("status") or {}).get("ag") or {}).get("outputs") or []:
        if not isinstance(output, dict):
            continue
        try:
            outputs[int(output["index"]) + 1] = output
        except KeyError, TypeError, ValueError:
            continue
    return outputs


def _ag_rain_delays(
    state: dict[str, Any], stations: frozenset[int] | None = None
) -> list[int]:
    """Each LR-AG output's ``rainDelay``: 0 on, 255 off for good, N off N days.

    An LR-AG has no module-wide status: every output carries its own, and the
    web app turns them all ON or OFF at once. Only the outputs that are one of
    the module's ``stations`` (by index) count, if given: an orphan output the
    all-stations command never touches could otherwise block an OFF ack.
    """
    delays: list[int] = []
    for index, output in _ag_outputs(state).items():
        if stations is not None and index not in stations:
            continue
        try:
            delays.append(int(output["rainDelay"]))
        except KeyError, TypeError, ValueError:
            continue
    return delays


def _reported_status(
    state: dict[str, Any],
    *,
    agricultural: bool = False,
    stations: frozenset[int] | None = None,
) -> tuple[bool, int] | None:
    """The ``(enabled, rain_delay_days)`` a live-state record carries, or None.

    Read the way SOLEM's web app reads it: ``state`` 0 (or "OFF") is off, any
    other value is on. ``state`` does *not* say whether a station is running --
    it reads 1 both while idle and while watering.

    An LR-AG (``agricultural``) reads as on while any of its outputs is, since
    it can then still water; it is off once every output is, and back on with
    the first output whose delay expires. Any other module reads only its
    ``status.watering``, whatever else its state carries.
    """
    if agricultural and (delays := _ag_rain_delays(state, stations)):
        if 0 in delays:
            return True, 0
        return False, min(
            (d for d in delays if 0 < d < RAIN_DELAY_PERMANENT), default=0
        )
    watering = state.get("status", {}).get("watering", {})
    value = watering.get("state")
    if value is None:
        return None
    if value not in WATERING_STATES_OFF:
        return True, 0
    try:
        days = int(watering.get("rainDelay", 0) or 0)
    except TypeError, ValueError:
        days = 0
    return False, 0 if days >= RAIN_DELAY_PERMANENT else max(days, 0)


def _scalable_inputs(
    inputs: list[dict[str, Any]], input_type: int, kind: str
) -> list[dict[str, Any]]:
    """Return a module's inputs of ``input_type``, in index order.

    Inputs whose scaling expression is not understood are dropped: both kinds
    modelled today are cumulative, and a mis-scaled cumulative value would
    poison long-term statistics, which a user cannot easily purge.
    """
    records: list[dict[str, Any]] = []
    for record in sorted(inputs, key=lambda r: int(r.get("index", 0) or 0)):
        if record.get("type") != input_type:
            continue
        expression = record.get("expression") or ""
        if apply_expression(expression, 1.0) is None:
            _LOGGER.warning(
                "Ignoring SOLEM %s %s: unsupported scaling expression %r",
                kind,
                record["id"],
                expression,
            )
            continue
        records.append(record)
    return records


def _input_name(record: dict[str, Any], fallback: str) -> str:
    """The user's label, else SOLEM's localised one, else a generic name."""
    return (
        record.get("name")
        or record.get("getName")
        or f"{fallback} {record.get('index', '?')}"
    )


def _build_flow_meters(inputs: list[dict[str, Any]]) -> list[SolemFlowMeter]:
    """Pick the flow meters out of a module's inputs, in index order."""
    return [
        SolemFlowMeter(
            id=record["id"],
            name=_input_name(record, "Flow meter"),
            index=int(record.get("index", 0) or 0),
            unit=int(record.get("unit", 0) or 0),
            expression=record.get("expression") or "",
            interval=int(record.get("interval", 0) or 0) or DEFAULT_INPUT_INTERVAL,
            raw=record,
        )
        for record in _scalable_inputs(inputs, INPUT_TYPE_FLOW_METER, "flow meter")
    ]


def _build_rain_gauges(inputs: list[dict[str, Any]]) -> list[SolemRainGauge]:
    """Pick the rain gauges out of a module's inputs, in index order."""
    return [
        SolemRainGauge(
            id=record["id"],
            name=_input_name(record, "Rain gauge"),
            index=int(record.get("index", 0) or 0),
            expression=record.get("expression") or "",
            raw=record,
        )
        for record in _scalable_inputs(inputs, INPUT_TYPE_RAIN_GAUGE, "rain gauge")
    ]


def _build_rain_sensors(inputs: list[dict[str, Any]]) -> list[SolemRainSensor]:
    """Pick the on/off rain sensors out of a module's inputs, in index order.

    No expression check, unlike the cumulative kinds: only whether the raw
    reading is zero is ever used.
    """
    return [
        SolemRainSensor(
            id=record["id"],
            name=_input_name(record, "Rain sensor"),
            index=int(record.get("index", 0) or 0),
            raw=record,
        )
        for record in sorted(inputs, key=lambda r: int(r.get("index", 0) or 0))
        if record.get("type") == INPUT_TYPE_RAIN_SENSOR and record.get("id")
    ]


def _build_turbines(inputs: list[dict[str, Any]]) -> list[SolemTurbine]:
    """Pick the turbine flow meters out of a module's inputs, in index order.

    No expression check, unlike the counters: the cloud applies it to the
    windowed ticks a turbine is read from, and its JavaScript curve is beyond
    :func:`apply_expression` anyway.
    """
    return [
        SolemTurbine(
            id=record["id"],
            name=_input_name(record, "Turbine"),
            index=int(record.get("index", 0) or 0),
            unit=int(record.get("unit", 0) or 0),
            interval=int(record.get("interval", 0) or 0) or DEFAULT_INPUT_INTERVAL,
            raw=record,
        )
        for record in sorted(inputs, key=lambda r: int(r.get("index", 0) or 0))
        if record.get("type") == INPUT_TYPE_TURBINE and record.get("id")
    ]


def _build_programs(obj: dict[str, Any]) -> list[SolemProgram]:
    return [
        SolemProgram(
            id=p["id"],
            name=p.get("name", f"Program {p.get('index', '?')}"),
            index=int(p.get("index", 0)),
            raw=p,
        )
        for p in (obj.get("programs") or [])
        if p.get("id")
    ]


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


def _compute_turbine_reading(
    record: dict[str, Any],
    now: datetime,
    day_start: datetime,
    interval: int,
    previous: SolemTurbineReading | None,
) -> SolemTurbineReading:
    """Add up today's turbine ticks and read the current flow off the newest one.

    ``day_start`` is local midnight, in local time: its date is the day counted.
    Each tick is the flow over one ``interval``, in litres per minute, so it
    stands for ``value * interval`` litres. MySOLEM adds them up the same way,
    without filling the odd missing minute. Only ``interval`` 1 has been checked
    against MySOLEM's figures.

    The day's total is rebuilt from the window on every poll, so no tick is ever
    counted twice and a restart loses nothing. Within a day it never goes down,
    though: Home Assistant would book the dip as negative consumption, and a
    window that comes back short must not take back litres already reported.
    """
    ticks = [
        (timestamp, float(tick["value"]))
        for tick in _usable_ticks(record)
        if (timestamp := dt_util.parse_datetime(tick["tickTimestamp"])) is not None
    ]
    day = day_start.date()
    volume = round(
        sum(value * interval for timestamp, value in ticks if timestamp >= day_start),
        2,
    )
    if previous is not None and previous.day == day:
        volume = max(volume, previous.volume_today)
    if not ticks:
        return SolemTurbineReading(
            day=day,
            volume_today=volume,
            rate=0.0,
            timestamp=previous.timestamp if previous else None,
        )
    timestamp, value = max(ticks, key=lambda tick: tick[0])
    # The 0 tick a turbine sends when the water stops normally ends the flow;
    # the age check only covers that tick never arriving.
    rate = value if now - timestamp <= TURBINE_IDLE_AFTER else 0.0
    return SolemTurbineReading(
        day=day, volume_today=volume, rate=round(rate, 2), timestamp=timestamp
    )


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


def _programming_out_of_sync(raw: dict[str, Any]) -> bool:
    """Are there cloud-side program edits not yet pushed to the controller?

    The per-program ``dataChanged`` flag does NOT reset after a send, so the
    reliable signal is ``programmingUpdatedAt`` (last cloud edit) being newer
    than ``programmingSentAt`` (last successful push to the device).
    """
    updated = dt_util.parse_datetime(str(raw.get("programmingUpdatedAt") or ""))
    sent = dt_util.parse_datetime(str(raw.get("programmingSentAt") or ""))
    if updated is None:
        return False
    if sent is None:
        return True
    try:
        return dt_util.as_utc(updated) > dt_util.as_utc(sent)
    except TypeError:
        return False


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
    rain_gauges: list[SolemRainGauge] = field(default_factory=list)
    rain_sensors: list[SolemRainSensor] = field(default_factory=list)
    turbines: list[SolemTurbine] = field(default_factory=list)
    # Every input as the cloud returned it, including the ones
    # ``_build_*`` helpers drop. Nothing reads this at runtime; it exists so
    # diagnostics can show a sensor the integration does not model yet (a
    # soil-moisture probe, a turbine), which is otherwise invisible -- ``raw`` is
    # the ``let module`` object and does not carry the inputs.
    raw_inputs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_agricultural(self) -> bool:
        """True for an agricultural controller (LR-AG).

        It waters like any controller, but SOLEM flags it ``typeIsWatering``
        False and ``typeIsAgricultural`` / ``typeIsLrag`` True instead. It also
        speaks its own command vocabulary, and reports one live state per
        output (``status.ag.outputs``), as several stations can water at once.
        """
        return bool(self.raw.get("typeIsAgricultural") or self.raw.get("typeIsLrag"))

    @property
    def is_controller(self) -> bool:
        """True for irrigation controllers the cloud can drive.

        Uses SOLEM's own ``typeIsWatering`` flag, which is the authoritative
        signal: it is True for irrigation controllers (LR-IS/LR-IP/WF-IS…) and
        False for pool controllers (which can *also* expose ``outputs``),
        sensors, and the gateway. Filtering on ``type`` prefixes or merely
        "has stations" would wrongly pick up the pool controller. An LR-AG is
        the exception, admitted on its agricultural flag instead.

        A Bluetooth-only controller is left out: MySOLEM cannot reach it, so
        its entities could neither read its state nor command it -- they only
        ever looked like they did.
        """
        return (
            bool(self.stations)
            and (bool(self.raw.get("typeIsWatering")) or self.is_agricultural)
            and not self.is_bluetooth_only
        )

    @property
    def has_schedule(self) -> bool:
        """True when the module has stored programs worth showing as a schedule.

        Unlike :attr:`is_controller` this does not care whether the cloud can
        reach the module: a Bluetooth-only controller still has its programs
        stored in MySOLEM, which is all a schedule needs. ``typeIsWatering``
        keeps pool controllers out, as they carry programs too.
        """
        return bool(self.programs) and (
            bool(self.raw.get("typeIsWatering")) or self.is_agricultural
        )

    @property
    def has_modelled_inputs(self) -> bool:
        """True when any of the module's sensors becomes an entity."""
        return bool(
            self.flow_meters or self.rain_gauges or self.rain_sensors or self.turbines
        )

    @property
    def is_bluetooth_only(self) -> bool:
        """True for a module only a phone can reach, over Bluetooth (BL-IP…).

        The cloud has no live link to such a module, so its state endpoint
        answers 503 on every request. SOLEM's own ``isBluetoothOnly`` flag
        decides, with the web app's list of types as a fallback.
        """
        return bool(self.raw.get("isBluetoothOnly")) or (
            self.type in BLUETOOTH_ONLY_TYPES
        )

    def find_station(self, token: str | int) -> SolemStation | None:
        """Return the station matching ``token`` (name or index), if any."""
        return _find_by_token(self.stations, token)

    def find_program(self, token: str | int) -> SolemProgram | None:
        """Return the program matching ``token`` (name or index), if any."""
        return _find_by_token(self.programs, token)

    def paired_program(self, station: SolemStation) -> SolemProgram | None:
        """Return the program an LR-AG binds to ``station``, if any.

        An LR-AG has exactly one program per station, that station's schedule.
        They pair by index, programs counting from 0 and stations from 1.
        """
        return next((p for p in self.programs if p.index == station.index - 1), None)


def _record_state(module: SolemModule) -> dict[str, Any]:
    """The live state the cloud last stored on ``module``'s record, or ``{}``.

    A LoRa module whose gateway is off answers 503 on the state endpoint, like
    a Bluetooth-only one, yet the module page still carries its last status,
    shaped like the endpoint's reply -- what MySOLEM's web app shows for an
    unreachable module. Its ``relay`` comes along, as it does in the reply,
    so the module stays linked to its gateway. A gateway's record has no
    status.
    """
    status = module.raw.get("status")
    if not isinstance(status, dict) or not status:
        return {}
    state: dict[str, Any] = {"status": status}
    if relay := module.raw.get("relay"):
        state["relay"] = relay
    return state


def _name_stations_after_programs(module: SolemModule) -> None:
    """Name each LR-AG station after its program, as the web app labels it.

    An LR-AG keeps its outputs at their factory names ("Station 1"…): a name
    given to a station in the app is stored on its program instead.
    """
    for station in module.stations:
        if (program := module.paired_program(station)) is not None and program.name:
            station.name = program.name


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
        # Latest rain-gauge reading, keyed by **gauge (input) id**.
        self.rain: dict[str, SolemRainReading] = {}
        # Latest rain-sensor reading, keyed by **sensor (input) id**.
        self.rain_sensor_readings: dict[str, SolemRainSensorReading] = {}
        # Latest turbine reading, keyed by **turbine (input) id**.
        self.turbine_readings: dict[str, SolemTurbineReading] = {}
        # Each modelled input's record as the sensor-data window last returned
        # it, keyed by **input id**. It carries SOLEM's live flags (threshold
        # exceeded, faulty probe), which the module page only snapshots at setup.
        self.input_records: dict[str, dict[str, Any]] = {}
        self._store: Store[dict[str, int]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self._refresh_unsub: Callable[[], None] | None = None
        entry.async_on_unload(self._cancel_scheduled_refresh)
        # ON/OFF commands the cloud has not confirmed yet:
        # module id -> (enabled, days, issued at). See ``watering_enabled``.
        self._status_pending: dict[str, tuple[bool, int, datetime]] = {}

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
                    programs=_build_programs(obj),
                    flow_meters=_build_flow_meters(inputs),
                    rain_gauges=_build_rain_gauges(inputs),
                    rain_sensors=_build_rain_sensors(inputs),
                    turbines=_build_turbines(inputs),
                    raw_inputs=inputs,
                )
                if module.is_agricultural:
                    _name_stations_after_programs(module)
                modules[module_id] = module
                # ``input_types`` is what makes a sensor this integration does
                # not model yet (a soil probe, a turbine) visible to a user who
                # can only send a log line rather than a diagnostics file.
                _LOGGER.debug(
                    "Discovered module %s: name=%s type=%s outputs=%d "
                    "programs=%d inputs=%d input_types=%s flow_meters=%d "
                    "rain_gauges=%d rain_sensors=%d turbines=%d controller=%s",
                    module_id,
                    module.name,
                    module.type,
                    len(module.stations),
                    len(module.programs),
                    len(module.raw_inputs),
                    sorted(
                        {
                            record.get("type")
                            for record in module.raw_inputs
                            if record.get("type") is not None
                        }
                    ),
                    len(module.flow_meters),
                    len(module.rain_gauges),
                    len(module.rain_sensors),
                    len(module.turbines),
                    module.is_controller,
                )
            self.modules = modules

            if bluetooth_only := [m for m in modules.values() if m.is_bluetooth_only]:
                _LOGGER.warning(
                    "SOLEM: %s can only be reached over Bluetooth from a phone. "
                    "MySOLEM has no live link to such a module, so Home "
                    "Assistant can neither read nor command it, and only its "
                    "stored schedule is shown",
                    ", ".join(f"{m.name} ({m.type})" for m in bluetooth_only),
                )

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
        fetched: set[str] = set()
        polled_at = dt_util.utcnow()
        last_error: SolemConnectionError | None = None
        for module_id, module in self.modules.items():
            if module.is_bluetooth_only:
                # Its 503 is permanent, not an outage. Polling it anyway made an
                # account holding only Bluetooth modules fail every cycle, so
                # setup never completed (#11). The web app skips it too.
                continue
            try:
                states[module_id] = await self.client.async_get_module_state(module_id)
                fetched.add(module_id)
            except SolemAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except SolemConnectionError as err:
                _LOGGER.debug("State poll failed for %s: %s", module_id, err)
                last_error = err
                # Keep the previous reading for this module or, before the
                # first one, the status the cloud stored on its record: with
                # every gateway powered down (off-season) no poll ever comes
                # back, and setup would otherwise never complete. Neither is
                # in ``fetched``, so neither can ack an ON/OFF command.
                states[module_id] = self.module_state(module_id) or _record_state(
                    module
                )
        # Only treat the cycle as failed if nothing at all came back.
        if last_error is not None and not any(states.values()):
            raise UpdateFailed(str(last_error))

        # Only a status fetched AFTER an ON/OFF command counts as its ack: the
        # cached one (kept above for a module whose poll failed) is patched
        # with the command itself, and a poll started before the command may
        # carry the status the command is reverting (ON, OFF, ON again). Both
        # ``enabled`` and the rain delay must match, so changing the delay on an
        # already-OFF controller holds until the new delay is confirmed too.
        for module_id, (enabled, days, issued_at) in list(self._status_pending.items()):
            if (
                module_id in fetched
                and issued_at < polled_at
                and _reported_status(states[module_id], **self._status_scope(module_id))
                == (enabled, 0 if enabled else days)
            ):
                del self._status_pending[module_id]

        # Re-read the handful of module fields that only the (huge, setup-only)
        # module page would otherwise provide. One small request for the whole
        # account; a failure just leaves the previous values in place.
        await self._async_refresh_module_fields()

        # Sensors live on their own endpoints. A failure here never fails the
        # cycle: the watering state above stays the sole authority.
        for module_id in self.relevant_module_ids():
            if self.modules[module_id].has_modelled_inputs:
                await self._async_poll_inputs(module_id)
        return states

    async def _async_refresh_module_fields(self) -> None:
        """Merge the cheap per-module projection back into each module record.

        ``raw`` is otherwise a setup-time snapshot: the module page costs more
        than a megabyte per module, so it is read once. That froze the gateway's
        ``seenAt`` -- its only communication timestamp, since its state endpoint
        answers 503 -- and every battery level, until the next reload.

        Merged rather than replaced, because the projection only returns stored
        columns: the capability flags this integration classifies modules with
        are computed getters and never come back.

        The projection also carries ``programmingUpdatedAt``, which moves when
        the programs are edited in MySOLEM. The projection cannot return the
        programs themselves, so a change re-reads that module's page. The new
        timestamp is only adopted once that succeeded, so a failed re-read is
        retried on the next poll.
        """
        try:
            records = await self.client.async_get_module_fields(LIVE_MODULE_FIELDS)
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemConnectionError as err:
            _LOGGER.debug("Module field refresh failed: %s", err)
            return
        for module_id, record in records.items():
            if module := self.modules.get(module_id):
                fresh = {k: v for k, v in record.items() if k in LIVE_MODULE_FIELDS}
                remote = fresh.get("programmingUpdatedAt")
                if (
                    module.has_schedule
                    and remote is not None
                    # Compared as datetimes: the same instant can be spelled
                    # with a ``Z`` or a ``+00:00`` suffix.
                    and dt_util.parse_datetime(str(remote))
                    != dt_util.parse_datetime(
                        str(module.raw.get("programmingUpdatedAt") or "")
                    )
                    and not await self._async_reread_programs(module)
                ):
                    fresh.pop("programmingUpdatedAt")
                    # Keep the pair consistent: a fresh sentAt against the stale
                    # updatedAt would hide edits the controller has not got.
                    fresh.pop("programmingSentAt", None)
                module.raw.update(fresh)

    async def _async_reread_programs(self, module: SolemModule) -> bool:
        """Rebuild ``module``'s programs from a fresh read of its page.

        Returns False, leaving the old programs in place, when the page cannot
        be read. Only the module record and its programs are touched: the live
        state and the pending ON/OFF bookkeeping are not, so the page's stored
        ``status`` never counts as the controller acknowledging a command.
        """
        try:
            obj, _inputs = await self.client.async_get_module_page(module.id)
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemError as err:
            _LOGGER.debug("Could not re-read programs of %s: %s", module.id, err)
            return False
        if not obj:
            _LOGGER.debug("Re-reading %s returned no parseable data", module.id)
            return False
        try:
            programs = _build_programs(obj)
        except TypeError, ValueError, KeyError, AttributeError:
            _LOGGER.debug("Re-reading %s returned undecodable programs", module.id)
            return False
        module.raw.update(obj)
        module.programs = programs
        if module.is_agricultural:
            _name_stations_after_programs(module)
        return True

    async def _async_poll_inputs(self, module_id: str) -> None:
        """Refresh every sensor reading on a module.

        Each cumulative total comes from the input's newest tick (which the
        cloud always answers with, however old it is), while a flow rate is
        derived from a short window of ticks. A turbine has no total to read,
        so for a module carrying one the window reaches back to local midnight
        and the day's ticks are added up. On failure the previous reading is
        kept: blanking a cumulative sensor punches a hole in long-term
        statistics.
        """
        module = self.modules[module_id]
        now = dt_util.utcnow()
        day_start = dt_util.start_of_local_day(dt_util.as_local(now))
        start = now - FLOW_WINDOW
        if module.turbines:
            start = min(start, dt_util.as_utc(day_start))
        try:
            records = await self.client.async_get_module_sensor_data(
                module_id,
                start.isoformat().replace("+00:00", "Z"),
                now.isoformat().replace("+00:00", "Z"),
            )
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemConnectionError as err:
            _LOGGER.debug("Sensor window fetch failed for %s: %s", module_id, err)
            records = []
        by_id = {r["id"]: r for r in records if r.get("id")}
        # The window returns every input's record even when it holds no ticks,
        # so it is also where the live flags come from.
        self.input_records.update(by_id)

        for meter in module.flow_meters:
            if (
                tick := await self._async_last_tick(meter.id, meter.expression)
            ) is None:
                continue
            volume, raw_volume, timestamp = tick
            record = by_id.get(meter.id, {})
            self.flow[meter.id] = SolemFlowReading(
                volume=volume,
                raw_volume=raw_volume,
                timestamp=timestamp,
                rate=_compute_flow_rate(record, now, meter.interval),
                record=record or meter.raw,
            )

        for gauge in module.rain_gauges:
            if (
                tick := await self._async_last_tick(gauge.id, gauge.expression)
            ) is None:
                continue
            self.rain[gauge.id] = SolemRainReading(*tick)

        for sensor in module.rain_sensors:
            # Its newest tick, however old. Ticks arrive only now and then (none
            # overnight in the #8 dump), and the last one stands until the next.
            if (tick := await self._async_last_tick(sensor.id, "")) is None:
                continue
            _, raw_value, timestamp = tick
            self.rain_sensor_readings[sensor.id] = SolemRainSensorReading(
                wet=raw_value != 0, timestamp=timestamp
            )

        for turbine in module.turbines:
            # No record means the window fetch failed. Keep the last reading
            # rather than let today's total drop to nothing.
            if (record := by_id.get(turbine.id)) is None:
                continue
            self.turbine_readings[turbine.id] = _compute_turbine_reading(
                record,
                now,
                day_start,
                turbine.interval,
                self.turbine_readings.get(turbine.id),
            )

    async def _async_last_tick(
        self, input_id: str, expression: str
    ) -> tuple[float, float, datetime] | None:
        """Return an input's newest tick as ``(scaled, raw, timestamp)``.

        None when the fetch failed, when the sensor has never reported, or when
        its value cannot be scaled -- the caller then keeps its previous reading
        rather than inventing a zero that would land in long-term statistics.
        """
        try:
            tick = await self.client.async_get_last_input_tick(input_id)
        except SolemAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolemConnectionError as err:
            _LOGGER.debug("Tick fetch failed for input %s: %s", input_id, err)
            return None
        raw_value = tick.get("value")
        timestamp = dt_util.parse_datetime(tick.get("timestamp") or "")
        if raw_value is None or timestamp is None:
            return None
        if (scaled := apply_expression(expression, float(raw_value))) is None:
            return None
        return scaled, float(raw_value), dt_util.as_utc(timestamp)

    def flow_reading(self, meter_id: str) -> SolemFlowReading | None:
        """Return the latest reading for a flow meter, if one has arrived."""
        return self.flow.get(meter_id)

    def rain_reading(self, gauge_id: str) -> SolemRainReading | None:
        """Return the latest reading for a rain gauge, if one has arrived."""
        return self.rain.get(gauge_id)

    def rain_sensor_reading(self, sensor_id: str) -> SolemRainSensorReading | None:
        """Return the latest reading for a rain sensor, if one has arrived."""
        return self.rain_sensor_readings.get(sensor_id)

    def turbine_reading(self, turbine_id: str) -> SolemTurbineReading | None:
        """Return the latest reading for a turbine, if one has been computed."""
        return self.turbine_readings.get(turbine_id)

    def input_record(self, input_id: str) -> dict[str, Any] | None:
        """Return an input's record as last polled, if it has been."""
        return self.input_records.get(input_id)

    # -- helpers used by entities ------------------------------------------

    def relevant_module_ids(self) -> set[str]:
        """Modules this integration owns: irrigation controllers + their gateway.

        Keeps pool modules on the same account (handled by other integrations)
        out of Home Assistant.

        A watering module carrying a flow meter is already a controller. SOLEM
        also sells sensors as modules of their own, which are not a watering
        type, so those are admitted on the strength of what they measure: a flow
        meter (lr-fl and friends -- untested, as I only have the
        controller-attached topology), a rain gauge (the LR-MS, #8), a rain
        sensor or a turbine.

        Nothing Bluetooth-only is owned: the cloud cannot reach it.
        """
        ids = {
            m.id
            for m in self.modules.values()
            if m.is_controller
            or (
                m.has_modelled_inputs
                and not m.raw.get("typeIsPoolProduct")
                and not m.is_bluetooth_only
            )
        }
        for module_id in list(ids):
            relay = self.module_state(module_id).get("relay")
            if relay and relay in self.modules:
                ids.add(relay)
        return ids

    def module_state(self, module_id: str) -> dict[str, Any]:
        """Return the cached live state for a module (possibly empty)."""
        return (self.data or {}).get(module_id, {})

    def _watering(self, module_id: str) -> dict[str, Any]:
        return self.module_state(module_id).get("status", {}).get("watering", {})

    def running_station_index(self, module_id: str) -> int:
        """Return the index of the station currently watering (0 = none)."""
        try:
            return int(self._watering(module_id).get("runningStation", 0) or 0)
        except TypeError, ValueError:
            return 0

    def ag_running_outputs(self, module_id: str) -> dict[int, dict[str, Any]]:
        """The LR-AG outputs watering right now, keyed by station index, in order.

        Several can water at once. Measured live on an LR-AG: an output reports
        ``state`` 1 during a manual run but 2 while a program runs it, so any
        non-zero ``state`` is watering.
        """
        running: dict[int, dict[str, Any]] = {}
        for index, output in sorted(_ag_outputs(self.module_state(module_id)).items()):
            try:
                if int(output.get("state") or 0) != 0:
                    running[index] = output
            except TypeError, ValueError:
                continue
        return running

    def _pending_status(self, module_id: str) -> tuple[bool, int] | None:
        """The ON/OFF command the cloud has yet to confirm, while it still counts.

        The cloud reports the old status until the controller acks over LoRa,
        which takes longer than the reconcile poll, so without this the switch
        and the rain delay would flip back after every toggle, until the next
        regular poll. A command never confirmed within the grace is dropped:
        the cloud is then shown as is.
        """
        if (pending := self._status_pending.get(module_id)) is None:
            return None
        enabled, days, issued_at = pending
        if dt_util.utcnow() - issued_at < ENABLE_ACK_GRACE:
            return enabled, days
        del self._status_pending[module_id]
        return None

    def _status_scope(self, module_id: str) -> dict[str, Any]:
        """How to read ``module_id``'s ON/OFF status: see ``_reported_status``."""
        module = self.modules.get(module_id)
        if module is None or not module.is_agricultural:
            return {"agricultural": False, "stations": None}
        return {
            "agricultural": True,
            "stations": frozenset(s.index for s in module.stations),
        }

    def watering_enabled(self, module_id: str) -> bool | None:
        """Return whether the controller is ON, or None if the cloud does not say.

        A just-issued ON/OFF command wins until the cloud confirms it (see
        ``_pending_status``).
        """
        if (pending := self._pending_status(module_id)) is not None:
            return pending[0]
        status = _reported_status(
            self.module_state(module_id), **self._status_scope(module_id)
        )
        return None if status is None else status[0]

    def rain_delay_days(self, module_id: str) -> int | None:
        """Return the days left before an OFF controller turns itself back on.

        0 when it is on or off permanently; None if the cloud does not say.
        """
        if (pending := self._pending_status(module_id)) is not None:
            enabled, days = pending
            return 0 if enabled else days
        status = _reported_status(
            self.module_state(module_id), **self._status_scope(module_id)
        )
        return None if status is None else status[1]

    def schedule_overview(self, module_id: str) -> dict[str, Any]:
        """Describe a module's stored programs and when they next run.

        Times are in Home Assistant's time zone: the module record carries no
        time zone of its own. The result holds ``next_run``, a summary of every
        program under ``programs``, the ``occurrences`` due from local midnight
        today through tomorrow (capped), and whether MySOLEM holds edits the
        controller has not received (``pending_push``). A controller that is
        off, or an LR-AG station under a rain delay, has no runs, but its
        programs are still listed. ``{}`` for an unknown module.
        """
        module = self.modules.get(module_id)
        if module is None:
            return {}
        now = dt_util.now()
        start = dt_util.start_of_local_day()
        end = dt_util.start_of_local_day(start.date() + timedelta(days=2))
        stations = [(s.index, s.name) for s in module.stations]
        agricultural = module.is_agricultural
        # None (a Bluetooth-only module, or no state yet) does not gate.
        enabled = self.watering_enabled(module_id)
        # An LR-AG suspends per station; a classic controller only globally.
        pending = self._pending_status(module_id)
        # A just-issued ON clears the rain delays, which the cloud still reports.
        suspended = (
            {
                idx
                for idx, output in _ag_outputs(self.module_state(module_id)).items()
                if output.get("rainDelay")
            }
            if agricultural and not (pending is not None and pending[0])
            else set()
        )
        active = [st for st in stations if st[0] not in suspended]

        programs: list[dict[str, Any]] = []
        occurrences: list[dict[str, Any]] = []
        next_starts: list[datetime] = []
        for program in module.programs:
            try:
                summary = schedule.program_summary(
                    program.raw, agricultural=agricultural
                )
                if agricultural:
                    paired = next(
                        (name for idx, name in stations if idx == program.index + 1),
                        None,
                    )
                    summary["stations"] = [paired] if paired else []
                else:
                    durations = summary.get("durations") or {}
                    summary["stations"] = [
                        name for idx, name in sorted(stations) if str(idx) in durations
                    ]
                program_runs: list[dict[str, Any]] = []
                run = None
                if enabled is not False:
                    program_runs = schedule.program_occurrences(
                        program.raw,
                        active,
                        start,
                        end,
                        agricultural=agricultural,
                        program_name=program.name,
                    )
                    run = schedule.next_run(
                        program.raw, active, now, agricultural=agricultural
                    )
            except (TypeError, ValueError) as err:
                _LOGGER.debug(
                    "Program %s of %s could not be decoded: %s",
                    program.id,
                    module_id,
                    err,
                )
                continue
            programs.append(summary)
            occurrences.extend(program_runs)
            if run is not None:
                next_starts.append(run)
        occurrences.sort(
            key=lambda o: (datetime.fromisoformat(o["start"]), o["station_index"])
        )
        return {
            "next_run": min(next_starts) if next_starts else None,
            "programs": programs,
            "occurrences": occurrences[:MAX_SCHEDULE_OCCURRENCES],
            "pending_push": _programming_out_of_sync(module.raw),
        }

    def _apply_optimistic_watering(self, module_id: str, **fields: Any) -> None:
        """Optimistically reflect a manual command before the next poll.

        LoRa downlinks are slow, so update the cache immediately and notify
        listeners; the next poll reconciles with the controller's real state.
        """
        data = dict(self.data or {})
        state = dict(data.get(module_id, {}))
        status = dict(state.get("status", {}))
        status["watering"] = {**status.get("watering", {}), **fields}
        state["status"] = status
        data[module_id] = state
        self.async_set_updated_data(data)

    def apply_optimistic_running_station(self, module_id: str, index: int) -> None:
        """Optimistically show ``index`` as the running station (0 = none)."""
        self._apply_optimistic_watering(module_id, runningStation=index)

    def _apply_optimistic_ag_outputs(
        self, module: SolemModule, stations: list[SolemStation], **fields: Any
    ) -> None:
        """Optimistically patch the live outputs of an LR-AG's ``stations``.

        The LR-AG counterpart of ``_apply_optimistic_watering``. An output the
        cache does not hold yet is added, as a classic running station would be.
        """
        data = dict(self.data or {})
        state = dict(data.get(module.id, {}))
        status = dict(state.get("status", {}))
        outputs = {index: dict(output) for index, output in _ag_outputs(state).items()}
        for station in stations:
            outputs.setdefault(station.index, {"index": station.index - 1}).update(
                fields
            )
        status["ag"] = {
            **status.get("ag", {}),
            "outputs": [outputs[index] for index in sorted(outputs)],
        }
        state["status"] = status
        data[module.id] = state
        self.async_set_updated_data(data)

    # -- commands (client call + optimistic update + reconcile) ------------
    # Shared by the station valves and the ``run`` service so the optimistic
    # update and the delayed reconcile are applied consistently on every path.

    async def async_command_stop(self, module: SolemModule) -> None:
        """Stop any running watering on a controller (global stop).

        An LR-AG has no global stop: every station is stopped in one command.
        """
        if module.is_agricultural:
            await self.client.async_stop_ag_stations(
                module.serial, [station.index - 1 for station in module.stations]
            )
            self._apply_optimistic_ag_outputs(module, module.stations, state=0)
        else:
            await self.client.async_stop(module.serial)
            self.apply_optimistic_running_station(module.id, 0)
        self.async_schedule_refresh()

    async def async_command_stop_ag_station(
        self, module: SolemModule, station: SolemStation
    ) -> None:
        """Stop a single LR-AG station, leaving any other one running."""
        await self.client.async_stop_ag_station(
            module.serial, module.id, station.id, station.index - 1
        )
        self._apply_optimistic_ag_outputs(module, [station], state=0)
        self.async_schedule_refresh()

    async def async_command_run_station(
        self, module: SolemModule, station: SolemStation, minutes: int
    ) -> None:
        """Run a single station for ``minutes`` (replaces any running station).

        On an LR-AG, any other running station keeps watering.
        """
        if module.is_agricultural:
            # The run names the station's program (the output id only for a
            # station that has none), see ``SolemModule.paired_program``.
            program = module.paired_program(station)
            await self.client.async_run_ag_station(
                module.serial,
                module.id,
                program.id if program else station.id,
                station.index - 1,
                minutes,
            )
            self._apply_optimistic_ag_outputs(module, [station], state=1)
        else:
            await self.client.async_run_station(module.serial, station.id, minutes)
            self.apply_optimistic_running_station(module.id, station.index)
        self.async_schedule_refresh()

    async def async_command_run_program(
        self, module: SolemModule, program: SolemProgram
    ) -> None:
        """Start a stored program (its first station appears on the next poll).

        An LR-AG has no command to start a program: each one is the schedule of
        a single station, so that station is run, for its run duration.
        """
        if module.is_agricultural:
            station = next(
                (s for s in module.stations if module.paired_program(s) is program),
                None,
            )
            if station is None:
                raise ServiceValidationError(
                    f"Program {program.name} has no station to run"
                )
            await self.async_command_run_station(
                module, station, self.get_run_minutes(station.id)
            )
            return
        await self.client.async_run_program(module.serial, program.id)
        self.async_schedule_refresh()

    async def async_command_set_status(
        self, module: SolemModule, enabled: bool, days: int = 0
    ) -> None:
        """Turn a controller ON, or OFF permanently (``days=0``) or for N days.

        An LR-AG takes it on an endpoint of its own, for all its stations.
        """
        if module.is_agricultural and days > MAX_RAIN_DELAY_DAYS_AG:
            raise ServiceValidationError(
                f"An agricultural module can be disabled for at most "
                f"{MAX_RAIN_DELAY_DAYS_AG} days"
            )
        issued_at = dt_util.utcnow()
        if module.is_agricultural:
            await self.client.async_set_ag_status(module.id, enabled=enabled, days=days)
        else:
            await self.client.async_set_status(
                module.serial, enabled=enabled, days=days
            )
        pending = self._status_pending.get(module.id)
        if _reported_status(
            self.module_state(module.id), **self._status_scope(module.id)
        ) is not None and (
            # Only where the cloud reports the status: inventing one would make
            # a controller that never does look like it reports it. And never
            # over a command issued later whose request happened to return first.
            pending is None or pending[2] <= issued_at
        ):
            self._status_pending[module.id] = (enabled, days, issued_at)
            if module.is_agricultural:
                self._apply_optimistic_ag_outputs(
                    module,
                    module.stations,
                    rainDelay=0 if enabled else (days or RAIN_DELAY_PERMANENT),
                )
            else:
                self._apply_optimistic_watering(
                    module.id,
                    state=1 if enabled else 0,
                    rainDelay=0 if enabled else days,
                )
        self.async_schedule_refresh()
