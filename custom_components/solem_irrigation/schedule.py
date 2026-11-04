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
  setting ``cycle=0, weekDays=0``, which :func:`program_is_enabled` recognises
  and :func:`park_program` / :func:`restore_program` reproduce.

This module decodes programs into summaries and expands them into concrete
runs, and applies schedule edits to a program record
(:func:`apply_schedule_changes`). All datetime math takes explicit
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
_TYPE_TO_CYCLE = {v: k for k, v in _CYCLE_TO_TYPE.items()}

MINUTES_PER_DAY = 24 * 60
_LAST_MONTH_DAY = 31
_WEEKDAY_MASK = 0x7F  # seven day bits; anything above is noise
_MAX_HOUR = 23
_MAX_MINUTE = 59

# What the wire, or the web app's own inputs, accept when a program is written.
MAX_START_TIMES = 8
MAX_STATION_SLOTS = 12
MAX_WINDOWS = 4
MAX_STATION_MINUTES = 12 * 60
MAX_WATER_BUDGET = 300
MAX_INTERVAL_DAYS = 31
MAX_OFFSET_DAYS = 30

_EMPTY_WINDOW = {"start": -1, "end": -1, "on": 0, "off": 0}

# The fields that make up a program's recurrence: saved when a program is
# parked, so that turning it back on restores the exact schedule.
_RECURRENCE_FIELDS = (
    "cycle",
    "weekDays",
    "numberOfDays",
    "synchroDay",
    "periodicStartDate",
)


class ScheduleError(ValueError):
    """A schedule computation or edit was given input it cannot work with."""


# -- conversions ------------------------------------------------------------


def mask_to_weekdays(mask: int) -> list[str]:
    """Convert the wire bitmask to weekday keys (``mon`` … ``sun``)."""
    mask = max(0, mask) & _WEEKDAY_MASK
    return [key for bit, key in enumerate(WEEKDAY_KEYS) if mask >> bit & 1]


def weekdays_to_mask(days: list[str]) -> int:
    """Convert weekday keys (``mon`` … ``sun``) to the wire bitmask."""
    mask = 0
    for day in days:
        try:
            mask |= 1 << WEEKDAY_KEYS.index(str(day).casefold())
        except ValueError:
            raise ScheduleError(f"Unknown weekday '{day}'") from None
    return mask


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


def hhmm_to_minutes(text: Any) -> int:
    """Parse ``HH:MM`` (or ``HH:MM:SS``) into minutes since midnight."""
    parts = str(text).strip().split(":")
    try:
        hours, mins = int(parts[0]), int(parts[1])
    except IndexError, ValueError:
        raise ScheduleError(f"Invalid time '{text}', expected HH:MM") from None
    if not (0 <= hours <= _MAX_HOUR and 0 <= mins <= _MAX_MINUTE):
        raise ScheduleError(f"Invalid time '{text}', expected HH:MM")
    return hours * 60 + mins


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
    """Can the program fire at all?

    Mirrors the portal's own convention: a weekly program with an empty
    day mask can never fire, and that is exactly how the portal "parks" a
    program (there is no dedicated enabled flag on the wire). A slot that
    was never configured (factory record: all days, no start time, every
    duration 0) cannot fire either, so it is not enabled.
    """
    cycle = int(raw.get("cycle", CYCLE_WEEKLY) or 0)
    if cycle == CYCLE_WEEKLY and not _weekday_mask(raw):
        return False
    return _has_runnable_content(raw)


def _has_runnable_content(raw: dict[str, Any]) -> bool:
    """A positive budget and a window (LR-AG) or a start plus a duration."""
    if _water_budget(raw) <= 0:
        return False
    if _active_windows(raw):
        return True
    return bool(_active_start_times(raw)) and any(
        int(seconds or 0) > 0 for seconds in raw.get("stationsDuration") or []
    )


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


# -- park / restore (the per-program switch) -----------------------------------


def is_parked(raw: dict[str, Any]) -> bool:
    """Is the program parked the way the portal disables one (weekly, no day)?

    Narrower than "not enabled": a program with no start time is not enabled
    either, but it is not parked, and there is no recurrence to restore.
    Unreadable fields read as not parked, so a restore never overwrites data
    that is not understood.
    """
    try:
        cycle = int(raw.get("cycle", CYCLE_WEEKLY) or 0)
        return cycle == CYCLE_WEEKLY and _weekday_mask(raw) == 0
    except TypeError, ValueError:
        return False


def park_program(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Disable a program as the portal does; return ``(parked, snapshot)``.

    The snapshot holds the whole recurrence, so :func:`restore_program` can
    bring the exact schedule back. Every other field is left as it is.
    """
    snapshot = {name: raw.get(name) for name in _RECURRENCE_FIELDS}
    parked = dict(raw)
    parked["cycle"] = CYCLE_WEEKLY
    parked["weekDays"] = 0
    return parked, snapshot


def restore_program(
    raw: dict[str, Any], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    """Re-enable a parked program from its snapshot, else for every day.

    A snapshot that is itself parked (or missing) cannot bring a schedule back,
    so the program then runs every day of the week.
    """
    restored = dict(raw)
    if snapshot and not is_parked(snapshot):
        for name in _RECURRENCE_FIELDS:
            if snapshot.get(name) is not None:
                restored[name] = snapshot[name]
    else:
        restored["cycle"] = CYCLE_WEEKLY
        restored["weekDays"] = _WEEKDAY_MASK
    return restored


# -- schedule editing ------------------------------------------------------------


def apply_schedule_changes(
    raw: dict[str, Any],
    changes: dict[str, Any],
    *,
    agricultural: bool,
    now_utc: datetime,
) -> dict[str, Any]:
    """Return a copy of ``raw`` with the requested schedule fields replaced.

    ``changes`` uses the service vocabulary: ``schedule_type``, ``weekdays``,
    ``every_days`` / ``offset_days``, ``water_budget``, and ``start_times`` plus
    ``durations`` for a classic controller or ``windows`` for an LR-AG. A field
    not named in ``changes`` is left exactly as it was: the wire replaces whole
    programs, so whatever is not edited must round-trip unchanged. Raises
    :class:`ScheduleError` on a value the controller would not take.
    """
    updated = dict(raw)
    _apply_recurrence_changes(updated, changes, now_utc)

    if "water_budget" in changes:
        budget = int(changes["water_budget"])
        if not 0 <= budget <= MAX_WATER_BUDGET:
            raise ScheduleError(f"water_budget must be 0-{MAX_WATER_BUDGET} %")
        updated["waterBudget"] = budget

    if "start_times" in changes:
        if agricultural:
            raise ScheduleError(
                "'start_times' is for classic controllers; use 'windows'"
            )
        updated["startTimes"] = _build_start_times(changes["start_times"])

    if "durations" in changes:
        if agricultural:
            raise ScheduleError("'durations' is for classic controllers; use 'windows'")
        updated["stationsDuration"] = _build_durations(
            changes["durations"], updated.get("stationsDuration") or []
        )

    if "windows" in changes:
        if not agricultural:
            raise ScheduleError("'windows' is for agricultural controllers (LR-AG)")
        updated["windows"] = _build_windows(changes["windows"])

    return updated


def _apply_recurrence_changes(
    updated: dict[str, Any], changes: dict[str, Any], now_utc: datetime
) -> None:
    """Apply ``schedule_type``, ``weekdays`` and the interval fields."""
    schedule_type = changes.get("schedule_type")
    if schedule_type is not None:
        if schedule_type not in _TYPE_TO_CYCLE:
            raise ScheduleError(f"Unknown schedule_type '{schedule_type}'")
        cycle = _TYPE_TO_CYCLE[schedule_type]
        updated["cycle"] = cycle
        if cycle == CYCLE_INTERVAL:
            _apply_interval(
                updated,
                changes.get("every_days", updated.get("numberOfDays") or 1),
                changes.get("offset_days", 0),
                now_utc,
            )
        elif (
            cycle == CYCLE_WEEKLY
            and "weekdays" not in changes
            and not _weekday_mask(updated)
        ):
            # Weekly needs days: without them the program would end up parked.
            raise ScheduleError("schedule_type 'weekly' requires 'weekdays'")
    elif "every_days" in changes or "offset_days" in changes:
        if int(updated.get("cycle", CYCLE_WEEKLY) or 0) != CYCLE_INTERVAL:
            raise ScheduleError(
                "'every_days'/'offset_days' need schedule_type 'interval'"
            )
        _apply_interval(
            updated,
            changes.get("every_days", updated.get("numberOfDays") or 1),
            changes.get("offset_days", updated.get("synchroDay") or 0),
            now_utc,
        )

    if "weekdays" in changes:
        if int(updated.get("cycle", CYCLE_WEEKLY) or 0) != CYCLE_WEEKLY:
            raise ScheduleError("'weekdays' only applies to schedule_type 'weekly'")
        if not (mask := weekdays_to_mask(list(changes["weekdays"]))):
            raise ScheduleError(
                "'weekdays' must name at least one day (turn the program's "
                "switch off to disable it)"
            )
        updated["weekDays"] = mask


def _apply_interval(
    updated: dict[str, Any], every: Any, offset: Any, now_utc: datetime
) -> None:
    """Set an every-N-days recurrence, anchored as the portal anchors it."""
    every = int(every)
    offset = int(offset)
    if not 1 <= every <= MAX_INTERVAL_DAYS:
        raise ScheduleError(f"every_days must be 1-{MAX_INTERVAL_DAYS}")
    if not 0 <= offset <= MAX_OFFSET_DAYS or offset >= every:
        raise ScheduleError(
            f"offset_days must be 0-{MAX_OFFSET_DAYS} and smaller than every_days"
        )
    updated["numberOfDays"] = every
    updated["synchroDay"] = offset
    anchor = now_utc.astimezone(UTC) + timedelta(days=offset)
    updated["periodicStartDate"] = anchor.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _build_start_times(times: list[Any]) -> list[int]:
    """The wire's eight start-time slots, sorted, -1 for an unused one."""
    if len(times) > MAX_START_TIMES:
        raise ScheduleError(f"At most {MAX_START_TIMES} start times are supported")
    minutes = sorted({hhmm_to_minutes(t) for t in times})
    return minutes + [-1] * (MAX_START_TIMES - len(minutes))


def _build_durations(durations: dict[Any, Any], current: list[Any]) -> list[Any]:
    """``current`` with the named stations' minutes replaced, in seconds.

    The slots that are not named keep their value as is.
    """
    result = list(current)
    for key, minutes in durations.items():
        try:
            slot = int(key)
            seconds = round(float(minutes) * 60)
        except TypeError, ValueError:
            raise ScheduleError(f"Invalid duration for station '{key}'") from None
        if not 1 <= slot <= MAX_STATION_SLOTS:
            raise ScheduleError(
                f"Station index {slot} out of range 1-{MAX_STATION_SLOTS}"
            )
        if not 0 <= seconds <= MAX_STATION_MINUTES * 60:
            raise ScheduleError(
                f"Station duration must be 0-{MAX_STATION_MINUTES} minutes"
            )
        result += [0] * (slot - len(result))
        result[slot - 1] = seconds
    return result


def _build_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An LR-AG's four watering windows, sorted, padded with unused slots."""
    if len(windows) > MAX_WINDOWS:
        raise ScheduleError(f"At most {MAX_WINDOWS} windows are supported")
    built: list[dict[str, Any]] = []
    for window in windows:
        try:
            start = hhmm_to_minutes(window["start"])
            end = hhmm_to_minutes(window["end"])
        except KeyError, TypeError:
            raise ScheduleError("Each window needs a 'start' and an 'end'") from None
        if end == start:
            raise ScheduleError("a window must end after it starts (or cross midnight)")
        duration = end - start
        if duration < 0:  # crosses midnight
            duration += MINUTES_PER_DAY
        soak_on = window.get("soak_on_minutes")
        soak_off = window.get("soak_off_minutes")
        if soak_on or soak_off:
            on_s = round(float(soak_on or 0) * 60)
            off_s = round(float(soak_off or 0) * 60)
            if on_s <= 0 or off_s < 0:
                raise ScheduleError("soak_on_minutes must be > 0 when soaking")
            if on_s + off_s > duration * 60:
                raise ScheduleError(
                    "Soak cycle (on + off) cannot exceed the window duration"
                )
        else:
            # Constant watering: the portal stores it as one cycle filling the
            # whole window.
            on_s = duration * 60
            off_s = 0
        built.append({"start": start, "end": end, "on": on_s, "off": off_s})
    built.sort(key=lambda w: w["start"])
    built += [dict(_EMPTY_WINDOW) for _ in range(MAX_WINDOWS - len(built))]
    return built
