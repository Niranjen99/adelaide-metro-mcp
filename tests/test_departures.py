"""Tests for next_departures, active_services and timetable_info (db.py).

Fixture reminder (tests/fixtures/mini_gtfs/README.md):
  WK = Mon-Fri, WE = Sat-Sun. 2026-12-25 (Fri) swaps WK for WE.
  S1: T5 07:30, T1 08:00, T2 09:00 (WK); T3 10:00 (WE); T4 24:50 (WK, tram)
  S2: T5 07:40 drop-off only; T1 08:10; T2 09:10
  S3: last stop of every trip, so never a departure.
Dates used: Mon 2026-11-09, Fri 2026-11-13, Sat 2026-11-14.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from adelaide_metro_mcp import db
from adelaide_metro_mcp.db import ADELAIDE_TZ


def at(iso: str) -> datetime:
    """An Adelaide local time, e.g. at('2026-11-09T07:00')."""
    return datetime.fromisoformat(iso).replace(tzinfo=ADELAIDE_TZ)


def trips(conn, stop_id, now, **kwargs):
    _, deps = db.next_departures(conn, stop_id, now, **kwargs)
    return [d.trip_id for d in deps]


# --- active_services ------------------------------------------------------------

@pytest.mark.parametrize(
    "day, expected",
    [
        (date(2026, 11, 9), {"WK"}),   # Monday
        (date(2026, 11, 14), {"WE"}),  # Saturday
        (date(2026, 12, 25), {"WE"}),  # Christmas (Friday): WK removed, WE added
        (date(2028, 1, 3), set()),     # after the calendar end date
    ],
)
def test_active_services(conn, day, expected):
    assert db.active_services(conn, day) == expected


# --- next_departures: which trips ---------------------------------------------------

def test_weekday_departures_in_time_order(conn):
    # Window 07:00-09:00 is inclusive, so the 09:00 trip is included.
    assert trips(conn, "S1", at("2026-11-09T07:00")) == ["T5", "T1", "T2"]


def test_weekend_uses_weekend_service(conn):
    assert trips(conn, "S1", at("2026-11-14T09:00")) == ["T3"]


def test_public_holiday_exception(conn):
    # Christmas is a Friday, but calendar_dates switches it to the weekend timetable.
    assert trips(conn, "S1", at("2026-12-25T07:00"), minutes_ahead=240) == ["T3"]


def test_departure_already_gone_is_not_listed(conn):
    assert trips(conn, "S1", at("2026-11-09T08:01")) == ["T2"]


def test_after_midnight_finds_previous_days_late_trip(conn):
    # Saturday 00:30: T4 is Friday's trip at 24:50, i.e. Saturday 00:50.
    _, deps = db.next_departures(conn, "S1", at("2026-11-14T00:30"))
    assert [d.trip_id for d in deps] == ["T4"]
    assert deps[0].departs_at == at("2026-11-14T00:50")


def test_window_crossing_midnight_finds_late_trip(conn):
    # Friday 23:30 + 2 h reaches past midnight.
    assert trips(conn, "S1", at("2026-11-13T23:30")) == ["T4"]


def test_late_trip_needs_its_service_to_run_the_day_before(conn):
    # Monday 00:30: Sunday has no WK service, so there is no T4.
    assert trips(conn, "S1", at("2026-11-09T00:30")) == []


def test_drop_off_only_stop_is_not_a_departure(conn):
    # T5 calls at S2 at 07:40 but pickup_type is 1 (no boarding).
    assert trips(conn, "S2", at("2026-11-09T07:00")) == ["T1"]


def test_last_stop_of_a_trip_is_not_a_departure(conn):
    assert trips(conn, "S3", at("2026-11-09T00:00"), minutes_ahead=db.MAX_MINUTES_AHEAD) == []


def test_limit(conn):
    assert trips(conn, "S1", at("2026-11-09T07:00"), limit=1) == ["T5"]


def test_minutes_ahead_narrows_the_window(conn):
    assert trips(conn, "S1", at("2026-11-09T07:00"), minutes_ahead=30) == ["T5"]


def test_route_filter_by_number_is_case_insensitive(conn):
    full_day = dict(minutes_ahead=db.MAX_MINUTES_AHEAD)
    assert trips(conn, "S1", at("2026-11-13T07:00"), route="t1", **full_day) == ["T4"]
    assert trips(conn, "S1", at("2026-11-13T07:00"), route="100", **full_day) == ["T5", "T1", "T2"]


def test_departure_fields(conn):
    stop, deps = db.next_departures(conn, "S1", at("2026-11-13T23:30"))
    assert stop.stop_name == "Central Station"
    assert deps[0] == db.Departure(
        departs_at=at("2026-11-14T00:50"),
        route_short_name="T1",
        route_long_name="Central Station to Beach (tram)",
        mode="Tram",
        headsign="Beach",
        trip_id="T4",
    )


# --- next_departures: time zones and daylight saving ---------------------------------------

def test_now_in_another_timezone_is_converted_to_adelaide(conn):
    # Sun 21:35 UTC = Mon 08:05 in Adelaide (ACDT, +10:30). Monday's service
    # applies, and T1 (08:00) has already left. Using +9:30 would wrongly give T1.
    now = datetime(2026, 11, 8, 21, 35, tzinfo=timezone.utc)
    _, deps = db.next_departures(conn, "S1", now)
    assert [d.trip_id for d in deps] == ["T2"]
    assert deps[0].departs_at.utcoffset() == timedelta(hours=10, minutes=30)


def test_service_day_start_is_midnight_on_normal_days():
    assert db.service_day_start(date(2026, 11, 9)) == at("2026-11-09T00:00")


def test_service_day_start_on_daylight_saving_start_day():
    # 2026-10-04: clocks go 02:00 -> 03:00. GTFS counts from "noon minus 12 h",
    # which is 23:00 the night before, so a trip at 08:00 is still 08:00 on the clock.
    start = db.service_day_start(date(2026, 10, 4))
    assert (start + timedelta(hours=8)).astimezone(ADELAIDE_TZ) == at("2026-10-04T08:00")


# --- next_departures: validation ---------------------------------------------------

def test_unknown_route_raises_not_found(conn):
    with pytest.raises(db.NotFoundError, match="No route '999'"):
        db.next_departures(conn, "S1", at("2026-11-09T07:00"), route="999")


def test_route_that_never_stops_here_raises_not_found(conn):
    # The tram (T1) runs from S1, but never calls at S2.
    with pytest.raises(db.NotFoundError, match="Route T1 does not stop at Market Street"):
        db.next_departures(conn, "S2", at("2026-11-09T07:00"), route="T1")


@pytest.mark.parametrize("bad", ["1 0 0", "100;", "x" * 21, 100])
def test_bad_route_rejected(conn, bad):
    with pytest.raises(ValueError):
        db.next_departures(conn, "S1", at("2026-11-09T07:00"), route=bad)


def test_blank_route_means_no_filter(conn):
    assert trips(conn, "S1", at("2026-11-09T07:00"), route="  ") == ["T5", "T1", "T2"]


@pytest.mark.parametrize("bad", [0, 1441, True, 1.5])
def test_bad_minutes_ahead_rejected(conn, bad):
    with pytest.raises(ValueError):
        db.next_departures(conn, "S1", at("2026-11-09T07:00"), minutes_ahead=bad)


def test_unknown_stop_raises_not_found(conn):
    with pytest.raises(db.NotFoundError):
        db.next_departures(conn, "NOPE", at("2026-11-09T07:00"))


def test_naive_datetime_is_a_programming_error(conn):
    with pytest.raises(TypeError):
        db.next_departures(conn, "S1", datetime(2026, 11, 9, 7, 0))


# --- timetable_info -------------------------------------------------------------

def test_timetable_info(conn):
    info = db.timetable_info(conn)
    assert info.feed_version == "999"
    assert info.start_date == date(2026, 1, 1)
    assert info.end_date == date(2027, 12, 31)
    assert info.loaded_at_utc  # set by the loader
    assert (info.stop_count, info.route_count, info.trip_count) == (3, 2, 5)
