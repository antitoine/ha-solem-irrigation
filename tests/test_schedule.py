"""Tests for the pure schedule logic in schedule.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.solem_irrigation import schedule
from custom_components.solem_irrigation.schedule import (
    ScheduleError,
    apply_schedule_changes,
    hhmm_to_minutes,
    is_parked,
    mask_to_weekdays,
    minutes_to_hhmm,
    next_run,
    park_program,
    program_is_enabled,
    program_occurrences,
    program_runs_on,
    program_summary,
    restore_program,
    weekdays_to_mask,
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


def test_program_is_enabled_needs_runnable_content():
    # An untouched factory slot, as stored by MySOLEM on an LR-IS: all days,
    # no start time, every duration 0. It can never fire.
    factory = classic_program(
        name="Programme C",
        index=2,
        cycle=schedule.CYCLE_WEEKLY,
        weekDays=127,
        waterBudget=100,
        startTimes=[-1] * 8,
        stationsDuration=[0] * 12,
    )
    assert program_is_enabled(factory) is False
    summary = program_summary(factory, agricultural=False)
    assert summary["enabled"] is False
    assert summary["start_times"] == []
    assert summary["durations"] == {}
    # A start time without any duration, or a duration without a start,
    # is just as empty.
    assert not program_is_enabled(classic_program(stationsDuration=[0] * 12))
    assert not program_is_enabled(classic_program(startTimes=[-1] * 8))
    # A zero water budget never waters.
    assert not program_is_enabled(classic_program(waterBudget=0))
    # LR-AG: a window is the runnable content.
    assert program_is_enabled(ag_program())
    empty_windows = [{"start": -1, "end": -1, "on": 0, "off": 0}] * 4
    assert not program_is_enabled(ag_program(windows=empty_windows))
    assert (
        program_summary(ag_program(windows=empty_windows), agricultural=True)["enabled"]
        is False
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


# -- park / restore ----------------------------------------------------------------


def test_park_and_restore_round_trip():
    program = classic_program(
        cycle=schedule.CYCLE_INTERVAL, numberOfDays=3, synchroDay=1
    )
    parked, snapshot = park_program(program)
    assert parked["cycle"] == schedule.CYCLE_WEEKLY
    assert parked["weekDays"] == 0
    assert not program_is_enabled(parked)
    # Everything else untouched.
    assert parked["startTimes"] == program["startTimes"]

    restored = restore_program(parked, snapshot)
    for field_name in ("cycle", "weekDays", "numberOfDays", "synchroDay"):
        assert restored[field_name] == program[field_name]
    assert program_is_enabled(restored)


def test_restore_without_snapshot_falls_back_to_every_day():
    parked, _ = park_program(classic_program())
    restored = restore_program(parked, None)
    assert restored["cycle"] == schedule.CYCLE_WEEKLY
    assert restored["weekDays"] == 127


def test_restore_with_parked_snapshot_falls_back():
    # Snapshot captured while already parked must not restore "never runs".
    parked, _ = park_program(classic_program(weekDays=0))
    restored = restore_program(parked, {"cycle": 0, "weekDays": 0})
    assert restored["weekDays"] == 127


def test_is_parked():
    parked, _ = park_program(classic_program())
    assert is_parked(parked) is True
    assert is_parked(classic_program()) is False  # weekly with days set
    assert is_parked({"cycle": 4, "weekDays": 0}) is False  # interval runs
    assert is_parked({"cycle": "junk", "weekDays": 0}) is False  # garbage


# -- apply_schedule_changes ------------------------------------------------------


def test_apply_changes_untouched_fields_are_identical():
    program = classic_program()
    updated = apply_schedule_changes(
        program, {"water_budget": 80}, agricultural=False, now_utc=NOW_UTC
    )
    assert updated["waterBudget"] == 80
    for key in program:
        if key != "waterBudget":
            assert updated[key] == program[key], key
    # Input never mutated.
    assert program["waterBudget"] == 100


def test_apply_changes_weekdays():
    updated = apply_schedule_changes(
        classic_program(),
        {"weekdays": ["mon", "wed", "fri"]},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["weekDays"] == 1 + 4 + 16


def test_apply_changes_weekdays_requires_weekly_cycle():
    program = classic_program(cycle=schedule.CYCLE_INTERVAL)
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            program, {"weekdays": ["mon"]}, agricultural=False, now_utc=NOW_UTC
        )


def test_apply_changes_empty_weekdays_rejected():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(), {"weekdays": []}, agricultural=False, now_utc=NOW_UTC
        )


def test_apply_changes_switch_to_weekly_requires_days_when_mask_empty():
    program = classic_program(cycle=schedule.CYCLE_INTERVAL, weekDays=0)
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            program,
            {"schedule_type": "weekly"},
            agricultural=False,
            now_utc=NOW_UTC,
        )
    updated = apply_schedule_changes(
        program,
        {"schedule_type": "weekly", "weekdays": ["sat", "sun"]},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["cycle"] == schedule.CYCLE_WEEKLY
    assert updated["weekDays"] == 32 + 64


def test_apply_changes_interval():
    updated = apply_schedule_changes(
        classic_program(),
        {"schedule_type": "interval", "every_days": 3, "offset_days": 1},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["cycle"] == schedule.CYCLE_INTERVAL
    assert updated["numberOfDays"] == 3
    assert updated["synchroDay"] == 1
    # Anchor = now_utc + offset, serialised like the portal does.
    assert updated["periodicStartDate"] == "2026-07-08T12:00:00.000Z"


def test_apply_changes_interval_validation():
    for changes in (
        {"schedule_type": "interval", "every_days": 0},
        {"schedule_type": "interval", "every_days": 32},
        {"schedule_type": "interval", "every_days": 3, "offset_days": 3},
        {"schedule_type": "interval", "every_days": 3, "offset_days": -1},
    ):
        with pytest.raises(ScheduleError):
            apply_schedule_changes(
                classic_program(), changes, agricultural=False, now_utc=NOW_UTC
            )


def test_apply_changes_every_days_requires_interval_cycle():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(), {"every_days": 3}, agricultural=False, now_utc=NOW_UTC
        )
    # But adjusting an already-interval program works without schedule_type.
    program = classic_program(cycle=schedule.CYCLE_INTERVAL)
    updated = apply_schedule_changes(
        program, {"every_days": 5}, agricultural=False, now_utc=NOW_UTC
    )
    assert updated["numberOfDays"] == 5


def test_apply_changes_start_times():
    updated = apply_schedule_changes(
        classic_program(),
        {"start_times": ["20:00", "06:00", "06:00"]},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["startTimes"] == [360, 1200, -1, -1, -1, -1, -1, -1]
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(),
            {"start_times": [f"0{h}:00" for h in range(9)]},
            agricultural=False,
            now_utc=NOW_UTC,
        )


def test_apply_changes_durations():
    updated = apply_schedule_changes(
        classic_program(),
        {"durations": {"1": 30, "4": 2.5}},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["stationsDuration"][0] == 1800
    assert updated["stationsDuration"][3] == 150
    assert len(updated["stationsDuration"]) == 12
    for changes in (
        {"durations": {"0": 5}},
        {"durations": {"13": 5}},
        {"durations": {"x": 5}},
        {"durations": {"1": 721}},
    ):
        with pytest.raises(ScheduleError):
            apply_schedule_changes(
                classic_program(), changes, agricultural=False, now_utc=NOW_UTC
            )


def test_apply_changes_family_mismatch():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(),
            {"windows": [{"start": "06:00", "end": "08:00"}]},
            agricultural=False,
            now_utc=NOW_UTC,
        )
    for changes in (
        {"start_times": ["06:00"]},
        {"durations": {"1": 5}},
    ):
        with pytest.raises(ScheduleError):
            apply_schedule_changes(
                ag_program(), changes, agricultural=True, now_utc=NOW_UTC
            )


def test_apply_changes_windows_constant_watering():
    updated = apply_schedule_changes(
        ag_program(),
        {"windows": [{"start": "06:00", "end": "08:00"}]},
        agricultural=True,
        now_utc=NOW_UTC,
    )
    assert updated["windows"][0] == {"start": 360, "end": 480, "on": 7200, "off": 0}
    # Padded to exactly 4 windows.
    assert len(updated["windows"]) == 4
    assert updated["windows"][1] == {"start": -1, "end": -1, "on": 0, "off": 0}


def test_apply_changes_windows_soak_cycle():
    updated = apply_schedule_changes(
        ag_program(),
        {
            "windows": [
                {
                    "start": "06:00",
                    "end": "08:00",
                    "soak_on_minutes": 10,
                    "soak_off_minutes": 20,
                }
            ]
        },
        agricultural=True,
        now_utc=NOW_UTC,
    )
    assert updated["windows"][0] == {"start": 360, "end": 480, "on": 600, "off": 1200}


def test_apply_changes_windows_midnight_crossing():
    updated = apply_schedule_changes(
        ag_program(),
        {"windows": [{"start": "23:00", "end": "02:00"}]},
        agricultural=True,
        now_utc=NOW_UTC,
    )
    # 3 h across midnight, constant watering.
    assert updated["windows"][0] == {"start": 1380, "end": 120, "on": 10800, "off": 0}


def test_apply_changes_window_ending_where_it_starts_is_refused():
    """Not a 24 h window: an end equal to the start is a typo."""
    with pytest.raises(ScheduleError, match="must end after it starts"):
        apply_schedule_changes(
            ag_program(),
            {"windows": [{"start": "06:00", "end": "06:00"}]},
            agricultural=True,
            now_utc=NOW_UTC,
        )


def test_apply_changes_windows_validation():
    with pytest.raises(ScheduleError):  # too many
        apply_schedule_changes(
            ag_program(),
            {"windows": [{"start": "06:00", "end": "07:00"}] * 5},
            agricultural=True,
            now_utc=NOW_UTC,
        )
    with pytest.raises(ScheduleError):  # soak longer than window
        apply_schedule_changes(
            ag_program(),
            {
                "windows": [
                    {
                        "start": "06:00",
                        "end": "06:30",
                        "soak_on_minutes": 20,
                        "soak_off_minutes": 20,
                    }
                ]
            },
            agricultural=True,
            now_utc=NOW_UTC,
        )


def test_apply_changes_windows_sorted():
    updated = apply_schedule_changes(
        ag_program(),
        {
            "windows": [
                {"start": "18:00", "end": "19:00"},
                {"start": "06:00", "end": "07:00"},
            ]
        },
        agricultural=True,
        now_utc=NOW_UTC,
    )
    assert updated["windows"][0]["start"] == 360
    assert updated["windows"][1]["start"] == 1080


def test_apply_changes_unknown_schedule_type():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(),
            {"schedule_type": "fortnightly"},
            agricultural=False,
            now_utc=NOW_UTC,
        )


def test_apply_changes_water_budget_range():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            classic_program(),
            {"water_budget": 500},
            agricultural=False,
            now_utc=NOW_UTC,
        )


# -- write-side additions ----------------------------------------------------------


def test_weekdays_to_mask():
    assert weekdays_to_mask(["mon", "SUN"]) == 1 + 64
    assert weekdays_to_mask([]) == 0
    with pytest.raises(ScheduleError):
        weekdays_to_mask(["funday"])


@pytest.mark.parametrize("text", ["06:00", "6:00", "06:00:00", " 23:59 "])
def test_hhmm_to_minutes_accepts(text):
    assert hhmm_to_minutes(text) in (360, 1439)


@pytest.mark.parametrize("text", ["24:00", "06:60", "6", "x:y", "", None])
def test_hhmm_to_minutes_rejects(text):
    with pytest.raises(ScheduleError):
        hhmm_to_minutes(text)


def test_is_parked_is_the_portals_convention_not_merely_disabled():
    """A program with no start time cannot run, but it is not parked."""
    idle = classic_program(startTimes=[-1] * 8)
    assert program_is_enabled(idle) is False
    assert is_parked(idle) is False
    assert is_parked(classic_program(weekDays=128)) is True  # only a noise bit
    assert is_parked({"weekDays": 0}) is True  # cycle defaults to weekly
    assert is_parked({"cycle": 0, "weekDays": "x"}) is False


def test_park_and_restore_leave_every_other_field_identical():
    program = classic_program(unknownField={"nested": [1, 2]}, dataChanged=True)
    parked, snapshot = park_program(program)
    restored = restore_program(parked, snapshot)
    assert restored == program
    assert program["weekDays"] == 127  # the input is never mutated


def test_restore_keeps_a_field_the_snapshot_lacks():
    parked, _ = park_program(classic_program(cycle=schedule.CYCLE_INTERVAL))
    restored = restore_program(
        parked, {"cycle": schedule.CYCLE_INTERVAL, "periodicStartDate": None}
    )
    assert restored["cycle"] == schedule.CYCLE_INTERVAL
    assert restored["periodicStartDate"] == parked["periodicStartDate"]


def test_apply_changes_durations_leave_the_other_slots_as_they_were():
    """Only the named stations change: the wire's own values round-trip."""
    program = classic_program(stationsDuration=[7200, None, 30])
    updated = apply_schedule_changes(
        program, {"durations": {"2": 1}}, agricultural=False, now_utc=NOW_UTC
    )
    assert updated["stationsDuration"] == [7200, 60, 30]
    updated = apply_schedule_changes(
        program, {"durations": {"5": 1}}, agricultural=False, now_utc=NOW_UTC
    )
    assert updated["stationsDuration"] == [7200, None, 30, 0, 60]


def test_apply_changes_interval_anchor_is_utc_whatever_now_zone():
    updated = apply_schedule_changes(
        classic_program(),
        {"schedule_type": "interval", "every_days": 2},
        agricultural=False,
        now_utc=NOW_UTC.astimezone(TZ),
    )
    assert updated["periodicStartDate"] == "2026-07-07T12:00:00.000Z"
    assert updated["synchroDay"] == 0


def test_apply_changes_switch_to_an_even_days_cycle_keeps_the_day_mask():
    updated = apply_schedule_changes(
        classic_program(),
        {"schedule_type": "even_days"},
        agricultural=False,
        now_utc=NOW_UTC,
    )
    assert updated["cycle"] == schedule.CYCLE_EVEN_DAYS
    assert updated["weekDays"] == 127


def test_apply_changes_window_without_bounds_is_refused():
    with pytest.raises(ScheduleError):
        apply_schedule_changes(
            ag_program(),
            {"windows": [{"start": "06:00"}]},
            agricultural=True,
            now_utc=NOW_UTC,
        )
    with pytest.raises(ScheduleError):  # soaking needs a watering phase
        apply_schedule_changes(
            ag_program(),
            {"windows": [{"start": "06:00", "end": "07:00", "soak_off_minutes": 5}]},
            agricultural=True,
            now_utc=NOW_UTC,
        )


def test_apply_changes_an_edited_program_still_decodes():
    """What the write side produces, the read side reads back."""
    updated = apply_schedule_changes(
        classic_program(),
        {
            "schedule_type": "weekly",
            "weekdays": ["tue"],
            "start_times": ["05:30"],
            "durations": {"1": 15},
        },
        agricultural=False,
        now_utc=NOW_UTC,
    )
    summary = program_summary(updated, agricultural=False)
    assert summary["days"] == ["tue"]
    assert summary["start_times"] == ["05:30"]
    assert summary["durations"] == {"1": 15.0}
    assert summary["enabled"] is True
