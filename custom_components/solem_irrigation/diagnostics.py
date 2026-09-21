"""Diagnostics for SOLEM irrigation.

Most of what this integration cannot do yet is blocked on one thing: SOLEM
sells hardware the maintainer does not own, and the MySOLEM API is private and
undocumented. This dump exists so a user can answer "what does your account
actually return?" in one click instead of a mail exchange.

It is therefore deliberately *raw*: the unparsed module record, **every** input
(not just the flow meters the integration models today), and the live state, for
**every** module on the account -- including the ones ``relevant_module_ids()``
filters out, since a sensor the integration ignores may be exactly the one being
asked about.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import SolemConfigEntry

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
    # Hardware identity
    "serial",
    "serialNumber",
    "moduleSerialNumber",
    "mac",
    "macAddress",
    "imei",
    "iccid",
    "deviceId",
    # Location. ``addressWeather`` carries the user's town and department, which
    # has no business in a file people paste into a public issue.
    "address",
    "addressWeather",
    "latitude",
    "longitude",
    "gpsCoordinates",
    "ssid",
}

# Keys dropped rather than redacted: large, third-party, and of no diagnostic
# value. ``weatherForecast`` is a multi-day AccuWeather payload (MySOLEM uses it
# for its automatic water budget) that would bury what anyone opens this file
# for. Deliberately *not* dropped: ``lastSentProgramsSnapshot``, which is bulky
# but carries the per-model program structure -- the one place another
# controller's programs can be compared against ours.
TO_DROP = ("weatherForecast",)


def _module_diagnostics(
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
        # The two raw payloads. ``raw`` is the embedded ``let module`` object;
        # ``raw_inputs`` is the unfiltered sensor list, which ``raw`` does not
        # contain and which is where an unmodelled sensor shows up.
        "raw": {k: v for k, v in module.raw.items() if k not in TO_DROP},
        "raw_inputs": module.raw_inputs,
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
        "modules": {
            module_id: _module_diagnostics(coordinator, module, relevant)
            for module_id, module in coordinator.modules.items()
        },
    }
    return async_redact_data(data, TO_REDACT)
