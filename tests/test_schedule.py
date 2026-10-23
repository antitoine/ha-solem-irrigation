"""Tests for the pure schedule logic in schedule.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.solem_irrigation import schedule
from custom_components.solem_irrigation.schedule import (
    ScheduleError,
    mask_to_weekdays,
    minutes_to_hhmm,
    next_run,
    program_is_enabled,
    program_occurrences,
    program_runs_on,
    program_summary,
)

TZ = ZoneInfo("Europe/Rome")
NOW_UTC = datetime(2026, 7, 7, 12, 0, tzinfo=ZoneInfo("UTC"))


def classic_program(**overrides) -> dict:
    """An LR-IP style program (from real module dumps)."""
    raw = {
        "id": "prog-ip-1",
        "module": "mod-ip",
        "index": 2,
        "name": "2h_tempo",
        "cycle": schedule.CYCLE_WEEKLY,
        "weekDays": 127,
        "numberOfDays": 1,
        "synchroDay": 0,
        "periodicStartDate": "2026-07-02T22:00:00.000Z",
        "waterBudget": 100,
        "timeBetweenStations": 0,
        "startTimes": [680, -1, -1, -1, -1, -1, -1, -1],  # 11:20
        "startTimesCycles": [0] * 8,
        "stationsDuration": [7200] + [0] * 11,  # 2 h on station 1
        "programCharacteristics": {"programType": 1},
        "mistingTimeOn": 5,
        "mistingTimeOff": 30,
        "template": False,
    }
    raw.update(overrides)
    return raw


def ag_program(**overrides) -> dict:
    """An LR-AG style program (windows, paired 1:1 with its station)."""
    raw = {
        "id": "prog-ag-1",
        "module": "mod-ag",
        "index": 0,
        "name": "Pinot",
        "cycle": schedule.CYCLE_INTERVAL,
        "weekDays": 127,
        "numberOfDays": 1,
        "synchroDay": 0,
        "periodicStartDate": "2026-07-02T22:00:00.000Z",
        "waterBudget": 100,
        "programmingType": "temporal",
        "windows": [
            {"start": 720, "end": 960, "on": 14400, "off": 0},  # 12:00-16:00
            {"start": -1, "end": -1, "on": 0, "off": 0},
            {"start": -1, "end": -1, "on": 0, "off": 0},
            {"start": -1, "end": -1, "on": 0, "off": 0},
        ],
        "startTimes": [-1] * 8,
        "stationsDuration": [0] * 12,
        "programCharacteristics": {"programType": 3},
        "template": False,
    }
    raw.update(overrides)
    return raw


STATIONS_IP = [(1, "casa")]
STATIONS_AG = [(1, "Pinot"), (2, "Merlot"), (3, "Chardonnay")]


# -- conversions ------------------------------------------------------------


def test_mask_to_weekdays():
    assert mask_to_weekdays(1) == ["mon"]
    assert mask_to_weekdays(64) == ["sun"]
    assert mask_to_weekdays(127) == list(schedule.WEEKDAY_KEYS)
    assert mask_to_weekdays(0) == []
    # 62 = Tue..Sat, 65 = Mon+Sun (values seen in the portal JS analysis)
    assert mask_to_weekdays(62) == ["tue", "wed", "thu", "fri", "sat"]
    assert mask_to_weekdays(65) == ["mon", "sun"]


def test_time_conversions():
    assert minutes_to_hhmm(0) == "00:00"
    assert minutes_to_hhmm(680) == "11:20"
    assert minutes_to_hhmm(1439) == "23:59"
    assert minutes_to_hhmm(1440 + 30) == "00:30"  # wraps past midnight


# -- predicates ---------------------------------------------------------------


def test_program_is_enabled_weekly_mask_convention():
    assert program_is_enabled(classic_program(weekDays=127))
    assert not program_is_enabled(classic_program(weekDays=0))
    # Non-weekly cycles are always considered enabled.
    assert program_is_enabled(
        classic_program(cycle=schedule.CYCLE_INTERVAL, weekDays=0)
    )


def test_program_runs_on_weekly():
    # Monday-only mask (bit 0). 2026-07-06 is a Monday.
    program = classic_program(weekDays=1)
    assert program_runs_on(program, date(2026, 7, 6), tz=TZ)
    assert not program_runs_on(program, date(2026, 7, 7), tz=TZ)
    # Sunday-only mask (bit 6). 2026-07-12 is a Sunday.
    program = classic_program(weekDays=64)
    assert program_runs_on(program, date(2026, 7, 12), tz=TZ)
    assert not program_runs_on(program, date(2026, 7, 11), tz=TZ)


def test_program_runs_on_even_odd_cycles():
    even = classic_program(cycle=schedule.CYCLE_EVEN_DAYS)
    odd = classic_program(cycle=schedule.CYCLE_ODD_DAYS)
    odd31 = classic_program(cycle=schedule.CYCLE_ODD_DAYS_NOT_31)
    assert program_runs_on(even, date(2026, 7, 8), tz=TZ)
    assert not program_runs_on(even, date(2026, 7, 7), tz=TZ)
    assert program_runs_on(odd, date(2026, 7, 7), tz=TZ)
    assert not program_runs_on(odd, date(2026, 7, 8), tz=TZ)
    assert program_runs_on(odd31, date(2026, 7, 29), tz=TZ)
    assert not program_runs_on(odd31, date(2026, 7, 31), tz=TZ)


def test_program_runs_on_interval():
    # Anchor 2026-07-02T22:00Z = 2026-07-03 in Europe/Rome (UTC+2).
    program = classic_program(
        cycle=schedule.CYCLE_INTERVAL,
        numberOfDays=3,
        periodicStartDate="2026-07-02T22:00:00.000Z",
    )
    assert program_runs_on(program, date(2026, 7, 3), tz=TZ)
    assert not program_runs_on(program, date(2026, 7, 4), tz=TZ)
    assert not program_runs_on(program, date(2026, 7, 5), tz=TZ)
    assert program_runs_on(program, date(2026, 7, 6), tz=TZ)
    # Before the anchor: never.
    assert not program_runs_on(program, date(2026, 7, 2), tz=TZ)
    # Month boundary: 2026-07-03 + 3n reaches 2026-08-02.
    assert program_runs_on(program, date(2026, 8, 2), tz=TZ)


def test_program_runs_on_interval_without_anchor():
    program = classic_program(cycle=schedule.CYCLE_INTERVAL, periodicStartDate=None)
    assert not program_runs_on(program, date(2026, 7, 7), tz=TZ)


def test_parked_program_never_runs():
    program = classic_program(weekDays=0)
    for offset in range(7):
        assert not program_runs_on(program, date(2026, 7, 6 + offset), tz=TZ)


# -- occurrences ----------------------------------------------------------------


def window(day: int, month: int = 7, year: int = 2026):
    start = datetime(year, month, day, 0, 0, tzinfo=TZ)
    return start, start + timedelta(days=1)


def test_classic_occurrences_single_station():
    start, end = window(7)  # Tuesday, weekDays=127
    occ = program_occurrences(
        classic_program(), STATIONS_IP, start, end, agricultural=False
    )
    assert len(occ) == 1
    assert occ[0]["program"] == "2h_tempo"
    assert occ[0]["station"] == "casa"
    assert occ[0]["station_index"] == 1
    assert occ[0]["start"] == "2026-07-07T11:20:00+02:00"
    assert occ[0]["end"] == "2026-07-07T13:20:00+02:00"


def test_classic_occurrences_sequential_stations_and_pause():
    program = classic_program(
        startTimes=[360, -1, -1, -1, -1, -1, -1, -1],  # 06:00
        stationsDuration=[600, 1200, 0, 300] + [0] * 8,
        timeBetweenStations=60,
    )
    stations = [(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4")]
    start, end = window(7)
    occ = program_occurrences(program, stations, start, end, agricultural=False)
    # Station 3 has no duration -> skipped; the rest run back to back + pause.
    assert [(o["station"], o["start"], o["end"]) for o in occ] == [
        ("S1", "2026-07-07T06:00:00+02:00", "2026-07-07T06:10:00+02:00"),
        ("S2", "2026-07-07T06:11:00+02:00", "2026-07-07T06:31:00+02:00"),
        ("S4", "2026-07-07T06:32:00+02:00", "2026-07-07T06:37:00+02:00"),
    ]


def test_classic_occurrences_water_budget_scaling():
    program = classic_program(waterBudget=50)
    start, end = window(7)
    occ = program_occurrences(program, STATIONS_IP, start, end, agricultural=False)
    assert occ[0]["end"] == "2026-07-07T12:20:00+02:00"  # 2 h scaled to 1 h


def test_classic_occurrences_multiple_start_times():
    program = classic_program(startTimes=[360, 1200, -1, -1, -1, -1, -1, -1])
    start, end = window(7)
    occ = program_occurrences(program, STATIONS_IP, start, end, agricultural=False)
    assert len(occ) == 2
    assert occ[0]["start"] == "2026-07-07T06:00:00+02:00"
    assert occ[1]["start"] == "2026-07-07T20:00:00+02:00"


def test_ag_occurrences_paired_station_and_daily_interval():
    start, end = window(7)
    occ = program_occurrences(ag_program(), STATIONS_AG, start, end, agricultural=True)
    assert len(occ) == 1
    assert occ[0]["station"] == "Pinot"
    assert occ[0]["start"] == "2026-07-07T12:00:00+02:00"
    assert occ[0]["end"] == "2026-07-07T16:00:00+02:00"


def test_ag_occurrences_midnight_crossing_window():
    program = ag_program(
        windows=[
            {"start": 1380, "end": 120, "on": 0, "off": 0},  # 23:00 -> 02:00
            {"start": -1, "end": -1, "on": 0, "off": 0},
            {"start": -1, "end": -1, "on": 0, "off": 0},
            {"start": -1, "end": -1, "on": 0, "off": 0},
        ]
    )
    start, end = window(7)
    occ = program_occurrences(program, STATIONS_AG, start, end, agricultural=True)
    # The 23:00 window of the 7th plus the tail of the 6th's window.
    tail = [o for o in occ if o["start"].startswith("2026-07-06")]
    main = [o for o in occ if o["start"].startswith("2026-07-07T23")]
    assert tail and tail[0]["end"] == "2026-07-07T02:00:00+02:00"
    assert main and main[0]["end"] == "2026-07-08T02:00:00+02:00"


def test_ag_occurrences_unpaired_program_is_empty():
    program = ag_program(index=5)  # station 6 not in the provided list
    start, end = window(7)
    assert (
        program_occurrences(program, STATIONS_AG, start, end, agricultural=True) == []
    )


def test_parked_program_yields_no_occurrences():
    program = classic_program(cycle=schedule.CYCLE_WEEKLY, weekDays=0)
    start, end = window(7)
    assert (
        program_occurrences(program, STATIONS_IP, start, end, agricultural=False) == []
    )


def test_occurrences_on_dst_transition_day():
    # EU DST starts 2026-03-29 (02:00 -> 03:00 in Europe/Rome).
    program = classic_program(startTimes=[240, -1, -1, -1, -1, -1, -1, -1])  # 04:00
    start, end = window(29, month=3)
    occ = program_occurrences(program, STATIONS_IP, start, end, agricultural=False)
    assert len(occ) == 1
    assert occ[0]["start"].startswith("2026-03-29T04:00:00")


def test_occurrences_require_aware_datetimes():
    with pytest.raises(ScheduleError):
        program_occurrences(
            classic_program(),
            STATIONS_IP,
            datetime(2026, 7, 7),
            datetime(2026, 7, 8),
            agricultural=False,
        )


def test_next_run():
    now = datetime(2026, 7, 7, 12, 0, tzinfo=TZ)  # after the 11:20 start
    run = next_run(classic_program(), STATIONS_IP, now, agricultural=False)
    assert run == datetime(2026, 7, 8, 11, 20, tzinfo=TZ)
    assert (
        next_run(classic_program(weekDays=0), STATIONS_IP, now, agricultural=False)
        is None
    )


# -- summaries -------------------------------------------------------------------


def test_program_summary_classic():
    summary = program_summary(classic_program(), agricultural=False)
    assert summary["name"] == "2h_tempo"
    assert summary["enabled"] is True
    assert summary["type"] == "weekly"
    assert summary["days"] == list(schedule.WEEKDAY_KEYS)
    assert summary["start_times"] == ["11:20"]
    assert summary["durations"] == {"1": 120.0}
    assert summary["water_budget"] == 100


def test_program_summary_interval_ag():
    summary = program_summary(ag_program(), agricultural=True)
    assert summary["type"] == "interval"
    assert summary["every_days"] == 1
    assert summary["offset_days"] == 0
    assert summary["windows"] == [
        {"start": "12:00", "end": "16:00", "on_min": 240.0, "off_min": 0.0}
    ]


# -- review fixes ---------------------------------------------------------------


def test_weekday_mask_ignores_bits_above_seven_and_negatives():
    parked = classic_program(weekDays=128)
    assert program_is_enabled(parked) is False
    assert program_summary(parked, agricultural=False)["days"] == []
    assert program_summary(parked, agricultural=False)["enabled"] is False
    assert not program_runs_on(parked, date(2026, 7, 6), tz=TZ)
    assert mask_to_weekdays(128) == []
    negative = classic_program(weekDays=-1)
    assert program_is_enabled(negative) is False
    assert program_summary(negative, agricultural=False)["days"] == []


@pytest.mark.parametrize("bad", [None, True, "x", 1.5])
def test_window_with_unusable_bounds_is_ignored(bad):
    windows = [
        {"start": bad, "end": 60, "on": 60, "off": 0},
        {"start": 720, "end": 960, "on": 14400, "off": 0},
    ]
    raw = ag_program(windows=windows)
    summary = program_summary(raw, agricultural=True)
    assert [w["start"] for w in summary["windows"]] == ["12:00"]
    start = datetime(2026, 7, 7, 0, 0, tzinfo=TZ)
    occurrences = program_occurrences(
        raw, STATIONS_AG, start, start + timedelta(days=1), agricultural=True
    )
    assert len(occurrences) == 1


def test_negative_window_bounds_are_ignored():
    raw = ag_program(windows=[{"start": -5, "end": 60, "on": 60, "off": 0}])
    assert program_summary(raw, agricultural=True)["windows"] == []


def test_zero_water_budget_stays_zero():
    raw = classic_program(waterBudget=0)
    assert program_summary(raw, agricultural=False)["water_budget"] == 0
    start = datetime(2026, 7, 7, 0, 0, tzinfo=TZ)
    assert (
        program_occurrences(
            raw, STATIONS_IP, start, start + timedelta(days=1), agricultural=False
        )
        == []
    )
    absent = classic_program()
    del absent["waterBudget"]
    assert program_summary(absent, agricultural=False)["water_budget"] == 100


def test_naive_periodic_start_date_is_read_as_utc():
    raw = classic_program(
        cycle=schedule.CYCLE_INTERVAL,
        numberOfDays=2,
        periodicStartDate="2026-08-01T22:00:00",
    )
    # 22:00 UTC on 1 Aug is already 2 Aug in Rome: anchor day is 2026-08-02.
    assert program_runs_on(raw, date(2026, 8, 2), tz=TZ)
    assert not program_runs_on(raw, date(2026, 8, 3), tz=TZ)
    assert program_runs_on(raw, date(2026, 8, 4), tz=TZ)


def test_classic_run_lasts_its_real_duration_across_the_dst_change():
    raw = classic_program(
        startTimes=[150, -1, -1, -1, -1, -1, -1, -1],  # 02:30
        stationsDuration=[1800] + [0] * 11,
    )
    start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    [run] = program_occurrences(
        raw, STATIONS_IP, start, start + timedelta(days=1), agricultural=False
    )
    began = datetime.fromisoformat(run["start"])
    ended = datetime.fromisoformat(run["end"])
    # 02:30 CEST, then the clock goes back at 03:00 CEST: 30 real minutes later.
    assert ended.astimezone(UTC) - began.astimezone(UTC) == timedelta(minutes=30)
    assert run["end"].endswith("02:00:00+01:00")


def test_occurrences_sort_by_instant_not_text():
    raw = classic_program(
        startTimes=[0, 600, -1, -1, -1, -1, -1, -1],
        stationsDuration=[60, 60] + [0] * 10,
    )
    start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    runs = program_occurrences(
        raw,
        [(1, "a"), (2, "b")],
        start,
        start + timedelta(days=1),
        agricultural=False,
    )
    instants = [datetime.fromisoformat(o["start"]) for o in runs]
    assert instants == sorted(instants)
