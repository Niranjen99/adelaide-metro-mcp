"""Tests for db.py queries against the mini GTFS fixture."""

import sqlite3

import pytest

from adelaide_metro_mcp import db
from conftest import set_schema_version


def ids(stops):
    return [s.stop_id for s in stops]


# --- find_stops -------------------------------------------------------------

def test_find_by_name_is_case_insensitive(conn):
    assert ids(db.find_stops(conn, "central station")) == ["S1"]


def test_find_matches_words_in_any_order(conn):
    assert ids(db.find_stops(conn, "street market")) == ["S2"]


def test_find_matches_description_suburb(conn):
    assert ids(db.find_stops(conn, "Seaside")) == ["S3"]


def test_find_by_stop_number_ranks_exact_match_first(conn):
    # "10002" is S2's stop code; it must come first.
    assert ids(db.find_stops(conn, "10002"))[0] == "S2"


def test_find_respects_limit(conn):
    # "Testville" appears in two stop descriptions.
    assert len(db.find_stops(conn, "Testville", limit=1)) == 1


def test_find_returns_empty_list_when_nothing_matches(conn):
    assert db.find_stops(conn, "nowhere at all") == []


def test_find_treats_like_wildcards_literally(conn):
    # Without escaping, "%%" would match every stop.
    assert db.find_stops(conn, "%%") == []


@pytest.mark.parametrize("bad", ["", " ", "a", "x" * 101])
def test_find_rejects_bad_query(conn, bad):
    with pytest.raises(ValueError):
        db.find_stops(conn, bad)


@pytest.mark.parametrize("bad", [0, 21, -1, True, "5"])
def test_find_rejects_bad_limit(conn, bad):
    with pytest.raises(ValueError):
        db.find_stops(conn, "Central", limit=bad)


# --- routes_at_stop ---------------------------------------------------------

def test_routes_at_stop_lists_bus_and_tram(conn):
    stop, routes = db.routes_at_stop(conn, "S1")
    assert stop.stop_name == "Central Station"
    assert [(r.short_name, r.mode) for r in routes] == [("100", "Bus"), ("T1", "Tram")]


def test_routes_at_stop_only_bus(conn):
    _, routes = db.routes_at_stop(conn, "S2")
    assert [r.short_name for r in routes] == ["100"]


def test_unknown_stop_raises_not_found(conn):
    with pytest.raises(db.NotFoundError, match="find_stops"):
        db.routes_at_stop(conn, "NOPE")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 21, "1; DROP TABLE stops"])
def test_bad_stop_id_rejected(conn, bad):
    with pytest.raises(ValueError):
        db.routes_at_stop(conn, bad)


def test_route_sort_is_natural():
    r = lambda n: db.RouteSummary(n, n, "", "Bus")
    names = [x.short_name for x in sorted([r("1000"), r("101"), r("100"), r("T1"), r("150P")], key=db._route_sort_key)]
    assert names == ["100", "101", "150P", "1000", "T1"]


# --- connection -------------------------------------------------------------

def test_missing_database_gives_helpful_error(tmp_path):
    with pytest.raises(db.DatabaseNotReadyError, match="gtfs_loader"):
        db.connect(tmp_path / "missing.sqlite")


def test_connection_is_read_only(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM stops")


# --- parent stations ------------------------------------------------------------
# P1 "Central Station" is a parent station (location_type 1) grouping S1.

def test_parent_station_is_not_a_boarding_stop(conn):
    with pytest.raises(db.NotFoundError, match="station grouping"):
        db.get_stop(conn, "P1")


def test_find_stops_excludes_parent_stations(conn):
    # By name, P1 and S1 are both "Central Station" and only S1 comes back
    # (test_find_by_name_is_case_insensitive). By id, P1 must not match either.
    assert db.find_stops(conn, "P1") == []


def test_database_from_older_version_gives_helpful_error(gtfs_db):
    set_schema_version(gtfs_db, "1")
    with pytest.raises(db.DatabaseNotReadyError, match="older version"):
        db.connect(gtfs_db)


# --- find_stops: near_stop_id and towards --------------------------------------------
# Fixture locations: S1 (-34.92, 138.60), S2 (-34.93, 138.61) about 1.4 km away,
# S3 (-34.98, 138.51) about 10 km away. P1 is at S1's location but is a parent station.

def test_near_stop_default_radius_finds_only_itself(conn):
    stops = db.find_stops(conn, near_stop_id="S1")
    assert ids(stops) == ["S1"]  # P1 is at the same spot but is not a boarding stop
    assert stops[0].distance_m == 0


def test_near_stop_bigger_radius_sorted_by_distance(conn):
    stops = db.find_stops(conn, near_stop_id="S1", radius_m=2000)
    assert ids(stops) == ["S1", "S2"]
    assert 1400 < stops[1].distance_m < 1480


def test_near_stop_combined_with_text(conn):
    assert ids(db.find_stops(conn, "market", near_stop_id="S1", radius_m=2000)) == ["S2"]


def test_near_stop_respects_limit(conn):
    assert ids(db.find_stops(conn, near_stop_id="S1", radius_m=2000, limit=1)) == ["S1"]


def test_find_stops_needs_query_or_near(conn):
    with pytest.raises(ValueError, match="query, a near_stop_id"):
        db.find_stops(conn)


@pytest.mark.parametrize("bad", [49, 2001, True, "400"])
def test_bad_radius_rejected(conn, bad):
    with pytest.raises(ValueError, match="radius_m"):
        db.find_stops(conn, near_stop_id="S1", radius_m=bad)


@pytest.mark.parametrize("bad, error", [("NOPE", db.NotFoundError), ("P1", db.NotFoundError), ("x y", ValueError)])
def test_bad_near_stop_rejected(conn, bad, error):
    with pytest.raises(error):
        db.find_stops(conn, near_stop_id=bad)


def test_towards_lists_main_headsigns(conn):
    towards = {s.stop_id: s.towards for s in db.find_stops(conn, near_stop_id="S1", radius_m=2000)}
    # S1: T1, T2, T3, T5 go to Beach Interchange; T4 (tram) to Beach.
    assert towards["S1"] == ("Beach Interchange", "Beach")
    # S2: T5 is set-down only there, so only T1 and T2 count.
    assert towards["S2"] == ("Beach Interchange",)


def test_towards_empty_where_trips_end(conn):
    stop = db.find_stops(conn, "Seaside")[0]
    assert stop.towards == ()
    assert stop.distance_m is None  # text search: no distance


def test_distance_one_hundredth_degree_of_latitude():
    assert round(db._distance_m(-34.92, 138.60, -34.93, 138.60)) == 1112
    assert db._distance_m(-34.92, 138.60, -34.92, 138.60) == 0
