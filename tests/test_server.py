"""Server-level tests: connect a real MCP client to the server in-process.

No network and no subprocess: the SDK's Client accepts an MCPServer instance
directly, so these tests exercise the full MCP request/response path quickly.
"""

import asyncio
from datetime import datetime

import pytest

from mcp import Client

from adelaide_metro_mcp import server
from adelaide_metro_mcp.db import ADELAIDE_TZ
from adelaide_metro_mcp.server import mcp


def call(tool: str, args: dict):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def list_tools():
    async def go():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def text(result) -> str:
    return result.content[0].text


def test_tools_are_listed_and_read_only():
    tools = {t.name: t for t in list_tools()}
    assert set(tools) == {"find_stops", "routes_at_stop", "next_departures", "trip_stops", "timetable_info"}
    for t in tools.values():
        assert t.description, f"{t.name} needs a description"
        assert t.annotations.read_only_hint is True


def test_limit_bounds_are_in_the_schema():
    find = next(t for t in list_tools() if t.name == "find_stops")
    limit = find.input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"]) == (1, 20)


def test_find_stops_returns_readable_text(server_data):
    result = call("find_stops", {"query": "central"})
    assert not result.is_error
    assert text(result) == (
        "Found 1 stop(s) matching 'central':\n"
        "- Central Station (stop 10001, id S1) - 1 Test Rd, Testville; towards Beach Interchange, Beach"
    )


def test_find_stops_no_match_message(server_data):
    result = call("find_stops", {"query": "nowhere"})
    assert not result.is_error
    assert "No stops found" in text(result)


def test_find_stops_bad_query_is_a_clear_error(server_data):
    result = call("find_stops", {"query": "a"})
    assert result.is_error
    assert "at least 2 characters" in text(result)


@pytest.mark.parametrize("limit", [0, 21])
def test_find_stops_limit_out_of_range_is_rejected(server_data, limit):
    result = call("find_stops", {"query": "central", "limit": limit})
    assert result.is_error


def test_routes_at_stop_returns_routes(server_data):
    result = call("routes_at_stop", {"stop_id": "S1"})
    assert not result.is_error
    assert text(result) == (
        "Routes at Central Station (stop 10001, id S1) - 1 Test Rd, Testville:\n"
        "- 100 (Bus): Central Station to Beach Interchange\n"
        "- T1 (Tram): Central Station to Beach (tram)"
    )


def test_routes_at_unknown_stop_is_a_clear_error(server_data):
    result = call("routes_at_stop", {"stop_id": "NOPE"})
    assert result.is_error
    assert "No stop with id 'NOPE'" in text(result)


def test_missing_database_tells_user_how_to_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("ADELAIDE_METRO_DATA_DIR", str(tmp_path))
    result = call("find_stops", {"query": "central"})
    assert result.is_error
    assert "python -m adelaide_metro_mcp.gtfs_loader" in text(result)


# --- next_departures and timetable_info -------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    """Set the server's 'now' to a fixed Adelaide time: clock('2026-11-09T07:00')."""
    def set_time(iso: str):
        fixed = datetime.fromisoformat(iso).replace(tzinfo=ADELAIDE_TZ)
        monkeypatch.setattr(server, "_now", lambda: fixed)
    return set_time


def test_next_departures_returns_readable_text(server_data, clock):
    clock("2026-11-09T07:00")  # a Monday
    result = call("next_departures", {"stop_id": "S1", "limit": 2})
    assert not result.is_error
    assert text(result) == (
        "Scheduled departures from Central Station (stop 10001, id S1) - 1 Test Rd, Testville\n"
        "As of Mon 09 Nov 07:00 (timetable only, no live delays):\n"
        "- 07:30  Bus 100 to Beach Interchange (in 30 min), trip T5\n"
        "- 08:00  Bus 100 to Beach Interchange (in 1 h), trip T1"
    )


def test_next_departures_after_midnight_shows_tomorrow(server_data, clock):
    clock("2026-11-13T23:30")  # Friday night
    result = call("next_departures", {"stop_id": "S1"})
    assert text(result).endswith("- 00:50 tomorrow  Tram T1 to Beach (in 1 h 20 min), trip T4")


def test_next_departures_route_filter(server_data, clock):
    clock("2026-11-13T23:30")
    result = call("next_departures", {"stop_id": "S1", "route": "100"})
    assert text(result) == (
        "No scheduled departures for route 100 from Central Station (stop 10001, id S1) "
        "- 1 Test Rd, Testville in the next 2 hours."
    )


def test_next_departures_none_message(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S3", "minutes_ahead": 30})
    assert not result.is_error
    assert "No scheduled departures from Beach Interchange" in text(result)
    assert "in the next 30 minutes" in text(result)


def test_next_departures_warns_when_timetable_expired(server_data, clock):
    clock("2028-01-10T07:00")  # the fixture timetable ends 2027-12-31
    result = call("next_departures", {"stop_id": "S1"})
    assert not result.is_error
    assert "No scheduled departures" in text(result)
    assert text(result).endswith("Warning: this timetable has expired. Run: python -m adelaide_metro_mcp.gtfs_loader")


def test_next_departures_notes_when_timetable_not_started(server_data, clock):
    clock("2025-12-01T07:00")  # the fixture timetable starts 2026-01-01
    assert "this timetable has not started yet" in text(call("next_departures", {"stop_id": "S1"}))


def test_next_departures_unknown_route_is_a_clear_error(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S1", "route": "999"})
    assert result.is_error
    assert "No route '999'" in text(result)


@pytest.mark.parametrize("minutes", [0, 1441])
def test_next_departures_minutes_out_of_range_is_rejected(server_data, minutes):
    result = call("next_departures", {"stop_id": "S1", "minutes_ahead": minutes})
    assert result.is_error


def test_timetable_info_text(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("timetable_info", {})
    assert not result.is_error
    body = text(result)
    assert "Timetable version 999" in body
    assert "Valid from 2026-01-01 to 2027-12-31" in body
    assert "3 stops, 2 routes, 5 trips" in body
    assert server.ATTRIBUTION in body
    assert "Warning" not in body


def test_timetable_info_warns_when_expired(server_data, clock):
    clock("2028-01-10T07:00")
    assert "this timetable has expired" in text(call("timetable_info", {}))


def test_parent_station_error_points_to_find_stops(server_data):
    result = call("routes_at_stop", {"stop_id": "P1"})
    assert result.is_error
    assert "Use find_stops to pick a specific platform" in text(result)


# --- next_departures with to_stop_id ----------------------------------------------------

def test_next_departures_to_destination_text(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S1", "to_stop_id": "S3", "limit": 1})
    assert not result.is_error
    assert text(result) == (
        "Scheduled direct trips from Central Station (stop 10001, id S1) - 1 Test Rd, Testville "
        "to Beach Interchange (stop 10003, id S3) - 5 Shore Rd, Seaside\n"
        "As of Mon 09 Nov 07:00 (timetable only, no live delays):\n"
        "- 07:30  Bus 100 to Beach Interchange (in 30 min), arrives 08:00 (30 min ride), trip T5"
    )


def test_next_departures_to_destination_none_soon(server_data, clock):
    clock("2026-11-14T09:00")  # Saturday: the only trip skips S2
    result = call("next_departures", {"stop_id": "S1", "to_stop_id": "S2"})
    assert not result.is_error
    assert text(result).startswith("No scheduled direct trips from Central Station")


def test_next_departures_wrong_direction_is_a_clear_error(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S2", "to_stop_id": "S1"})
    assert result.is_error
    assert "No trip goes directly" in text(result)
    assert "direction of travel" in text(result)


# --- trip_stops -------------------------------------------------------------------------

def test_trip_stops_from_boarding_stop_text(server_data):
    result = call("trip_stops", {"trip_id": "T1", "from_stop_id": "S1"})
    assert not result.is_error
    assert text(result).splitlines() == [
        "Bus 100 to Beach Interchange (trip T1)",
        "Stops after Central Station (stop 10001, id S1) - 1 Test Rd, Testville, timetable times:",
        "- 08:10  Market Street - Stop 5 (stop 10002, id S2) - 20 Market St, Testville",
        "- 08:30  Beach Interchange (stop 10003, id S3) - 5 Shore Rd, Seaside (last stop)",
    ]


def test_trip_stops_marks_restricted_stops(server_data):
    t2 = text(call("trip_stops", {"trip_id": "T2"}))
    assert "id S2) - 20 Market St, Testville (pick up only)" in t2
    t5 = text(call("trip_stops", {"trip_id": "T5"}))
    assert "id S2) - 20 Market St, Testville (set down only)" in t5


def test_trip_stops_after_midnight_shows_clock_time(server_data):
    lines = text(call("trip_stops", {"trip_id": "T4"})).splitlines()
    assert lines[0] == "Tram T1 to Beach (trip T4)"
    # The whole trip runs after midnight, so neither stop is marked "(next day)".
    assert lines[2].startswith("- 00:50  Central Station")
    assert lines[3].startswith("- 01:10  Beach Interchange")


@pytest.mark.parametrize(
    "secs, first, expected",
    [
        (8 * 3600 + 5 * 60, 8 * 3600, "08:05"),
        (24 * 3600 + 10 * 60, 23 * 3600 + 50 * 60, "00:10 (next day)"),  # crosses midnight
        (25 * 3600, 24 * 3600 + 50 * 60, "01:00"),  # started after midnight: no marker
        (None, 0, "--:--"),
    ],
)
def test_format_trip_time(secs, first, expected):
    assert server._format_trip_time(secs, first) == expected


def test_trip_stops_from_last_stop_says_so(server_data):
    result = call("trip_stops", {"trip_id": "T1", "from_stop_id": "S3"})
    assert text(result).endswith("- none: this is the trip's last stop")


def test_trip_stops_unknown_trip_is_a_clear_error(server_data):
    result = call("trip_stops", {"trip_id": "NOPE"})
    assert result.is_error
    assert "No trip with id 'NOPE'" in text(result)


def test_departure_trip_id_works_with_trip_stops(server_data, clock):
    # The intended flow: take the trip id from next_departures and pass it to trip_stops.
    clock("2026-11-09T07:00")
    first_line = text(call("next_departures", {"stop_id": "S1", "limit": 1})).splitlines()[2]
    trip_id = first_line.rsplit("trip ", 1)[1]
    result = call("trip_stops", {"trip_id": trip_id, "from_stop_id": "S1"})
    assert not result.is_error
    assert "Beach Interchange (stop 10003, id S3)" in text(result)


# --- find_stops near / towards, trip_stops max_stops ------------------------------------

def test_find_stops_near_text(server_data):
    result = call("find_stops", {"near_stop_id": "S1", "radius_m": 2000})
    assert not result.is_error
    lines = text(result).splitlines()
    assert lines[0] == "Found 2 stop(s) within 2000 m of stop id S1:"
    assert lines[1].endswith("Testville, 0 m away; towards Beach Interchange, Beach")
    assert "m away; towards Beach Interchange" in lines[2]


def test_find_stops_shows_where_trips_end(server_data):
    assert text(call("find_stops", {"query": "Seaside"})).endswith("; no departures (trips end here)")


def test_find_stops_without_query_or_near_is_a_clear_error(server_data):
    result = call("find_stops", {})
    assert result.is_error
    assert "query, a near_stop_id, or both" in text(result)


def test_trip_stops_max_stops_names_the_last_stop(server_data):
    lines = text(call("trip_stops", {"trip_id": "T1", "max_stops": 1})).splitlines()
    assert lines[2] == "- 08:00  Central Station (stop 10001, id S1) - 1 Test Rd, Testville"
    assert lines[3] == (
        "... 2 more stop(s), ending at Beach Interchange (id S3) at 08:30. Ask with a bigger max_stops to see them."
    )
