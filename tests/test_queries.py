"""Tests for db.py queries against the mini GTFS fixture."""

import pytest

from adelaide_metro_mcp import db


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
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM stops")
