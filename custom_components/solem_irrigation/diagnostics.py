"""Diagnostics for SOLEM irrigation.

Most of what this integration cannot do yet is blocked on one thing: SOLEM
sells hardware the maintainer does not own, and the MySOLEM API is private and
undocumented. This dump exists so a user can answer "what does your account
actually return?" in one click instead of a mail exchange.

It is therefore deliberately *raw*: the unparsed module record, **every** input
(not just the sensors the integration models today) with what it reported over
the last day, and the live state, for **every** module on the account --
including the ones ``relevant_module_ids()`` filters out, since a sensor the
integration ignores may be exactly the one being asked about.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import SolemApiClient, SolemError
from .coordinator import SolemConfigEntry, SolemModule

# How far back each sensor's readings are fetched, and how many are kept. A day
# covers the watering run or the shower a reporter is asked to reproduce; the
# cap stops a busy meter (a tick a minute) from burying everything else.
SAMPLE_WINDOW = timedelta(hours=24)
SAMPLE_MAX_TICKS = 60

# Redaction is a *denylist*, on purpose. The whole value of this file is that it
# shows keys nobody has modelled yet, so an allowlist would filter out the one
# thing it is for.
#
# NOTE: ``async_redact_data`` matches keys **exactly** (``key in to_redact``),
# with no substring matching -- so every spelling has to be listed. ``serial``
# does not cover ``serialNumber``, which does not cover ``moduleSerialNumber``.
TO_REDACT = {
    # Credentials and account identity
    "email",
    "password",
    "userEmail",
    "user",
    "userId",
    "owner",
    "token",
    "apiKey",
    # The user id again, under the name it takes inside a program snapshot.
    "snapshotBy",
    # Hardware identity. ``uuid`` matters as much as the two obvious ones: it is
    # built from the MAC with the separators stripped and repeats the serial's
    # significant half (MAC C8:B9:61:0D:C2:66 / serial 1000000DC2660001 ->
    # uuid 100058B9610DC26600000001000DC266), so leaving it in hands back both
    # of the values redacted just above. ``defaultName`` is the factory name and
    # ends in the MAC tail; the user-set ``name`` is deliberately kept.
    "serial",
    "serialNumber",
    "moduleSerialNumber",
    "mac",
    "macAddress",
    "uuid",
    "defaultName",
    "imei",
    "iccid",
    "deviceId",
    # Location. ``addressWeather`` carries the user's town and department, which
    # has no business in a file people paste into a public issue.
    # ``locationKey`` is an AccuWeather location id, which resolves straight
    # back to the town that ``addressWeather`` and the coordinates hide.
    "address",
    "addressWeather",
    "locationKey",
    "latitude",
    "longitude",
    "gpsCoordinates",
    "ssid",
}

# Module, program, input and relay ids stay: they are account-internal handles
# with no meaning outside it, and without them a dump cannot be cross-read
# (which input belongs to which module, which gateway a controller sits behind).

# Keys dropped rather than redacted: large, third-party, and of no diagnostic
# value. ``weatherForecast`` is a multi-day AccuWeather payload (MySOLEM uses it
# for its automatic water budget) that would bury what anyone opens this file
# for. Deliberately *not* dropped: ``lastSentProgramsSnapshot``, which is bulky
# but carries the per-model program structure -- the one place another
# controller's programs can be compared against ours.
TO_DROP = ("weatherForecast",)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _input_samples(client: SolemApiClient, module: SolemModule) -> dict[str, Any]:
    """Fetch, live, what each of a module's sensors reported lately.

    ``raw_inputs`` holds no readings at all, and the integration only polls the
    inputs it models -- so for any other sensor, this is the one place its
    values, and therefore their scale, can be seen. A failure is recorded
    rather than raised: a partial dump beats none.
    """
    # Type 0 is an input slot nothing is wired to.
    inputs = [record for record in module.raw_inputs if record.get("type")]
    if not inputs:
        return {}
    now = dt_util.utcnow()
    samples: dict[str, Any] = {}
    try:
        window = await client.async_get_module_sensor_data(
            module.id, _iso(now - SAMPLE_WINDOW), _iso(now)
        )
    except SolemError as err:
        window = []
        samples["window_error"] = str(err)
    by_id = {record["id"]: record for record in window if record.get("id")}
    for record in inputs:
        live = by_id.get(record["id"], {})
        ticks = live.get("computedSensorData") or []
        sample: dict[str, Any] = {
            "type": record.get("type"),
            "window_tick_count": len(ticks),
            # Already scaled by the cloud (``forceRawOrComputed=computed``).
            "window_ticks": ticks[-SAMPLE_MAX_TICKS:],
            # Only what moved since setup: the rest is in ``raw_inputs``. The
            # window's ``name`` is always blank (the client fills the setup copy
            # from the page's label), so it would only add noise.
            "changed_since_setup": {
                key: value
                for key, value in live.items()
                if key not in ("computedSensorData", "name")
                and record.get(key) != value
            },
        }
        try:
            # Raw: scale it with the input's ``expression``.
            sample["last_tick"] = await client.async_get_last_input_tick(record["id"])
        except SolemError as err:
            sample["last_tick_error"] = str(err)
        samples[record["id"]] = sample
    return samples


async def _module_diagnostics(
    coordinator: Any, module: Any, relevant: set[str]
) -> dict[str, Any]:
    """Return everything known about one module."""
    return {
        "type": module.type,
        "display_type": module.display_type,
        "is_controller": module.is_controller,
        "is_relevant": module.id in relevant,
        "stations": [{"name": s.name, "index": s.index} for s in module.stations],
        "programs": [{"name": p.name, "index": p.index} for p in module.programs],
        "flow_meters": [
            {
                "name": m.name,
                "index": m.index,
                "unit": m.unit,
                "expression": m.expression,
                "interval": m.interval,
            }
            for m in module.flow_meters
        ],
        "rain_gauges": [
            {"name": g.name, "index": g.index, "expression": g.expression}
            for g in module.rain_gauges
        ],
        # The two raw payloads. ``raw`` is the embedded ``let module`` object;
        # ``raw_inputs`` is the unfiltered sensor list, which ``raw`` does not
        # contain and which is where an unmodelled sensor shows up.
        "raw": {k: v for k, v in module.raw.items() if k not in TO_DROP},
        "raw_inputs": module.raw_inputs,
        "input_samples": await _input_samples(coordinator.client, module),
        "state": coordinator.module_state(module.id),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolemConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    # Computed once: it walks every module's state, and the result is shared by
    # the summary below and by each module's ``is_relevant``.
    relevant = coordinator.relevant_module_ids()
    data = {
        "entry": {
            "region": entry.data.get("region"),
            "data": dict(entry.data),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "module_count": len(coordinator.modules),
            "relevant_module_ids": sorted(relevant),
            "run_minutes": coordinator.run_minutes,
        },
        "flow_readings": {
            meter_id: {
                "volume": reading.volume,
                "raw_volume": reading.raw_volume,
                "timestamp": reading.timestamp.isoformat(),
                "rate": reading.rate,
            }
            for meter_id, reading in coordinator.flow.items()
        },
        "rain_readings": {
            gauge_id: {
                "total": reading.total,
                "raw_total": reading.raw_total,
                "timestamp": reading.timestamp.isoformat(),
            }
            for gauge_id, reading in coordinator.rain.items()
        },
        "modules": {
            module_id: await _module_diagnostics(coordinator, module, relevant)
            for module_id, module in coordinator.modules.items()
        },
    }
    return async_redact_data(data, TO_REDACT)
