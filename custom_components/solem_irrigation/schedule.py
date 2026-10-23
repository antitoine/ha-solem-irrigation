"""Pure schedule logic for SOLEM programs (no Home Assistant imports).

The MySOLEM wire format (reverse-engineered from the web app) stores one
``programs[]`` schema for both controller families:

* Recurrence — ``cycle``: 0 = weekly (``weekDays`` bitmask, Monday = bit 0 …
  Sunday = bit 6), 1 = even days of month, 2 = odd days, 3 = odd days except
  the 31st, 4 = interval (every ``numberOfDays`` days anchored at
  ``periodicStartDate`` = UTC now + ``synchroDay`` days when saved).
* Classic controllers (LR-IP…, ``programType`` 1) — ``startTimes[8]``
  (minutes since midnight, -1 unset) fire the stations sequentially, each for
  ``stationsDuration[12]`` seconds (indexed by 0-based station position),
  scaled by ``waterBudget`` %, separated by ``timeBetweenStations`` seconds.
* Agricultural (LR-AG, ``programType`` 3) — exactly four ``windows[]``
  ``{start, end, on, off}``: start/end minutes since midnight (-1 = unused
  slot), on/off watering/soak cycle seconds; a window whose end ≤ start
  crosses midnight. Programs pair 1:1 with stations (program.index 0-based ==
  station.index - 1).
* There is no per-program enabled flag: the portal "parks" a program by
  setting ``cycle=0, weekDays=0``, which :func:`program_is_enabled` recognises.

This module only reads programs: it decodes them into summaries and
expands them into concrete runs. All datetime math takes explicit
``now``/timezone inputs so it is testable and DST-safe.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

CYCLE_WEEKLY = 0
CYCLE_EVEN_DAYS = 1
CYCLE_ODD_DAYS = 2
CYCLE_ODD_DAYS_NOT_31 = 3
CYCLE_INTERVAL = 4
CYCLE_ALWAYS = 5  # internal portal value (fertigation)

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

SCHEDULE_TYPE_WEEKLY = "weekly"
SCHEDULE_TYPE_EVEN_DAYS = "even_days"
SCHEDULE_TYPE_ODD_DAYS = "odd_days"
SCHEDULE_TYPE_ODD_DAYS_NOT_31 = "odd_days_not_31"
SCHEDULE_TYPE_INTERVAL = "interval"

_CYCLE_TO_TYPE = {
    CYCLE_WEEKLY: SCHEDULE_TYPE_WEEKLY,
    CYCLE_EVEN_DAYS: SCHEDULE_TYPE_EVEN_DAYS,
    CYCLE_ODD_DAYS: SCHEDULE_TYPE_ODD_DAYS,
    CYCLE_ODD_DAYS_NOT_31: SCHEDULE_TYPE_ODD_DAYS_NOT_31,
    CYCLE_INTERVAL: SCHEDULE_TYPE_INTERVAL,
}

MINUTES_PER_DAY = 24 * 60
_LAST_MONTH_DAY = 31
_WEEKDAY_MASK = 0x7F  # seven day bits; anything above is noise


class ScheduleError(ValueError):
    """A schedule computation was given input it cannot work with."""


# -- conversions ------------------------------------------------------------


def mask_to_weekdays(mask: int) -> list[str]:
    """Convert the wire bitmask to weekday keys (``mon`` … ``sun``)."""
    mask = max(0, mask) & _WEEKDAY_MASK
    return [key for bit, key in enumerate(WEEKDAY_KEYS) if mask >> bit & 1]


def _weekday_mask(raw: dict[str, Any]) -> int:
    """The program's day bitmask, limited to the seven day bits."""
    return max(0, int(raw.get("weekDays") or 0)) & _WEEKDAY_MASK


def _water_budget(raw: dict[str, Any]) -> int:
    """The water budget in percent: 100 only when absent (0 means 0)."""
    budget = raw.get("waterBudget")
    return 100 if budget is None else int(budget)


def minutes_to_hhmm(minutes: int) -> str:
    """Render minutes since midnight as ``HH:MM`` (wraps past midnight)."""
    minutes = int(minutes) % MINUTES_PER_DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _parse_wire_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp as stored by the cloud (UTC, ``Z`` suffix)."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


# -- program predicates ------------------------------------------------------


def program_is_enabled(raw: dict[str, Any]) -> bool:
    """Is the program allowed to run at all?

    Mirrors the portal's own convention: a weekly program with an empty
    day mask can never fire, and that is exactly how the portal "parks" a
    program (there is no dedicated enabled flag on the wire).
    """
    cycle = int(raw.get("cycle", CYCLE_WEEKLY) or 0)
    if cycle == CYCLE_WEEKLY:
        return bool(_weekday_mask(raw))
    return True


def _active_start_times(raw: dict[str, Any]) -> list[int]:
    return sorted(
        int(t)
        for t in (raw.get("startTimes") or [])
        if isinstance(t, (int, float)) and int(t) >= 0
    )


def _is_minute(value: Any) -> bool:
    """Is ``value`` a usable minutes-since-midnight (-1 = unused slot)?"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _active_windows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        w
        for w in (raw.get("windows") or [])
        if isinstance(w, dict)
        and _is_minute(w.get("start"))
        and _is_minute(w.get("end"))
    ]


def program_runs_on(raw: dict[str, Any], day: date, *, tz: tzinfo) -> bool:
    """Does the program's recurrence match calendar ``day`` (local)?

    ``tz`` localises the UTC ``periodicStartDate`` anchor for interval
    programs; the other cycles are pure calendar tests.
    """
    if not program_is_enabled(raw):
        return False
    cycle = int(raw.get("cycle", CYCLE_WEEKLY) or 0)
    matches = False
    if cycle == CYCLE_WEEKLY:
        matches = bool(_weekday_mask(raw) >> (day.isoweekday() - 1) & 1)
    elif cycle == CYCLE_EVEN_DAYS:
        matches = day.day % 2 == 0
    elif cycle == CYCLE_ODD_DAYS:
        matches = day.day % 2 == 1
    elif cycle == CYCLE_ODD_DAYS_NOT_31:
        matches = day.day % 2 == 1 and day.day != _LAST_MONTH_DAY
    elif cycle == CYCLE_INTERVAL:
        anchor = _parse_wire_datetime(raw.get("periodicStartDate"))
        if anchor is not None:
            anchor_day = anchor.astimezone(tz).date()
            every = max(1, int(raw.get("numberOfDays", 1) or 1))
            delta = (day - anchor_day).days
            matches = delta >= 0 and delta % every == 0
    elif cycle == CYCLE_ALWAYS:
        matches = True
    return matches


# -- occurrence expansion -----------------------------------------------------


def program_occurrences(
    raw: dict[str, Any],
    stations: list[tuple[int, str]],
    start: datetime,
    end: datetime,
    *,
    agricultural: bool,
    program_name: str | None = None,
) -> list[dict[str, Any]]:
    """Expand a program into concrete watering segments within ``start``-``end``.

    ``stations`` is ``[(1-based index, name), …]``. Returns dicts with
    ``program``, ``station``, ``station_index`` and timezone-aware ISO
    ``start``/``end`` — the shape the dashboard timeline consumes. Segments
    that merely overlap the range are included (clipping is the chart's job).
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ScheduleError("program_occurrences needs timezone-aware datetimes")
    tz = start.tzinfo
    name = program_name or raw.get("name") or f"Program {raw.get('index', '?')}"
    occurrences: list[dict[str, Any]] = []

    day = start.date() - timedelta(days=1)  # catch segments crossing midnight in
    last_day = end.date()
    while day <= last_day:
        if program_runs_on(raw, day, tz=tz):
            day_start = datetime.combine(day, time(0), tzinfo=tz)
            if agricultural:
                segments = _ag_day_segments(raw, stations, day_start)
            else:
                segments = _classic_day_segments(raw, stations, day_start)
            for segment in segments:
                if segment["start_dt"] < end and segment["end_dt"] > start:
                    occurrences.append(
                        {
                            "program": name,
                            "station": segment["station"],
                            "station_index": segment["station_index"],
                            "start": segment["start_dt"].isoformat(),
                            "end": segment["end_dt"].isoformat(),
                        }
                    )
        day += timedelta(days=1)

    occurrences.sort(
        key=lambda o: (datetime.fromisoformat(o["start"]), o["station_index"])
    )
    return occurrences


def _add_real_time(moment: datetime, delta: timedelta) -> datetime:
    """``moment`` plus ``delta`` of elapsed time, in ``moment``'s zone."""
    return (moment.astimezone(UTC) + delta).astimezone(moment.tzinfo)


def _classic_day_segments(
    raw: dict[str, Any],
    stations: list[tuple[int, str]],
    day_start: datetime,
) -> list[dict[str, Any]]:
    """One day of an LR-IP program: stations run back to back per start time."""
    budget = _water_budget(raw)
    pause = max(0, int(raw.get("timeBetweenStations", 0) or 0))
    durations = raw.get("stationsDuration") or []
    segments: list[dict[str, Any]] = []
    for start_minutes in _active_start_times(raw):
        cursor = day_start + timedelta(minutes=start_minutes)
        for station_index, station_name in sorted(stations):
            try:
                duration_s = int(durations[station_index - 1] or 0)
            except IndexError, TypeError, ValueError:
                duration_s = 0
            if duration_s <= 0:
                continue
            scaled = duration_s * budget / 100
            if scaled <= 0:
                continue
            # Durations are real time: add them in UTC so a DST change inside
            # the run does not stretch or shrink it.
            segment_end = _add_real_time(cursor, timedelta(seconds=scaled))
            segments.append(
                {
                    "station": station_name,
                    "station_index": station_index,
                    "start_dt": cursor,
                    "end_dt": segment_end,
                }
            )
            cursor = _add_real_time(segment_end, timedelta(seconds=pause))
    return segments


def _ag_day_segments(
    raw: dict[str, Any],
    stations: list[tuple[int, str]],
    day_start: datetime,
) -> list[dict[str, Any]]:
    """One day of an LR-AG program: its windows on the paired station."""
    program_index = int(raw.get("index", 0) or 0)
    paired = next(
        ((idx, name) for idx, name in stations if idx == program_index + 1), None
    )
    if paired is None:
        return []
    station_index, station_name = paired
    segments: list[dict[str, Any]] = []
    for window in _active_windows(raw):
        start_minutes = int(window["start"])
        end_minutes = int(window["end"])
        if end_minutes <= start_minutes:  # crosses midnight
            end_minutes += MINUTES_PER_DAY
        segments.append(
            {
                "station": station_name,
                "station_index": station_index,
                "start_dt": day_start + timedelta(minutes=start_minutes),
                "end_dt": day_start + timedelta(minutes=end_minutes),
            }
        )
    return segments


def next_run(
    raw: dict[str, Any],
    stations: list[tuple[int, str]],
    now: datetime,
    *,
    agricultural: bool,
    horizon_days: int = 32,
) -> datetime | None:
    """First scheduled segment start at or after ``now`` (None = never).

    The horizon covers the longest possible interval cycle (every 31 days).
    """
    occurrences = program_occurrences(
        raw,
        stations,
        now,
        now + timedelta(days=horizon_days),
        agricultural=agricultural,
    )
    for occurrence in occurrences:
        start = datetime.fromisoformat(occurrence["start"])
        if start >= now:
            return start
    return None


# -- summaries ----------------------------------------------------------------


def program_summary(raw: dict[str, Any], *, agricultural: bool) -> dict[str, Any]:
    """Normalised, human/dashboard-friendly view of one program."""
    cycle = int(raw.get("cycle", CYCLE_WEEKLY) or 0)
    summary: dict[str, Any] = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "index": int(raw.get("index", 0) or 0),
        "enabled": program_is_enabled(raw),
        "type": _CYCLE_TO_TYPE.get(cycle, f"cycle_{cycle}"),
        "water_budget": _water_budget(raw),
    }
    if cycle == CYCLE_WEEKLY:
        summary["days"] = mask_to_weekdays(_weekday_mask(raw))
    elif cycle == CYCLE_INTERVAL:
        summary["every_days"] = int(raw.get("numberOfDays", 1) or 1)
        summary["offset_days"] = int(raw.get("synchroDay", 0) or 0)
    if agricultural:
        summary["windows"] = [
            {
                "start": minutes_to_hhmm(int(w["start"])),
                "end": minutes_to_hhmm(int(w["end"])),
                "on_min": round(int(w.get("on", 0) or 0) / 60, 1),
                "off_min": round(int(w.get("off", 0) or 0) / 60, 1),
            }
            for w in _active_windows(raw)
        ]
    else:
        summary["start_times"] = [minutes_to_hhmm(t) for t in _active_start_times(raw)]
        summary["durations"] = {
            str(slot + 1): round(int(seconds or 0) / 60, 1)
            for slot, seconds in enumerate(raw.get("stationsDuration") or [])
            if int(seconds or 0) > 0
        }
    return summary
