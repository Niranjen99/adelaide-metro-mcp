"""Tests for trip_stops (db.py): the stops one trip calls at, in order.

Fixture reminder (tests/fixtures/mini_gtfs/README.md):
  T1: S1 08:00, S2 08:10, S3 08:30
  T2: S1 09:00, S2 09:10 (pick up only: drop_off_type 1), S3 09:30
  T3: S1 10:00, S3 10:30 (skips S2)
  T4: tram, S1 24:50, S3 25:10 (after midnight)
  T5: S1 07:30, S2 07:40 (set down only: pickup_type 1), S3 08:00
"""

import pytest

from adelaide_metro_mcp import db


def ids(trip: db.TripStops) -> list[str]:
    return [ts.stop.stop_id for ts in trip.stops]


def test_all_stops_in_order_with_times(conn):
    trip = db.trip_stops(conn, "T1")
    assert ids(trip) == ["S1", "S2", "S3"]
    assert [ts.time_secs for ts in trip.stops] == [8 * 3600, 8 * 3600 + 600, 8 * 3600 + 1800]
    assert (trip.route_short_name, trip.mode, trip.headsign) == ("100", "Bus", "Beach Interchange")
    assert trip.from_stop is None


def test_from_stop_keeps_only_later_stops(conn):
    trip = db.trip_stops(conn, "T1", from_stop_id="S2")
    assert ids(trip) == ["S3"]
    assert trip.from_stop.stop_id == "S2"


def test_from_last_stop_returns_no_stops(conn):
    # S3 is T1's last stop, but you can still be standing there with the trip id.
    assert db.trip_stops(conn, "T1", from_stop_id="S3").stops == []


def test_pickup_and_drop_off_flags(conn):
    t2 = {ts.stop.stop_id: ts for ts in db.trip_stops(conn, "T2").stops}
    assert (t2["S2"].can_board, t2["S2"].can_alight) == (True, False)
    t5 = {ts.stop.stop_id: ts for ts in db.trip_stops(conn, "T5").stops}
    assert (t5["S2"].can_board, t5["S2"].can_alight) == (False, True)


def test_after_midnight_times_are_kept_as_service_day_seconds(conn):
    trip = db.trip_stops(conn, "T4")
    assert [ts.time_secs for ts in trip.stops] == [24 * 3600 + 50 * 60, 25 * 3600 + 10 * 60]
    assert trip.mode == "Tram"


def test_unknown_trip_is_not_found(conn):
    with pytest.raises(db.NotFoundError, match="next_departures"):
        db.trip_stops(conn, "NOPE")


def test_trip_that_skips_the_from_stop_is_an_error(conn):
    with pytest.raises(db.NotFoundError, match="does not pick up"):
        db.trip_stops(conn, "T3", from_stop_id="S2")


def test_from_stop_where_boarding_is_not_allowed_is_an_error(conn):
    # T5 calls at S2 but only to set down, so you cannot be riding it from there.
    with pytest.raises(db.NotFoundError, match="does not pick up"):
        db.trip_stops(conn, "T5", from_stop_id="S2")


def test_parent_station_as_from_stop_is_rejected(conn):
    with pytest.raises(db.NotFoundError, match="station grouping"):
        db.trip_stops(conn, "T1", from_stop_id="P1")


@pytest.mark.parametrize("bad", ["", "   ", "T1; DROP TABLE trips", "x" * 21, None, 5])
def test_bad_trip_id_rejected(conn, bad):
    with pytest.raises(ValueError, match="trip_id"):
        db.trip_stops(conn, bad)


def test_trip_id_is_trimmed(conn):
    assert ids(db.trip_stops(conn, "  T1 ")) == ["S1", "S2", "S3"]


def test_max_stops_cuts_the_list_but_keeps_the_last_stop(conn):
    trip = db.trip_stops(conn, "T1", max_stops=1)
    assert ids(trip) == ["S1"]
    assert trip.more_stops == 2
    assert trip.last_stop.stop.stop_id == "S3"


def test_no_cut_when_trip_fits(conn):
    trip = db.trip_stops(conn, "T1", max_stops=3)
    assert (trip.more_stops, trip.last_stop) == (0, None)


@pytest.mark.parametrize("bad", [0, 201, True])
def test_bad_max_stops_rejected(conn, bad):
    with pytest.raises(ValueError, match="max_stops"):
        db.trip_stops(conn, "T1", max_stops=bad)
